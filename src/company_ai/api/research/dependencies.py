"""Research service graph depends only on application ports."""

from typing import Annotated

from fastapi import Depends

from company_ai.application.research.jobs import JobService
from company_ai.application.research.ports import JobPublisher, ResearchJobRepository


async def get_research_repository() -> ResearchJobRepository:
    raise RuntimeError("Research repository dependency is not configured")


async def get_job_publisher() -> JobPublisher:
    raise RuntimeError("Job publisher dependency is not configured")


async def get_job_service(
    repo: Annotated[ResearchJobRepository, Depends(get_research_repository)],
    publisher: Annotated[JobPublisher, Depends(get_job_publisher)],
) -> JobService:
    return JobService(repo, publisher)


Jobs = Annotated[JobService, Depends(get_job_service)]
