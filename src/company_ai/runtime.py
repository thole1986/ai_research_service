"""Composition root for Celery's synchronous task entrypoints."""

import asyncio
from contextlib import asynccontextmanager
from functools import lru_cache

from company_ai.infrastructure.database.session import Database
from company_ai.infrastructure.research.persistence.repositories import SqlAlchemyResearchRepository
from company_ai.infrastructure.telemetry.langfuse import create_observer
from company_ai.settings import get_settings


@lru_cache
def get_worker_observer():
    # Created within the worker process, never at import time/before a fork.
    return create_observer(get_settings())


@asynccontextmanager
async def repository():
    settings = get_settings()
    db = Database(settings.database_dsn, settings=settings)
    try:
        yield SqlAlchemyResearchRepository(db, settings)
    finally:
        # Dispose on the owning loop BEFORE asyncio.run closes it. Never cache async engines
        # globally across Celery tasks, event loops or forked worker processes.
        await db.dispose()


def run_async(operation):
    async def run():
        async with repository() as repo:
            return await operation(repo)

    return asyncio.run(run())
