"""Administrative read use cases; deliberately has no publisher/provider dependency."""

from company_ai.application.research.admin_ports import ResearchAdminReader
from company_ai.application.research.admin_queries import (
    AttemptFilters,
    EventFilters,
    JobFilters,
    Paging,
)
from company_ai.application.research.jobs import JobNotFound


class ResearchAdminService:
    def __init__(self, reader: ResearchAdminReader):
        self.reader = reader

    async def list_jobs(self, filters: JobFilters, page: Paging):
        if page.limit > 100:
            raise ValueError("Job page limit must be <= 100")
        return await self.reader.list_jobs(filters, page)

    async def get_job_detail(self, job_id: str):
        detail = await self.reader.get_job_detail(job_id)
        if detail is None:
            raise JobNotFound(job_id)
        return detail

    async def list_job_events(self, job_id: str, filters: EventFilters, page: Paging):
        result = await self.reader.list_job_events(job_id, filters, page)
        if result is None:
            raise JobNotFound(job_id)
        return result

    async def list_job_attempts(self, job_id: str, filters: AttemptFilters, page: Paging):
        result = await self.reader.list_job_attempts(job_id, filters, page)
        if result is None:
            raise JobNotFound(job_id)
        return result

    async def get_metrics(self):
        return await self.reader.get_metrics()
