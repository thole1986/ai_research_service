import pytest
from sqlalchemy import text

from company_ai.bootstrap import lifespan as lifecycle
from company_ai.main import create_app


async def test_engine_is_created_only_in_lifespan_and_disposed_on_error(settings, monkeypatch):
    databases, disposed = [], []
    original_database = lifecycle.Database

    class TrackingDatabase(original_database):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            databases.append(self)

        async def dispose(self):
            await super().dispose()
            disposed.append(self)

    monkeypatch.setattr(lifecycle, "Database", TrackingDatabase)
    app = create_app(settings)
    assert databases == []
    assert not hasattr(app.state, "database")
    assert not hasattr(app.state, "jobs")

    with pytest.raises(RuntimeError, match="startup body failed"):
        async with app.router.lifespan_context(app):
            assert app.state.database is databases[0]
            async with app.state.database.engine.connect() as connection:
                assert await connection.scalar(text("SELECT 1")) == 1
            raise RuntimeError("startup body failed")

    assert disposed == databases
    assert not hasattr(app.state, "database")
    assert not hasattr(app.state, "broker")
    async with app.router.lifespan_context(app):
        assert app.state.database is databases[1]
    assert len(databases) == 2
    assert disposed == databases
