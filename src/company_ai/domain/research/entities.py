"""Persistence-independent research job aggregate and immutable read snapshots."""

from dataclasses import dataclass
from datetime import datetime

from company_ai.domain.research.contracts import STEPS
from company_ai.domain.research.errors import Conflict


@dataclass(frozen=True)
class ResearchStep:
    id: str
    job_id: str
    step_name: str
    status: str
    input_hash: str | None
    output_json: dict | None
    output_hash: str | None
    attempt_count: int
    lease_token: str | None
    lease_expires_at: datetime | None
    last_heartbeat_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    updated_at: datetime
    next_retry_at: datetime | None


@dataclass(frozen=True)
class ResearchJob:
    id: str
    idempotency_key: str
    request_hash: str
    status: str
    input_json: dict
    result_json: dict | None
    error_code: str | None
    error_message: str | None
    total_estimated_cost_usd: float
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None

    @property
    def can_execute(self):
        return self.status in ("PENDING", "RUNNING")

    @staticmethod
    def retry_candidate(steps, step_name, max_attempts):
        ordered = sorted(steps, key=lambda step: STEPS.index(step.step_name))
        candidate = next((step for step in ordered if step.status != "COMPLETED"), None)
        if (
            not candidate
            or candidate.step_name != step_name
            or candidate.status not in ("UNKNOWN", "NEEDS_REVIEW")
            or candidate.attempt_count >= max_attempts
            or any(step.status != "PENDING" for step in ordered[STEPS.index(step_name) + 1 :])
        ):
            raise Conflict("RETRY_NOT_ALLOWED")
        return candidate.id


@dataclass(frozen=True)
class JobEvent:
    id: str
    job_id: str
    step_id: str | None
    event_type: str
    details_json: dict
    created_at: datetime
