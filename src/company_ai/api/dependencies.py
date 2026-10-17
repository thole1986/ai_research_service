"""HTTP dependency contracts; concrete adapters are bound in bootstrap."""

import secrets
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request

from company_ai.settings import Settings

ReadinessCheck = Callable[[], Awaitable[object]]


async def get_settings(request: Request) -> Settings:
    return request.app.state.settings


AppSettings = Annotated[Settings, Depends(get_settings)]


async def require_admin(
    settings: AppSettings, authorization: Annotated[str | None, Header()] = None
) -> None:
    token = settings.admin_token.get_secret_value()
    if not token or not secrets.compare_digest(authorization or "", f"Bearer {token}"):
        raise HTTPException(401, "ADMIN_AUTH_REQUIRED")


async def get_database_ready() -> ReadinessCheck:
    raise RuntimeError("Database readiness dependency is not configured")


async def get_broker_ping() -> ReadinessCheck:
    raise RuntimeError("Broker readiness dependency is not configured")
