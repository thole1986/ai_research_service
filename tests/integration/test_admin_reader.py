from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import event, select, update

from company_ai.application.research.admin_queries import (
    AttemptFilters,
    EventFilters,
    JobFilters,
    Paging,
)
from company_ai.domain.research.contracts import ALL_STEPS
from company_ai.infrastructure.research.persistence.admin_reader import SqlAlchemyAdminReader
from company_ai.infrastructure.research.persistence.models import Attempt, Event, Job, Step

STAMP = datetime(2026, 10, 17, 12, tzinfo=UTC)


@contextmanager
def captured_queries(repo):
    queries = []

    def capture(_connection, _cursor, statement, _parameters, _context, _many):
        queries.append(statement)

    engine = repo.db.engine.sync_engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        yield queries
    finally:
        event.remove(engine, "before_cursor_execute", capture)


async def test_summary_is_bounded_light_projection_with_stable_ties(repo, payload):
    ids = []
    for index in range(5):
        job, _ = await repo.create_job_idempotently(f"summary-{index}", payload)
        ids.append(job.id)

    async def align(session):
        await session.execute(update(Job).values(created_at=STAMP))

    await repo.db.write(align)
    async with repo.db.session() as session:
        reader = SqlAlchemyAdminReader(session)
        with captured_queries(repo) as queries:
            page = await reader.list_jobs(JobFilters(), Paging(offset=1, limit=2))
        assert not session.in_transaction()
        assert not session.identity_map
    assert page.total == 5 and len(page.items) == 2
    assert [item.job_id for item in page.items] == sorted(ids, reverse=True)[1:3]
    assert len(queries) == 2
    projection = queries[1].lower().split("from")[0]
    for forbidden in ("result_json", "execution_config", "request_hash", "idempotency_key"):
        assert forbidden not in projection
    assert "ai_steps" not in queries[1]
    assert "limit" in queries[1].lower() and "offset" in queries[1].lower()
    assert "input_json as" not in projection  # only JSON scalar extraction, never the input object
    assert page.items[0].company_name == payload["company_name"]
    async with repo.db.session() as session:
        beyond = await SqlAlchemyAdminReader(session).list_jobs(JobFilters(), Paging(100, 2))
    assert beyond.items == () and beyond.total == 5


async def test_filters_unicode_literals_inclusive_time_and_count(repo, payload):
    literal = "ĐẶNG 100%_\\ Holdings"
    target, _ = await repo.create_job_idempotently("literal", {**payload, "company_name": literal})
    await repo.create_job_idempotently(
        "wildcard-decoy", {**payload, "company_name": "ĐẶNG 100abz Holdings"}
    )

    async def adjust(session):
        await session.execute(
            update(Job).where(Job.id == target.id).values(created_at=STAMP, status="NEEDS_REVIEW")
        )

    await repo.db.write(adjust)
    local = STAMP.astimezone(timezone(timedelta(hours=7)))
    async with repo.db.session() as session:
        reader = SqlAlchemyAdminReader(session)
        page = await reader.list_jobs(
            JobFilters(
                status="NEEDS_REVIEW",
                company_name="  đặng 100%_\\  ",
                created_from=local,
                created_to=local,
            ),
            Paging(),
        )
        assert page.total == 1 and page.items[0].job_id == target.id
        assert (await reader.list_jobs(JobFilters(company_name="đặng"), Paging())).total == 2
        assert (await reader.list_jobs(JobFilters(status="COMPLETED"), Paging())).total == 0


async def test_detail_is_detached_safe_and_preserves_saved_metadata(repo, payload):
    job, _ = await repo.create_job_idempotently("detail", {**payload, "collection_mode": "tavily"})

    async def finish_collection(session):
        await session.execute(
            update(Job)
            .where(Job.id == job.id)
            .values(error_code="PROVIDER_REJECTED", error_message="secret-provider-body")
        )
        await session.execute(
            update(Step)
            .where(Step.job_id == job.id, Step.step_name == "COLLECT")
            .values(
                status="COMPLETED",
                output_json={"sources": [{"title": "Example"}]},
                lease_token="hidden-lease",
                input_hash="hidden-hash",
            )
        )

    await repo.db.write(finish_collection)
    async with repo.db.session() as session:
        reader = SqlAlchemyAdminReader(session)
        with captured_queries(repo) as queries:
            detail = await reader.get_job_detail(job.id)
        assert len(queries) == 2 and not session.in_transaction()
        assert not session.identity_map
    assert tuple(step.step_name for step in detail.steps) == ALL_STEPS
    assert detail.input == {**payload, "collection_mode": "tavily"}
    assert detail.result == {"collect": {"sources": [{"title": "Example"}]}}
    assert detail.execution_metadata["RESEARCH"].model == repo.settings.openai_model
    assert detail.steps[1].execution_metadata == detail.execution_metadata["RESEARCH"]
    assert detail.steps[0].execution_metadata is None
    serialized = repr(asdict(detail))
    for secret in (
        "secret-provider-body",
        "hidden-lease",
        "hidden-hash",
        "endpoint_fingerprint",
        "config_fingerprint",
        "idempotency_key",
        "connection_ref",
    ):
        assert secret not in serialized
    assert detail.error.code == "PROVIDER_REJECTED"
    assert detail.error.message == "Research job stopped; inspect sanitized events and attempts."
    detail.input["company_name"] = "edited outside session"
    detail.steps[0].output["sources"].clear()
    saved, steps = await repo.get_job(job.id)
    assert saved.input_json["company_name"] == payload["company_name"]
    assert steps[0].output_json == {"sources": [{"title": "Example"}]}


