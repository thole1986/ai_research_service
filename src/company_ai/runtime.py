"""Composition root for Celery's synchronous task entrypoints."""

import asyncio
from contextlib import asynccontextmanager

from company_ai.infrastructure.database.repositories import Repository
from company_ai.infrastructure.database.session import Database
from company_ai.settings import get_settings


@asynccontextmanager
async def repository():
    settings = get_settings()
    db = Database(settings.database_dsn, settings=settings)
    try:
        yield Repository(db, settings)
    finally:
        # Dispose on the owning loop BEFORE asyncio.run closes it. Never cache async engines
        # globally across Celery tasks, event loops or forked worker processes.
        await db.dispose()


def run_async(operation):
    async def run():
        async with repository() as repo:
            return await operation(repo)

    return asyncio.run(run())
