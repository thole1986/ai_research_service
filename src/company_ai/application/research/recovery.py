from company_ai.observability import log_event


async def resume_pending(repo, publish):
    count = 0
    for job_id in await repo.list_resumable_jobs():
        try:
            await publish(job_id)
            count += 1
            log_event("RECOVERY_ENQUEUED", job_id=job_id)
        except Exception:
            log_event(
                "RECOVERY_ENQUEUE_DEFERRED", job_id=job_id, error_category="BROKER_UNAVAILABLE"
            )
    return count


def reconcile_unknown_step():
    # No reliable provider result lookup/idempotency contract is configured in this MVP.
    # Never infer remote success from REQUEST_STARTED or local logs.
    return {"status": "NEEDS_REVIEW", "automatic_reconciliation_supported": False}
