import asyncio
from dataclasses import is_dataclass

from company_ai.application.research.execute_workflow import run_workflow
from tests.fakes import CountingAdapter


async def test_concurrent_idempotency_returns_one_domain_job(repo, payload):
    results = await asyncio.gather(
        *(repo.create_job_idempotently("same-race", payload) for _ in range(4))
    )
    assert len({job.id for job, _ in results}) == 1
    assert sum(created for _, created in results) == 1
    assert all(is_dataclass(job) for job, _ in results)


async def test_session_is_closed_during_llm_and_event_loop_remains_responsive(repo, payload):
    job, _ = await repo.create_job_idempotently("async-io", payload)
    ticks = []
    done = asyncio.Event()

    class SlowAdapter(CountingAdapter):
        async def execute(self, step, data, profile=None):
            # No operation/session/transaction survives while waiting for the provider.
            assert repo.db.engine.pool.checkedout() == 0
            await asyncio.sleep(0.05)
            return await super().execute(step, data, profile)

    async def ticker():
        while not done.is_set():
            ticks.append(1)
            await asyncio.sleep(0.005)

    clock = asyncio.create_task(ticker())
    try:
        await run_workflow(job.id, repo, SlowAdapter(), repo.settings)
    finally:
        done.set()
        await clock
    assert len(ticks) >= 5
    assert (await repo.get_job(job.id))[0].status == "COMPLETED"


async def test_duplicate_request_started_is_not_a_second_billable_request(repo, payload):
    job, _ = await repo.create_job_idempotently("request-gate", payload)
    _, steps = await repo.get_job(job.id)
    claim = await repo.claim_step(steps[0].id, "hash")
    outcomes = await asyncio.gather(*(repo.request_started(claim, 0.01) for _ in range(2)))
    assert outcomes.count(True) == 1
    assert (await repo.metrics())["llm_calls"] == 1
    assert float((await repo.get_job(job.id))[0].total_estimated_cost_usd) == 0.01
