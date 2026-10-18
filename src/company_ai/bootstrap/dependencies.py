"""Concrete FastAPI bindings; new domains can reuse get_session independently."""

from collections.abc import AsyncIterator
from functools import partial
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncSession

from company_ai.api.admin.dependencies import get_admin_reader
from company_ai.api.dependencies import (
    AppSettings,
    ReadinessCheck,
    get_broker_ping,
    get_database_ready,
)
from company_ai.api.research.dependencies import get_job_publisher, get_research_repository
from company_ai.application.research.admin_ports import ResearchAdminReader
from company_ai.application.research.ports import JobPublisher, ResearchJobRepository
from company_ai.infrastructure.database.session import Database
from company_ai.infrastructure.database.transactions import SessionOperations
from company_ai.infrastructure.messaging import publish
from company_ai.infrastructure.research.persistence.admin_reader import SqlAlchemyAdminReader
from company_ai.infrastructure.research.persistence.readiness import research_database_ready
from company_ai.infrastructure.research.persistence.repositories import SqlAlchemyResearchRepository


async def get_database(request: Request) -> Database:
    return request.app.state.database


async def get_session(
    database: Annotated[Database, Depends(get_database)],
) -> AsyncIterator[AsyncSession]:
    # Session close rolls back unfinished work on success, errors and cancellation.
    # Explicit operations commit before enqueue; the dependency never auto-commits.
    async with database.session() as session:
        yield session


async def get_sqlalchemy_research_repository(
    session: Annotated[AsyncSession, Depends(get_session)], settings: AppSettings
) -> ResearchJobRepository:
    return SqlAlchemyResearchRepository(SessionOperations(session), settings)


async def get_celery_publisher() -> JobPublisher:
    return publish


async def get_sqlalchemy_admin_reader(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> ResearchAdminReader:
    return SqlAlchemyAdminReader(session)


async def get_sqlalchemy_readiness(
    database: Annotated[Database, Depends(get_database)],
) -> ReadinessCheck:
    return partial(research_database_ready, database)


async def get_redis_ping(request: Request) -> ReadinessCheck:
    return request.app.state.broker.ping


def configure_dependencies(app: FastAPI) -> None:
    app.dependency_overrides.update(
        {
            get_admin_reader: get_sqlalchemy_admin_reader,
            get_research_repository: get_sqlalchemy_research_repository,
            get_job_publisher: get_celery_publisher,
            get_database_ready: get_sqlalchemy_readiness,
            get_broker_ping: get_redis_ping,
        }
    )
