"""Bounded SQLAlchemy read adapter; a request supplies the AsyncSession lifetime."""

from copy import deepcopy

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from company_ai.application.research.admin_queries import (
    AdminAttempt,
    AdminError,
    AdminEvent,
    AdminStep,
    AttemptFilters,
    EventFilters,
    JobDetail,
    JobFilters,
    JobSummary,
    Page,
    Paging,
)
from company_ai.domain.research.contracts import ALL_STEPS
from company_ai.infrastructure.database.transactions import SessionOperations
from company_ai.infrastructure.research.persistence.admin_mapping import (
    execution_metadata,
    safe_collection_usage,
    safe_error_code,
    safe_event_details,
    safe_request_id,
    safe_text,
)
from company_ai.infrastructure.research.persistence.metrics import read_metrics
from company_ai.infrastructure.research.persistence.models import Attempt, Event, Job, Step


class SqlAlchemyAdminReader:
    def __init__(self, session: AsyncSession):
        self.db = SessionOperations(session)

    async def list_jobs(self, filters: JobFilters, page: Paging) -> Page[JobSummary]:
        name = Job.input_json["company_name"].as_string()
        predicates = []
        if filters.status is not None:
            predicates.append(Job.status == filters.status)
        if filters.company_name is not None:
            literal = filters.company_name.lower()
            escaped = literal.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            lowered = func.unicode_lower(name) if self.db.backend == "sqlite" else func.lower(name)
            predicates.append(lowered.like(f"%{escaped}%", escape="\\"))
        if filters.created_from is not None:
            predicates.append(Job.created_at >= filters.created_from)
        if filters.created_to is not None:
            predicates.append(Job.created_at <= filters.created_to)
        async with self.db.session() as session:
            total = await session.scalar(select(func.count()).select_from(Job).where(*predicates))
            rows = (
                await session.execute(
                    select(
                        Job.id.label("job_id"),
                        name.label("company_name"),
                        Job.input_json["website"].as_string().label("website"),
                        Job.status,
                        Job.created_at,
                        Job.updated_at,
                        Job.completed_at,
                        Job.total_estimated_cost_usd.label("estimated_cost_usd"),
                        Job.error_code,
                    )
                    .where(*predicates)
                    .order_by(Job.created_at.desc(), Job.id.desc())
                    .offset(page.offset)
                    .limit(page.limit)
                )
            ).mappings()
            items = tuple(
                JobSummary(
                    **{
                        **row,
                        "estimated_cost_usd": float(row["estimated_cost_usd"]),
                        "error_code": safe_error_code(row["error_code"]),
                    }
                )
                for row in rows
            )
            return Page(items, total, page.offset, page.limit)

    async def get_job_detail(self, job_id: str) -> JobDetail | None:
        async with self.db.session() as session:
            row = (
                (
                    await session.execute(
                        select(
                            Job.id.label("job_id"),
                            Job.status,
                            Job.workflow_version,
                            Job.input_json,
                            Job.result_json,
                            Job.execution_config,
                            Job.created_at,
                            Job.updated_at,
                            Job.completed_at,
                            Job.total_estimated_cost_usd,
                            Job.error_code,
                        ).where(Job.id == job_id)
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            metadata = execution_metadata(row["execution_config"])
            step_rows = (
                await session.execute(
                    select(
                        Step.step_name,
                        Step.status,
                        Step.attempt_count,
                        Step.started_at,
                        Step.completed_at,
                        Step.updated_at,
                        Step.next_retry_at,
                        Step.lease_expires_at,
                        Step.last_heartbeat_at,
                        Step.output_json,
                    )
                    .where(Step.job_id == job_id)
                    .order_by(
                        case(
                            {name: index for index, name in enumerate(ALL_STEPS)},
                            value=Step.step_name,
                            else_=len(ALL_STEPS),
                        ),
                        Step.id,
                    )
                )
            ).mappings()
            steps = tuple(
                AdminStep(
                    **{key: value for key, value in step.items() if key != "output_json"},
                    output=deepcopy(step["output_json"]),
                    execution_metadata=(metadata or {}).get(step["step_name"]),
                )
                for step in step_rows
            )
            code = safe_error_code(row["error_code"])
            result = deepcopy(row["result_json"])
            if result is None and any(step.step_name == "COLLECT" for step in steps):
                result = {
                    step.step_name.lower(): deepcopy(step.output)
                    for step in steps
                    if step.status == "COMPLETED"
                } or None
            return JobDetail(
                job_id=row["job_id"],
                status=row["status"],
                workflow_version=row["workflow_version"],
                input=deepcopy(row["input_json"]),
                result=result,
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                completed_at=row["completed_at"],
                estimated_cost_usd=float(row["total_estimated_cost_usd"]),
                error=AdminError(
                    code, "Research job stopped; inspect sanitized events and attempts."
                )
                if code
                else None,
                steps=steps,
                execution_metadata=metadata,
            )

    async def list_job_events(
        self, job_id: str, filters: EventFilters, page: Paging
    ) -> Page[AdminEvent] | None:
        predicates = [Event.job_id == job_id]
        if filters.event_type is not None:
            predicates.append(Event.event_type == filters.event_type)
        async with self.db.session() as session:
            if await session.scalar(select(Job.id).where(Job.id == job_id)) is None:
                return None
            total = await session.scalar(select(func.count()).select_from(Event).where(*predicates))
            rows = (
                await session.execute(
                    select(
                        Event.id,
                        Event.step_id,
                        Event.event_type,
                        Event.details_json,
                        Event.created_at,
                    )
                    .where(*predicates)
                    .order_by(Event.created_at, Event.id)
                    .offset(page.offset)
                    .limit(page.limit)
                )
            ).mappings()
            items = tuple(
                AdminEvent(
                    id=row["id"],
                    step_id=row["step_id"],
                    event_type=row["event_type"],
                    details=safe_event_details(row["event_type"], row["details_json"]),
                    created_at=row["created_at"],
                )
                for row in rows
            )
            return Page(items, total, page.offset, page.limit)

    async def list_job_attempts(
        self, job_id: str, filters: AttemptFilters, page: Paging
    ) -> Page[AdminAttempt] | None:
        predicates = [Step.job_id == job_id]
        if filters.step_name is not None:
            predicates.append(Step.step_name == filters.step_name)
        if filters.status is not None:
            predicates.append(Attempt.status == filters.status)
        async with self.db.session() as session:
            if await session.scalar(select(Job.id).where(Job.id == job_id)) is None:
                return None
            total = await session.scalar(
                select(func.count()).select_from(Attempt).join(Step).where(*predicates)
            )
            rows = (
                await session.execute(
                    select(
                        Attempt.id.label("attempt_id"),
                        Attempt.step_id,
                        Step.step_name,
                        Attempt.attempt_no,
                        Attempt.status,
                        Attempt.error_type,
                        Attempt.created_at,
                        Attempt.llm_started_at,
                        Attempt.finished_at,
                        Attempt.tokens_input,
                        Attempt.tokens_output,
                        Attempt.estimated_cost_usd,
                        Attempt.provider_request_id,
                        Attempt.provider,
                        Attempt.model,
                        Attempt.pricing_kind,
                        Attempt.collection_started_at,
                        Attempt.collection_usage,
                    )
                    .join(Step)
                    .where(*predicates)
                    .order_by(Attempt.created_at, Attempt.id)
                    .offset(page.offset)
                    .limit(page.limit)
                )
            ).mappings()
            items = tuple(
                AdminAttempt(
                    **{
                        **row,
                        "estimated_cost_usd": (
                            float(row["estimated_cost_usd"])
                            if row["estimated_cost_usd"] is not None
                            else None
                        ),
                        "error_type": safe_error_code(row["error_type"]),
                        "provider_request_id": safe_request_id(row["provider_request_id"]),
                        "provider": safe_text(row["provider"], 40),
                        "model": safe_text(row["model"], 256),
                        "pricing_kind": safe_text(row["pricing_kind"], 40),
                        "collection_usage": safe_collection_usage(row["collection_usage"]),
                    },
                    execution_type=(
                        "collection"
                        if row["step_name"] == "COLLECT"
                        else "deterministic"
                        if row["step_name"] == "SCORE"
                        else "llm"
                    ),
                )
                for row in rows
            )
            return Page(items, total, page.offset, page.limit)

    async def get_metrics(self):
        async with self.db.session() as session:
            return await read_metrics(session)
