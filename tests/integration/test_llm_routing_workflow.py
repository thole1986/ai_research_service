"""F002 routing, compatibility and budget acceptance without provider network I/O."""

import json
from dataclasses import replace

import pytest
from sqlalchemy import select, update

from company_ai.application.research.execute_workflow import input_hash, run_workflow, step_payload
from company_ai.domain.research.contracts import InjectedCrash
from company_ai.domain.research.llm import resolve_execution_config
from company_ai.infrastructure.research.persistence.models import Attempt, Job
from tests.fakes import CountingAdapter
from tests.integration.test_collection_workflow import (
    CollectedAdapter,
    FakeCollector,
    collection_payload,
)


def configure(settings, *, report_provider="ollama", all_provider=None, context=32000):
    profiles = {}
    routes = {}
    for step in ("RESEARCH", "CLASSIFY", "REPORT"):
        provider = all_provider or (report_provider if step == "REPORT" else "openai")
        profiles[step.lower()] = {
            "provider": provider,
            "model": f"test-{provider}-model",
            "temperature": 0,
            "max_output_tokens": 500,
            "timeout_seconds": 60,
            "context_window_tokens": context,
            "pricing": {"kind": "local_unmetered"}
            if provider == "ollama"
            else {
                "kind": "estimated_token_rates",
                "input_usd_per_million": 2 if step == "CLASSIFY" else 1,
                "output_usd_per_million": 4,
            },
        }
        routes[step] = step.lower()
    settings.llm_routing_json = json.dumps({"version": 1, "profiles": profiles, "steps": routes})


class RoutedAdapter(CountingAdapter):
    def __init__(self, *, usage=True, model_digest="sha256:test"):
        super().__init__()
        self.profiles = []
        self.usage = usage
        self.model_digest = model_digest

    async def model_identity(self, profile):
        assert profile.provider == "ollama"
        return self.model_digest

    async def execute(self, step, payload, profile=None):
        self.profiles.append(profile)
        result = await super().execute(step, payload, profile)
        if not self.usage:
            result.tokens_input = result.tokens_output = None
        return result


@pytest.mark.parametrize("provider", ["ollama", "anthropic"])
async def test_mixed_routes_are_audited_and_reconciled(repo, payload, provider):
    configure(repo.settings, report_provider=provider)
    job, _ = await repo.create_job_idempotently("mixed-" + provider, payload)
    adapter = RoutedAdapter()
    await run_workflow(job.id, repo, adapter, repo.settings)
    state, steps = await repo.get_job(job.id)
    assert state.status == "COMPLETED"
    assert adapter.calls == ["RESEARCH", "CLASSIFY", "REPORT"]
    assert [p.provider for p in adapter.profiles] == ["openai", "openai", provider]
    expected = 0.0003 + 0.0004 + (0 if provider == "ollama" else 0.0003)
    assert float(state.total_estimated_cost_usd) == pytest.approx(expected)
    async with repo.db.session() as session:
        attempts = list(await session.scalars(select(Attempt)))
    llm = [a for a in attempts if a.llm_started_at]
    assert {a.provider for a in llm} == {"openai", provider}
    assert all(a.config_fingerprint and a.profile_id and a.adapter_version for a in llm)
    score = next(a for a in attempts if not a.llm_started_at)
    assert score.provider is None and score.model is None
    assert all(step.status == "COMPLETED" for step in steps)


async def test_idempotency_and_resume_keep_saved_routes_after_environment_change(repo, payload):
    configure(repo.settings)
    original, _ = await repo.create_job_idempotently("frozen", payload)
    adapter = RoutedAdapter()
    repo.settings.failure_point = "checkpoint"
    with pytest.raises(InjectedCrash):
        await run_workflow(original.id, repo, adapter, repo.settings)
    repo.settings.failure_point = ""
    configure(repo.settings, all_provider="anthropic")
    repo.settings.openai_model = "different-legacy-model"
    same, created = await repo.create_job_idempotently("frozen", payload)
    assert not created and same.id == original.id
    assert same.execution_config == original.execution_config
    await run_workflow(same.id, repo, adapter, repo.settings)
    assert [p.provider for p in adapter.profiles] == ["openai", "openai", "ollama"]
    assert (await repo.get_job(same.id))[0].status == "COMPLETED"
    new, _ = await repo.create_job_idempotently("new-routes", payload)
    assert new.execution_config != same.execution_config
    assert {p["provider"] for p in new.execution_config["steps"].values()} == {"anthropic"}


