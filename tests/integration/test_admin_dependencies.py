"""Verify the admin port binding before HTTP route behavior is involved."""

from sqlalchemy import event

from company_ai.api.admin.dependencies import get_admin_reader, get_admin_service
from company_ai.bootstrap.dependencies import get_sqlalchemy_admin_reader
from company_ai.infrastructure.research.persistence.admin_reader import SqlAlchemyAdminReader


async def test_admin_binding_constructs_without_queries_and_port_is_replaceable(repo, app_factory):
    queries = []

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        queries.append(statement)

    event.listen(repo.db.engine.sync_engine, "before_cursor_execute", record)
    try:
        app = app_factory()
        assert app.dependency_overrides[get_admin_reader] is get_sqlalchemy_admin_reader
        async with repo.db.session() as session:
            reader = await get_sqlalchemy_admin_reader(session)
            service = await get_admin_service(reader)
            assert isinstance(reader, SqlAlchemyAdminReader)
            assert service.reader is reader
            assert not session.in_transaction()
        assert queries == []

        sentinel = object()
        assert (await get_admin_service(sentinel)).reader is sentinel
    finally:
        event.remove(repo.db.engine.sync_engine, "before_cursor_execute", record)
