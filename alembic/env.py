import asyncio

from alembic import context
from company_ai.infrastructure.database.base import Base
from company_ai.infrastructure.database.session import Database
from company_ai.infrastructure.research.persistence import models  # noqa: F401
from company_ai.settings import get_settings

target_metadata = Base.metadata


def migrate(connection):
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_online():
    settings = get_settings()
    db = Database(settings.database_dsn, settings=settings, null_pool=True)
    try:
        async with db.engine.connect() as connection:
            await connection.run_sync(migrate)
    finally:
        await db.dispose()


if context.is_offline_mode():
    context.configure(
        url=get_settings().database_dsn,
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=get_settings().database_dsn.get_backend_name() == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(run_online())
