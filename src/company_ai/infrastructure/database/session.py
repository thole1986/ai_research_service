from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TypeVar

from sqlalchemy import NullPool, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from company_ai.infrastructure.database.transactions import write_transaction

T = TypeVar("T")


class Database:
    """One engine per owning loop; session factory shared across domain adapters."""

    def __init__(self, url, *, settings=None, null_pool=False):
        parsed = make_url(url)
        backend = parsed.get_backend_name()
        if parsed.drivername not in ("sqlite+aiosqlite", "postgresql+asyncpg"):
            raise ValueError("An async SQLite/PostgreSQL driver is required")
        options = {"hide_parameters": True, "pool_pre_ping": True}
        use_null = null_pool or (settings and settings.db_pool_mode == "null")
        if use_null:
            options["poolclass"] = NullPool
        if backend == "sqlite":
            if parsed.database and parsed.database != ":memory:":
                Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
            options["connect_args"] = {"timeout": 5}
        elif settings:
            options["pool_recycle"] = settings.db_pool_recycle
            if not use_null:
                options.update(
                    pool_size=settings.db_pool_size,
                    max_overflow=settings.db_max_overflow,
                    pool_timeout=settings.db_pool_timeout,
                )
        self.backend = backend
        self.engine = create_async_engine(parsed, **options)
        self.session = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        if backend == "sqlite":

            @event.listens_for(self.engine.sync_engine, "connect")
            def pragmas(connection, _):
                # SQLite's built-in lower()/LIKE only fold ASCII. Match the
                # administrative JSON-name search with Python's Unicode lower.
                connection.create_function(
                    "unicode_lower",
                    1,
                    lambda value: value.lower() if isinstance(value, str) else value,
                    deterministic=True,
                )
                cursor = connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA busy_timeout=5000")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

    async def write(self, operation: Callable[[AsyncSession], Awaitable[T]]) -> T:
        return await write_transaction(self.session.begin, self.backend, operation)

    async def get_session(self):
        """Async DI generator for short-lived CRUD; callers own commit/rollback boundaries."""
        async with self.session() as session:
            yield session

    async def ready(self):
        async with self.engine.connect() as connection:
            version = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            if not version:
                raise RuntimeError("Database is not migrated")
            await connection.execute(text("SELECT 1"))

    async def dispose(self):
        await self.engine.dispose()
