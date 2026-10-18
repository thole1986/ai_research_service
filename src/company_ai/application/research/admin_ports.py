"""Read-only research port. Missing detail/history is represented by None."""

from typing import Protocol

from company_ai.application.research.admin_queries import (
    AdminAttempt,
    AdminEvent,
    AdminMetrics,
    AttemptFilters,
    EventFilters,
    JobDetail,
    JobFilters,
    JobSummary,
    Page,
    Paging,
)


class ResearchAdminReader(Protocol):
    async def list_jobs(self, filters: JobFilters, page: Paging) -> Page[JobSummary]: ...

    async def get_job_detail(self, job_id: str) -> JobDetail | None: ...

    async def list_job_events(
        self, job_id: str, filters: EventFilters, page: Paging
    ) -> Page[AdminEvent] | None: ...

    async def list_job_attempts(
        self, job_id: str, filters: AttemptFilters, page: Paging
    ) -> Page[AdminAttempt] | None: ...

    async def get_metrics(self) -> AdminMetrics: ...
