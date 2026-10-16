from company_ai.main import create_app


async def test_readiness_awaits_database_and_broker(repo, settings, client_factory):
    checked = []

    async def database_ready():
        checked.append("db")

    async def broker_ping():
        checked.append("broker")

    app = create_app(settings, repo, broker_ping=broker_ping)
    app.state.database_ready = database_ready
    client = await client_factory(app)
    assert (await client.get("/ready")).status_code == 200
    assert checked == ["db", "broker"]

    async def unavailable():
        raise ConnectionError("Private connection details")

    app.state.broker_ping = unavailable
    response = await client.get("/ready")
    assert response.status_code == 503
    assert "Private" not in response.text
