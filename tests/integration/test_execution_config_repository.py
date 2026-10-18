import asyncio
import json
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import update

from company_ai.domain.research.contracts import AIResult
from company_ai.domain.research.errors import Conflict
from company_ai.domain.research.llm import ExecutionConfig
from company_ai.infrastructure.research.persistence.models import Attempt, Job, Step, now


def route_metadata(job, name="RESEARCH"):
    profile = ExecutionConfig.model_validate(job.execution_config).steps[name]
    return {
        "provider": profile.provider,
        "model": profile.model,
        "profile_id": profile.profile_id,
        "config_fingerprint": profile.config_fingerprint,
        "pricing_kind": profile.pricing.kind,
        "adapter_version": profile.adapter_version,
    }


async def first_claim(repo, payload, key="execution"):
    job, _ = await repo.create_job_idempotently(key, payload)
    _, steps = await repo.get_job(job.id)
    return job, steps, await repo.claim_step(steps[0].id, "research-input")


async def test_snapshot_idempotency_ignores_changed_or_unavailable_server_config(repo, payload):
    repo.settings.openai_api_key = "operator-secret"
    job, created = await repo.create_job_idempotently("snapshot", payload)
    assert created
    config = ExecutionConfig.model_validate(job.execution_config)
    assert set(config.steps) == {"RESEARCH", "CLASSIFY", "REPORT"}
    assert config.steps["REPORT"].model == repo.settings.openai_model
    assert "operator-secret" not in json.dumps(job.execution_config)

    repo.settings.llm_routing_json = "invalid changed configuration"
    same, created = await repo.create_job_idempotently("snapshot", payload)
    assert not created and same.id == job.id
    assert same.execution_config == job.execution_config
    with pytest.raises(Conflict, match="IDEMPOTENCY_KEY_CONFLICT"):
        await repo.create_job_idempotently("snapshot", {**payload, "icp": "changed"})
    with pytest.raises(ValueError):
        await repo.create_job_idempotently("new-invalid", payload)

    repo.settings.llm_routing_json = None
    repo.settings.openai_model = "new-operator-model"
    new, _ = await repo.create_job_idempotently("new-snapshot", payload)
    assert new.request_hash == job.request_hash
    assert new.execution_config != job.execution_config
    assert ExecutionConfig.model_validate(new.execution_config).steps["REPORT"].model == (
        "new-operator-model"
    )
    assert (await repo.get_job(job.id))[0].execution_config == job.execution_config


async def test_concurrent_job_creation_keeps_one_snapshot(repo, payload):
    jobs = await asyncio.gather(
        *(repo.create_job_idempotently("concurrent-snapshot", payload) for _ in range(3))
    )
    assert len({job.id for job, _ in jobs}) == 1
    assert sum(created for _, created in jobs) == 1
    assert all(job.execution_config == jobs[0][0].execution_config for job, _ in jobs)


async def test_attempt_audit_is_atomic_sanitized_and_preserved_after_unknown(repo, payload):
    job, _, claim = await first_claim(repo, payload)
    metadata = route_metadata(job)
    unsafe_metadata = {
        **metadata,
        "endpoint_fingerprint": "private-endpoint-fingerprint",
        "connection_ref": "private-connection",
        "url": "http://internal-host:11434",
        "secret": "private-api-key",
    }
    starts = await asyncio.gather(
        *(repo.request_started(claim, 0.05, unsafe_metadata) for _ in range(3))
    )
    assert starts.count(True) == 1
    assert await repo.fail(claim, "UNKNOWN", "PROVIDER_TIMEOUT")
    saved = (await repo.get_job(job.id))[0]
    assert float(saved.total_estimated_cost_usd) == 0.05
    async with repo.db.session() as session:
        attempt = await session.get(Attempt, claim.attempt_id)
        assert all(getattr(attempt, key) == value for key, value in metadata.items())
        assert attempt.llm_started_at is not None
        assert float(attempt.estimated_cost_usd) == 0.05
    audit = json.dumps([event.details_json for event in await repo.events(job.id)])
    assert "private-" not in audit and "internal-host" not in audit
    assert metadata["config_fingerprint"] in audit
    usage = (await repo.metrics())["llm_usage"]
    assert usage == [
        {
            "provider": metadata["provider"],
            "model": metadata["model"],
            "pricing_kind": metadata["pricing_kind"],
            "calls": 1,
            "tokens_input": None,
            "tokens_output": None,
            "estimated_cost_usd": 0.05,
        }
    ]


async def test_local_zero_reservation_still_counts_calls_and_reconciles_usage(repo, payload):
    repo.settings.max_llm_calls_per_job = 1
    job, steps, claim = await first_claim(repo, payload)
    metadata = {
        **route_metadata(job),
        "provider": "ollama",
        "model": "installed-local-model",
        "pricing_kind": "local_unmetered",
        "adapter_version": "ollama-native-v1",
    }
    assert await repo.request_started(claim, 0, metadata)
    assert await repo.complete_step_with_fencing(
        claim, {"accepted": True}, AIResult({}, tokens_input=23, tokens_output=11), cost=0
    )
    classify = await repo.claim_step(steps[1].id, "classify-input")
    assert not await repo.request_started(classify, 0, metadata)
    saved = (await repo.get_job(job.id))[0]
    assert saved.error_code == "BUDGET_EXCEEDED"
    assert float(saved.total_estimated_cost_usd) == 0
    metrics = await repo.metrics()
    assert metrics["llm_calls"] == 1
    assert metrics["llm_usage"][0]["provider"] == "ollama"
    assert metrics["llm_usage"][0]["tokens_input"] == 23
    assert metrics["llm_usage"][0]["tokens_output"] == 11
    assert metrics["llm_usage"][0]["estimated_cost_usd"] == 0


