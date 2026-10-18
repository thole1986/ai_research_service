"""HTTP validation and explicit public projections for administrative reads."""

from datetime import UTC, datetime
from typing import Any, Generic, TypeVar

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from company_ai.application.research.admin_queries import (
    AttemptStatus,
    JobStatus,
    StepName,
)

T = TypeVar("T")


class JobListQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: JobStatus | None = None
    company_name: str | None = Field(None, min_length=1, max_length=200)
    created_from: AwareDatetime | None = None
    created_to: AwareDatetime | None = None
    offset: int = Field(0, ge=0)
    limit: int = Field(50, ge=1, le=100)

    @field_validator("company_name", mode="before")
    @classmethod
    def trim_name(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("created_from", "created_to", mode="before")
    @classmethod
    def iso_datetime(cls, value):
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                raise ValueError("Use an ISO 8601 datetime with timezone") from None
        return value

    @field_validator("created_from", "created_to")
    @classmethod
    def utc_datetime(cls, value):
        return value.astimezone(UTC) if value is not None else None

    @model_validator(mode="after")
    def ordered_range(self):
        if self.created_from and self.created_to and self.created_from > self.created_to:
            raise ValueError("created_from must be <= created_to")
        return self


class EventListQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_type: str | None = Field(None, min_length=1, max_length=80)
    offset: int = Field(0, ge=0)
    limit: int = Field(100, ge=1, le=500)


class AttemptListQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step_name: StepName | None = None
    status: AttemptStatus | None = None
    offset: int = Field(0, ge=0)
    limit: int = Field(100, ge=1, le=500)


class ReadResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class PageResponse(ReadResponse, Generic[T]):
    items: list[T]
    total: int
    offset: int
    limit: int


class JobSummaryResponse(ReadResponse):
    job_id: str
    company_name: str | None
    website: str | None
    status: str
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    estimated_cost_usd: float
    error_code: str | None


class ExecutionMetadataResponse(ReadResponse):
    provider: str
    model: str
    profile_id: str
    pricing_kind: str


class StepResponse(ReadResponse):
    step_name: str
    status: str
    attempt_count: int
    started_at: datetime | None
    completed_at: datetime | None
    updated_at: datetime
    next_retry_at: datetime | None
    lease_expires_at: datetime | None
    last_heartbeat_at: datetime | None
    output: dict[str, Any] | None
    execution_metadata: ExecutionMetadataResponse | None


class ErrorResponse(ReadResponse):
    code: str
    message: str


class JobDetailResponse(ReadResponse):
    job_id: str
    status: str
    workflow_version: str
    input: dict[str, Any]
    result: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    estimated_cost_usd: float
    error: ErrorResponse | None
    steps: list[StepResponse]
    execution_metadata: dict[str, ExecutionMetadataResponse] | None


class EventResponse(ReadResponse):
    id: str
    step_id: str | None
    event_type: str
    details: dict[str, Any]
    created_at: datetime


class AttemptResponse(ReadResponse):
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
    collection_usage: dict[str, Any] | None


class LLMUsageResponse(ReadResponse):
    provider: str | None
    model: str | None
    pricing_kind: str | None
    calls: int
    tokens_input: int | None
    tokens_output: int | None
    estimated_cost_usd: float


class MetricsResponse(ReadResponse):
    jobs: dict[str, int]
    steps: dict[str, int]
    events: dict[str, int]
    llm_calls: int
    llm_usage: list[LLMUsageResponse]
    collection_calls: int
    collection_usage: dict[str, int | float]
    collection_credits: int | float | None
    tokens_input: int
    tokens_output: int
    estimated_cost_usd: float


class HTTPErrorResponse(BaseModel):
    detail: str
