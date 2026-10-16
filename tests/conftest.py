import os
from contextlib import AsyncExitStack
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.schema import CreateSchema, DropSchema

os.environ["CREWAI_TELEMETRY_DISABLED"] = "true"
os.environ["OTEL_SDK_DISABLED"] = "true"
os.environ["CREWAI_STORAGE_DIR"] = "./data/crewai-tests"

from company_ai.infrastructure.database.base import Base  # noqa: E402
from company_ai.infrastructure.database.repositories import Repository  # noqa: E402
from company_ai.infrastructure.database.session import Database  # noqa: E402
from company_ai.settings import Settings  # noqa: E402


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        admin_token="test-admin",
        openai_api_key="",
        failure_point="",
        max_llm_calls_per_job=6,
        db_pool_mode="pooled",
    )


@pytest.fixture(params=["sqlite", "postgresql"])
async def repo(settings, request):
    schema = None
    if request.param == "postgresql":
        dsn = os.environ.get("TEST_POSTGRES_URL")
        if not dsn:
            pytest.skip("Set TEST_POSTGRES_URL to enable real PostgreSQL coverage")
        settings.database_url = SecretStr(dsn)
        # Tests own only their random schema, never existing tables/data.
        schema = f"test_{uuid4().hex}"
    db = Database(settings.database_dsn, settings=settings)
    try:
        if schema:
            async with db.engine.begin() as connection:
                await connection.execute(CreateSchema(schema))
            db.engine = db.engine.execution_options(schema_translate_map={None: schema})
            db.session.configure(bind=db.engine)
        async with db.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield Repository(db, settings)
    finally:
        if schema:
            async with db.engine.begin() as connection:
                await connection.execute(DropSchema(schema, cascade=True))
        await db.dispose()


@pytest.fixture
async def client_factory():
    async with AsyncExitStack() as stack:

        async def create(app):
            await stack.enter_async_context(app.router.lifespan_context(app))
            return await stack.enter_async_context(
                httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
            )

        yield create


@pytest.fixture
def payload():
    return {
        "company_name": "Acme CRM",
        "website": "https://example.com/",
        "company_description": "Acme provides B2B SaaS CRM software for SMEs in Vietnam.",
        "website_text": "",
        "icp": "B2B SaaS for SMEs in Vietnam",
    }
