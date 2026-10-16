import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response

from company_ai.api.schemas import EventView, JobAccepted, JobInput, JobView, RetryInput
from company_ai.application.jobs import JobService
from company_ai.domain.errors import Conflict

router = APIRouter()


async def job_service(request: Request):
    return request.app.state.jobs


Jobs = Annotated[JobService, Depends(job_service)]


async def admin(request: Request, authorization: Annotated[str | None, Header()] = None):
    token = request.app.state.settings.admin_token.get_secret_value()
    if not token or not secrets.compare_digest(authorization or "", f"Bearer {token}"):
        raise HTTPException(401, "ADMIN_AUTH_REQUIRED")


@router.post("/api/v1/research/jobs", status_code=202, response_model=JobAccepted)
async def create_job(
    body: JobInput,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    service: Jobs,
):
    payload = body.model_dump(mode="json")
    if (
        sum(len(str(v or "")) for v in payload.values())
        > request.app.state.settings.max_input_chars
    ):
        raise HTTPException(422, "INPUT_TOO_LONG")
    try:
        job, created = await service.create(idempotency_key, payload)
    except Conflict as exc:
        raise HTTPException(409, str(exc)) from None
    if not created:
        response.status_code = 200
    return {"job_id": job.id, "status": job.status, "status_url": f"/api/v1/research/jobs/{job.id}"}


@router.get("/api/v1/research/jobs/{job_id}", response_model=JobView)
async def get_job(job_id: str, service: Jobs):
    job, steps = await service.get(job_id)
    return {
        "job_id": job.id,
        "status": job.status,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "completed_at": job.completed_at,
        "estimated_cost_usd": float(job.total_estimated_cost_usd),
        "error": {"code": job.error_code, "message": job.error_message} if job.error_code else None,
        "steps": [
            {
                "step_name": s.step_name,
                "status": s.status,
                "attempt_count": s.attempt_count,
                "started_at": s.started_at,
                "completed_at": s.completed_at,
                "updated_at": s.updated_at,
                "output": s.output_json,
            }
            for s in steps
        ],
        "result": job.result_json,
    }


@router.get("/api/v1/research/jobs/{job_id}/events", response_model=list[EventView])
async def events(
    job_id: str,
    service: Jobs,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
):
    return [
        {
            "id": e.id,
            "step_id": e.step_id,
            "event_type": e.event_type,
            "details": e.details_json,
            "created_at": e.created_at,
        }
        for e in await service.events(job_id, offset, limit)
    ]


@router.post("/api/v1/research/jobs/{job_id}/retry", status_code=202, dependencies=[Depends(admin)])
async def retry(job_id: str, body: RetryInput, request: Request, service: Jobs):
    try:
        await service.retry(job_id, body.step_name)
    except Conflict as exc:
        raise HTTPException(409, str(exc)) from None
    return {"job_id": job_id, "status": "PENDING"}


@router.get("/api/v1/admin/metrics", dependencies=[Depends(admin)])
async def metrics(service: Jobs):
    return await service.metrics()


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request, service: Jobs):
    try:
        await request.app.state.database_ready()
        await request.app.state.broker_ping()
    except Exception:
        raise HTTPException(503, "DEPENDENCY_UNAVAILABLE") from None
    return {"status": "ready"}