@pytest.mark.parametrize("usage", [True, False])
async def test_local_calls_cost_zero_and_still_obey_call_limit(repo, payload, usage):
    configure(repo.settings, all_provider="ollama")
    repo.settings.max_llm_calls_per_job = 2
    job, _ = await repo.create_job_idempotently("local-limit", payload)
    adapter = RoutedAdapter(usage=usage)
    await run_workflow(job.id, repo, adapter, repo.settings)
    state, _ = await repo.get_job(job.id)
    assert state.error_code == "BUDGET_EXCEEDED"
    assert adapter.calls == ["RESEARCH", "CLASSIFY"]
    assert state.total_estimated_cost_usd == 0
    assert (await repo.metrics())["llm_calls"] == 2


async def test_unknown_usage_keeps_paid_reservations(repo, payload):
    configure(repo.settings, all_provider="openai")
    job, _ = await repo.create_job_idempotently("unknown-usage", payload)
    await run_workflow(job.id, repo, RoutedAdapter(usage=False), repo.settings)
    state, _ = await repo.get_job(job.id)
    assert state.status == "COMPLETED" and state.total_estimated_cost_usd > 0
    async with repo.db.session() as session:
        attempts = list(
            await session.scalars(select(Attempt).where(Attempt.llm_started_at.is_not(None)))
        )
    assert float(state.total_estimated_cost_usd) == pytest.approx(
        sum(float(a.estimated_cost_usd) for a in attempts)
    )
    assert all(a.tokens_input is None for a in attempts)


@pytest.mark.parametrize("change", ["endpoint", "lease", "adapter"])
async def test_unavailable_snapshot_stops_before_generation(repo, payload, change):
    configure(repo.settings)
    job, _ = await repo.create_job_idempotently("unavailable", payload)
    if change == "endpoint":
        repo.settings.ollama_base_url = "http://other-desktop:11434"
    elif change == "lease":
        repo.settings.lease_ttl_seconds = 65
    else:

        async def corrupt(session):
            config = json.loads(json.dumps(job.execution_config))
            config["steps"]["REPORT"]["adapter_version"] = "unsupported-v2"
            await session.execute(
                update(Job).where(Job.id == job.id).values(execution_config=config)
            )

        await repo.db.write(corrupt)
    adapter = RoutedAdapter()
    await run_workflow(job.id, repo, adapter, repo.settings)
    assert adapter.calls == []
    assert (await repo.get_job(job.id))[0].error_code == "EXECUTION_CONFIG_UNAVAILABLE"
    assert (await repo.metrics())["llm_calls"] == 0


async def test_context_guard_rejects_without_truncation_or_reservation(repo, payload):
    configure(repo.settings, all_provider="ollama", context=1024)
    job, _ = await repo.create_job_idempotently("small-context", payload)
    adapter = RoutedAdapter()
    await run_workflow(job.id, repo, adapter, repo.settings)
    assert adapter.calls == []
    assert (await repo.get_job(job.id))[0].error_code == "CONTEXT_LIMIT_EXCEEDED"
    assert (await repo.metrics())["llm_calls"] == 0


async def test_unavailable_future_snapshot_remains_inspectable(
    repo, payload, client_factory, app_factory
):
    job, _ = await repo.create_job_idempotently("future-snapshot", payload)

    async def future_config(session):
        config = {**job.execution_config, "version": 999}
        await session.execute(update(Job).where(Job.id == job.id).values(execution_config=config))

    await repo.db.write(future_config)
    adapter = RoutedAdapter()
    await run_workflow(job.id, repo, adapter, repo.settings)
    client = await client_factory(app_factory())
    response = await client.get(f"/api/v1/research/jobs/{job.id}")
    assert response.status_code == 200
    assert response.json()["error"]["code"] == "EXECUTION_CONFIG_UNAVAILABLE"
    assert adapter.calls == []


