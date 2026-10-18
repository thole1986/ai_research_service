"""Single-request provider transports; no orchestration, repair, or implicit fallback."""

import asyncio
import re
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from company_ai.domain.research.contracts import InvalidOutput, SafeFailure
from company_ai.domain.research.llm import (
    ANTHROPIC_BASE_URL,
    OPENAI_BASE_URL,
    ResolvedProfile,
    normalize_endpoint_url,
)


@dataclass(frozen=True)
class LLMRequest:
    step: str
    messages: list[dict]
    output_schema: dict
    wire_schema: dict
    schema_name: str
    profile: ResolvedProfile


@dataclass(frozen=True)
class LLMResponse:
    content: str
    provider: str
    model: str
    tokens_input: int | None = None
    tokens_output: int | None = None
    provider_request_id: str | None = None
    finish_category: str = "stop"
    metadata: dict = field(default_factory=dict)


class LLMTransport(Protocol):
    async def generate(self, request: LLMRequest) -> LLMResponse: ...


class ProviderOutcomeUnknown(Exception):
    """Dispatch may have reached the server. Never automatically repeat this request."""


def _token_count(value):
    return value if type(value) is int and 0 <= value <= 2**63 - 1 else None


def _request_id(value):
    if isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_.:-]{1,200}", value):
        return value
    return None


def _finish_reason(value):
    # Provider values are untrusted and must not become arbitrary telemetry metadata.
    return {
        "stop": "stop",
        "end_turn": "stop",
        "length": "length",
        "max_tokens": "length",
        "refusal": "refused",
        "content_filter": "content_filter",
    }.get(value, "incomplete")


class LoopOwnedTransport:
    def __init__(self):
        self._owner_loop = None
        self._owns_client = False

    def _check_loop(self):
        loop = asyncio.get_running_loop()
        if self._owner_loop is None:
            self._owner_loop = loop
        elif self._owner_loop is not loop:
            raise InvalidOutput("EXECUTION_CONFIG_UNAVAILABLE")


class OpenAITransport(LoopOwnedTransport):
    def __init__(self, settings, client=None):
        super().__init__()
        self.settings = settings
        self.client = client
        self._owns_client = client is None

    def _client(self, profile):
        self._check_loop()
        if self.client is None:
            from openai import AsyncOpenAI

            self.client = AsyncOpenAI(
                api_key=self.settings.openai_api_key.get_secret_value(),
                base_url=OPENAI_BASE_URL,
                timeout=profile.timeout_seconds,
                max_retries=0,
                http_client=httpx.AsyncClient(follow_redirects=False, trust_env=False),
            )
        return self.client

    async def generate(self, request):
        from openai import APIStatusError

        profile = request.profile
        try:
            response = await self._client(profile).chat.completions.create(
                model=profile.model,
                messages=request.messages,
                temperature=profile.temperature,
                max_tokens=profile.max_output_tokens,
                timeout=profile.timeout_seconds,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": request.schema_name,
                        "strict": True,
                        "schema": request.wire_schema,
                    },
                },
            )
        except APIStatusError as exc:
            if exc.status_code == 429:
                raise SafeFailure("RATE_LIMITED") from None
            body = exc.body if isinstance(exc.body, dict) else {}
            error = body.get("error", body)
            if isinstance(error, dict) and error.get("code") == "context_length_exceeded":
                raise InvalidOutput("CONTEXT_LIMIT_EXCEEDED") from None
            categories = {
                400: "PROVIDER_REQUEST_INVALID",
                401: "PROVIDER_AUTHENTICATION_FAILED",
                403: "PROVIDER_PERMISSION_DENIED",
                404: "PROVIDER_MODEL_NOT_FOUND",
                422: "PROVIDER_REQUEST_INVALID",
            }
            if exc.status_code in categories:
                raise InvalidOutput(categories[exc.status_code]) from None
            raise ProviderOutcomeUnknown("PROVIDER_OUTCOME_UNKNOWN") from None
        except InvalidOutput:
            raise
        except Exception:
            raise ProviderOutcomeUnknown("PROVIDER_OUTCOME_UNKNOWN") from None
        try:
            choice = response.choices[0]
            refusal = getattr(choice.message, "refusal", None)
            finish = "refused" if refusal else _finish_reason(choice.finish_reason)
            content = choice.message.content
            if content is None:
                content = ""
            if not isinstance(content, str):
                raise ValueError
            usage = getattr(response, "usage", None)
            return LLMResponse(
                content=content,
                provider="openai",
                model=profile.model,
                tokens_input=_token_count(getattr(usage, "prompt_tokens", None)),
                tokens_output=_token_count(getattr(usage, "completion_tokens", None)),
                provider_request_id=_request_id(getattr(response, "_request_id", None)),
                finish_category=finish,
            )
        except (AttributeError, IndexError, TypeError, ValueError):
            raise InvalidOutput("PROVIDER_RESPONSE_INVALID") from None

    async def aclose(self):
        if self.client is not None and self._owns_client:
            self._check_loop()
            await self.client.close()
            self.client = None


