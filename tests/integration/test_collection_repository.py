import asyncio
import json
from datetime import timedelta

import pytest
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import select, text, update
from sqlalchemy.exc import OperationalError

from alembic import command
from company_ai.domain.research.collection import CollectionResult
from company_ai.domain.research.contracts import ALL_STEPS, STEPS
from company_ai.domain.research.errors import Conflict
from company_ai.domain.research.hashing import digest
from company_ai.infrastructure.database.session import Database
from company_ai.infrastructure.research.persistence.models import Attempt, Job, Step, now
from company_ai.infrastructure.research.persistence.readiness import research_database_ready
from company_ai.infrastructure.research.persistence.repositories import SqlAlchemyResearchRepository


async def create_collection(repo, payload, key="collection"):
    job, _ = await repo.create_job_idempotently(key, {**payload, "collection_mode": "tavily"})
    _, steps = await repo.get_job(job.id)
    return job, steps


async def change_step(repo, step_id, **values):
    async def change(s):
        await s.execute(update(Step).where(Step.id == step_id).values(**values))

    await repo.db.write(change)


async def test_manual_and_collection_workflows_are_versioned_separately(repo, payload):
    manual, _ = await repo.create_job_idempotently("manual", payload)
    _, manual_steps = await repo.get_job(manual.id)
    collected, collection_steps = await create_collection(repo, payload)
    assert manual.workflow_version == "manual-v1"
    assert collected.workflow_version == "tavily-v1"
    assert tuple(step.step_name for step in manual_steps) == STEPS
    assert tuple(step.step_name for step in collection_steps) == ALL_STEPS
    assert await repo.claim_step(collection_steps[1].id, "research-hash") is None


async def test_legacy_idempotency_defaults_preserve_original_input_and_hash(repo, payload):
    legacy = {name: value for name, value in payload.items() if name != "website_text"}
    job, _ = await repo.create_job_idempotently("legacy", legacy)
    duplicate, created = await repo.create_job_idempotently(
        "legacy", {**legacy, "website_text": "", "collection_mode": "manual"}
    )
    assert not created and duplicate.id == job.id
    assert duplicate.input_json == legacy
    assert duplicate.request_hash == digest(legacy)
    with pytest.raises(Conflict, match="IDEMPOTENCY_KEY_CONFLICT"):
        await repo.create_job_idempotently("legacy", {**legacy, "collection_mode": "tavily"})
    with pytest.raises(Conflict, match="IDEMPOTENCY_KEY_CONFLICT"):
        await repo.create_job_idempotently("legacy", {**legacy, "website_text": "New source"})


async def test_concurrent_collection_claim_and_dispatch_intent_are_atomic(repo, payload):
    job, steps = await create_collection(repo, payload)
    claims = await asyncio.gather(*(repo.claim_step(steps[0].id, "collect-hash") for _ in range(3)))
    assert sum(claim is not None for claim in claims) == 1
    claim = next(claim for claim in claims if claim is not None)
    starts = await asyncio.gather(*(repo.collection_request_started(claim) for _ in range(3)))
    assert starts.count(True) == 1
    assert not await repo.request_started(claim, 0.20)
    metrics = await repo.metrics()
    assert metrics["collection_calls"] == 1
    assert metrics["llm_calls"] == 0
    assert metrics["estimated_cost_usd"] == 0
    assert metrics["collection_credits"] is None
    async with repo.db.session() as s:
        attempt = await s.get(Attempt, claim.attempt_id)
        assert attempt.collection_started_at is not None
        assert attempt.llm_started_at is None
        assert attempt.estimated_cost_usd is None
    assert float((await repo.get_job(job.id))[0].total_estimated_cost_usd) == 0


