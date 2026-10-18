"""Application ports. No SQLAlchemy, Redis, FastAPI or CrewAI imports belong here."""

from contextlib import AbstractContextManager
from typing import Any, Protocol

from company_ai.domain.research.collection import CollectionInput, CollectionResult
from company_ai.domain.research.contracts import AIResult, Claim
from company_ai.domain.research.entities import JobEvent, ResearchJob, ResearchStep


class ResearchJobRepository(Protocol):
    """Port for HTTP job use cases, independent of worker lease operations."""

    async def create_job_idempotently(
        self, key: str, payload: dict
    ) -> tuple[ResearchJob, bool]: ...
    async def get_job(self, job_id: str) -> tuple[ResearchJob | None, list[ResearchStep]]: ...
    async def events(self, job_id: str, offset: int = 0, limit: int = 100) -> list[JobEvent]: ...
    async def retry_unknown(self, job_id: str, step_name: str) -> None: ...
    async def metrics(self) -> dict: ...


class ResearchRepository(ResearchJobRepository, Protocol):
    """Full checkpoint/lease persistence port used by research workers."""

    async def claim_step(self, step_id: str, input_hash: str) -> Claim | None: ...
    async def heartbeat(self, claim: Claim) -> bool: ...
    async def request_started(
        self, claim: Claim, reserved_cost: float, execution_metadata: dict | None = None
    ) -> bool: ...
    async def pin_model_identity(
        self, claim: Claim, profile_key: str, digest: str | None
    ) -> bool: ...
    async def record_llm_usage(
        self, claim: Claim, result: AIResult, cost: float | None
    ) -> bool: ...
    async def collection_request_started(self, claim: Claim) -> bool: ...
    async def complete_step_with_fencing(
        self,
        claim: Claim,
        output: dict,
        result: AIResult | CollectionResult | None = None,
        cost: float | None = None,
    ) -> bool: ...
    async def review_job(self, job_id: str, category: str) -> None: ...
    async def fail(self, claim: Claim, status: str, category: str) -> bool: ...
    async def safe_retry(self, claim: Claim) -> bool: ...
    async def scan_stale_steps(self, limit: int = 100) -> list[str]: ...
    async def list_resumable_jobs(self, limit: int = 100) -> list[str]: ...
    async def cleanup_events(self) -> int: ...


class JobPublisher(Protocol):
    async def __call__(self, job_id: str) -> None: ...


class CompanySourceCollector(Protocol):
    async def collect(self, input: CollectionInput) -> CollectionResult: ...


class Observation(Protocol):
    def update(self, **fields: Any) -> None: ...


class WorkflowObserver(Protocol):
    def span(
        self,
        name: str,
        *,
        metadata: dict | None = None,
        input: Any = None,
        generation: bool = False,
        model: str | None = None,
        model_parameters: dict | None = None,
        session_id: str | None = None,
    ) -> AbstractContextManager[Observation]: ...
