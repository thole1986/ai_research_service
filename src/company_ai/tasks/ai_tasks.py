import asyncio

from company_ai.application.research.execute_workflow import run_workflow
from company_ai.celery_app import celery_app
from company_ai.domain.research.contracts import InjectedCrash
from company_ai.infrastructure.research.ai.llm_adapter import CrewAIAdapter
from company_ai.runtime import get_worker_observer, run_async


@celery_app.task(name="company_ai.tasks.ai_tasks.run_job", ignore_result=True)
def run_job(job_id: str):
    async def execute(repo):
        observer = get_worker_observer()
        try:
            adapter = CrewAIAdapter(repo.settings, observer=observer)
            await run_workflow(job_id, repo, adapter, repo.settings, observer=observer)
        finally:
            await asyncio.to_thread(observer.flush)

    try:
        run_async(execute)
    except InjectedCrash:
        # Deliberate development demonstration: exit without Celery's exception wrapper.
        import os

        os._exit(86)
