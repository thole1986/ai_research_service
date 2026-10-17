import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from company_ai.application.research.execute_workflow import run_workflow
from company_ai.application.research.recovery import reconcile_unknown_step, resume_pending
from company_ai.domain.research.contracts import InjectedCrash
from company_ai.infrastructure.research.persistence.models import Event, Step, now
from tests.fakes import CountingAdapter


async def expire(repo, job_id):

    async def db_operation_0(s):
        return await s.execute(
            update(Step)
            .where(Step.job_id == job_id, Step.status == "RUNNING")
            .values(lease_expires_at=now() - timedelta(seconds=1))
        )

    await repo.db.write(db_operation_0)


async def test_score_crash_and_scanner_overlap(repo, payload):
    job, _ = await repo.create_job_idempotently("score-crash", payload)
    repo.settings.failure_step = "SCORE"
    repo.settings.failure_point = "claim"
    adapter = CountingAdapter()
    with pytest.raises(InjectedCrash):
        await run_workflow(job.id, repo, adapter, repo.settings)
    await expire(repo, job.id)
    results = await asyncio.gather(*(repo.scan_stale_steps() for _ in range(2)))
    assert sum(map(len, results)) == 1
    repo.settings.failure_point = ""
    await run_workflow(job.id, repo, adapter, repo.settings)
    assert (await repo.get_job(job.id))[0].status == "COMPLETED"
    assert (await repo.get_job(job.id))[1][2].attempt_count == 2
    assert adapter.calls == ["RESEARCH", "CLASSIFY", "REPORT"]


async def test_manual_retry_auth_ack_and_fence(
    repo, settings, payload, client_factory, app_factory
):
    job, _ = await repo.create_job_idempotently("manual", payload)

    async def record(job_id):
        published.append(job_id)

    _, steps = await repo.get_job(job.id)
    old = await repo.claim_step(steps[0].id, "old-hash")
    await expire(repo, job.id)
    await repo.scan_stale_steps()
    published = []
    client = await client_factory(app_factory(record))
    url = f"/api/v1/research/jobs/{job.id}/retry"
    headers = {"Authorization": "Bearer test-admin"}
    body = {"step_name": "RESEARCH", "acknowledge_possible_duplicate_charge": True}
    assert (await client.post(url, json=body)).status_code == 401
    assert (
        await client.post(
            url, headers=headers, json={**body, "acknowledge_possible_duplicate_charge": False}
        )
    ).status_code == 422
    assert (
        await client.post(url, headers=headers, json={**body, "step_name": "REPORT"})
    ).status_code == 409
    assert (await client.post(url, headers=headers, json=body)).status_code == 202
    assert (await client.post(url, headers=headers, json=body)).status_code == 409
    assert published == [job.id]
    fresh = await repo.claim_step(old.step_id, "fresh-hash")
    assert not await repo.complete_step_with_fencing(old, {"wrong": True})
    assert await repo.complete_step_with_fencing(fresh, {"fresh": True})
    assert (await client.post(url, headers=headers, json=body)).status_code == 409


async def test_enqueue_gap_recovery_and_retention(repo, payload):
    job, _ = await repo.create_job_idempotently("publish-gap", payload)

    async def record(job_id):
        published.append(job_id)

    published = []
    assert await resume_pending(repo, record) == 1
    assert published == [job.id]
    assert reconcile_unknown_step()["automatic_reconciliation_supported"] is False
    old = now() - timedelta(days=40)

    async def db_operation_1(s):
        return await s.execute(update(Event).values(created_at=old))

    await repo.db.write(db_operation_1)
    assert await repo.cleanup_events() == 0
    await run_workflow(job.id, repo, CountingAdapter(), repo.settings)
    assert await repo.cleanup_events() == 1
    async with repo.db.session() as s:
        assert await s.scalar(select(Event).where(Event.event_type == "JOB_COMPLETED"))


async def test_beat_schedule_and_task_redelivery(repo, payload, monkeypatch):
    from company_ai.celery_app import celery_app
    from company_ai.tasks import ai_tasks, maintenance_tasks

    assert len(celery_app.conf.beat_schedule) == 3
    assert celery_app.conf.task_acks_late
    assert celery_app.backend.__class__.__name__ == "DisabledBackend"
    adapter = CountingAdapter()
    from unittest.mock import Mock

    from company_ai.observability import NoOpObserver

    observer = NoOpObserver()
    observer.flush = Mock()

    def adapter_factory(settings, **kwargs):
        assert kwargs["observer"] is observer
        return adapter

    monkeypatch.setattr(ai_tasks, "CrewAIAdapter", adapter_factory)
    monkeypatch.setattr(ai_tasks, "get_worker_observer", lambda: observer)

    from sqlalchemy import event

    from company_ai import runtime
    from company_ai.infrastructure.database.session import Database

    disposed = []

    def database_factory(*args, **kwargs):
        db = Database(*args, **kwargs)
        schema_map = repo.db.engine.get_execution_options().get("schema_translate_map")
        if schema_map:
            db.engine = db.engine.execution_options(schema_translate_map=schema_map)
            db.session.configure(bind=db.engine)
        event.listen(db.engine.sync_engine, "engine_disposed", lambda _: disposed.append(True))
        return db

    monkeypatch.setattr(runtime, "Database", database_factory)
    monkeypatch.setattr(runtime, "get_settings", lambda: repo.settings)
    job, _ = await repo.create_job_idempotently("celery", payload)
    assert await asyncio.to_thread(maintenance_tasks.scan_stale_steps.run) == 0
    await asyncio.to_thread(ai_tasks.run_job.run, job.id)
    await asyncio.to_thread(ai_tasks.run_job.run, job.id)
    assert adapter.calls == ["RESEARCH", "CLASSIFY", "REPORT"]
    assert (await repo.get_job(job.id))[0].status == "COMPLETED"
    assert len(disposed) == 3
    assert observer.flush.call_count == 2
