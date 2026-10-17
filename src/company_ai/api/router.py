from fastapi import APIRouter

from company_ai.api.admin.router import router as admin_router
from company_ai.api.health.router import router as health_router
from company_ai.api.research.router import router as research_router

router = APIRouter()
router.include_router(research_router)
router.include_router(admin_router)
router.include_router(health_router)