async def test_pinned_digest_drift_blocks_retry_without_generation(repo, payload):
    configure(repo.settings, all_provider="ollama")
    job, _ = await repo.create_job_idempotently("digest", payload)
    adapter = RoutedAdapter()
    repo.settings.failure_step = "CLASSIFY"
    repo.settings.failure_point = "request_started"
    with pytest.raises(InjectedCrash):
        await run_workflow(job.id, repo, adapter, repo.settings)
    # An operator acknowledges the ambiguous attempt after its lease is lost.
    from datetime import timedelta

    from company_ai.infrastructure.research.persistence.models import Step, now

    async def expire(session):
        await session.execute(
            update(Step)
            .where(Step.job_id == job.id, Step.status == "RUNNING")
            .values(lease_expires_at=now() - timedelta(seconds=1))
        )

    await repo.db.write(expire)
    await repo.scan_stale_steps()
    await repo.retry_unknown(job.id, "CLASSIFY")
    repo.settings.failure_point = ""
    adapter.model_digest = "sha256:different"
    await run_workflow(job.id, repo, adapter, repo.settings)
    assert adapter.calls == ["RESEARCH"]
    assert (await repo.get_job(job.id))[0].error_code == "MODEL_IDENTITY_CHANGED"


async def test_legacy_partial_job_preserves_legacy_hash_and_openai_route(repo, payload):
    job, _ = await repo.create_job_idempotently("legacy-partial", payload)

    async def legacy(session):
        await session.execute(update(Job).where(Job.id == job.id).values(execution_config=None))

    await repo.db.write(legacy)
    adapter = RoutedAdapter()
    repo.settings.failure_point = "checkpoint"
    with pytest.raises(InjectedCrash):
        await run_workflow(job.id, repo, adapter, repo.settings)
    configure(repo.settings, all_provider="ollama")
    repo.settings.failure_point = ""
    await run_workflow(job.id, repo, adapter, repo.settings)
    assert (await repo.get_job(job.id))[0].status == "COMPLETED"
    assert adapter.calls == ["RESEARCH", "CLASSIFY", "REPORT"]
    assert adapter.profiles == [None, None, None]


async def test_non_llm_hashes_are_independent_of_provider_routes(repo, payload):
    job, _ = await repo.create_job_idempotently("hashes", payload)
    config = resolve_execution_config(repo.settings)
    research = step_payload("RESEARCH", job, {})
    original = input_hash("RESEARCH", research, repo.settings, config.steps["RESEARCH"])
    assert (
        input_hash(
            "RESEARCH",
            research,
            repo.settings,
            config.steps["RESEARCH"].model_copy(update={"model": "other"}),
        )
        != original
    )
    score_payload = {"research": {}, "classification": {}}
    before = input_hash("SCORE", score_payload, repo.settings)
    configure(repo.settings)
    assert input_hash("SCORE", score_payload, repo.settings) == before
    # Workflow version remains independent from LLM execution config version.
    assert replace(job, execution_config=None).workflow_version == job.workflow_version


async def test_tavily_collection_with_mixed_routes(repo):
    configure(repo.settings)
    job, _ = await repo.create_job_idempotently("tavily-mixed", collection_payload())

    class MixedCollected(CollectedAdapter):
        async def model_identity(self, profile):
            return None

        async def execute(self, step, payload, profile=None):
            assert profile.provider == ("ollama" if step == "REPORT" else "openai")
            return await super().execute(step, payload, profile)

    collector = FakeCollector()
    await run_workflow(job.id, repo, MixedCollected(), repo.settings, collector=collector)
    state, steps = await repo.get_job(job.id)
    assert state.status == "COMPLETED"
    assert len(collector.calls) == 1
    assert [s.step_name for s in steps] == ["COLLECT", "RESEARCH", "CLASSIFY", "SCORE", "REPORT"]
    assert (await repo.metrics())["llm_calls"] == 3
