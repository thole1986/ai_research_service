import asyncio
from contextlib import asynccontextmanager, suppress

from company_ai.domain.contracts import InjectedCrash


def inject_failure(settings, step, point):
    if (
        settings.app_env == "development"
        and settings.failure_step == step
        and settings.failure_point == point
    ):
        raise InjectedCrash(f"{step}:{point}")


@asynccontextmanager
async def keep_lease(repo, claim, settings):
    async def heartbeat():
        while True:
            await asyncio.sleep(max(1, settings.lease_ttl_seconds / 3))
            try:
                if not await repo.heartbeat(claim):
                    return
            except Exception:
                return  # Completion still has to pass the unexpired-lease fence.

    task = asyncio.create_task(heartbeat())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
