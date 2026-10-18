"""Admin HTTP contracts against real persistence, without a worker or provider."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import SecretStr
from sqlalchemy import event, select

from company_ai.api.admin.dependencies import get_admin_reader
from company_ai.application.research.admin_queries import Page
from company_ai.bootstrap.dependencies import get_session
from company_ai.domain.research.llm import resolve_execution_config
from company_ai.infrastructure.research.persistence.models import Attempt, Event, Job, Step

BASE = "/api/v1/admin"
AUTH = {"Authorization": "Bearer test-admin"}
STAMP = datetime(2026, 10, 17, 3, 0, tzinfo=UTC)


def identifier(number):
    return str(UUID(int=number))


@pytest.fixture
async def admin_data(repo, payload):
    job_id, other_id, old_id = (identifier(number) for number in (1, 2, 3))
    config = resolve_execution_config(repo.settings).model_dump(mode="json")
    steps = {
        name: identifier(10 + index)
        for index, name in enumerate(("COLLECT", "RESEARCH", "CLASSIFY", "SCORE", "REPORT"))
    }
    company = "Công ty Ánh_Sáng 100%\\LAB"
    async with repo.db.session.begin() as session:
        session.add_all(
            [
                Job(
                    id=job_id,
                    idempotency_key="private-idempotency-key",
                    request_hash="private-request-hash",
                    status="NEEDS_REVIEW",
                    input_json={**payload, "company_name": company, "collection_mode": "tavily"},
                    workflow_version="tavily-v1",
                    execution_config=config,
                    execution_identity={"private-profile": {"digest": "private-model-digest"}},
                    result_json=None,
                    error_code="AMBIGUOUS_PROVIDER_OUTCOME",
                    error_message="provider body with private-error-secret",
                    total_estimated_cost_usd=0.05,
                    created_at=STAMP,
                    updated_at=STAMP,
                ),
                Job(
                    id=other_id,
                    idempotency_key="other",
                    request_hash="other",
                    status="PENDING",
                    input_json={**payload, "company_name": "ÁnhXSáng 100x\\LAB"},
                    created_at=STAMP,
                    updated_at=STAMP,
                ),
                Job(
                    id=old_id,
                    idempotency_key="old",
                    request_hash="old",
                    status="COMPLETED",
                    input_json={**payload, "company_name": "Legacy company"},
                    result_json={"report": {"summary": "Legacy report"}},
                    execution_config=None,
                    created_at=STAMP - timedelta(days=1),
                    updated_at=STAMP,
                    completed_at=STAMP,
                ),
            ]
        )
        await session.flush()
        for name, step_id in steps.items():
            completed = name in ("COLLECT", "SCORE")
            session.add(
                Step(
                    id=step_id,
                    job_id=job_id,
                    step_name=name,
                    status="COMPLETED"
                    if completed
                    else ("UNKNOWN" if name == "RESEARCH" else "PENDING"),
                    input_hash="private-input-hash",
                    output_hash="private-output-hash",
                    output_json={"sources": [], "warnings": ["Missing evidence"]}
                    if name == "COLLECT"
                    else ({"score": 0} if name == "SCORE" else None),
                    attempt_count=1 if name in ("COLLECT", "RESEARCH", "SCORE") else 0,
                    lease_token="private-lease-token",
                    lease_expires_at=STAMP + timedelta(hours=1),
                    last_heartbeat_at=STAMP,
                    started_at=STAMP,
                    completed_at=STAMP if completed else None,
                    updated_at=STAMP,
                    next_retry_at=STAMP + timedelta(minutes=1) if name == "RESEARCH" else None,
                )
            )
        await session.flush()
        session.add_all(
            [
                Attempt(
                    id=identifier(21),
                    step_id=steps["COLLECT"],
                    attempt_no=1,
                    lease_token="private-attempt-lease",
                    status="SUCCEEDED",
                    collection_started_at=STAMP,
                    collection_usage={"usage": {"credits": 0}, "credits": 0},
                    provider_request_id="collect-request-1",
                    created_at=STAMP,
                    finished_at=STAMP,
                ),
                Attempt(
                    id=identifier(22),
                    step_id=steps["RESEARCH"],
                    attempt_no=1,
                    lease_token="private-attempt-lease",
                    status="UNKNOWN",
                    error_type="AMBIGUOUS_PROVIDER_OUTCOME",
                    llm_started_at=STAMP,
                    provider="openai",
                    model="stored-model",
                    profile_id="stored-profile",
                    pricing_kind="estimated_token_rates",
                    config_fingerprint="private-fingerprint",
                    provider_request_id="request-safe-123",
                    estimated_cost_usd=0.05,
                    created_at=STAMP,
                    finished_at=STAMP,
                ),
                Attempt(
                    id=identifier(23),
                    step_id=steps["SCORE"],
                    attempt_no=1,
                    lease_token="private-attempt-lease",
                    status="SUCCEEDED",
                    tokens_input=0,
                    tokens_output=0,
                    estimated_cost_usd=0,
                    created_at=STAMP + timedelta(seconds=1),
                    finished_at=STAMP + timedelta(seconds=1),
                ),
                Event(
                    id=identifier(31),
                    job_id=job_id,
                    step_id=steps["RESEARCH"],
                    event_type="STEP_NEEDS_REVIEW",
                    created_at=STAMP,
                    details_json={
                        "attempt_id": identifier(22),
                        "error_category": "AMBIGUOUS_PROVIDER_OUTCOME",
                        "provider": "openai",
                        "model": "stored-model",
                        "pricing_kind": "estimated_token_rates",
                        "config_fingerprint": "private-fingerprint",
                        "api_key": "private-event-key",
                        "provider_response": {"body": "private-body"},
                    },
                ),
                Event(
                    id=identifier(32),
                    job_id=job_id,
                    step_id=steps["RESEARCH"],
                    event_type="STEP_NEEDS_REVIEW",
                    created_at=STAMP,
                    details_json={
                        "attempt_id": {"secret": "private-nested"},
                        "error_category": "raw provider body private-error-secret",
                        "provider": {"secret": "private-nested"},
                    },
                ),
                Event(
                    id=identifier(33),
                    job_id=job_id,
                    event_type="UNRECOGNIZED_HISTORY_EVENT",
                    details_json={"attempt_id": identifier(22), "secret": "private-event-key"},
                    created_at=STAMP + timedelta(seconds=1),
                ),
            ]
        )
    return {
        "job_id": job_id,
        "other_id": other_id,
        "old_id": old_id,
        "steps": steps,
        "company": company,
    }


class ForbiddenReader:
    def __getattr__(self, method):
        async def forbidden(*args, **kwargs):
            pytest.fail(f"Admin reader was called before request validation: {method}")

        return forbidden


async def test_all_admin_endpoints_authenticate_before_reading(
    app_factory, client_factory, settings
):
    app = app_factory()

    async def reader():
        return ForbiddenReader()

    async def unused_session():
        pytest.fail("An overridden admin reader must bypass SQLAlchemy session resolution")

    app.dependency_overrides[get_admin_reader] = reader
    app.dependency_overrides[get_session] = unused_session
    client = await client_factory(app)
    paths = [
        "/metrics",
        "/jobs",
        f"/jobs/{identifier(1)}",
        f"/jobs/{identifier(1)}/events",
        f"/jobs/{identifier(1)}/attempts",
    ]
    for path in paths:
        for headers in (
            {},
            {"Authorization": "Bearer wrong"},
            {"Authorization": ""},
            {"Authorization": "Basic test-admin"},
            [(b"authorization", b"Bearer \xc3\xa1")],
        ):
            response = await client.get(BASE + path, headers=headers)
            assert response.status_code == 401
            assert response.json() == {"detail": "ADMIN_AUTH_REQUIRED"}
    settings.admin_token = SecretStr("")
    for path in paths:
        response = await client.get(BASE + path, headers=AUTH)
        assert response.status_code == 401
        assert response.json() == {"detail": "ADMIN_AUTH_REQUIRED"}


async def test_query_and_path_validation_precede_reader_calls(app_factory, client_factory):
    app = app_factory()

    async def reader():
        return ForbiddenReader()

    app.dependency_overrides[get_admin_reader] = reader
    client = await client_factory(app)
    invalid = [
        ("/jobs", {"status": "UNKNOWN"}),
        ("/jobs", {"offset": -1}),
        ("/jobs", {"limit": 0}),
        ("/jobs", {"limit": 101}),
        ("/jobs", {"company_name": "   "}),
        ("/jobs", {"company_name": "x" * 201}),
        ("/jobs", {"created_from": "2026-10-17T03:00:00"}),
        ("/jobs", {"created_to": "2026-10-17T03:00:00"}),
        ("/jobs", {"created_from": "1792196400"}),
        ("/jobs", {"created_from": "2026-10-18T00:00:00Z", "created_to": "2026-10-17T00:00:00Z"}),
        (f"/jobs/{identifier(1)}/events", {"event_type": ""}),
        (f"/jobs/{identifier(1)}/events", {"event_type": "x" * 81}),
        (f"/jobs/{identifier(1)}/events", {"limit": 501}),
        (f"/jobs/{identifier(1)}/attempts", {"status": "NEEDS_REVIEW"}),
        (f"/jobs/{identifier(1)}/attempts", {"step_name": "research"}),
        (f"/jobs/{identifier(1)}/attempts", {"offset": -1}),
        (f"/jobs/{identifier(1)}/attempts", {"limit": 501}),
        ("/jobs/not-a-uuid", {}),
        ("/jobs/not-a-uuid/events", {}),
        ("/jobs/not-a-uuid/attempts", {}),
    ]
    for path, params in invalid:
        response = await client.get(BASE + path, params=params, headers=AUTH)
        assert response.status_code == 422, (path, params, response.text)


async def test_job_list_filters_literal_search_and_stable_paging(
    admin_data, app_factory, client_factory
):
    client = await client_factory(app_factory())
    response = await client.get(BASE + "/jobs", headers=AUTH)
    assert response.status_code == 200
    page = response.json()
    assert (page["total"], page["offset"], page["limit"]) == (3, 0, 50)
    assert [item["job_id"] for item in page["items"]] == [
        admin_data["other_id"],
        admin_data["job_id"],
        admin_data["old_id"],
    ]
    assert set(page["items"][0]) == {
        "job_id",
        "company_name",
        "website",
        "status",
        "created_at",
        "updated_at",
        "completed_at",
        "estimated_cost_usd",
        "error_code",
    }
    for search in ("  cÔNG tY ÁNH_sÁNG  ", "%", "_", "100%\\lab"):
        found = await client.get(BASE + "/jobs", params={"company_name": search}, headers=AUTH)
        assert found.status_code == 200
        assert found.json()["total"] == 1, search
        assert found.json()["items"][0]["job_id"] == admin_data["job_id"]
    filtered = (
        await client.get(
            BASE + "/jobs",
            params={
                "status": "NEEDS_REVIEW",
                "created_from": "2026-10-17T10:00:00+07:00",
                "created_to": "2026-10-17T03:00:00Z",
                "offset": 0,
                "limit": 1,
            },
            headers=AUTH,
        )
    ).json()
    assert filtered["total"] == 1
    assert filtered["items"][0]["job_id"] == admin_data["job_id"]
    second = (
        await client.get(BASE + "/jobs", params={"offset": 1, "limit": 1}, headers=AUTH)
    ).json()
    assert second["items"][0]["job_id"] == admin_data["job_id"]
    beyond = (
        await client.get(BASE + "/jobs", params={"offset": 99, "limit": 1}, headers=AUTH)
    ).json()
    assert beyond == {"items": [], "total": 3, "offset": 99, "limit": 1}


async def test_empty_database_and_missing_history_contracts(repo, app_factory, client_factory):
    client = await client_factory(app_factory())
    assert (await client.get(BASE + "/jobs", headers=AUTH)).json() == {
        "items": [],
        "total": 0,
        "offset": 0,
        "limit": 50,
    }
    for suffix in ("", "/events", "/attempts"):
        response = await client.get(f"{BASE}/jobs/{identifier(99)}{suffix}", headers=AUTH)
        assert response.status_code == 404
        assert response.json() == {"detail": "JOB_NOT_FOUND"}


async def test_detail_projects_safe_fields_and_keeps_collection_and_route_metadata(
    admin_data, app_factory, client_factory
):
    client = await client_factory(app_factory())
    response = await client.get(f"{BASE}/jobs/{admin_data['job_id']}", headers=AUTH)
    assert response.status_code == 200
    detail = response.json()
    assert detail["input"]["company_name"] == admin_data["company"]
    assert detail["workflow_version"] == "tavily-v1"
    assert detail["estimated_cost_usd"] == 0.05
    assert detail["error"]["code"] == "AMBIGUOUS_PROVIDER_OUTCOME"
    assert [step["step_name"] for step in detail["steps"]] == [
        "COLLECT",
        "RESEARCH",
        "CLASSIFY",
        "SCORE",
        "REPORT",
    ]
    assert detail["steps"][0]["output"]["warnings"] == ["Missing evidence"]
    assert detail["result"]["collect"] == detail["steps"][0]["output"]
    assert detail["result"]["score"] == {"score": 0}
    research = detail["steps"][1]
    assert research["status"] == "UNKNOWN" and research["attempt_count"] == 1
    assert research["next_retry_at"] is not None and research["lease_expires_at"] is not None
    assert research["execution_metadata"] == detail["execution_metadata"]["RESEARCH"]
    assert set(research["execution_metadata"]) == {
        "provider",
        "model",
        "profile_id",
        "pricing_kind",
    }
    assert "private-" not in response.text
    for forbidden in (
        "lease_token",
        "request_hash",
        "idempotency_key",
        "input_hash",
        "output_hash",
        "execution_identity",
        "endpoint_fingerprint",
        "connection_ref",
        "config_fingerprint",
    ):
        assert forbidden not in response.text
    legacy = (await client.get(f"{BASE}/jobs/{admin_data['old_id']}", headers=AUTH)).json()
    assert legacy["execution_metadata"] is None
    assert legacy["result"] == {"report": {"summary": "Legacy report"}}


async def test_event_history_orders_pages_filters_and_sanitizes_historical_details(
    admin_data, app_factory, client_factory
):
    client = await client_factory(app_factory())
    url = f"{BASE}/jobs/{admin_data['job_id']}/events"
    response = await client.get(url, headers=AUTH)
    assert response.status_code == 200
    page = response.json()
    assert [item["id"] for item in page["items"]] == [
        identifier(31),
        identifier(32),
        identifier(33),
    ]
    assert (page["total"], page["offset"], page["limit"]) == (3, 0, 100)
    safe, malformed, unknown = page["items"]
    assert safe["details"] == {
        "attempt_id": identifier(22),
        "error_category": "AMBIGUOUS_PROVIDER_OUTCOME",
        "provider": "openai",
        "model": "stored-model",
        "pricing_kind": "estimated_token_rates",
    }
    assert malformed["details"] == {"error_category": "UNKNOWN_ERROR"}
    assert unknown["details"] == {}
    assert "private-" not in response.text
    filtered = (
        await client.get(
            url, params={"event_type": "STEP_NEEDS_REVIEW", "offset": 1, "limit": 1}, headers=AUTH
        )
    ).json()
    assert filtered["total"] == 2 and filtered["items"] == [malformed]
    beyond = (await client.get(url, params={"offset": 100}, headers=AUTH)).json()
    assert beyond["items"] == [] and beyond["total"] == 3
    empty = (await client.get(f"{BASE}/jobs/{admin_data['other_id']}/events", headers=AUTH)).json()
    assert empty == {"items": [], "total": 0, "offset": 0, "limit": 100}


async def test_attempt_history_retains_null_usage_and_collection_metadata(
    admin_data, app_factory, client_factory
):
    client = await client_factory(app_factory())
    url = f"{BASE}/jobs/{admin_data['job_id']}/attempts"
    response = await client.get(url, headers=AUTH)
    assert response.status_code == 200
    page = response.json()
    assert [item["attempt_id"] for item in page["items"]] == [
        identifier(21),
        identifier(22),
        identifier(23),
    ]
    assert (page["total"], page["offset"], page["limit"]) == (3, 0, 100)
    collect, llm, score = page["items"]
    assert collect["execution_type"] == "collection"
    assert collect["collection_usage"] == {"usage": {"credits": 0}, "credits": 0}
    assert collect["tokens_input"] is None and collect["estimated_cost_usd"] is None
    assert llm["execution_type"] == "llm" and llm["error_type"] == "AMBIGUOUS_PROVIDER_OUTCOME"
    assert llm["tokens_input"] is None and llm["tokens_output"] is None
    assert llm["estimated_cost_usd"] == 0.05
    assert llm["provider_request_id"] == "request-safe-123"
    assert (llm["provider"], llm["model"], llm["pricing_kind"]) == (
        "openai",
        "stored-model",
        "estimated_token_rates",
    )
    assert score["execution_type"] == "deterministic"
    assert score["tokens_input"] == 0 and score["tokens_output"] == 0
    assert score["estimated_cost_usd"] == 0
    assert "private-" not in response.text and "lease_token" not in response.text
    selected = (
        await client.get(url, params={"step_name": "RESEARCH", "status": "UNKNOWN"}, headers=AUTH)
    ).json()
    assert selected["total"] == 1 and selected["items"] == [llm]
    second = (await client.get(url, params={"offset": 1, "limit": 1}, headers=AUTH)).json()
    assert second == {"items": [llm], "total": 3, "offset": 1, "limit": 1}
    beyond = (await client.get(url, params={"offset": 100}, headers=AUTH)).json()
    assert beyond["total"] == 3 and beyond["items"] == []
    empty = (
        await client.get(f"{BASE}/jobs/{admin_data['other_id']}/attempts", headers=AUTH)
    ).json()
    assert empty == {"items": [], "total": 0, "offset": 0, "limit": 100}


async def table_snapshot(repo):
    async with repo.db.session() as session:
        return {
            model.__tablename__: [
                dict(row)
                for row in (
                    await session.execute(select(model.__table__).order_by(model.id))
                ).mappings()
            ]
            for model in (Job, Step, Attempt, Event)
        }


async def test_gets_are_read_only_keep_metrics_contract_and_never_publish(
    repo, admin_data, app_factory, client_factory
):
    async def forbidden_publish(_):
        pytest.fail("Admin reads must never enqueue jobs")

    client = await client_factory(app_factory(forbidden_publish))
    before = await table_snapshot(repo)
    statements = []

    def capture(connection, cursor, statement, parameters, context, many):
        statements.append(statement)

    event.listen(repo.db.engine.sync_engine, "before_cursor_execute", capture)
    try:
        for suffix in (
            "/jobs",
            f"/jobs/{admin_data['job_id']}",
            f"/jobs/{admin_data['job_id']}/events",
            f"/jobs/{admin_data['job_id']}/attempts",
        ):
            assert (await client.get(BASE + suffix, headers=AUTH)).status_code == 200
        response = await client.get(BASE + "/metrics", headers=AUTH)
        assert response.status_code == 200
        metrics = response.json()
    finally:
        event.remove(repo.db.engine.sync_engine, "before_cursor_execute", capture)
    assert statements
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    assert all("FOR UPDATE" not in statement.upper() for statement in statements)
    assert await table_snapshot(repo) == before
    assert metrics == await repo.metrics()
    assert metrics["jobs"] == {"PENDING": 1, "NEEDS_REVIEW": 1, "COMPLETED": 1}
    assert metrics["llm_calls"] == 1 and metrics["collection_calls"] == 1
    assert metrics["tokens_input"] == 0 and metrics["tokens_output"] == 0
    assert metrics["estimated_cost_usd"] == 0.05
    assert metrics["collection_usage"] == {"credits": 0}
    assert metrics["collection_credits"] == 0
    assert metrics["llm_usage"][0]["tokens_input"] is None


async def test_admin_sessions_are_request_scoped_and_reader_override_is_independent(
    repo, admin_data, app_factory, client_factory
):
    sessions, closed = [], []

    async def tracked_session():
        async with repo.db.session() as session:
            sessions.append(session)
            yield session
        closed.append(session)

    app = app_factory()
    app.dependency_overrides[get_session] = tracked_session
    client = await client_factory(app)
    responses = [
        await client.get(BASE + "/jobs", headers=AUTH),
        await client.get(f"{BASE}/jobs/{admin_data['job_id']}", headers=AUTH),
    ]
    assert all(response.status_code == 200 for response in responses)
    assert len(sessions) == 2 and sessions[0] is not sessions[1]
    assert closed == sessions
    assert all(not session.in_transaction() for session in sessions)
    assert responses[1].json()["input"]["company_name"] == admin_data["company"]
    assert repo.db.engine.pool.checkedout() == 0

    class EmptyReader:
        async def list_jobs(self, filters, page):
            return Page(items=(), total=0, offset=page.offset, limit=page.limit)

    async def reader():
        return EmptyReader()

    async def unused_session():
        pytest.fail("Contract override must not resolve a concrete adapter session")

    app.dependency_overrides[get_admin_reader] = reader
    app.dependency_overrides[get_session] = unused_session
    assert (await client.get(BASE + "/jobs", headers=AUTH)).json()["items"] == []


async def test_swagger_declares_bearer_for_every_admin_operation(app_factory, client_factory):
    client = await client_factory(app_factory())
    schema = (await client.get("/openapi.json")).json()
    schemes = schema["components"]["securitySchemes"]
    bearer_names = {
        name
        for name, value in schemes.items()
        if value.get("type") == "http" and value.get("scheme") == "bearer"
    }
    assert bearer_names
    paths = [path for path in schema["paths"] if path.startswith(BASE)]
    assert len(paths) == 5
    for path in paths:
        assert any(
            bearer_names.intersection(requirement)
            for requirement in schema["paths"][path]["get"]["security"]
        )
    swagger = await client.get("/docs")
    assert "persistAuthorization" not in swagger.text
