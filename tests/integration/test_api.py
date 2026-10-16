from company_ai.main import create_app


async def test_api_contracts(repo, settings, payload, client_factory):
    published = []

    async def record(job_id):
        published.append(job_id)

    client = await client_factory(create_app(settings, repo, record, lambda: True))
    headers = {"Idempotency-Key": "api-test"}
    response = await client.post("/api/v1/research/jobs", json=payload, headers=headers)
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    assert (
        await client.post("/api/v1/research/jobs", json=payload, headers=headers)
    ).status_code == 200
    assert published == [job_id]
    assert (
        await client.post(
            "/api/v1/research/jobs", json={**payload, "icp": "different"}, headers=headers
        )
    ).status_code == 409
    assert (await client.post("/api/v1/research/jobs", json=payload)).status_code == 422
    assert (
        await client.post(
            "/api/v1/research/jobs",
            json={**payload, "website": "file:///etc/passwd"},
            headers={"Idempotency-Key": "invalid"},
        )
    ).status_code == 422
    assert (await client.get("/api/v1/research/jobs/missing")).status_code == 404
    state = (await client.get(f"/api/v1/research/jobs/{job_id}")).json()
    assert [s["step_name"] for s in state["steps"]] == ["RESEARCH", "CLASSIFY", "SCORE", "REPORT"]
    assert (await client.get(f"/api/v1/research/jobs/{job_id}/events")).json()[0][
        "event_type"
    ] == "JOB_CREATED"
    assert (await client.get("/health")).status_code == 200
    assert (await client.get("/api/v1/admin/metrics")).status_code == 401
    assert (
        await client.get("/api/v1/admin/metrics", headers={"Authorization": "Bearer test-admin"})
    ).json()["jobs"]["PENDING"] == 1
    assert (
        await client.post(
            f"/api/v1/research/jobs/{job_id}/retry",
            json={"step_name": "RESEARCH", "acknowledge_possible_duplicate_charge": True},
        )
    ).status_code == 401


async def test_enqueue_gap_and_limits(repo, settings, payload, client_factory):

    async def unavailable(_):
        raise ConnectionError("private error must not be returned")

    client = await client_factory(create_app(settings, repo, unavailable, lambda: True))
    response = await client.post(
        "/api/v1/research/jobs", json=payload, headers={"Idempotency-Key": "gap"}
    )
    assert response.status_code == 202
    assert await repo.list_resumable_jobs() == [response.json()["job_id"]]
    assert (
        await client.post(
            "/api/v1/research/jobs",
            json={**payload, "company_description": "x" * 6500},
            headers={"Idempotency-Key": "long"},
        )
    ).status_code == 422
    assert (await client.post("/api/v1/research/jobs", content=b"x" * 50000)).status_code == 413
