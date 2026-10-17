from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, StringConstraints

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class JobInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company_name: Annotated[Text, Field(max_length=200)]
    website: HttpUrl | None = None
    company_description: Annotated[Text, Field(max_length=20000)]
    website_text: str = Field("", max_length=20000)
    icp: Annotated[Text, Field(max_length=2000)]


class RetryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step_name: Literal["RESEARCH", "CLASSIFY", "SCORE", "REPORT"]
    acknowledge_possible_duplicate_charge: Literal[True]


class JobAccepted(BaseModel):
    job_id: str
    status: str
    status_url: str


class StepView(BaseModel):
    step_name: str
    status: str
    attempt_count: int
    started_at: datetime | None
    completed_at: datetime | None
    updated_at: datetime
    output: dict[str, Any] | None


class ErrorView(BaseModel):
    code: str
    message: str | None


class JobView(BaseModel):
    job_id: str
    status: str
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    estimated_cost_usd: float
    error: ErrorView | None
    steps: list[StepView]
    result: dict[str, Any] | None


class EventView(BaseModel):
    id: str
    step_id: str | None
    event_type: str
    details: dict[str, Any]
    created_at: datetime
