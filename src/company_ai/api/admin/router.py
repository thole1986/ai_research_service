from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from company_ai.api.admin.dependencies import Admin
from company_ai.api.admin.schemas import (
    AttemptListQuery,
    AttemptResponse,
    EventListQuery,
    EventResponse,
    HTTPErrorResponse,
    JobDetailResponse,
    JobListQuery,
    JobSummaryResponse,
    MetricsResponse,
    PageResponse,
)
from company_ai.api.dependencies import require_admin
from company_ai.application.research.admin_queries import (
    AttemptFilters,
    EventFilters,
    JobFilters,
    Paging,
)

router = APIRouter(
    prefix="/api/v1/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
    responses={401: {"model": HTTPErrorResponse, "description": "ADMIN_AUTH_REQUIRED"}},
)
NOT_FOUND = {404: {"model": HTTPErrorResponse, "description": "JOB_NOT_FOUND"}}


@router.get("/metrics", response_model=MetricsResponse)
async def metrics(service: Admin):
    """Read persisted counters and estimates, without contacting providers."""
    return MetricsResponse.model_validate(await service.get_metrics())


@router.get("/jobs", response_model=PageResponse[JobSummaryResponse])
async def list_jobs(query: Annotated[JobListQuery, Query()], service: Admin):
    """List summaries ordered by created_at DESC, id DESC; company search is literal."""
    filters = JobFilters(query.status, query.company_name, query.created_from, query.created_to)
    page = await service.list_jobs(filters, Paging(query.offset, query.limit))
    return PageResponse[JobSummaryResponse].model_validate(page)


@router.get("/jobs/{job_id}", response_model=JobDetailResponse, responses=NOT_FOUND)
async def get_job_detail(job_id: UUID, service: Admin):
    """Read saved company input, ordered checkpoints, result and safe execution metadata."""
    return JobDetailResponse.model_validate(await service.get_job_detail(str(job_id)))


@router.get(
    "/jobs/{job_id}/events", response_model=PageResponse[EventResponse], responses=NOT_FOUND
)
async def list_job_events(job_id: UUID, query: Annotated[EventListQuery, Query()], service: Admin):
    """Read sanitized audit events ordered by created_at ASC, id ASC."""
    page = await service.list_job_events(
        str(job_id), EventFilters(query.event_type), Paging(query.offset, query.limit)
    )
    return PageResponse[EventResponse].model_validate(page)


@router.get(
    "/jobs/{job_id}/attempts", response_model=PageResponse[AttemptResponse], responses=NOT_FOUND
)
async def list_job_attempts(
    job_id: UUID, query: Annotated[AttemptListQuery, Query()], service: Admin
):
    """Read attempts and nullable saved usage, ordered by created_at ASC, id ASC."""
    page = await service.list_job_attempts(
        str(job_id),
        AttemptFilters(query.step_name, query.status),
        Paging(query.offset, query.limit),
    )
    return PageResponse[AttemptResponse].model_validate(page)
