from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis

from company_ai.bootstrap.dependencies import get_database
from company_ai.infrastructure.database.session import Database


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = app.state.settings
    async with AsyncExitStack() as resources:
        # External DB overrides own their resource lifetime (e.g. test fixtures).
        # No engine is created when importing main or constructing the application.
        if get_database not in app.dependency_overrides:
            database = Database(settings.database_dsn, settings=settings)
            resources.push_async_callback(database.dispose)
            app.state.database = database
            resources.callback(delattr, app.state, "database")

        broker = Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)
        resources.push_async_callback(broker.aclose)
        app.state.broker = broker
        resources.callback(delattr, app.state, "broker")
        yield
