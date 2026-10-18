from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from company_ai.infrastructure.database.base import Base


def now():
    return datetime.now(UTC)


def uid():
    return str(uuid4())


class UTCDateTime(TypeDecorator):
    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(DateTime(timezone=dialect.name == "postgresql"))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Timezone-aware UTC datetime required")
        value = value.astimezone(UTC)
        return value if dialect.name == "postgresql" else value.replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


class Job(Base):
    __tablename__ = "ai_jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    workflow_version: Mapped[str] = mapped_column(
        String(40), default="manual-v1", server_default="manual-v1"
    )
    status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    input_json: Mapped[dict] = mapped_column(JSON)
    execution_config: Mapped[dict | None] = mapped_column(JSON)
    execution_identity: Mapped[dict | None] = mapped_column(JSON)
    result_json: Mapped[dict | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(String(200))
    total_estimated_cost_usd: Mapped[float] = mapped_column(Numeric(12, 8), default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=now)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class Step(Base):
    __tablename__ = "ai_steps"
    __table_args__ = (UniqueConstraint("job_id", "step_name"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    job_id: Mapped[str] = mapped_column(ForeignKey("ai_jobs.id"), index=True)
    step_name: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="PENDING", index=True)
    input_hash: Mapped[str | None] = mapped_column(String(64))
    output_json: Mapped[dict | None] = mapped_column(JSON)
    output_hash: Mapped[str | None] = mapped_column(String(64))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    lease_token: Mapped[str | None] = mapped_column(String(36))
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=now)
    next_retry_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class Attempt(Base):
    __tablename__ = "ai_step_attempts"
    __table_args__ = (UniqueConstraint("step_id", "attempt_no"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    step_id: Mapped[str] = mapped_column(ForeignKey("ai_steps.id"), index=True)
    lease_token: Mapped[str] = mapped_column(String(36))
    attempt_no: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="CLAIMED")
    provider_request_id: Mapped[str | None] = mapped_column(String(200))
    provider: Mapped[str | None] = mapped_column(String(40))
    model: Mapped[str | None] = mapped_column(String(256))
    profile_id: Mapped[str | None] = mapped_column(String(128))
    config_fingerprint: Mapped[str | None] = mapped_column(String(64))
    pricing_kind: Mapped[str | None] = mapped_column(String(40))
    adapter_version: Mapped[str | None] = mapped_column(String(80))
    llm_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    collection_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    collection_usage: Mapped[dict | None] = mapped_column(JSON)
    error_type: Mapped[str | None] = mapped_column(String(80))
    tokens_input: Mapped[int | None] = mapped_column(Integer)
    tokens_output: Mapped[int | None] = mapped_column(Integer)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 8))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=now)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class Event(Base):
    __tablename__ = "ai_job_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    job_id: Mapped[str] = mapped_column(ForeignKey("ai_jobs.id"), index=True)
    step_id: Mapped[str | None] = mapped_column(ForeignKey("ai_steps.id"))
    event_type: Mapped[str] = mapped_column(String(80))
    details_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=now, index=True)
