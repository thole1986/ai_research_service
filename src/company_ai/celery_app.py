from celery import Celery
from celery.schedules import crontab
from celery.signals import setup_logging

from company_ai.observability import configure_logging
from company_ai.settings import get_settings

settings = get_settings()
celery_app = Celery(
    "company_ai",
    broker=settings.redis_url,
    include=["company_ai.tasks.ai_tasks", "company_ai.tasks.maintenance_tasks"],
)

celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=False,
    worker_prefetch_multiplier=1,
    worker_concurrency=1,
    task_ignore_result=True,
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    task_default_queue="ai",
    broker_connection_retry_on_startup=True,
    broker_connection_timeout=2,
    broker_transport_options={
        "visibility_timeout": 3600,
        "socket_connect_timeout": 2,
        "socket_timeout": 2,
    },
    task_publish_retry=True,
    task_publish_retry_policy={
        "max_retries": 2,
        "interval_start": 0,
        "interval_step": 0.2,
        "interval_max": 0.5,
    },
    task_routes={
        "company_ai.tasks.ai_tasks.run_job": {"queue": "ai"},
        "company_ai.tasks.maintenance_tasks.*": {"queue": "maintenance"},
    },
    timezone="UTC",
    enable_utc=True,
    beat_schedule={
        "scan-stale-ai-steps": {
            "task": "company_ai.tasks.maintenance_tasks.scan_stale_steps",
            "schedule": settings.recovery_scan_seconds,
            "options": {"queue": "maintenance"},
        },
        "resume-pending-jobs": {
            "task": "company_ai.tasks.maintenance_tasks.resume_pending_jobs",
            "schedule": settings.recovery_scan_seconds,
            "options": {"queue": "maintenance"},
        },
        "cleanup-old-job-events": {
            "task": "company_ai.tasks.maintenance_tasks.cleanup_old_job_events",
            "schedule": crontab(hour=2, minute=0),
            "options": {"queue": "maintenance"},
        },
    },
)


@setup_logging.connect
def logging_setup(**kwargs):
    configure_logging(settings.log_level)
