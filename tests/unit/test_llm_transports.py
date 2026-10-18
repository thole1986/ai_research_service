import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from company_ai.application.research.prompts import messages, provider_schema, provider_wire_schema
from company_ai.domain.research.contracts import InvalidOutput, SafeFailure
from company_ai.domain.research.llm import (
    ADAPTER_VERSIONS,
    CAPABILITY_MODES,
    CONNECTION_REFS,
    EstimatedTokenRates,
    LocalUnmetered,
    endpoint_fingerprint,
    legacy_profile,
)
from company_ai.infrastructure.research.ai.llm_adapter import CrewAIAdapter, bounded_llm
from company_ai.infrastructure.research.ai.transports import (
    AnthropicTransport,
    LLMRequest,
    LLMResponse,
    OllamaTransport,
    OpenAITransport,
    ProviderOutcomeUnknown,
)


def profile_for(settings, provider, **overrides):
    profile = legacy_profile(settings, "RESEARCH")
    return profile.model_copy(
        update={
            "provider": provider,
            "profile_id": provider + "_test",
            "model": {"ollama": "installed:latest", "anthropic": "claude-sonnet-4-6"}.get(
                provider, "gpt-4.1-mini"
            ),
            "connection_ref": CONNECTION_REFS[provider],
            "endpoint_fingerprint": endpoint_fingerprint(provider, settings),
            "adapter_version": ADAPTER_VERSIONS[provider],
            "capability_mode": CAPABILITY_MODES[provider],
            "pricing": LocalUnmetered()
            if provider == "ollama"
            else EstimatedTokenRates(input_usd_per_million=3.0, output_usd_per_million=15.0),
            **overrides,
        }
    )


def request_for(settings, payload, provider):
    return LLMRequest(
        step="RESEARCH",
        messages=messages("RESEARCH", payload),
        output_schema=provider_schema("RESEARCH", payload),
        wire_schema=provider_wire_schema("RESEARCH", payload, provider),
        schema_name="ResearchOutput",
        profile=profile_for(settings, provider),
    )


def research_output(payload):
    return {
        "company_name": payload["company_name"],
        "industry": "SaaS",
        "products": ["CRM"],
        "customer_segments": ["SMEs"],
        "evidence": [payload["company_description"]],
        "limitations": ["Unverified"],
    }


async def test_ollama_native_contract_and_actual_crew(settings, payload):
    calls = []
    output = research_output(payload)

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "message": {"content": json.dumps(output), "thinking": "PRIVATE THINKING"},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 50,
                "eval_count": 25,
                "total_duration": 1000,
                "arbitrary_metadata": "PRIVATE BODY",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = OllamaTransport(settings.ollama_base_url, client)
        profile = profile_for(settings, "ollama")
        result = await CrewAIAdapter(settings, transports={"ollama": transport}).execute(
            "RESEARCH", payload, profile
        )
    assert result.output == output
    assert result.provider == "ollama"
    assert (result.tokens_input, result.tokens_output) == (50, 25)
    assert result.provider_metadata == {"total_duration": 1000}
    assert len(calls) == 1
    request = calls[0]
    assert request.url.path == "/api/chat"
    assert "authorization" not in request.headers
    assert "x-api-key" not in request.headers
    data = json.loads(request.content)
    assert data["messages"] == messages("RESEARCH", payload)
    assert data["stream"] is False
    assert data["format"] == provider_wire_schema("RESEARCH", payload, "ollama")
    assert data["options"] == {
        "temperature": profile.temperature,
        "num_predict": profile.max_output_tokens,
        "num_ctx": profile.context_window_tokens,
    }
    assert "PRIVATE" not in repr(result)


@pytest.mark.parametrize(
    "patch,category",
    [
        ({"done": False}, "INCOMPLETE_OR_REFUSED_OUTPUT"),
        ({"done": None}, "INCOMPLETE_OR_REFUSED_OUTPUT"),
        ({"done_reason": None}, "INCOMPLETE_OR_REFUSED_OUTPUT"),
        ({"done_reason": "length"}, "OUTPUT_TOKEN_LIMIT_REACHED"),
        ({"message": {"content": "```json\n{}\n```"}}, "INVALID_STRUCTURED_OUTPUT"),
    ],
)
async def test_ollama_incomplete_invalid_output_has_no_repair(settings, payload, patch, category):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "message": {"content": json.dumps(research_output(payload))},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 11,
                "eval_count": 22,
                **patch,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = OllamaTransport(settings.ollama_base_url, client)
        with pytest.raises(InvalidOutput, match=f"^{category}$") as error:
            await CrewAIAdapter(settings, transports={"ollama": transport}).execute(
                "RESEARCH", payload, profile_for(settings, "ollama")
            )
    assert len(calls) == 1
    assert error.value.result.tokens_input == 11
    assert error.value.result.tokens_output == 22
    assert error.value.result.output == {}