@pytest.mark.parametrize("config", [None, {"version": 999}, {"endpoint": "secret"}])
async def test_legacy_or_malformed_configuration_stays_inspectable(repo, payload, config):
    job, _ = await repo.create_job_idempotently("legacy-config", payload)

    async def make_legacy(session):
        await session.execute(
            update(Job)
            .where(Job.id == job.id)
            .values(execution_config=config, error_code="raw secret", error_message="raw secret")
        )

    await repo.db.write(make_legacy)
    async with repo.db.session() as session:
        reader = SqlAlchemyAdminReader(session)
        detail = await reader.get_job_detail(job.id)
        summary = await reader.list_jobs(JobFilters(), Paging())
    assert detail.execution_metadata is None
    assert all(step.execution_metadata is None for step in detail.steps)
    assert detail.error.code == summary.items[0].error_code == "UNKNOWN_ERROR"
    assert "raw secret" not in repr(asdict(detail))


async def test_event_history_allowlist_paging_ties_and_existence(repo, payload):
    job, _ = await repo.create_job_idempotently("events", payload)
    attempt_id = str(uuid4())
    event_ids = sorted(str(uuid4()) for _ in range(3))

    async def seed(session):
        session.add_all(
            [
                Event(
                    id=event_ids[0],
                    job_id=job.id,
                    event_type="LLM_USAGE_RECORDED",
                    created_at=STAMP,
                    details_json={
                        "attempt_id": attempt_id,
                        "provider": "openai",
                        "model": "saved-model",
                        "pricing_kind": "estimated_token_rates",
                        "usage_known": True,
                        "cost_reconciled": {"api_key": "secret"},
                        "config_fingerprint": "hidden",
                        "response": "secret",
                    },
                ),
                Event(
                    id=event_ids[1],
                    job_id=job.id,
                    event_type="LLM_USAGE_RECORDED",
                    created_at=STAMP,
                    details_json={
                        "attempt_id": {"api_key": "secret"},
                        "model": ["secret"],
                        "usage_known": 1,
                    },
                ),
                Event(
                    id=event_ids[2],
                    job_id=job.id,
                    event_type="FUTURE_EVENT",
                    created_at=STAMP,
                    details_json={"attempt_id": attempt_id, "api_key": "secret"},
                ),
            ]
        )

    await repo.db.write(seed)
    async with repo.db.session() as session:
        reader = SqlAlchemyAdminReader(session)
        with captured_queries(repo) as queries:
            page = await reader.list_job_events(
                job.id, EventFilters("LLM_USAGE_RECORDED"), Paging(0, 1)
            )
        assert len(queries) == 3 and not session.in_transaction()
        assert page.total == 2 and page.items[0].id == event_ids[0]
        assert page.items[0].details == {
            "attempt_id": attempt_id,
            "provider": "openai",
            "model": "saved-model",
            "pricing_kind": "estimated_token_rates",
            "usage_known": True,
        }
        second = await reader.list_job_events(
            job.id, EventFilters("LLM_USAGE_RECORDED"), Paging(1, 1)
        )
        assert second.items[0].details == {}
        unknown = await reader.list_job_events(job.id, EventFilters("FUTURE_EVENT"), Paging())
        assert unknown.items[0].details == {}
        empty = await reader.list_job_events(job.id, EventFilters("NO_EVENTS"), Paging())
        assert empty.items == () and empty.total == 0
        assert await reader.list_job_events(str(uuid4()), EventFilters(), Paging()) is None


