import asyncio


async def publish(job_id):
    def send():
        from company_ai.tasks.ai_tasks import run_job

        run_job.apply_async(args=[job_id], queue="ai")

    # Celery exposes synchronous broker publishing; isolate it from the API's event loop.
    await asyncio.to_thread(send)
