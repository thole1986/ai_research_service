from company_ai.application.execute_workflow import run_workflow
from company_ai.celery_app import celery_app
from company_ai.domain.contracts import InjectedCrash
from company_ai.infrastructure.ai.llm_adapter import CrewAIAdapter
from company_ai.runtime import run_async


@celery_app.task(name="company_ai.tasks.ai_tasks.run_job", ignore_result=True)
def run_job(job_id: str):
    async def execute(repo):
        await run_workflow(job_id, repo, CrewAIAdapter(repo.settings), repo.settings)

    try:
        run_async(execute)
    except InjectedCrash:
        # Deliberate development demonstration: exit without Celery's exception wrapper.
        import os

        os._exit(86)