class HTTPTransport(LoopOwnedTransport):
    def __init__(self, base_url, client=None):
        super().__init__()
        self.base_url = normalize_endpoint_url(base_url)
        self.client = client
        self._owns_client = client is None

    def _client(self):
        self._check_loop()
        if self.client is None:
            self.client = httpx.AsyncClient(
                follow_redirects=False,
                trust_env=False,
                transport=httpx.AsyncHTTPTransport(retries=0),
            )
        return self.client

    async def _post(self, path, *, timeout, json, headers=None):
        try:
            response = await self._client().post(
                self.base_url + path,
                json=json,
                headers=headers,
                timeout=timeout,
                follow_redirects=False,
            )
        except InvalidOutput:
            raise
        except Exception:
            raise ProviderOutcomeUnknown("PROVIDER_OUTCOME_UNKNOWN") from None
        if response.status_code == 429:
            raise SafeFailure("RATE_LIMITED")
        if response.status_code >= 500 or response.status_code in (408, 409):
            raise ProviderOutcomeUnknown("PROVIDER_OUTCOME_UNKNOWN")
        if response.status_code != 200:
            categories = {
                400: "PROVIDER_REQUEST_INVALID",
                401: "PROVIDER_AUTHENTICATION_FAILED",
                403: "PROVIDER_PERMISSION_DENIED",
                404: "MODEL_NOT_AVAILABLE",
                413: "CONTEXT_LIMIT_EXCEEDED",
                422: "PROVIDER_REQUEST_INVALID",
            }
            raise InvalidOutput(
                categories.get(response.status_code, "EXECUTION_CONFIG_UNAVAILABLE")
            )
        try:
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError
        except (TypeError, ValueError):
            raise InvalidOutput("PROVIDER_RESPONSE_INVALID") from None
        return response, data

    async def aclose(self):
        if self.client is not None and self._owns_client:
            self._check_loop()
            await self.client.aclose()
            self.client = None


