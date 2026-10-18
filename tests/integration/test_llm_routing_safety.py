"""Provider-independent rejection, credential, and privacy boundaries for F002."""

import json
import logging
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from company_ai.application.research.execute_workflow import run_workflow
from company_ai.infrastructure.research.ai.llm_adapter import CrewAIAdapter
from company_ai.infrastructure.research.ai.transports import LLMResponse
from company_ai.infrastructure.research.persistence.models import Attempt
from company_ai.infrastructure.telemetry.langfuse import LangfuseObserver
from tests.integration.test_llm_routing_workflow import RoutedAdapter, configure


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_missing_key_blocks_only_the_selected_hosted_step(
    repo, payload, provider, monkeypatch
):
    configure(repo.settings, all_provider="ollama")
    config = json.loads(repo.settings.llm_routing_json)
    hosted = config["profiles"]["report"]
    hosted["provider"] = provider
    hosted["model"] = f"test-{provider}"
    hosted["pricing"] = {
        "kind": "estimated_token_rates",
        "input_usd_per_million": 1,
        "output_usd_per_million": 4,
    }
    repo.settings.llm_routing_json = json.dumps(config)
    # Neither key is needed to create the job or execute its two local steps.
    repo.settings.openai_api_key = repo.settings.anthropic_api_key = SecretStr("")
    job, _ = await repo.create_job_idempotently("missing-selected-key", payload)
    adapter = CrewAIAdapter(repo.settings)
    generations = RoutedAdapter()

    async def local_generation(request):
        result = await generations.execute(
            request.step, json.loads(request.messages[1]["content"]), request.profile
        )
        return LLMResponse(
            content=json.dumps(result.output),
            provider="ollama",
            model=request.profile.model,
            tokens_input=100,
            tokens_output=50,
        )

    async def unavailable_identity(profile):
        return None

    async def forbidden_network(*args, **kwargs):
        raise AssertionError("Credential test must not perform provider network requests")

    import httpx

    monkeypatch.setattr(httpx.AsyncClient, "request", forbidden_network)
    monkeypatch.setattr(adapter.transports["ollama"], "generate", local_generation)
    monkeypatch.setattr(adapter.transports["ollama"], "model_identity", unavailable_identity)
    try:
        await run_workflow(job.id, repo, adapter, repo.settings)
    finally:
        await adapter.aclose()
    state, steps = await repo.get_job(job.id)
    assert generations.calls == ["RESEARCH", "CLASSIFY"]
    assert [step.status for step in steps] == [
        "COMPLETED",
        "COMPLETED",
        "COMPLETED",
        "NEEDS_REVIEW",
    ]
    assert state.error_code == "EXECUTION_CONFIG_UNAVAILABLE"
    assert state.total_estimated_cost_usd == 0
    assert (await repo.metrics())["llm_calls"] == 2


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_unknown_generation_keeps_profile_reservation_and_never_auto_retries(
    repo, payload, provider
):
    configure(repo.settings, all_provider=provider)

    class TimeoutAdapter(RoutedAdapter):
        async def execute(self, step, payload, profile=None):
            self.calls.append(step)
            raise TimeoutError("private-provider-body-and-api-key")

    job, _ = await repo.create_job_idempotently("unknown-paid-generation", payload)
    adapter = TimeoutAdapter()
    await run_workflow(job.id, repo, adapter, repo.settings)
    state, steps = await repo.get_job(job.id)
    assert state.status == "NEEDS_REVIEW" and steps[0].status == "UNKNOWN"
    assert state.total_estimated_cost_usd > 0
    async with repo.db.session() as session:
        attempt = await session.scalar(select(Attempt).where(Attempt.step_id == steps[0].id))
    assert attempt.provider == provider and attempt.pricing_kind == "estimated_token_rates"
    assert attempt.tokens_input is None and attempt.tokens_output is None
    assert attempt.estimated_cost_usd == state.total_estimated_cost_usd
    assert "private" not in state.error_message
    await run_workflow(job.id, repo, adapter, repo.settings)
    assert adapter.calls == ["RESEARCH"]
    assert (await repo.metrics())["llm_calls"] == 1


@pytest.mark.parametrize("provider", ["openai", "anthropic", "ollama"])
async def test_invalid_evidence_never_becomes_a_checkpoint_for_any_provider(
    repo, payload, provider
):
    configure(repo.settings, all_provider=provider)

    class UnsupportedEvidence(RoutedAdapter):
        async def execute(self, step, payload, profile=None):
            result = await super().execute(step, payload, profile)
            result.output["evidence"] = ["A fabricated customer count of 100000 customers"]
            return result

    job, _ = await repo.create_job_idempotently("unsupported-evidence", payload)
    adapter = UnsupportedEvidence()
    await run_workflow(job.id, repo, adapter, repo.settings)
    state, steps = await repo.get_job(job.id)
    assert state.status == "NEEDS_REVIEW" and state.error_code == "UNSUPPORTED_EVIDENCE"
    assert steps[0].status == "NEEDS_REVIEW" and steps[0].output_json is None
    assert all(step.status == "PENDING" for step in steps[1:])
    assert adapter.calls == ["RESEARCH"]
    async with repo.db.session() as session:
        attempt = await session.scalar(select(Attempt).where(Attempt.step_id == steps[0].id))
    assert attempt.tokens_input == 100 and attempt.tokens_output == 50
    expected_cost = 0 if provider == "ollama" else 0.0003
    assert float(attempt.estimated_cost_usd) == pytest.approx(expected_cost)
    assert float(state.total_estimated_cost_usd) == pytest.approx(expected_cost)
    await run_workflow(job.id, repo, adapter, repo.settings)
    assert adapter.calls == ["RESEARCH"]


async def test_route_endpoints_and_secrets_are_absent_from_public_and_telemetry_metadata(
    repo, payload, caplog, client_factory, app_factory
):
    configure(repo.settings, all_provider="ollama")
    repo.settings.ollama_base_url = "http://private-worker-identity:11434"
    repo.settings.openai_api_key = SecretStr("private-hosted-api-key")
    captured = []

    class TelemetryClient:
        @contextmanager
        def start_as_current_observation(self, **fields):
            captured.append(fields)
            yield SimpleNamespace(update=lambda **fields: captured.append(fields))

    observer = LangfuseObserver(TelemetryClient(), None, capture_content=False)
    job, _ = await repo.create_job_idempotently("private-route", payload)
    with caplog.at_level(logging.INFO, logger="company_ai"):
        await run_workflow(job.id, repo, RoutedAdapter(), repo.settings, observer=observer)
    client = await client_factory(app_factory())
    job_response = await client.get(f"/api/v1/research/jobs/{job.id}")
    events_response = await client.get(f"/api/v1/research/jobs/{job.id}/events")
    assert job_response.status_code == events_response.status_code == 200
    exposed = json.dumps(captured) + caplog.text + job_response.text + events_response.text
    assert "private-worker-identity" not in exposed
    assert "private-hosted-api-key" not in exposed
    assert "endpoint_fingerprint" not in exposed
    assert "connection_ref" not in exposed
    assert all("input" not in record and "output" not in record for record in captured)
    assert payload["company_description"] not in json.dumps(captured) + caplog.text
