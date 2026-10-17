import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import update

from company_ai.infrastructure.research.persistence.models import Step, now
from company_ai.infrastructure.research.persistence.repositories import Conflict


async def test_idempotency(repo, payload):
    job, created = await repo.create_job_idempotently("same", payload)
    duplicate, created_again = await repo.create_job_idempotently("same", payload)
    assert created and (not created_again) and (job.id == duplicate.id)
    with pytest.raises(Conflict):
        await repo.create_job_idempotently("same", {**payload, "icp": "other"})


async def test_concurrent_claim_and_fencing(repo, payload):
    job, _ = await repo.create_job_idempotently("claim", payload)
    _, steps = await repo.get_job(job.id)
    claims = await asyncio.gather(*(repo.claim_step(steps[0].id, "hash") for _ in range(2)))
    assert sum((c is not None for c in claims)) == 1
    claim = next((c for c in claims if c))
    assert await repo.claim_step(steps[1].id, "hash") is None

    async def db_operation_2(s):
        return await s.execute(
            update(Step)
            .where(Step.id == claim.step_id)
            .values(lease_expires_at=now() - timedelta(seconds=1))
        )

    await repo.db.write(db_operation_2)
    assert not await repo.complete_step_with_fencing(claim, {"stale": True})
    assert await repo.scan_stale_steps() == [job.id]
    assert await repo.scan_stale_steps() == []
    assert (await repo.get_job(job.id))[0].status == "NEEDS_REVIEW"
    assert await repo.claim_step(claim.step_id, "hash") is None


async def test_completed_cannot_be_overwritten(repo, payload):
    job, _ = await repo.create_job_idempotently("done", payload)
    _, steps = await repo.get_job(job.id)
    claim = await repo.claim_step(steps[0].id, "hash")
    assert await repo.complete_step_with_fencing(claim, {"first": True})
    assert not await repo.complete_step_with_fencing(claim, {"second": True})
    assert (await repo.get_job(job.id))[1][0].output_json == {"first": True}
