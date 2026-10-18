from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from pydantic import ValidationError

from company_ai.api.dependencies import AppSettings, require_admin
from company_ai.api.research.dependencies import Jobs
from company_ai.api.research.schemas import EventView, JobAccepted, JobInput, JobView, RetryInput
from company_ai.domain.research.errors import Conflict
from company_ai.domain.research.llm import ExecutionConfig

router = APIRouter(prefix="/api/v1/research/jobs", tags=["research"])


@router.post("", status_code=202, response_model=JobAccepted)
async def create_job(
    body: JobInput,
    settings: AppSettings,
    response: Response,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    service: Jobs,
):
    payload = body.model_dump(mode="json")
    input_chars = sum(
        len(str(value or "")) for key, value in payload.items() if key != "collection_mode"
    )
    if input_chars > settings.max_input_chars:
        raise HTTPException(422, "INPUT_TOO_LONG")
    try:
        job, created = await service.create(idempotency_key, payload)
    except Conflict as exc:
        raise HTTPException(409, str(exc)) from None
    if not created:
        response.status_code = 200
    return {"job_id": job.id, "status": job.status, "status_url": f"/api/v1/research/jobs/{job.id}"}


@router.get("/{job_id}", response_model=JobView)
async def get_job(job_id: str, service: Jobs):
    job, steps = await service.get(job_id)
    execution_metadata = None
    if job.execution_config is not None:
        try:
            profiles = ExecutionConfig.model_validate(job.execution_config).steps
        except ValidationError:
            # A worker with an unavailable/future snapshot must remain inspectable.
            profiles = {}
        execution_metadata = {
            name: {
                "provider": profile.provider,
                "model": profile.model,
                "profile_id": profile.profile_id,
                "config_fingerprint": profile.config_fingerprint,
                "pricing_kind": profile.pricing.kind,
            }
            for name, profile in profiles.items()
        }
    result = job.result_json
    if result is None and any(step.step_name == "COLLECT" for step in steps):
        result = {
            step.step_name.lower(): step.output_json for step in steps if step.status == "COMPLETED"
        } or None
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
                "execution_metadata": (execution_metadata or {}).get(s.step_name),
            }
            for s in steps
        ],
        "result": result,
        "execution_metadata": execution_metadata,
    }


@router.get("/{job_id}/events", response_model=list[EventView])
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


@router.post("/{job_id}/retry", status_code=202, dependencies=[Depends(require_admin)])
async def retry(job_id: str, body: RetryInput, service: Jobs):
    try:
        await service.retry(job_id, body.step_name)
    except Conflict as exc:
        raise HTTPException(409, str(exc)) from None
    return {"job_id": job_id, "status": "PENDING"}