async def test_attempts_join_filters_nulls_safe_usage_and_shared_metrics(repo, payload):
    job, _ = await repo.create_job_idempotently(
        "attempts", {**payload, "collection_mode": "tavily"}
    )
    _, steps = await repo.get_job(job.id)
    attempt_ids = sorted(str(uuid4()) for _ in range(4))
    by_name = {step.step_name: step for step in steps}

    async def seed(session):
        session.add_all(
            [
                Attempt(
                    id=attempt_ids[0],
                    step_id=by_name["COLLECT"].id,
                    lease_token="secret",
                    attempt_no=1,
                    status="SUCCEEDED",
                    created_at=STAMP,
                    collection_started_at=STAMP,
                    collection_usage={
                        "usage": {"credits": 0, "api_key": "secret"},
                        "credits": 0,
                        "body": "secret",
                    },
                ),
                Attempt(
                    id=attempt_ids[1],
                    step_id=by_name["RESEARCH"].id,
                    lease_token="secret",
                    attempt_no=1,
                    status="FAILED",
                    created_at=STAMP,
                    llm_started_at=STAMP,
                    error_type="secret error",
                    tokens_input=12,
                    tokens_output=0,
                    estimated_cost_usd=0.125,
                    provider="openai",
                    model="saved-model",
                    pricing_kind="estimated_token_rates",
                    provider_request_id="req-safe-1",
                ),
                Attempt(
                    id=attempt_ids[2],
                    step_id=by_name["SCORE"].id,
                    lease_token="secret",
                    attempt_no=1,
                    status="SUCCEEDED",
                    created_at=STAMP,
                ),
                Attempt(
                    id=attempt_ids[3],
                    step_id=by_name["COLLECT"].id,
                    lease_token="secret",
                    attempt_no=2,
                    status="FAILED",
                    created_at=STAMP,
                    collection_started_at=STAMP,
                    collection_usage={"usage": {"credits": True}, "credits": {"api_key": "secret"}},
                    provider_request_id="https://internal.example/secret",
                ),
            ]
        )
        await session.execute(
            update(Job).where(Job.id == job.id).values(total_estimated_cost_usd=0.125)
        )

    await repo.db.write(seed)
    async with repo.db.session() as session:
        reader = SqlAlchemyAdminReader(session)
        with captured_queries(repo) as queries:
            page = await reader.list_job_attempts(job.id, AttemptFilters(), Paging())
        assert len(queries) == 3 and not session.in_transaction()
        assert not session.identity_map
        assert [item.attempt_id for item in page.items] == attempt_ids
        assert [item.execution_type for item in page.items] == [
            "collection",
            "llm",
            "deterministic",
            "collection",
        ]
        assert page.items[0].collection_usage == {"usage": {"credits": 0}, "credits": 0}
        assert page.items[0].tokens_input is None and page.items[0].estimated_cost_usd is None
        assert page.items[1].tokens_output == 0 and page.items[1].error_type == "UNKNOWN_ERROR"
        assert page.items[1].provider_request_id == "req-safe-1"
        assert page.items[3].collection_usage == {"usage": {}}
        assert page.items[3].provider_request_id is None
        filtered = await reader.list_job_attempts(
            job.id, AttemptFilters(step_name="COLLECT", status="FAILED"), Paging(0, 1)
        )
        assert filtered.total == 1 and filtered.items[0].attempt_id == attempt_ids[3]
        assert await reader.list_job_attempts(str(uuid4()), AttemptFilters(), Paging()) is None
        metrics = await reader.get_metrics()
    assert metrics == await repo.metrics()
    assert metrics["collection_calls"] == 2 and metrics["collection_credits"] == 0
    assert metrics["collection_usage"] == {"credits": 0}
    assert metrics["llm_calls"] == 1 and metrics["tokens_input"] == 12
    assert metrics["tokens_output"] == 0 and metrics["estimated_cost_usd"] == 0.125


async def test_missing_detail_and_existing_empty_attempts_are_distinct(repo, payload):
    job, _ = await repo.create_job_idempotently("no-attempts", payload)
    async with repo.db.session() as session:
        reader = SqlAlchemyAdminReader(session)
        assert await reader.get_job_detail(str(uuid4())) is None
        page = await reader.list_job_attempts(job.id, AttemptFilters(), Paging())
        assert page.items == () and page.total == 0
        assert not session.in_transaction()


async def test_all_read_methods_preserve_rows_timestamps_and_leases(repo, payload):
    job, _ = await repo.create_job_idempotently("readonly", payload)
    _, steps = await repo.get_job(job.id)
    claim = await repo.claim_step(steps[0].id, "read-input-hash")
    assert claim is not None

    async def state():
        async with repo.db.session() as session:
            return {
                model.__tablename__: (await session.execute(select(*model.__table__.c))).all()
                for model in (Job, Step, Event, Attempt)
            }

    before = await state()
    async with repo.db.session() as session:
        reader = SqlAlchemyAdminReader(session)
        with captured_queries(repo) as queries:
            await reader.list_jobs(JobFilters(), Paging())
            await reader.get_job_detail(job.id)
            await reader.list_job_events(job.id, EventFilters(), Paging())
            await reader.list_job_attempts(job.id, AttemptFilters(), Paging())
            await reader.get_metrics()
        assert all(query.lstrip().upper().startswith("SELECT") for query in queries)
    assert await state() == before
