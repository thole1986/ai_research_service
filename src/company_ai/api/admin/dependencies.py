"""Administrative service graph depends only on application read contracts."""

from typing import Annotated

from fastapi import Depends

from company_ai.application.research.admin_ports import ResearchAdminReader
from company_ai.application.research.admin_service import ResearchAdminService


async def get_admin_reader() -> ResearchAdminReader:
    raise RuntimeError("Research admin reader dependency is not configured")


async def get_admin_service(
    reader: Annotated[ResearchAdminReader, Depends(get_admin_reader)],
) -> ResearchAdminService:
    return ResearchAdminService(reader)


Admin = Annotated[ResearchAdminService, Depends(get_admin_service)]