async def test_legacy_attempt_metrics_keep_unknown_provider_and_model(repo, payload):
    job, _, claim = await first_claim(repo, payload)

    async def make_legacy(session):
        await session.execute(update(Job).where(Job.id == job.id).values(execution_config=None))

    await repo.db.write(make_legacy)
    assert await repo.request_started(claim, 0.01)
    assert await repo.complete_step_with_fencing(claim, {"legacy": True})
    saved = (await repo.get_job(job.id))[0]
    assert saved.execution_config is None
    usage = (await repo.metrics())["llm_usage"][0]
    assert usage["provider"] is None and usage["model"] is None
    assert usage["pricing_kind"] is None
    assert float(saved.total_estimated_cost_usd) == 0.01


@pytest.mark.parametrize("cost", [0.00012, None])
async def test_rejected_output_keeps_usage_and_reconciles_only_known_cost(repo, payload, cost):
    job, _, claim = await first_claim(repo, payload)
    result = AIResult(
        {"rejected": True}, tokens_input=100, tokens_output=50, provider_request_id="request-known"
    )
    assert not await repo.record_llm_usage(claim, result, cost)
    assert await repo.request_started(claim, 0.04, route_metadata(job))
    stale = replace(claim, token="stale-token")
    assert not await repo.record_llm_usage(stale, result, cost)
    assert await repo.record_llm_usage(claim, result, cost)
    assert await repo.fail(claim, "NEEDS_REVIEW", "UNSUPPORTED_EVIDENCE")
    assert not await repo.record_llm_usage(claim, result, 0)
    saved, steps = await repo.get_job(job.id)
    assert steps[0].output_json is None
    assert saved.error_code == "UNSUPPORTED_EVIDENCE"
    assert float(saved.total_estimated_cost_usd) == (0.04 if cost is None else cost)
    async with repo.db.session() as session:
        attempt = await session.get(Attempt, claim.attempt_id)
        assert attempt.status == "FAILED"
        assert attempt.tokens_input == 100 and attempt.tokens_output == 50
        assert attempt.provider_request_id == "request-known"


async def test_model_digest_pin_is_atomic_fenced_and_does_not_mutate_snapshot(repo, payload):
    job, _, claim = await first_claim(repo, payload)
    stale_claim = replace(claim, token="stale-token")
    assert not await repo.pin_model_identity(stale_claim, "profile", "digest-stale")
    pins = await asyncio.gather(
        *(repo.pin_model_identity(claim, "profile", value) for value in ("digest-a", "digest-b"))
    )
    assert pins.count(True) == 1
    saved = (await repo.get_job(job.id))[0]
    expected = "digest-a" if pins[0] else "digest-b"
    assert saved.execution_identity == {"profile": {"digest": expected, "verified": True}}
    assert await repo.pin_model_identity(claim, "profile", expected)
    assert saved.execution_config == job.execution_config
    assert saved.request_hash == job.request_hash

    async def expire(session):
        await session.execute(
            update(Step)
            .where(Step.id == claim.step_id)
            .values(lease_expires_at=now() - timedelta(seconds=1))
        )

    await repo.db.write(expire)
    assert not await repo.pin_model_identity(claim, "another-profile", "digest-c")
    assert not await repo.request_started(claim, 0, route_metadata(job))
    assert (await repo.metrics())["llm_calls"] == 0


async def test_model_digest_unknown_is_explicit_and_not_fabricated(repo, payload):
    job, _, claim = await first_claim(repo, payload)
    assert await repo.pin_model_identity(claim, "profile", None)
    assert await repo.pin_model_identity(claim, "profile", None)
    assert not await repo.pin_model_identity(claim, "profile", "later-digest")
    saved = (await repo.get_job(job.id))[0]
    assert saved.execution_identity == {"profile": {"digest": None, "verified": False}}
    async with repo.db.session() as session:
        attempt = await session.get(Attempt, claim.attempt_id)
        assert attempt.status == "CLAIMED"


async def test_job_api_exposes_only_public_route_metadata(
    repo, payload, client_factory, app_factory
):
    client = await client_factory(app_factory())
    created = await client.post(
        "/api/v1/research/jobs", json=payload, headers={"Idempotency-Key": "metadata"}
    )
    job_id = created.json()["job_id"]
    response = await client.get(f"/api/v1/research/jobs/{job_id}")
    assert response.status_code == 200
    data = response.json()
    metadata = data["execution_metadata"]
    assert set(metadata) == {"RESEARCH", "CLASSIFY", "REPORT"}
    for route in metadata.values():
        assert set(route) == {
            "provider",
            "model",
            "profile_id",
            "config_fingerprint",
            "pricing_kind",
        }
    for step in data["steps"]:
        assert step["execution_metadata"] == metadata.get(step["step_name"])
    assert "endpoint" not in response.text
    assert "connection_ref" not in response.text

    async def make_legacy(session):
        await session.execute(update(Job).where(Job.id == job_id).values(execution_config=None))

    await repo.db.write(make_legacy)
    legacy = (await client.get(f"/api/v1/research/jobs/{job_id}")).json()
    assert legacy["execution_metadata"] is None
    assert all(step["execution_metadata"] is None for step in legacy["steps"])
