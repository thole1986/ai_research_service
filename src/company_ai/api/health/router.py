from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from company_ai.api.dependencies import ReadinessCheck, get_broker_ping, get_database_ready

router = APIRouter(tags=["health"])


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/ready")
async def ready(
    database_ready: Annotated[ReadinessCheck, Depends(get_database_ready)],
    broker_ping: Annotated[ReadinessCheck, Depends(get_broker_ping)],
):
    try:
        await database_ready()
        await broker_ping()
    except Exception:
        raise HTTPException(503, "DEPENDENCY_UNAVAILABLE") from None
    return {"status": "ready"}
