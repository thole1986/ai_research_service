"""Short operation scopes shared by HTTP sessions and worker session factories."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import TypeVar

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

T = TypeVar("T")


async def write_transaction(
    begin: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    backend: str,
    operation: Callable[[AsyncSession], Awaitable[T]],
) -> T:
    # Retry only confirmed rollbacks. Never replay uncertain commit/network failures,
    # or include broker/provider I/O in the operation callback.
    for retry in range(4):
        try:
            async with begin() as session:
                return await operation(session)
        except DBAPIError as exc:
            code = getattr(exc.orig, "sqlstate", None)
            retryable = backend == "sqlite" and "locked" in str(exc.orig).lower()
            retryable |= code in ("40001", "40P01")
            if not retryable or retry == 3:
                raise
            await asyncio.sleep(0.025 * 2**retry)
    raise AssertionError("Unreachable transaction retry state")


class SessionOperations:
    """Reuse an injected HTTP session, ending each transaction before external I/O.

    This object owns operation boundaries, not the session lifetime. It must not be
    used concurrently or nested inside a caller-owned transaction.
    """

    def __init__(self, session: AsyncSession):
        self._session = session
        self.backend = session.get_bind().dialect.name

    def _ensure_idle(self) -> None:
        if self._session.in_transaction():
            raise RuntimeError("Research operation requires an idle session")

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        self._ensure_idle()
        try:
            yield self._session
        finally:
            # End read autobegin too: GET followed by retry must start a fresh write,
            # and broker publication must never hold a checked-out connection.
            await self._session.rollback()

    @asynccontextmanager
    async def _begin(self) -> AsyncIterator[AsyncSession]:
        self._ensure_idle()
        async with self._session.begin():
            yield self._session

    async def write(self, operation: Callable[[AsyncSession], Awaitable[T]]) -> T:
        return await write_transaction(self._begin, self.backend, operation)
