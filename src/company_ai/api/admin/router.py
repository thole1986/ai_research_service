from fastapi import APIRouter, Depends

from company_ai.api.dependencies import require_admin
from company_ai.api.research.dependencies import Jobs

router = APIRouter(prefix="/api/v1/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@router.get("/metrics")
async def metrics(service: Jobs):
    return await service.metrics()