async def test_collection_checkpoint_has_separate_usage_and_unknown_usd(repo, payload):
    job, steps = await create_collection(repo, payload)
    claim = await repo.claim_step(steps[0].id, "collect-hash")
    assert await repo.collection_request_started(claim)
    output = {"sources": [], "usage": {"credits": 1}, "credits": 1.0}
    result = CollectionResult(output=output, provider_request_id="tavily-request-1")
    assert await repo.complete_step_with_fencing(claim, output, result, cost=100)
    saved, saved_steps = await repo.get_job(job.id)
    assert saved_steps[0].output_json == output
    assert float(saved.total_estimated_cost_usd) == 0
    async with repo.db.session() as s:
        attempt = await s.get(Attempt, claim.attempt_id)
        assert attempt.collection_usage == {"usage": output["usage"], "credits": 1.0}
        assert attempt.provider_request_id == "tavily-request-1"
        assert attempt.tokens_input is None and attempt.tokens_output is None
        assert attempt.estimated_cost_usd is None
    metrics = await repo.metrics()
    assert metrics["collection_usage"] == {"credits": 1}
    assert metrics["collection_credits"] == 1
    assert metrics["llm_calls"] == 0
    assert await repo.claim_step(steps[1].id, "research-hash") is not None
    assert await repo.claim_step(steps[0].id, "new-collect-hash") is None


async def test_collection_without_usage_retains_unknown_credits(repo, payload):
    _, steps = await create_collection(repo, payload)
    claim = await repo.claim_step(steps[0].id, "collect-hash")
    assert await repo.collection_request_started(claim)
    result = CollectionResult(output={"sources": []})
    assert await repo.complete_step_with_fencing(claim, result.output, result)
    metrics = await repo.metrics()
    assert metrics["collection_usage"] == {}
    assert metrics["collection_credits"] is None


@pytest.mark.parametrize("collect_limit, workflow_limit", [(2, 3), (3, 2)])
async def test_collection_safe_retry_backoff_and_attempt_ceiling(
    repo, payload, collect_limit, workflow_limit
):
    repo.settings.collect_max_attempts = collect_limit
    repo.settings.max_step_attempts = workflow_limit
    job, steps = await create_collection(repo, payload)
    first = await repo.claim_step(steps[0].id, "collect-hash")
    assert await repo.collection_request_started(first)
    assert await repo.safe_retry(first)
    assert await repo.claim_step(steps[0].id, "collect-hash") is None
    assert job.id not in await repo.list_resumable_jobs()
    saved, saved_steps = await repo.get_job(job.id)
    assert saved.status == "PENDING"
    assert saved_steps[0].next_retry_at > now()
    await change_step(repo, steps[0].id, next_retry_at=now() - timedelta(seconds=1))
    assert job.id in await repo.list_resumable_jobs()
    second = await repo.claim_step(steps[0].id, "collect-hash")
    assert await repo.collection_request_started(second)
    assert await repo.safe_retry(second)
    assert (await repo.get_job(job.id))[0].status == "FAILED"
    assert await repo.claim_step(steps[0].id, "collect-hash") is None
    assert job.id not in await repo.list_resumable_jobs()
    metrics = await repo.metrics()
    assert metrics["collection_calls"] == min(collect_limit, workflow_limit)
    assert metrics["llm_calls"] == 0 and metrics["estimated_cost_usd"] == 0
    async with repo.db.session() as s:
        attempts = list(await s.scalars(select(Attempt).where(Attempt.step_id == steps[0].id)))
        assert all(attempt.estimated_cost_usd is None for attempt in attempts)


async def test_unknown_collection_requires_manual_retry_and_stale_worker_is_fenced(repo, payload):
    job, steps = await create_collection(repo, payload)
    old = await repo.claim_step(steps[0].id, "collect-hash")
    assert await repo.collection_request_started(old)
    await change_step(repo, old.step_id, lease_expires_at=now() - timedelta(seconds=1))
    assert not await repo.collection_request_started(old)
    assert not await repo.complete_step_with_fencing(old, {"late": True})
    recovered = await asyncio.gather(repo.scan_stale_steps(), repo.scan_stale_steps())
    assert sum(map(len, recovered)) == 1
    assert (await repo.get_job(job.id))[0].status == "NEEDS_REVIEW"
    assert job.id not in await repo.list_resumable_jobs()
    await repo.retry_unknown(job.id, "COLLECT")
    fresh = await repo.claim_step(steps[0].id, "collect-hash")
    assert fresh is not None
    assert not await repo.complete_step_with_fencing(old, {"late": True})
    assert await repo.collection_request_started(fresh)
    assert await repo.fail(fresh, "UNKNOWN", "COLLECT_REQUEST_UNCERTAIN")
    with pytest.raises(Conflict, match="RETRY_NOT_ALLOWED"):
        await repo.retry_unknown(job.id, "COLLECT")


