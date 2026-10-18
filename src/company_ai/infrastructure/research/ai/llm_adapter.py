import asyncio
import contextlib
import io
import os
from pathlib import Path

from pydantic import ValidationError

from company_ai.application.research.prompts import messages, provider_schema, provider_wire_schema
from company_ai.domain.research.contracts import AIResult, InvalidOutput
from company_ai.domain.research.hashing import digest
from company_ai.domain.research.llm import (
    estimate_input_tokens,
    legacy_profile,
    price_usage,
    validate_profile_runtime,
)
from company_ai.domain.research.outputs import schema_for
from company_ai.infrastructure.research.ai.crew_factory import make_crew
from company_ai.infrastructure.research.ai.transports import (
    LLMRequest,
    LoopOwnedTransport,
    OpenAITransport,
    create_transports,
)
from company_ai.observability import NoOpObserver


def configure_crewai():
    os.environ["CREWAI_TELEMETRY_DISABLED"] = "true"
    # CrewAI interprets a relative value as an app-data name on Windows.
    os.environ["CREWAI_STORAGE_DIR"] = str(
        Path(os.environ.get("CREWAI_STORAGE_DIR", "./data/crewai")).resolve()
    )


def bounded_llm(
    settings, step, payload, client=None, observer=None, *, profile=None, transport=None
):
    configure_crewai()
    from crewai import BaseLLM

    observer = observer or NoOpObserver()
    resolved = profile or legacy_profile(settings, step)
    transport = transport or OpenAITransport(settings, client)
    output_type = schema_for(step, payload)
    domain_schema = provider_schema(step, payload)
    request = LLMRequest(
        step=step,
        messages=messages(step, payload),
        output_schema=domain_schema,
        wire_schema=(
            provider_wire_schema(step, payload, resolved.provider) if profile else domain_schema
        ),
        schema_name=output_type.__name__,
        profile=resolved,
    )

    class BoundedProviderLLM(BaseLLM):
        """One generation per real Crew, using only the materialized trusted messages."""

        def __init__(self):
            super().__init__(model=resolved.model, temperature=resolved.temperature)
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
                        self.failure.result = self.result
                    raise self.failure
                self.calls += 1
            usage_result = None
            try:
                with observer.span(
                    {
                        "openai": "openai.chat.completions",
                        "ollama": "ollama.chat",
                        "anthropic": "anthropic.messages",
                    }[resolved.provider],
                    generation=True,
                    model=resolved.model,
                    model_parameters={
                        "temperature": resolved.temperature,
                        "max_tokens": resolved.max_output_tokens,
                        "response_format": resolved.capability_mode,
                        "context_window_tokens": resolved.context_window_tokens,
                    },
                    metadata={
                        "step_name": step,
                        "provider": resolved.provider,
                        "profile_id": resolved.profile_id,
                        "config_fingerprint": resolved.config_fingerprint,
                        "pricing_kind": resolved.pricing.kind,
                    },
                    input=request.messages,
                ) as generation:
                    response = await transport.generate(request)
                    usage_result = AIResult(
                        output={},
                        tokens_input=response.tokens_input,
                        tokens_output=response.tokens_output,
                        provider_request_id=response.provider_request_id,
                        provider=resolved.provider,
                        model=resolved.model,
                        finish_category=response.finish_category,
                        provider_metadata=response.metadata,
                    )
                    usage_details = {
                        key: value
                        for key, value in (
                            ("input", response.tokens_input),
                            ("output", response.tokens_output),
                        )
                        if value is not None
                    }
                    cost_details = {}
                    cost = price_usage(resolved, response.tokens_input, response.tokens_output)
                    if cost is not None:
                        cost_details = {"total": cost}
                        if response.tokens_input is not None and response.tokens_output is not None:
                            cost_details = {
                                "input": price_usage(resolved, response.tokens_input, 0),
                                "output": price_usage(resolved, 0, response.tokens_output),
                            }
                    generation.update(
                        output=response.content,
                        usage_details=usage_details,
                        cost_details=cost_details,
                        metadata={
                            **response.metadata,
                            "provider": resolved.provider,
                            "provider_request_id": response.provider_request_id,
                            "finish_reason": response.finish_category,
                            "cost_is_estimate": True,
                            "pricing_kind": resolved.pricing.kind,
                        },
                    )
                    errors = {
                        "refused": "PROVIDER_REFUSED_OUTPUT",
                        "length": "OUTPUT_TOKEN_LIMIT_REACHED",
                        "content_filter": "PROVIDER_CONTENT_FILTERED",
                    }
                    if response.finish_category != "stop":
                        raise InvalidOutput(
                            errors.get(response.finish_category, "INCOMPLETE_OR_REFUSED_OUTPUT")
                        )
                    output = output_type.model_validate_json(response.content)
                    self.result = AIResult(
                        output=output.model_dump(),
                        tokens_input=response.tokens_input,
                        tokens_output=response.tokens_output,
                        provider_request_id=response.provider_request_id,
                        provider=resolved.provider,
                        model=resolved.model,
                        finish_category=response.finish_category,
                        provider_metadata=response.metadata,
                    )
                    return "Final Answer: " + output.model_dump_json()
            except ValidationError:
                self.failure = InvalidOutput("INVALID_STRUCTURED_OUTPUT")
                self.failure.result = usage_result
                raise self.failure from None
            except Exception as exc:
                if isinstance(exc, InvalidOutput) and usage_result is not None:
                    exc.result = usage_result
                self.failure = exc
                raise

        def call(self, *args, **kwargs):
            # CrewAI calls this hook from a worker thread. All provider I/O remains on
            # the owning event loop; creating a fresh loop here would leak async clients.
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
            return resolved.context_window_tokens

    return BoundedProviderLLM()


