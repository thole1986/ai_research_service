import asyncio
import contextlib
import io
import os
from pathlib import Path

from pydantic import ValidationError

from company_ai.application.research.prompts import messages, provider_schema
from company_ai.domain.research.contracts import AIResult, InvalidOutput, SafeFailure
from company_ai.domain.research.outputs import SCHEMAS
from company_ai.infrastructure.research.ai.crew_factory import make_crew
from company_ai.observability import NoOpObserver


def configure_crewai():
    os.environ["CREWAI_TELEMETRY_DISABLED"] = "true"
    # CrewAI interprets a relative value as an app-data name on Windows.
    os.environ["CREWAI_STORAGE_DIR"] = str(
        Path(os.environ.get("CREWAI_STORAGE_DIR", "./data/crewai")).resolve()
    )


def bounded_llm(settings, step, payload, client, observer=None):
    configure_crewai()
    from crewai import BaseLLM
    from openai import APIStatusError

    observer = observer or NoOpObserver()

    class BoundedOpenAI(BaseLLM):
        """Exactly one SDK request per Crew, with transport retries explicitly disabled.

        The trusted prompt is materialized here, so Crew's formatting/repair prompts cannot
        add calls or reinterpret user text as instructions. No tools are exposed.
        """

        def __init__(self):
            super().__init__(model=settings.openai_model, temperature=settings.llm_temperature)
            self.result = None
            self.failure = None
            self.calls = 0
            self.lock = asyncio.Lock()
            self.owner_loop = asyncio.get_running_loop()

        async def acall(
            self, messages=None, tools=None, callbacks=None, available_functions=None, **kwargs
        ):
            async with self.lock:
                if self.calls:
                    if self.failure is None:
                        self.failure = InvalidOutput("ADDITIONAL_PROVIDER_CALL_BLOCKED")
                    raise self.failure
                self.calls += 1
            try:
                with observer.span(
                    "openai.chat.completions",
                    generation=True,
                    model=settings.openai_model,
                    model_parameters={
                        "temperature": settings.llm_temperature,
                        "max_tokens": settings.max_output_tokens_per_step,
                        "response_format": "json_schema",
                    },
                    metadata={"step_name": step},
                    input=provider_messages,
                ) as generation:
                    response = await client.chat.completions.create(
                        model=settings.openai_model,
                        messages=provider_messages,
                        temperature=settings.llm_temperature,
                        max_tokens=settings.max_output_tokens_per_step,
                        response_format={
                            "type": "json_schema",
                            "json_schema": {
                                "name": SCHEMAS[step].__name__,
                                "strict": True,
                                "schema": provider_schema(step, payload),
                            },
                        },
                    )
                    usage = getattr(response, "usage", None)
                    usage_details = {}
                    cost_details = {}
                    if usage is not None:
                        usage_details = {
                            "input": usage.prompt_tokens,
                            "output": usage.completion_tokens,
                        }
                        cost_details = {
                            "input": usage.prompt_tokens * settings.input_usd_per_million / 1e6,
                            "output": usage.completion_tokens
                            * settings.output_usd_per_million
                            / 1e6,
                        }
                    choice = response.choices[0]
                    generation.update(
                        output=choice.message.content,
                        usage_details=usage_details,
                        cost_details=cost_details,
                        metadata={
                            "provider_request_id": getattr(response, "_request_id", None),
                            "finish_reason": choice.finish_reason,
                            "cost_is_estimate": True,
                        },
                    )
                    if choice.message.refusal:
                        raise InvalidOutput("PROVIDER_REFUSED_OUTPUT")
                    if choice.finish_reason == "length":
                        raise InvalidOutput("OUTPUT_TOKEN_LIMIT_REACHED")
                    if choice.finish_reason == "content_filter":
                        raise InvalidOutput("PROVIDER_CONTENT_FILTERED")
                    if choice.finish_reason != "stop":
                        raise InvalidOutput("INCOMPLETE_OR_REFUSED_OUTPUT")
                    output = SCHEMAS[step].model_validate_json(choice.message.content or "")
                    self.result = AIResult(
                        output.model_dump(),
                        usage.prompt_tokens if usage else None,
                        usage.completion_tokens if usage else None,
                        getattr(response, "_request_id", None),
                    )
                    return "Final Answer: " + output.model_dump_json()
            except APIStatusError as exc:
                # Only explicit 429 rejection is automatically retryable. No SDK retry.
                categories = {
                    400: "PROVIDER_REQUEST_INVALID",
                    401: "PROVIDER_AUTHENTICATION_FAILED",
                    403: "PROVIDER_PERMISSION_DENIED",
                    404: "PROVIDER_MODEL_NOT_FOUND",
                    422: "PROVIDER_REQUEST_INVALID",
                }
                if exc.status_code == 429:
                    self.failure = SafeFailure("RATE_LIMITED")
                elif exc.status_code in categories:
                    self.failure = InvalidOutput(categories[exc.status_code])
                else:
                    self.failure = exc
                raise self.failure from None
            except ValidationError:
                self.failure = InvalidOutput("INVALID_STRUCTURED_OUTPUT")
                raise self.failure from None
            except Exception as exc:
                self.failure = exc
                raise

        def call(self, *args, **kwargs):
            # CrewAI 1.15's native async executor still calls this hook from a worker
            # thread. Marshal onto the owning loop; the SDK and all provider I/O stay async.
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is self.owner_loop:
                raise InvalidOutput("SYNC_CALL_ON_EVENT_LOOP_BLOCKED")
            return asyncio.run_coroutine_threadsafe(
                self.acall(*args, **kwargs), self.owner_loop
            ).result()

        def supports_function_calling(self):
            return False

        def supports_stop_words(self):
            return False

        def get_context_window_size(self):
            return 32000

    provider_messages = messages(step, payload)
    return BoundedOpenAI()


class CrewAIAdapter:
    def __init__(self, settings, client=None, observer=None):
        self.settings, self.client = settings, client
        self.observer = observer or NoOpObserver()

    async def execute(self, step, payload):
        from openai import AsyncOpenAI

        if not self.client and not self.settings.openai_api_key.get_secret_value():
            raise InvalidOutput("PROVIDER_NOT_CONFIGURED")
        client = self.client or AsyncOpenAI(
            api_key=self.settings.openai_api_key.get_secret_value(),
            timeout=self.settings.llm_request_timeout_seconds,
            max_retries=0,
        )
        llm = bounded_llm(self.settings, step, payload, client, self.observer)
        # CrewAI can print raw responses on error even with verbose=False. Worker is solo;
        # suppress these streams, and emit our own sanitized structured events in the runner.
        try:
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                crew = make_crew(step, llm, self.settings, SCHEMAS[step])
                await crew.akickoff()
        except Exception:
            if llm.failure:
                raise llm.failure from None
            raise InvalidOutput("CREW_OUTPUT_REJECTED") from None
        finally:
            if self.client is None:
                await client.close()
        if llm.failure:
            raise llm.failure
        if llm.result is None:
            raise InvalidOutput("NO_STRUCTURED_RESULT")
        return llm.result