async def test_collection_dispatch_enforces_durable_request_counter(repo, payload):
    repo.settings.collect_max_attempts = 1
    job, steps = await create_collection(repo, payload)
    claim = await repo.claim_step(steps[0].id, "collect-hash")
    assert await repo.collection_request_started(claim)
    assert await repo.fail(claim, "UNKNOWN", "COLLECT_REQUEST_UNCERTAIN")
    # Even inconsistent legacy/admin state cannot bypass the durable intent ledger.
    await change_step(repo, steps[0].id, status="PENDING", attempt_count=0)

    async def reopen(s):
        await s.execute(update(Job).where(Job.id == job.id).values(status="PENDING"))
        # Keep the unique attempt number valid while testing the independent gate.
        await s.execute(update(Attempt).where(Attempt.id == claim.attempt_id).values(attempt_no=10))

    await repo.db.write(reopen)
    extra = await repo.claim_step(steps[0].id, "collect-hash")
    assert extra is not None
    assert not await repo.collection_request_started(extra)
    saved, _ = await repo.get_job(job.id)
    assert saved.status == "NEEDS_REVIEW"
    assert saved.error_code == "COLLECT_ATTEMPT_LIMIT_EXCEEDED"
    assert (await repo.metrics())["collection_calls"] == 1


async def test_collection_gate_cannot_accept_a_manual_step(repo, payload):
    job, _ = await repo.create_job_idempotently("manual", payload)
    _, steps = await repo.get_job(job.id)
    claim = await repo.claim_step(steps[0].id, "hash")
    assert not await repo.collection_request_started(claim)
    assert (await repo.metrics())["collection_calls"] == 0


async def test_collection_migration_preserves_populated_legacy_sqlite(
    settings, tmp_path, monkeypatch, payload
):
    from company_ai import settings as settings_module

    settings.database_url = SecretStr(f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}")
    monkeypatch.setattr(settings_module, "get_settings", lambda: settings)
    config = Config("alembic.ini")
    await asyncio.to_thread(command.upgrade, config, "26a1016_async_postgres")
    db = Database(settings.database_dsn, settings=settings)
    try:
        # Connectivity and an old Alembic revision alone are insufficient readiness.
        with pytest.raises(OperationalError):
            await research_database_ready(db)
        async with db.engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO ai_jobs (id,idempotency_key,request_hash,status,input_json,"
                    "total_estimated_cost_usd,created_at,updated_at) VALUES "
                    "(:id,:key,:hash,'PENDING',:input,0,:stamp,:stamp)"
                ),
                {
                    "id": "legacy-job",
                    "key": "legacy-key",
                    "hash": digest(payload),
                    "input": json.dumps(payload),
                    "stamp": now().replace(tzinfo=None),
                },
            )
            for name in STEPS:
                await connection.execute(
                    text(
                        "INSERT INTO ai_steps "
                        "(id,job_id,step_name,status,attempt_count,updated_at) "
                        "VALUES (:id,'legacy-job',:name,'PENDING',0,:stamp)"
                    ),
                    {"id": f"legacy-{name}", "name": name, "stamp": now().replace(tzinfo=None)},
                )
        await asyncio.to_thread(command.upgrade, config, "head")
        await asyncio.to_thread(command.check, config)
        await research_database_ready(db)
        repository = SqlAlchemyResearchRepository(db, settings)
        legacy, steps = await repository.get_job("legacy-job")
        assert legacy.workflow_version == "manual-v1"
        assert legacy.input_json == payload
        assert tuple(step.step_name for step in steps) == STEPS
        duplicate, created = await repository.create_job_idempotently(
            "legacy-key", {**payload, "collection_mode": "manual"}
        )
        assert not created and duplicate.id == legacy.id
        assert await repository.claim_step(steps[0].id, "legacy-hash") is not None
    finally:
        await db.dispose()
