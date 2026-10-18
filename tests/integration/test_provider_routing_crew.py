"""API/workflow -> actual Crew -> SDK/native HTTP mocks -> persisted checkpoints."""

import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from company_ai.application.research.execute_workflow import run_workflow
from company_ai.infrastructure.research.ai.llm_adapter import CrewAIAdapter
from company_ai.infrastructure.research.ai.transports import (
    AnthropicTransport,
    OllamaTransport,
    OpenAITransport,
)
from company_ai.infrastructure.research.persistence.models import Attempt
from tests.fakes import CountingAdapter
from tests.integration.test_collection_workflow import (
    CollectedAdapter,
    FakeCollector,
    collection_payload,
)
from tests.integration.test_llm_routing_workflow import configure


@pytest.mark.parametrize("provider", ["ollama", "anthropic"])
@pytest.mark.parametrize("collected", [False, True])
async def test_mixed_provider_boundaries_through_actual_crew(
    repo, payload, provider, collected, client_factory, app_factory, caplog
):
    configure(repo.settings, report_provider=provider)
    fake = CollectedAdapter() if collected else CountingAdapter()
    sdk_calls, native_calls = [], []

    async def create(**kwargs):
        step = {
            "ResearchOutput": "RESEARCH",
            "ResearchCollectedOutput": "RESEARCH",
            "ClassificationOutput": "CLASSIFY",
        }[kwargs["response_format"]["json_schema"]["name"]]
        sdk_calls.append(step)
        result = await fake.execute(step, json.loads(kwargs["messages"][1]["content"]))
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content=json.dumps(result.output), refusal=None),
                )
            ],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50),
            _request_id="req-openai-test",
        )

    async def native(request):
        if request.url.path == "/api/tags":
            assert request.method == "GET"
            return httpx.Response(
                200, json={"models": [{"name": "test-ollama-model:latest", "digest": "a" * 64}]}
            )
        data = json.loads(request.content)
        native_calls.append(request.url.path)
        assert data["stream"] is False
        result = await fake.execute("REPORT", json.loads(data["messages"][-1]["content"]))
        if provider == "ollama":
            assert "x-api-key" not in request.headers and "authorization" not in request.headers
            assert data["options"]["num_ctx"] == 32000
            return httpx.Response(
                200,
                json={
                    "message": {
                        "content": json.dumps(result.output),
                        "thinking": "PRIVATE THINKING",
                    },
                    "done": True,
                    "done_reason": "stop",
                    "prompt_eval_count": 100,
                    "eval_count": 50,
                },
            )
        assert data["output_config"]["format"]["type"] == "json_schema"
        return httpx.Response(
            200,
            headers={"request-id": "req-claude-test"},
            json={
                "content": [
                    {"type": "thinking", "thinking": "PRIVATE THINKING"},
                    {"type": "text", "text": json.dumps(result.output)},
                ],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
        )

    native_client = httpx.AsyncClient(transport=httpx.MockTransport(native))
    sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    adapter = CrewAIAdapter(
        repo.settings,
        transports={
            "openai": OpenAITransport(repo.settings, sdk),
            provider: OllamaTransport(repo.settings.ollama_base_url, native_client)
            if provider == "ollama"
            else AnthropicTransport(repo.settings, native_client),
        },
    )
    try:
        client = await client_factory(app_factory())
        accepted = await client.post(
            "/api/v1/research/jobs",
            json=collection_payload() if collected else payload,
            headers={"Idempotency-Key": "mixed-boundaries"},
        )
        assert accepted.status_code == 202
        job_id = accepted.json()["job_id"]
        collector = FakeCollector() if collected else None
        await run_workflow(job_id, repo, adapter, repo.settings, collector=collector)
        response = await client.get(f"/api/v1/research/jobs/{job_id}")
        assert response.json()["status"] == "COMPLETED"
        assert response.json()["execution_metadata"]["REPORT"]["provider"] == provider
        assert sdk_calls == ["RESEARCH", "CLASSIFY"]
        assert native_calls == (["/api/chat"] if provider == "ollama" else ["/v1/messages"])
        assert fake.calls == ["RESEARCH", "CLASSIFY", "REPORT"]
        await run_workflow(job_id, repo, adapter, repo.settings, collector=collector)
        assert len(sdk_calls) == 2 and len(native_calls) == 1
        assert "PRIVATE THINKING" not in response.text + caplog.text
        if collected:
            assert len(collector.calls) == 1
    finally:
        await adapter.aclose()
        await native_client.aclose()


@pytest.mark.parametrize("provider", ["openai", "ollama", "anthropic"])
async def test_rejected_json_keeps_real_usage_without_repair(repo, payload, provider):
    configure(repo.settings, all_provider=provider)
    calls = []

    async def create(**kwargs):
        calls.append("openai")
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content="INVALID JSON", refusal=None),
                )
            ],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50),
        )

    async def native(request):
        if request.url.path == "/api/tags":
            return httpx.Response(
                200, json={"models": [{"name": "test-ollama-model", "digest": "b" * 64}]}
            )
        calls.append(provider)
        return httpx.Response(
            200,
            json={
                "message": {"content": "INVALID JSON"},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 100,
                "eval_count": 50,
            }
            if provider == "ollama"
            else {
                "content": [{"type": "text", "text": "INVALID JSON"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
        )

    native_client = httpx.AsyncClient(transport=httpx.MockTransport(native))
    sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    transport = {
        "openai": OpenAITransport(repo.settings, sdk),
        "ollama": OllamaTransport(repo.settings.ollama_base_url, native_client),
        "anthropic": AnthropicTransport(repo.settings, native_client),
    }[provider]
    adapter = CrewAIAdapter(repo.settings, transports={provider: transport})
    try:
        job, _ = await repo.create_job_idempotently("invalid-json", payload)
        await run_workflow(job.id, repo, adapter, repo.settings)
        state, steps = await repo.get_job(job.id)
        assert calls == [provider]
        assert state.error_code == "INVALID_STRUCTURED_OUTPUT"
        assert steps[0].output_json is None
        async with repo.db.session() as session:
            attempt = await session.scalar(select(Attempt).where(Attempt.step_id == steps[0].id))
        assert (attempt.tokens_input, attempt.tokens_output) == (100, 50)
        assert float(state.total_estimated_cost_usd) == pytest.approx(
            0 if provider == "ollama" else 0.0003
        )
    finally:
        await adapter.aclose()
        await native_client.aclose()
