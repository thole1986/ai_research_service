"""Detached read contracts for research administration; no transport or ORM types."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Generic, Literal, TypedDict, TypeVar

JobStatus = Literal["PENDING", "RUNNING", "COMPLETED", "NEEDS_REVIEW", "FAILED"]
AttemptStatus = Literal["CLAIMED", "REQUEST_STARTED", "SUCCEEDED", "FAILED", "UNKNOWN"]
StepName = Literal["COLLECT", "RESEARCH", "CLASSIFY", "SCORE", "REPORT"]
JOB_STATUSES = ("PENDING", "RUNNING", "COMPLETED", "NEEDS_REVIEW", "FAILED")
ATTEMPT_STATUSES = ("CLAIMED", "REQUEST_STARTED", "SUCCEEDED", "FAILED", "UNKNOWN")
STEP_NAMES = ("COLLECT", "RESEARCH", "CLASSIFY", "SCORE", "REPORT")
T = TypeVar("T")


@dataclass(frozen=True)
class Paging:
    offset: int = 0
    limit: int = 50

    def __post_init__(self):
        if self.offset < 0 or not 1 <= self.limit <= 500:
            raise ValueError("Invalid page bounds")


@dataclass(frozen=True)
class JobFilters:
    status: JobStatus | None = None
    company_name: str | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None

    def __post_init__(self):
        if self.status is not None and self.status not in JOB_STATUSES:
            raise ValueError("Unknown job status")
        if self.company_name is not None:
            name = self.company_name.strip()
            if not 1 <= len(name) <= 200:
                raise ValueError("company_name must contain 1–200 characters after trim")
            object.__setattr__(self, "company_name", name)
        for field in ("created_from", "created_to"):
            value = getattr(self, field)
            if value is not None:
                if value.tzinfo is None or value.utcoffset() is None:
                    raise ValueError("Date filters require a timezone")
                object.__setattr__(self, field, value.astimezone(UTC))
        if self.created_from and self.created_to and self.created_from > self.created_to:
            raise ValueError("created_from must be <= created_to")


@dataclass(frozen=True)
class EventFilters:
    event_type: str | None = None

    def __post_init__(self):
        if self.event_type is not None and not 1 <= len(self.event_type) <= 80:
            raise ValueError("event_type must contain 1–80 characters")


@dataclass(frozen=True)
class AttemptFilters:
    step_name: StepName | None = None
    status: AttemptStatus | None = None

    def __post_init__(self):
        if self.step_name is not None and self.step_name not in STEP_NAMES:
            raise ValueError("Unknown step name")
        if self.status is not None and self.status not in ATTEMPT_STATUSES:
            raise ValueError("Unknown attempt status")


@dataclass(frozen=True)
class Page(Generic[T]):
    items: tuple[T, ...]
    total: int
    offset: int
    limit: int


@dataclass(frozen=True)
class JobSummary:
    job_id: str
    company_name: str | None
    website: str | None
    status: str
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    estimated_cost_usd: float
    error_code: str | None


@dataclass(frozen=True)
class ExecutionMetadata:
    provider: str
    model: str
    profile_id: str
    pricing_kind: str


@dataclass(frozen=True)
class AdminStep:
    step_name: str
    status: str
    attempt_count: int
    started_at: datetime | None
    completed_at: datetime | None
    updated_at: datetime
    next_retry_at: datetime | None
    lease_expires_at: datetime | None
    last_heartbeat_at: datetime | None
    output: dict | None
    execution_metadata: ExecutionMetadata | None


@dataclass(frozen=True)
class AdminError:
    code: str
    message: str


@dataclass(frozen=True)
class JobDetail:
    job_id: str
    status: str
    workflow_version: str
    input: dict
    result: dict | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    estimated_cost_usd: float
    error: AdminError | None
    steps: tuple[AdminStep, ...]
    execution_metadata: dict[str, ExecutionMetadata] | None


@dataclass(frozen=True)
class AdminEvent:
    id: str
    step_id: str | None
    event_type: str
    details: dict
    created_at: datetime


@dataclass(frozen=True)
class AdminAttempt:
    attempt_id: str
    step_id: str
    step_name: str
    attempt_no: int
    status: str
    error_type: str | None
    created_at: datetime
    llm_started_at: datetime | None
    finished_at: datetime | None
    tokens_input: int | None
    tokens_output: int | None
    estimated_cost_usd: float | None
    provider_request_id: str | None
    execution_type: str
    provider: str | None
    model: str | None
    pricing_kind: str | None
    collection_started_at: datetime | None
    collection_usage: dict | None


class LLMUsage(TypedDict):
    provider: str | None
    model: str | None
    pricing_kind: str | None
    calls: int
    tokens_input: int | None
    tokens_output: int | None
    estimated_cost_usd: float


class AdminMetrics(TypedDict):
    jobs: dict[str, int]
    steps: dict[str, int]
    events: dict[str, int]
    llm_calls: int
    llm_usage: list[LLMUsage]
    collection_calls: int
    collection_usage: dict[str, int | float]
    collection_credits: int | float | None
    tokens_input: int
    tokens_output: int
    estimated_cost_usd: float
