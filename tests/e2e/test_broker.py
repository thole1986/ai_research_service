"""Opt-in real Redis/Celery E2E, always with a mocked LLM boundary."""

import asyncio
import os
from uuid import uuid4

import pytest
from celery import Celery
from celery.contrib.testing.worker import start_worker

from company_ai.application.research.execute_workflow import run_workflow
from company_ai.infrastructure.database.session import Database
from company_ai.infrastructure.research.persistence.repositories import SqlAlchemyResearchRepository
from tests.fakes import CountingAdapter


@pytest.mark.skipif(os.environ.get("RUN_REDIS_E2E") != "1", reason="Opt-in real Redis test")
async def test_api_broker_worker_checkpoints(repo, settings, payload, client_factory, app_factory):
    queue = f"company-ai-test-{uuid4()}"
    app = Celery("test", broker=os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15"))
    app.conf.update(
        task_default_queue=queue,
        task_ignore_result=True,
        worker_prefetch_multiplier=1,
        broker_connection_timeout=2,
    )
    adapter = CountingAdapter()

    @app.task(name=f"test.{queue}")
    def execute(job_id):
        async def run():
            db = Database(settings.database_dsn, settings=settings)
            schema_map = repo.db.engine.get_execution_options().get("schema_translate_map")
            if schema_map:
                db.engine = db.engine.execution_options(schema_translate_map=schema_map)
                db.session.configure(bind=db.engine)
            try:
                await run_workflow(
                    job_id, SqlAlchemyResearchRepository(db, settings), adapter, settings
                )
            finally:
                await db.dispose()

        asyncio.run(run())

    async def publish(job_id):
        await asyncio.to_thread(execute.apply_async, args=[job_id], queue=queue)

    worker = start_worker(
        app,
        pool="solo",
        concurrency=1,
        queues=[queue],
        perform_ping_check=False,
        shutdown_timeout=15,
    )
    await asyncio.to_thread(worker.__enter__)
    try:
        client = await client_factory(app_factory(publish))
        response = await client.post(
            "/api/v1/research/jobs", json=payload, headers={"Idempotency-Key": queue}
        )
        assert response.status_code == 202
        job_id = response.json()["job_id"]
        for _ in range(100):
            state = (await client.get(f"/api/v1/research/jobs/{job_id}")).json()
            if state["status"] == "COMPLETED":
                break
            await asyncio.sleep(0.1)
        assert state["status"] == "COMPLETED"
        await publish(job_id)
        await asyncio.sleep(0.5)
        assert adapter.calls == ["RESEARCH", "CLASSIFY", "REPORT"]
        assert all(step["attempt_count"] == 1 for step in state["steps"])
    finally:
        await asyncio.to_thread(worker.__exit__, None, None, None)

        def cleanup():
            with app.connection() as connection:
                connection.default_channel.queue_delete(queue)
            app.close()

        await asyncio.to_thread(cleanup)
