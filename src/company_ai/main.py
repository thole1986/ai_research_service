from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

from company_ai.api.routes import router
from company_ai.application.jobs import JobNotFound, JobService
from company_ai.infrastructure.database.repositories import Repository
from company_ai.infrastructure.database.session import Database
from company_ai.infrastructure.messaging import publish
from company_ai.observability import configure_logging, log_event
from company_ai.settings import get_settings


class PayloadLimit:
    def __init__(self, app, limit):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        # Bound streamed/chunked payloads before Pydantic parses them.
        messages, total = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            total += len(message.get("body", b""))
            if total > self.limit:
                return await JSONResponse({"detail": "PAYLOAD_TOO_LARGE"}, 413)(
                    scope, receive, send
                )
            messages.append(message)
            if not message.get("more_body", False):
                break
        index = 0

        async def replay():
            nonlocal index
            if index < len(messages):
                message = messages[index]
                index += 1
                return message
            return await receive()

        await self.app(scope, replay, send)


def create_app(settings=None, repo=None, enqueue=None, broker_ping=None):
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    owned_db = None if repo is not None else Database(settings.database_dsn, settings=settings)
    repo = repo if repo is not None else Repository(owned_db, settings)
    broker = Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            await broker.aclose()
            if owned_db is not None:
                await owned_db.dispose()

    app = FastAPI(title="AI Company Research", version="0.2.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.jobs = JobService(repo, enqueue or publish)
    app.state.database = repo.db
    app.state.database_ready = repo.db.ready
    app.state.broker_ping = broker_ping or broker.ping
    app.add_middleware(PayloadLimit, limit=settings.max_input_chars * 6 + 4096)

    @app.middleware("http")
    async def tracing(request: Request, call_next):
        # Generate a trusted ID rather than logging arbitrary client-controlled strings.
        request_id = str(uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        log_event("HTTP_REQUEST", request_id=request_id, status_code=response.status_code)
        return response

    app.include_router(router)

    @app.exception_handler(JobNotFound)
    async def not_found(request, exc):
        return JSONResponse({"detail": "JOB_NOT_FOUND"}, status_code=404)

    return app


app = create_app()
