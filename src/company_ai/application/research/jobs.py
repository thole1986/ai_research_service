from company_ai.application.research.ports import JobPublisher, ResearchJobRepository
from company_ai.observability import log_event


class JobNotFound(Exception):
    pass


class JobService:
    def __init__(self, repo: ResearchJobRepository, publish: JobPublisher):
        self.repo, self.publish = repo, publish

    async def _enqueue(self, job_id):
        try:
            await self.publish(job_id)
        except Exception:
            log_event("ENQUEUE_DEFERRED", job_id=job_id, error_category="BROKER_UNAVAILABLE")

    async def create(self, key, payload):
        job, created = await self.repo.create_job_idempotently(key, payload)
        if created:
            await self._enqueue(job.id)
        return job, created

    async def get(self, job_id):
        job, steps = await self.repo.get_job(job_id)
        if job is None:
            raise JobNotFound(job_id)
        return job, steps

    async def events(self, job_id, offset=0, limit=100):
        await self.get(job_id)
        return await self.repo.events(job_id, offset, limit)

    async def retry(self, job_id, step_name):
        await self.get(job_id)
        await self.repo.retry_unknown(job_id, step_name)
        await self._enqueue(job_id)

    async def metrics(self):
        return await self.repo.metrics()
