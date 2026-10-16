import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import update

from company_ai.application.execute_workflow import run_workflow
from company_ai.domain.contracts import InjectedCrash, SafeFailure
from company_ai.infrastructure.database.models import Step, now
from tests.fakes import CountingAdapter


async def test_full_job_and_redelivery(repo, payload):
    job, _ = await repo.create_job_idempotently("full", payload)
    adapter = CountingAdapter()
    await run_workflow(job.id, repo, adapter, repo.settings)
    completed, steps = await repo.get_job(job.id)
    assert completed.status == "COMPLETED"
    assert completed.result_json["score"]["score"] == 100
    assert all((step.attempt_count == 1 for step in steps))
    await run_workflow(job.id, repo, adapter, repo.settings)
    assert adapter.calls == ["RESEARCH", "CLASSIFY", "REPORT"]


async def test_checkpoint_resume_and_version_change(repo, payload):
    job, _ = await repo.create_job_idempotently("resume", payload)
    adapter = CountingAdapter()
    repo.settings.failure_point = "checkpoint"
    with pytest.raises(InjectedCrash):
        await run_workflow(job.id, repo, adapter, repo.settings)
    assert adapter.calls == ["RESEARCH"]
    repo.settings.failure_point = ""
    await run_workflow(job.id, repo, adapter, repo.settings)
    assert (await repo.get_job(job.id))[0].status == "COMPLETED"
    assert adapter.calls == ["RESEARCH", "CLASSIFY", "REPORT"]
    second, _ = await repo.create_job_idempotently("version", payload)
    repo.settings.failure_point = "checkpoint"
    with pytest.raises(InjectedCrash):
        await run_workflow(second.id, repo, adapter, repo.settings)
    repo.settings.failure_point = ""
    repo.settings.openai_model = "changed-model"
    before = len(adapter.calls)
    await run_workflow(second.id, repo, adapter, repo.settings)
    assert len(adapter.calls) == before
    assert (await repo.get_job(second.id))[0].error_code == "CHECKPOINT_VERSION_MISMATCH"


@pytest.mark.parametrize("point,calls", [("claim", 0), ("request_started", 0), ("response", 1)])
async def test_ambiguous_crash_never_auto_retried(repo, payload, point, calls):
    job, _ = await repo.create_job_idempotently(point, payload)
    adapter = CountingAdapter()
    repo.settings.failure_point = point
    with pytest.raises(InjectedCrash):
        await run_workflow(job.id, repo, adapter, repo.settings)
    assert len(adapter.calls) == calls

    async def db_operation_3(s):
        return await s.execute(
            update(Step)
            .where(Step.job_id == job.id, Step.status == "RUNNING")
            .values(lease_expires_at=now() - timedelta(seconds=1))
        )

    await repo.db.write(db_operation_3)
    await repo.scan_stale_steps()
    repo.settings.failure_point = ""
    await run_workflow(job.id, repo, adapter, repo.settings)
    assert len(adapter.calls) == calls
    assert (await repo.get_job(job.id))[0].status == "NEEDS_REVIEW"
    assert not await repo.list_resumable_jobs()


async def test_timeout_and_budget(repo, payload):

    class TimeoutAdapter:
        async def execute(self, *_):
            raise TimeoutError("DO NOT LEAK PRIVATE DATA")

    job, _ = await repo.create_job_idempotently("timeout", payload)
    await run_workflow(job.id, repo, TimeoutAdapter(), repo.settings)
    state, steps = await repo.get_job(job.id)
    assert state.status == "NEEDS_REVIEW" and steps[0].status == "UNKNOWN"
    assert "PRIVATE" not in state.error_message
    repo.settings.max_estimated_usd_per_job = 1e-07
    job, _ = await repo.create_job_idempotently("budget", payload)
    adapter = CountingAdapter()
    await run_workflow(job.id, repo, adapter, repo.settings)
    assert adapter.calls == []
    assert (await repo.get_job(job.id))[0].error_code == "BUDGET_EXCEEDED"


async def test_concurrent_deliveries(repo, payload):
    job, _ = await repo.create_job_idempotently("race", payload)
    adapter = CountingAdapter()
    await asyncio.gather(*(run_workflow(job.id, repo, adapter, repo.settings) for _ in range(2)))
    assert (await repo.get_job(job.id))[0].status == "COMPLETED"
    assert adapter.calls == ["RESEARCH", "CLASSIFY", "REPORT"]


async def test_429_bounded_backoff(repo, payload):

    class RateLimited:
        async def execute(self, *_):
            raise SafeFailure("RATE_LIMITED")

    job, _ = await repo.create_job_idempotently("429", payload)
    for attempt in range(repo.settings.max_step_attempts):
        await run_workflow(job.id, repo, RateLimited(), repo.settings)
        state, steps = await repo.get_job(job.id)
        assert steps[0].attempt_count == attempt + 1
        if attempt < repo.settings.max_step_attempts - 1:
            assert steps[0].status == "PENDING"
            await run_workflow(job.id, repo, RateLimited(), repo.settings)
            assert (await repo.get_job(job.id))[1][0].attempt_count == attempt + 1

            async def db_operation_4(s, step_id=steps[0].id):
                return await s.execute(
                    update(Step)
                    .where(Step.id == step_id)
                    .values(next_retry_at=now() - timedelta(seconds=1))
                )

            await repo.db.write(db_operation_4)
    assert state.status == "FAILED"
    assert float(state.total_estimated_cost_usd) == 0