class CrewAIAdapter:
    def __init__(self, settings, client=None, observer=None, transports=None):
        self.settings, self.client = settings, client
        self.observer = observer or NoOpObserver()
        self.transports = (
            dict(transports)
            if transports is not None
            else create_transports(settings, openai_client=client)
        )
        self._injected_providers = {
            provider
            for provider, transport in self.transports.items()
            if not isinstance(transport, LoopOwnedTransport) or not transport._owns_client
        }

    def validate_profile(self, profile):
        if profile.provider not in self.transports:
            raise InvalidOutput("EXECUTION_CONFIG_UNAVAILABLE")
        validate_profile_runtime(
            profile,
            self.settings,
            require_credentials=profile.provider not in self._injected_providers,
        )
        endpoint = getattr(self.transports[profile.provider], "base_url", None)
        if endpoint is not None and digest(endpoint) != profile.endpoint_fingerprint:
            raise InvalidOutput("EXECUTION_CONFIG_UNAVAILABLE")

    async def model_identity(self, profile):
        self.validate_profile(profile)
        lookup = getattr(self.transports[profile.provider], "model_identity", None)
        return await lookup(profile) if lookup else None

    async def execute(self, step, payload, profile=None):
        resolved = profile or legacy_profile(self.settings, step)
        if profile is None and "openai" not in self._injected_providers:
            if not self.settings.openai_api_key.get_secret_value().strip():
                raise InvalidOutput("PROVIDER_NOT_CONFIGURED")
        self.validate_profile(resolved)
        if profile is not None:
            schema = provider_wire_schema(step, payload, resolved.provider)
            if (
                estimate_input_tokens(messages(step, payload), schema) + resolved.max_output_tokens
                > resolved.context_window_tokens
            ):
                raise InvalidOutput("CONTEXT_LIMIT_EXCEEDED")
        llm = bounded_llm(
            self.settings,
            step,
            payload,
            observer=self.observer,
            profile=profile,
            transport=self.transports[resolved.provider],
        )
        # CrewAI can print raw responses even with verbose=False. The solo worker emits
        # sanitized events itself; its stdout/stderr must never expose provider bodies.
        try:
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                crew = make_crew(step, llm, self.settings, schema_for(step, payload))
                await crew.akickoff()
        except Exception:
            if llm.failure:
                raise llm.failure from None
            raise InvalidOutput("CREW_OUTPUT_REJECTED") from None
        if llm.failure:
            raise llm.failure
        if llm.result is None:
            raise InvalidOutput("NO_STRUCTURED_RESULT")
        return llm.result

    async def aclose(self):
        seen = set()
        for transport in self.transports.values():
            if id(transport) in seen:
                continue
            seen.add(id(transport))
            close = getattr(transport, "aclose", None)
            if close is not None:
                await close()