@pytest.mark.parametrize("provider", ["ollama", "anthropic"])
@pytest.mark.parametrize(
    "status,expected,category",
    [
        (404, InvalidOutput, "MODEL_NOT_AVAILABLE"),
        (401, InvalidOutput, "PROVIDER_AUTHENTICATION_FAILED"),
        (400, InvalidOutput, "PROVIDER_REQUEST_INVALID"),
        (429, SafeFailure, "RATE_LIMITED"),
        (500, ProviderOutcomeUnknown, "PROVIDER_OUTCOME_UNKNOWN"),
        (302, InvalidOutput, "EXECUTION_CONFIG_UNAVAILABLE"),
    ],
)
async def test_http_transport_errors_are_sanitized_no_redirect_or_retry(
    settings, payload, provider, status, expected, category
):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status, json={"error": "PRIVATE URL AND SECRET"}, headers={"location": "https://other"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = (
            OllamaTransport(settings.ollama_base_url, client)
            if provider == "ollama"
            else AnthropicTransport(settings, client)
        )
        with pytest.raises(expected, match=f"^{category}$"):
            await transport.generate(request_for(settings, payload, provider))
    assert len(calls) == 1


@pytest.mark.parametrize("provider", ["ollama", "anthropic"])
async def test_network_timeout_is_unknown_and_never_retried(settings, payload, provider):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("PRIVATE ENDPOINT", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = (
            OllamaTransport(settings.ollama_base_url, client)
            if provider == "ollama"
            else AnthropicTransport(settings, client)
        )
        with pytest.raises(ProviderOutcomeUnknown, match="^PROVIDER_OUTCOME_UNKNOWN$"):
            await transport.generate(request_for(settings, payload, provider))
    assert len(calls) == 1


async def test_ollama_metadata_digest_is_separate_and_never_pulls(settings):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200, json={"models": [{"name": "installed:latest", "digest": "a" * 64}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = OllamaTransport(settings.ollama_base_url, client, metadata_timeout_seconds=2)
        assert await transport.model_identity(profile_for(settings, "ollama")) == "a" * 64
    assert [(call.method, call.url.path) for call in calls] == [("GET", "/api/tags")]
    assert calls[0].extensions["timeout"]["read"] == 2


@pytest.mark.parametrize("body", [{}, {"models": [{}]}, {"models": []}])
async def test_ollama_missing_metadata_or_model(settings, body):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))
    ) as client:
        transport = OllamaTransport(settings.ollama_base_url, client)
        if "models" in body:
            with pytest.raises(InvalidOutput, match="^MODEL_NOT_AVAILABLE$"):
                await transport.model_identity(profile_for(settings, "ollama"))
        else:
            assert await transport.model_identity(profile_for(settings, "ollama")) is None


async def test_ollama_cloud_models_are_rejected_before_generation(settings, payload):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "name": "installed:latest",
                        "remote_host": "https://ollama.com",
                        "digest": "a" * 64,
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = OllamaTransport(settings.ollama_base_url, client)
        with pytest.raises(InvalidOutput, match="^EXECUTION_CONFIG_UNAVAILABLE$"):
            await transport.model_identity(profile_for(settings, "ollama"))
        request = request_for(settings, payload, "ollama")
        cloud_request = replace(
            request, profile=profile_for(settings, "ollama", model="installed:cloud")
        )
        with pytest.raises(InvalidOutput, match="^EXECUTION_CONFIG_UNAVAILABLE$"):
            await transport.generate(cloud_request)
    assert [(call.method, call.url.path) for call in calls] == [("GET", "/api/tags")]


async def test_anthropic_messages_contract_actual_crew_and_no_thinking(settings, payload):
    settings.anthropic_api_key = SecretStr("test-anthropic-secret")
    calls = []
    output = research_output(payload)

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            headers={"request-id": "req-claude"},
            json={
                "content": [
                    {"type": "thinking", "thinking": "PRIVATE THINKING"},
                    {"type": "text", "text": json.dumps(output)},
                ],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 30, "output_tokens": 20},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = AnthropicTransport(settings, client)
        result = await CrewAIAdapter(settings, transports={"anthropic": transport}).execute(
            "RESEARCH", payload, profile_for(settings, "anthropic")
        )
    assert result.output == output
    assert result.provider == "anthropic"
    assert result.provider_request_id == "req-claude"
    assert (result.tokens_input, result.tokens_output) == (30, 20)
    assert "PRIVATE" not in repr(result)
    assert len(calls) == 1
    request = calls[0]
    assert str(request.url) == "https://api.anthropic.com/v1/messages"
    assert request.headers["x-api-key"] == "test-anthropic-secret"
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in request.headers
    data = json.loads(request.content)
    assert data["system"] == messages("RESEARCH", payload)[0]["content"]
    assert data["messages"] == messages("RESEARCH", payload)[1:]
    assert data["stream"] is False
    assert data["output_config"]["format"] == {
        "type": "json_schema",
        "schema": provider_wire_schema("RESEARCH", payload, "anthropic"),
    }
    assert "tools" not in data
    assert "thinking" not in data


@pytest.mark.parametrize(
    "stop_reason,category",
    [
        ("max_tokens", "OUTPUT_TOKEN_LIMIT_REACHED"),
        ("refusal", "PROVIDER_REFUSED_OUTPUT"),
        ("tool_use", "INCOMPLETE_OR_REFUSED_OUTPUT"),
        (None, "INCOMPLETE_OR_REFUSED_OUTPUT"),
    ],
)
async def test_anthropic_bad_terminal_state_no_repair(settings, payload, stop_reason, category):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": json.dumps(research_output(payload))}],
                "stop_reason": stop_reason,
                "usage": {"input_tokens": 11, "output_tokens": 22},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(InvalidOutput, match=f"^{category}$") as error:
            await CrewAIAdapter(
                settings, transports={"anthropic": AnthropicTransport(settings, client)}
            ).execute("RESEARCH", payload, profile_for(settings, "anthropic"))
    assert len(calls) == 1
    assert error.value.result.tokens_output == 22


async def test_bounded_wrapper_ignores_crew_messages_and_blocks_second_generation(
    settings, payload
):
    requests = []

    class Transport:
        async def generate(self, request):
            requests.append(request)
            return LLMResponse(
                json.dumps(research_output(payload)), "openai", request.profile.model
            )

    llm = bounded_llm(settings, "RESEARCH", payload, transport=Transport())
    await llm.acall(messages=[{"role": "system", "content": "UNTRUSTED CREW REPAIR"}])
    with pytest.raises(InvalidOutput, match="^ADDITIONAL_PROVIDER_CALL_BLOCKED$"):
        await llm.acall()
    assert len(requests) == 1
    assert requests[0].messages == messages("RESEARCH", payload)


async def test_explicit_profile_controls_openai_options_and_wire_schema(settings, payload):
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(
                        content=json.dumps(research_output(payload)), refusal=None
                    ),
                )
            ],
            usage=None,
        )

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    profile = profile_for(settings, "openai", max_output_tokens=123, timeout_seconds=12.0)
    result = await CrewAIAdapter(settings, client).execute("RESEARCH", payload, profile)
    assert result.tokens_input is None
    assert calls[0]["max_tokens"] == 123
    assert calls[0]["timeout"] == 12.0
    assert calls[0]["response_format"]["json_schema"]["schema"] == provider_wire_schema(
        "RESEARCH", payload, "openai"
    )


