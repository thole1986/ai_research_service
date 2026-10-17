from company_ai.api.dependencies import get_broker_ping, get_database_ready
from company_ai.api.research.dependencies import get_research_repository


async def test_readiness_awaits_database_and_broker(app_factory, client_factory):
    checked = []

    async def database_ready():
        checked.append("db")

    async def broker_ping():
        checked.append("broker")

    app = app_factory()

    async def database_check():
        return database_ready

    async def broker_check():
        return broker_ping

    async def unused_repository():
        raise AssertionError("Readiness must not resolve a research repository")

    app.dependency_overrides[get_database_ready] = database_check
    app.dependency_overrides[get_broker_ping] = broker_check
    app.dependency_overrides[get_research_repository] = unused_repository
    client = await client_factory(app)
    assert (await client.get("/ready")).status_code == 200
    assert checked == ["db", "broker"]

    async def unavailable():
        raise ConnectionError("Private connection details")

    async def unavailable_check():
        return unavailable

    app.dependency_overrides[get_broker_ping] = unavailable_check
    response = await client.get("/ready")
    assert response.status_code == 503
    assert "Private" not in response.text
