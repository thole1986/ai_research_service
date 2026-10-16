from sqlalchemy import NullPool

from company_ai.infrastructure.database.session import Database
from company_ai.settings import Settings


async def test_postgres_url_escapes_credentials_and_masks_secrets():
    settings = Settings(
        _env_file=None,
        db_backend="postgresql",
        postgres_user="user@company",
        postgres_password="p@ss:/?#",
        db_pool_size=16,
        db_max_overflow=48,
    )
    url = settings.database_dsn
    assert url.drivername == "postgresql+asyncpg"
    assert url.username == "user@company" and url.password == "p@ss:/?#"
    assert "p@ss:/?#" not in repr(settings)
    db = Database(url, settings=settings)
    assert db.engine.pool.size() == 16
    await db.dispose()


async def test_null_pool_and_legacy_sqlite_url(tmp_path):
    settings = Settings(
        _env_file=None, database_url=f"sqlite:///{tmp_path / 'db.sqlite'}", db_pool_mode="null"
    )
    assert settings.database_dsn.drivername == "sqlite+aiosqlite"
    db = Database(settings.database_dsn, settings=settings)
    assert isinstance(db.engine.pool, NullPool)
    await db.dispose()