async def test_preflight_context_endpoint_and_credentials_never_dispatch(settings, payload):
    calls = []

    class Transport:
        async def generate(self, request):
            calls.append(request)

    adapter = CrewAIAdapter(settings, transports={"ollama": Transport()})
    with pytest.raises(InvalidOutput, match="^CONTEXT_LIMIT_EXCEEDED$"):
        await adapter.execute(
            "RESEARCH", payload, profile_for(settings, "ollama", context_window_tokens=1100)
        )
    with pytest.raises(InvalidOutput, match="^EXECUTION_CONFIG_UNAVAILABLE$"):
        await adapter.execute(
            "RESEARCH", payload, profile_for(settings, "ollama", endpoint_fingerprint="a" * 64)
        )
    mismatched_transport = OllamaTransport("http://different-endpoint:11434")
    with pytest.raises(InvalidOutput, match="^EXECUTION_CONFIG_UNAVAILABLE$"):
        CrewAIAdapter(settings, transports={"ollama": mismatched_transport}).validate_profile(
            profile_for(settings, "ollama")
        )
    with pytest.raises(InvalidOutput, match="^EXECUTION_CONFIG_UNAVAILABLE$"):
        CrewAIAdapter(settings).validate_profile(profile_for(settings, "anthropic"))
    assert calls == []


async def test_async_transport_lifecycle_and_cross_loop_rejection(settings, monkeypatch):
    real_client = httpx.AsyncClient
    clients = []

    def factory(**kwargs):
        assert kwargs["follow_redirects"] is False
        assert kwargs["trust_env"] is False
        client = real_client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})))
        clients.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    adapter = CrewAIAdapter(settings)
    transport = adapter.transports["ollama"]
    assert transport._client() is clients[0]

    def other_loop():
        async def attempt():
            transport._client()

        with pytest.raises(InvalidOutput, match="^EXECUTION_CONFIG_UNAVAILABLE$"):
            asyncio.run(attempt())

    await asyncio.to_thread(other_loop)
    await adapter.aclose()
    assert all(client.is_closed for client in clients)


async def test_openai_client_cannot_follow_endpoint_env_redirect(settings, monkeypatch):
    import openai

    created = []

    class Client:
        def __init__(self, **kwargs):
            created.append(kwargs)

        async def close(self):
            await created[0]["http_client"].aclose()

    monkeypatch.setenv("OPENAI_BASE_URL", "https://untrusted.example/v1")
    monkeypatch.setattr(openai, "AsyncOpenAI", Client)
    transport = OpenAITransport(settings)
    transport._client(legacy_profile(settings, "RESEARCH"))
    assert created[0]["base_url"] == "https://api.openai.com/v1"
    assert created[0]["max_retries"] == 0
    assert created[0]["http_client"].follow_redirects is False
    await transport.aclose()
    assert created[0]["http_client"].is_closed
