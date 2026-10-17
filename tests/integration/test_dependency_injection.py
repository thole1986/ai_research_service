import asyncio
from typing import Annotated

import pytest
from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from company_ai.api.research.dependencies import get_research_repository
from company_ai.bootstrap.dependencies import get_session
from company_ai.infrastructure.research.persistence.models import Job


async def test_request_scopes_share_session_within_request_and_close_it(
    repo, app_factory, client_factory, payload
):
    sessions, closed, published = [], [], []

    async def track_session():
        async with repo.db.session() as session:
            sessions.append(session)
            try:
                yield session
            finally:
                assert not session.in_transaction()
        closed.append(session)

    async def publish(job_id):
        assert not sessions[-1].in_transaction()
        assert repo.db.engine.pool.checkedout() == 0
        # A separate worker-style session can already see the committed job.
        assert (await repo.get_job(job_id))[0] is not None
        published.append(job_id)

    app = app_factory(publish)
    app.dependency_overrides[get_session] = track_session

    async def another_domain_session(session: Annotated[AsyncSession, Depends(get_session)]):
        return session

    @app.get("/test/session-sharing")
    async def session_sharing(
        first: Annotated[AsyncSession, Depends(get_session)],
        second: Annotated[AsyncSession, Depends(another_domain_session)],
    ):
        assert first is second
        return {"shared": True}

    client = await client_factory(app)
    response = await client.post(
        "/api/v1/research/jobs", json=payload, headers={"Idempotency-Key": "request-scope"}
    )
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    assert (await client.get(f"/api/v1/research/jobs/{job_id}")).status_code == 200
    assert (await client.get("/test/session-sharing")).json() == {"shared": True}
    assert len(sessions) == 3
    assert len({id(session) for session in sessions}) == 3
    assert closed == sessions
    assert published == [job_id]


async def test_session_dependency_rolls_back_failed_request(repo, app_factory, client_factory):
    app = app_factory()

    @app.post("/test/rollback")
    async def fail_after_flush(session: Annotated[AsyncSession, Depends(get_session)]):
        session.add(Job(idempotency_key="rollback", request_hash="hash", input_json={}))
        await session.flush()
        raise HTTPException(409, "EXPECTED_FAILURE")

    client = await client_factory(app)
    assert (await client.post("/test/rollback")).status_code == 409
    async with repo.db.session() as session:
        assert await session.scalar(select(Job).where(Job.idempotency_key == "rollback")) is None
    assert repo.db.engine.pool.checkedout() == 0


async def test_concurrent_http_idempotency_uses_independent_sessions(
    app_factory, client_factory, payload
):
    published = []

    async def publish(job_id):
        published.append(job_id)

    client = await client_factory(app_factory(publish))
    responses = await asyncio.gather(
        *(
            client.post(
                "/api/v1/research/jobs", json=payload, headers={"Idempotency-Key": "http-race"}
            )
            for _ in range(4)
        )
    )
    assert sorted(response.status_code for response in responses) == [200, 200, 200, 202]
    assert len({response.json()["job_id"] for response in responses}) == 1
    assert len(published) == 1


async def test_repository_port_override_does_not_need_database_attributes(
    app_factory, client_factory
):
    class MissingJobs:
        async def get_job(self, job_id):
            return None, []

    async def repository():
        return MissingJobs()

    async def unused_session():
        pytest.fail("Overridden repository must bypass SQLAlchemy session resolution")

    app = app_factory()
    app.dependency_overrides[get_research_repository] = repository
    app.dependency_overrides[get_session] = unused_session
    client = await client_factory(app)
    assert (await client.get("/api/v1/research/jobs/missing")).status_code == 404
