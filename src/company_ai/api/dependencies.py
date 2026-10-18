"""HTTP dependency contracts; concrete adapters are bound in bootstrap."""

import secrets
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from company_ai.settings import Settings

ReadinessCheck = Callable[[], Awaitable[object]]


async def get_settings(request: Request) -> Settings:
    return request.app.state.settings


AppSettings = Annotated[Settings, Depends(get_settings)]
admin_bearer = HTTPBearer(
    auto_error=False,
    scheme_name="AdminBearer",
    description="Shared ADMIN_TOKEN for internal administrative access.",
)


async def require_admin(
    settings: AppSettings,
    request: Request,
    _credentials: Annotated[HTTPAuthorizationCredentials | None, Security(admin_bearer)],
) -> None:
    token = settings.admin_token.get_secret_value()
    authorization = request.headers.get("Authorization", "")
    if not token or not secrets.compare_digest(
        authorization.encode("utf-8"), f"Bearer {token}".encode("utf-8")
    ):
        raise HTTPException(401, "ADMIN_AUTH_REQUIRED", headers={"WWW-Authenticate": "Bearer"})


async def get_database_ready() -> ReadinessCheck:
    raise RuntimeError("Database readiness dependency is not configured")


async def get_broker_ping() -> ReadinessCheck:
    raise RuntimeError("Broker readiness dependency is not configured")