class OllamaTransport(HTTPTransport):
    """Native local Ollama API. Metadata reads never pull or generate a model."""

    def __init__(self, base_url, client=None, *, metadata_timeout_seconds=5.0):
        super().__init__(base_url, client)
        self.metadata_timeout_seconds = metadata_timeout_seconds

    def _validate_local_model(self, profile):
        host = urlsplit(self.base_url).hostname or ""
        if (
            host == "ollama.com"
            or host.endswith(".ollama.com")
            or profile.model.endswith((":cloud", "-cloud"))
        ):
            raise InvalidOutput("EXECUTION_CONFIG_UNAVAILABLE")

    async def generate(self, request):
        profile = request.profile
        self._validate_local_model(profile)
        _, data = await self._post(
            "/api/chat",
            timeout=profile.timeout_seconds,
            json={
                "model": profile.model,
                "messages": request.messages,
                "stream": False,
                "format": request.wire_schema,
                "options": {
                    "temperature": profile.temperature,
                    "num_predict": profile.max_output_tokens,
                    "num_ctx": profile.context_window_tokens,
                },
            },
        )
        try:
            content = data["message"]["content"]
            if not isinstance(content, str):
                raise ValueError
            finish = (
                _finish_reason(data.get("done_reason"))
                if data.get("done") is True
                else "incomplete"
            )
            metadata = {
                key: count
                for key in (
                    "total_duration",
                    "load_duration",
                    "prompt_eval_duration",
                    "eval_duration",
                )
                if (count := _token_count(data.get(key))) is not None
            }
            return LLMResponse(
                content=content,
                provider="ollama",
                model=profile.model,
                tokens_input=_token_count(data.get("prompt_eval_count")),
                tokens_output=_token_count(data.get("eval_count")),
                finish_category=finish,
                metadata=metadata,
            )
        except (KeyError, TypeError, ValueError):
            raise InvalidOutput("PROVIDER_RESPONSE_INVALID") from None

    async def model_identity(self, profile):
        self._validate_local_model(profile)
        try:
            response = await self._client().get(
                self.base_url + "/api/tags",
                timeout=self.metadata_timeout_seconds,
                follow_redirects=False,
            )
            if response.status_code != 200:
                return None
            models = response.json().get("models")
            if not isinstance(models, list):
                return None
            # A missing tag suffix refers to :latest, never a different model.
            requested = profile.model
            names = {requested}
            if ":" not in requested.rsplit("/", 1)[-1]:
                names.add(requested + ":latest")
            for model in models:
                if not isinstance(model, dict):
                    continue
                if model.get("name") in names or model.get("model") in names:
                    if model.get("remote_host") or model.get("remote_model"):
                        raise InvalidOutput("EXECUTION_CONFIG_UNAVAILABLE")
                    digest = model.get("digest")
                    if isinstance(digest, str) and re.fullmatch(
                        r"(?:sha256:)?[0-9a-f]{64}", digest
                    ):
                        return digest.removeprefix("sha256:")
                    return None
            raise InvalidOutput("MODEL_NOT_AVAILABLE")
        except InvalidOutput:
            raise
        except Exception:
            # Metadata is best effort: absence must never invent a digest or pull a model.
            return None


class AnthropicTransport(HTTPTransport):
    def __init__(self, settings, client=None):
        super().__init__(ANTHROPIC_BASE_URL, client)
        self.settings = settings

    async def generate(self, request):
        profile = request.profile
        system = [item["content"] for item in request.messages if item["role"] == "system"]
        conversation = [item for item in request.messages if item["role"] != "system"]
        response, data = await self._post(
            "/v1/messages",
            timeout=profile.timeout_seconds,
            headers={
                "x-api-key": self.settings.anthropic_api_key.get_secret_value(),
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": profile.model,
                "system": "\n\n".join(system),
                "messages": conversation,
                "max_tokens": profile.max_output_tokens,
                "temperature": profile.temperature,
                "stream": False,
                "output_config": {"format": {"type": "json_schema", "schema": request.wire_schema}},
            },
        )
        try:
            blocks = data["content"]
            if not isinstance(blocks, list):
                raise ValueError
            text_blocks = [block["text"] for block in blocks if block.get("type") == "text"]
            if any(not isinstance(value, str) for value in text_blocks):
                raise ValueError
            usage = data.get("usage") or {}
            return LLMResponse(
                content="".join(text_blocks),
                provider="anthropic",
                model=profile.model,
                tokens_input=_token_count(usage.get("input_tokens")),
                tokens_output=_token_count(usage.get("output_tokens")),
                provider_request_id=_request_id(response.headers.get("request-id")),
                finish_category=_finish_reason(data.get("stop_reason")),
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            raise InvalidOutput("PROVIDER_RESPONSE_INVALID") from None


def create_transports(settings, *, openai_client=None):
    """Composition registry. Clients are lazy and belong to one executing event loop."""
    return {
        "openai": OpenAITransport(settings, openai_client),
        "ollama": OllamaTransport(settings.ollama_base_url),
        "anthropic": AnthropicTransport(settings),
    }
