from company_ai.application.research.recovery import resume_pending
from company_ai.celery_app import celery_app
from company_ai.infrastructure.messaging import publish
from company_ai.runtime import run_async


@celery_app.task(name="company_ai.tasks.maintenance_tasks.scan_stale_steps", ignore_result=True)
def scan_stale_steps():
    async def scan(repo):
        return len(await repo.scan_stale_steps())

    return run_async(scan)


@celery_app.task(name="company_ai.tasks.maintenance_tasks.resume_pending_jobs", ignore_result=True)
def resume_pending_jobs():
    async def resume(repo):
        return await resume_pending(repo, publish)

    return run_async(resume)


@celery_app.task(
    name="company_ai.tasks.maintenance_tasks.cleanup_old_job_events", ignore_result=True
)
def cleanup_old_job_events():
    async def cleanup(repo):
        return await repo.cleanup_events()

    return run_async(cleanup)
