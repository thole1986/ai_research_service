from datetime import timedelta

from sqlalchemy import and_, case, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from company_ai.domain.research.contracts import (
    ALL_STEPS,
    COLLECTION_WORKFLOW_VERSION,
    LEGACY_WORKFLOW_VERSION,
    Claim,
    step_order,
    steps_for_mode,
)
from company_ai.domain.research.entities import JobEvent, ResearchJob, ResearchStep
from company_ai.domain.research.errors import Conflict
from company_ai.domain.research.hashing import digest
from company_ai.domain.research.llm import resolve_execution_config
from company_ai.domain.research.policies import (
    budget_allows,
    expired_step_outcome,
    failure_job_status,
)
from company_ai.infrastructure.database.session import Database
from company_ai.infrastructure.database.transactions import SessionOperations
from company_ai.infrastructure.research.persistence.mapping import snapshot
from company_ai.infrastructure.research.persistence.models import (
    Attempt,
    Event,
    Job,
    Step,
    now,
    uid,
)
from company_ai.settings import Settings


def event(session, job_id, kind, step_id=None, **details):
    session.add(Event(job_id=job_id, step_id=step_id, event_type=kind, details_json=details))


def canonical_payload(payload):
    """Compare old stored requests with current API defaults without rewriting inputs."""
    return {
        "website": None,
        "website_text": "",
        "company_description": "",
        "collection_mode": "manual",
        **payload,
    }


EXECUTION_AUDIT_FIELDS = (
    "provider",
    "model",
    "profile_id",
    "config_fingerprint",
    "pricing_kind",
    "adapter_version",
)


def execution_audit(metadata):
    """Persist only execution identity, never endpoint details or provider content."""
    return {name: metadata[name] for name in EXECUTION_AUDIT_FIELDS if name in (metadata or {})}


def attempt_audit(attempt):
    return {
        name: getattr(attempt, name)
        for name in EXECUTION_AUDIT_FIELDS
        if getattr(attempt, name) is not None
    }


class SqlAlchemyResearchRepository:
    """Research aggregate persistence; HTTP and workers inject their session scope."""

    def __init__(self, db: Database | SessionOperations, settings: Settings):
        self.db, self.settings = (db, settings)

    async def _lock_job(self, s, job_id):
        await s.execute(update(Job).where(Job.id == job_id).values(updated_at=Job.updated_at))

    def _max_attempts(self, step_name):
        if step_name == "COLLECT":
            return min(self.settings.collect_max_attempts, self.settings.max_step_attempts)
        return self.settings.max_step_attempts

    def _attempt_ceiling(self):
        return case(
            (Step.step_name == "COLLECT", self._max_attempts("COLLECT")),
            else_=self.settings.max_step_attempts,
        )

    async def create_job_idempotently(self, key, payload):
        request_hash = digest(payload)
        mode = payload.get("collection_mode", "manual")

        async def create(s):
            existing = await s.scalar(select(Job).where(Job.idempotency_key == key))
            if existing is not None:
                return existing_job(existing)
            job = Job(
                id=uid(),
                idempotency_key=key,
                request_hash=request_hash,
                input_json=payload,
                execution_config=resolve_execution_config(self.settings).model_dump(mode="json"),
                workflow_version=(
                    COLLECTION_WORKFLOW_VERSION if mode == "tavily" else LEGACY_WORKFLOW_VERSION
                ),
            )
            s.add(job)
            await s.flush()
            s.add_all((Step(job_id=job.id, step_name=name) for name in steps_for_mode(mode)))
            event(s, job.id, "JOB_CREATED")
            return (snapshot(ResearchJob, job), True)

        def existing_job(job):
            if job.request_hash != request_hash and canonical_payload(
                job.input_json
            ) != canonical_payload(payload):
                raise Conflict("IDEMPOTENCY_KEY_CONFLICT")
            return (snapshot(ResearchJob, job), False)

        try:
            return await self.db.write(create)
        except IntegrityError:
            async with self.db.session() as s:
                job = await s.scalar(select(Job).where(Job.idempotency_key == key))
                if job is None:
                    raise
                return existing_job(job)

    async def get_job(self, job_id):
        async with self.db.session() as s:
            job = await s.get(Job, job_id)
            steps = list(await s.scalars(select(Step).where(Step.job_id == job_id)))
            return (
                snapshot(ResearchJob, job),
                [
                    snapshot(ResearchStep, step)
                    for step in sorted(steps, key=lambda x: step_order(x.step_name))
                ],
            )

    async def events(self, job_id, offset=0, limit=100):
        async with self.db.session() as s:
            return [
                snapshot(JobEvent, row)
                for row in list(
                    await s.scalars(
                        select(Event)
                        .where(Event.job_id == job_id)
                        .order_by(Event.created_at, Event.id)
                        .offset(offset)
                        .limit(limit)
                    )
                )
            ]

    async def claim_step(self, step_id, input_hash):

        async def claim(s):
            stamp, token, attempt_id = (now(), uid(), uid())
            target = await s.get(Step, step_id)
            if target is None:
                return None
            await self._lock_job(s, target.job_id)
            predecessors = (
                select(Step.id)
                .where(
                    Step.job_id == target.job_id,
                    Step.step_name.in_(ALL_STEPS[: step_order(target.step_name)]),
                    Step.status != "COMPLETED",
                )
                .correlate(None)
            )
            row = (
                await s.execute(
                    update(Step)
                    .where(
                        Step.id == step_id,
                        Step.status == "PENDING",
                        Step.attempt_count < self._max_attempts(target.step_name),
                        or_(Step.next_retry_at.is_(None), Step.next_retry_at <= stamp),
                        Step.job_id.in_(
                            select(Job.id).where(Job.status.in_(["PENDING", "RUNNING"]))
                        ),
                        ~predecessors.exists(),
                    )
                    .values(
                        status="RUNNING",
                        lease_token=token,
                        input_hash=input_hash,
                        lease_expires_at=stamp + timedelta(seconds=self.settings.lease_ttl_seconds),
                        last_heartbeat_at=stamp,
                        started_at=stamp,
                        updated_at=stamp,
                        next_retry_at=None,
                        attempt_count=Step.attempt_count + 1,
                    )
                    .returning(Step.id, Step.job_id, Step.step_name, Step.attempt_count)
                )
            ).first()
            if not row:
                return None
            s.add(
                Attempt(
                    id=attempt_id, step_id=row.id, lease_token=token, attempt_no=row.attempt_count
                )
            )
            await s.execute(
                update(Job)
                .where(Job.id == row.job_id)
                .values(status="RUNNING", updated_at=stamp, error_code=None, error_message=None)
            )
            event(
                s,
                row.job_id,
                "STEP_CLAIMED",
                row.id,
                attempt_id=attempt_id,
                step_name=row.step_name,
            )
            return Claim(row.id, row.job_id, row.step_name, token, attempt_id, input_hash)

        return await self.db.write(claim)

    @staticmethod
    def fence(claim):
        return and_(
            Step.id == claim.step_id,
            Step.job_id == claim.job_id,
            Step.step_name == claim.step_name,
            Step.status == "RUNNING",
            Step.lease_token == claim.token,
            Step.input_hash == claim.input_hash,
            Step.lease_expires_at > now(),
        )

    async def heartbeat(self, claim):

        async def operation_0(s):
            await self._lock_job(s, claim.job_id)
            return (
                await s.execute(
                    update(Step)
                    .where(self.fence(claim))
                    .values(
                        last_heartbeat_at=now(),
                        lease_expires_at=now() + timedelta(seconds=self.settings.lease_ttl_seconds),
                    )
                )
            ).rowcount == 1

        return await self.db.write(operation_0)

    async def request_started(self, claim, reserved_cost, execution_metadata=None):
        if claim.step_name not in ("RESEARCH", "CLASSIFY", "REPORT"):
            return False
        audit = execution_audit(execution_metadata)

        async def start(s):
            await self._lock_job(s, claim.job_id)
            if (
                await s.execute(update(Step).where(self.fence(claim)).values(updated_at=now()))
            ).rowcount != 1:
                return False
            attempt = await s.get(Attempt, claim.attempt_id)
            if (
                attempt is None
                or attempt.step_id != claim.step_id
                or attempt.lease_token != claim.token
                or attempt.status != "CLAIMED"
            ):
                return False
            job = await s.get(Job, claim.job_id)
            calls = await s.scalar(
                select(func.count(Attempt.id))
                .join(Step)
                .where(Step.job_id == claim.job_id, Attempt.llm_started_at.is_not(None))
            )
            if not budget_allows(
                calls,
                float(job.total_estimated_cost_usd),
                reserved_cost,
                self.settings.max_llm_calls_per_job,
                self.settings.max_estimated_usd_per_job,
            ):
                await self._failure(s, claim, "NEEDS_REVIEW", "BUDGET_EXCEEDED")
                return False
            await s.execute(
                update(Attempt)
                .where(Attempt.id == claim.attempt_id)
                .values(
                    status="REQUEST_STARTED",
                    llm_started_at=now(),
                    estimated_cost_usd=reserved_cost,
                    **audit,
                )
            )
            job.total_estimated_cost_usd = float(job.total_estimated_cost_usd) + reserved_cost
            event(
                s,
                claim.job_id,
                "LLM_REQUEST_STARTED",
                claim.step_id,
                attempt_id=claim.attempt_id,
                **audit,
            )
            return True

        return await self.db.write(start)

    async def pin_model_identity(self, claim, profile_key, digest):
        """Record the first metadata check under the same lock and lease as dispatch."""
        if claim.step_name not in ("RESEARCH", "CLASSIFY", "REPORT"):
            return False

        async def pin(s):
            await self._lock_job(s, claim.job_id)
            if (
                await s.execute(update(Step).where(self.fence(claim)).values(updated_at=now()))
            ).rowcount != 1:
                return False
            attempt = await s.get(Attempt, claim.attempt_id)
            if (
                attempt is None
                or attempt.step_id != claim.step_id
                or attempt.lease_token != claim.token
                or attempt.status != "CLAIMED"
            ):
                return False
            job = await s.get(Job, claim.job_id)
            identities = dict(job.execution_identity or {})
            identity = {"digest": digest, "verified": digest is not None}
            if profile_key in identities:
                return identities[profile_key] == identity
            identities[profile_key] = identity
            job.execution_identity = identities
            event(
                s,
                claim.job_id,
                "MODEL_IDENTITY_PINNED",
                claim.step_id,
                attempt_id=claim.attempt_id,
                identity_verified=digest is not None,
            )
            return True

        return await self.db.write(pin)

    async def record_llm_usage(self, claim, result, cost):
        """Retain known usage for a response rejected after generation, under fencing."""
        if claim.step_name not in ("RESEARCH", "CLASSIFY", "REPORT"):
            return False

        async def record(s):
            await self._lock_job(s, claim.job_id)
            if (
                await s.execute(update(Step).where(self.fence(claim)).values(updated_at=now()))
            ).rowcount != 1:
                return False
            attempt = await s.get(Attempt, claim.attempt_id)
            if (
                attempt is None
                or attempt.step_id != claim.step_id
                or attempt.lease_token != claim.token
                or attempt.status != "REQUEST_STARTED"
            ):
                return False
            attempt.tokens_input, attempt.tokens_output = result.tokens_input, result.tokens_output
            attempt.provider_request_id = result.provider_request_id
            if cost is not None:
                job = await s.get(Job, claim.job_id)
                job.total_estimated_cost_usd = max(
                    0,
                    float(job.total_estimated_cost_usd)
                    - float(attempt.estimated_cost_usd or 0)
                    + cost,
                )
                attempt.estimated_cost_usd = cost
            event(
                s,
                claim.job_id,
                "LLM_USAGE_RECORDED",
                claim.step_id,
                attempt_id=claim.attempt_id,
                usage_known=result.tokens_input is not None and result.tokens_output is not None,
                cost_reconciled=cost is not None,
                **attempt_audit(attempt),
            )
            return True

        return await self.db.write(record)

    async def collection_request_started(self, claim):
        if claim.step_name != "COLLECT":
            return False

        async def start(s):
            await self._lock_job(s, claim.job_id)
            if (
                await s.execute(update(Step).where(self.fence(claim)).values(updated_at=now()))
            ).rowcount != 1:
                return False
            attempt = await s.get(Attempt, claim.attempt_id)
            if (
                attempt is None
                or attempt.step_id != claim.step_id
                or attempt.lease_token != claim.token
                or attempt.status != "CLAIMED"
            ):
                return False
            calls = await s.scalar(
                select(func.count(Attempt.id))
                .join(Step)
                .where(Step.job_id == claim.job_id, Attempt.collection_started_at.is_not(None))
            )
            if calls >= self._max_attempts("COLLECT"):
                await self._failure(s, claim, "NEEDS_REVIEW", "COLLECT_ATTEMPT_LIMIT_EXCEEDED")
                return False
            attempt.status, attempt.collection_started_at = "REQUEST_STARTED", now()
            event(
                s,
                claim.job_id,
                "COLLECTION_REQUEST_STARTED",
                claim.step_id,
                attempt_id=claim.attempt_id,
            )
            return True

        return await self.db.write(start)

    async def complete_step_with_fencing(self, claim, output, result=None, cost=None):

        async def complete(s):
            await self._lock_job(s, claim.job_id)
            stamp = now()
            changed = (
                await s.execute(
                    update(Step)
                    .where(self.fence(claim))
                    .values(
                        status="COMPLETED",
                        output_json=output,
                        output_hash=digest(output),
                        completed_at=stamp,
                        updated_at=stamp,
                        lease_token=None,
                        lease_expires_at=None,
                    )
                )
            ).rowcount
            if changed != 1:
                return False
            attempt = await s.get(Attempt, claim.attempt_id)
            if result:
                if claim.step_name == "COLLECT":
                    usage = result.output.get("usage")
                    credits = result.output.get("credits")
                    attempt.collection_usage = (
                        {"usage": usage, "credits": credits}
                        if usage is not None or credits is not None
                        else None
                    )
                else:
                    attempt.tokens_input, attempt.tokens_output = (
                        result.tokens_input,
                        result.tokens_output,
                    )
                attempt.provider_request_id = result.provider_request_id
            job = await s.get(Job, claim.job_id)
            if cost is not None and claim.step_name != "COLLECT":
                job.total_estimated_cost_usd = max(
                    0,
                    float(job.total_estimated_cost_usd)
                    - float(attempt.estimated_cost_usd or 0)
                    + cost,
                )
                attempt.estimated_cost_usd = cost
            attempt.status, attempt.finished_at = ("SUCCEEDED", stamp)
            event(
                s,
                claim.job_id,
                "STEP_CHECKPOINTED",
                claim.step_id,
                attempt_id=claim.attempt_id,
                step_name=claim.step_name,
                **attempt_audit(attempt),
            )
            steps = list(await s.scalars(select(Step).where(Step.job_id == claim.job_id)))
            if all((step.status == "COMPLETED" for step in steps)):
                job.status, job.completed_at = ("COMPLETED", stamp)
                job.result_json = {step.step_name.lower(): step.output_json for step in steps}
                event(s, job.id, "JOB_COMPLETED")
            job.updated_at = stamp
            return True

        return await self.db.write(complete)

    async def _failure(self, s, claim, status, category):
        await self._lock_job(s, claim.job_id)
        changed = (
            await s.execute(
                update(Step)
                .where(self.fence(claim))
                .values(status=status, lease_token=None, lease_expires_at=None, updated_at=now())
            )
        ).rowcount
        if not changed:
            return False
        attempt = await s.get(Attempt, claim.attempt_id)
        attempt.status = "UNKNOWN" if status == "UNKNOWN" else "FAILED"
        attempt.error_type, attempt.finished_at = (category, now())
        job = await s.get(Job, claim.job_id)
        job.status = failure_job_status(status)
        job.error_code, job.error_message = (category, "Step stopped; inspect audit events.")
        job.updated_at = now()
        event(
            s,
            job.id,
            "STEP_" + status,
            claim.step_id,
            attempt_id=claim.attempt_id,
            error_category=category,
            **attempt_audit(attempt),
        )
        return True

    async def fail(self, claim, status, category):

        async def operation_0(s):
            return await self._failure(s, claim, status, category)

        return await self.db.write(operation_0)

    async def safe_retry(self, claim):

        async def retry(s):
            if not await self._failure(s, claim, "FAILED", "PROVIDER_REJECTED"):
                return False
            attempt = await s.get(Attempt, claim.attempt_id)
            job = await s.get(Job, claim.job_id)
            if claim.step_name != "COLLECT":
                job.total_estimated_cost_usd = max(
                    0, float(job.total_estimated_cost_usd) - float(attempt.estimated_cost_usd or 0)
                )
                attempt.estimated_cost_usd = 0
            if attempt.attempt_no < self._max_attempts(claim.step_name):
                step = await s.get(Step, claim.step_id)
                step.status = "PENDING"
                step.next_retry_at = now() + timedelta(seconds=min(300, 2**attempt.attempt_no))
                job.status = "PENDING"
                event(s, job.id, "SAFE_RETRY_SCHEDULED", step.id)
            return True

        return await self.db.write(retry)

    async def review_job(self, job_id, category):

        async def review(s):
            changed = (
                await s.execute(
                    update(Job)
                    .where(Job.id == job_id, Job.status.in_(["PENDING", "RUNNING"]))
                    .values(
                        status="NEEDS_REVIEW",
                        error_code=category,
                        error_message="Create a new job for changed pipeline configuration.",
                        updated_at=now(),
                    )
                )
            ).rowcount
            if changed:
                event(s, job_id, "JOB_NEEDS_REVIEW", error_category=category)

        await self.db.write(review)

    async def scan_stale_steps(self, limit=100):

        async def scan(s):
            stamp = now()
            ids = list(
                await s.scalars(
                    select(Step.id)
                    .where(Step.status == "RUNNING", Step.lease_expires_at <= stamp)
                    .order_by(Step.job_id, Step.id)
                    .limit(limit)
                )
            )
            recovered = []
            for step_id in ids:
                target = await s.get(Step, step_id)
                await self._lock_job(s, target.job_id)
                row = (
                    await s.execute(
                        update(Step)
                        .where(
                            Step.id == step_id,
                            Step.status == "RUNNING",
                            Step.lease_expires_at <= stamp,
                        )
                        .values(status="UNKNOWN", lease_expires_at=None, updated_at=stamp)
                        .returning(
                            Step.job_id, Step.step_name, Step.lease_token, Step.attempt_count
                        )
                    )
                ).first()
                if not row:
                    continue
                deterministic = row.step_name == "SCORE"
                status, job_status, attempt_status = expired_step_outcome(
                    row.step_name, row.attempt_count, self._max_attempts(row.step_name)
                )
                await s.execute(
                    update(Step).where(Step.id == step_id).values(status=status, lease_token=None)
                )
                await s.execute(
                    update(Attempt)
                    .where(Attempt.step_id == step_id, Attempt.lease_token == row.lease_token)
                    .values(
                        status=attempt_status,
                        error_type="LEASE_EXPIRED",
                        finished_at=stamp,
                    )
                )
                await s.execute(
                    update(Job)
                    .where(Job.id == row.job_id)
                    .values(status=job_status, error_code="LEASE_EXPIRED", updated_at=stamp)
                )
                event(s, row.job_id, "LEASE_EXPIRED", step_id)
                event(s, row.job_id, "STEP_RECOVERED" if deterministic else "STEP_UNKNOWN", step_id)
                recovered.append(row.job_id)
            return recovered

        return await self.db.write(scan)

    async def list_resumable_jobs(self, limit=100):
        async with self.db.session() as s:
            blocked = select(Step.job_id).where(
                or_(
                    Step.status.in_(["RUNNING", "UNKNOWN", "NEEDS_REVIEW", "FAILED"]),
                    and_(
                        Step.status == "PENDING",
                        or_(
                            Step.next_retry_at > now(),
                            Step.attempt_count >= self._attempt_ceiling(),
                        ),
                    ),
                )
            )
            eligible = select(Step.job_id).where(
                Step.status == "PENDING",
                Step.attempt_count < self._attempt_ceiling(),
                or_(Step.next_retry_at.is_(None), Step.next_retry_at <= now()),
            )
            return list(
                await s.scalars(
                    select(Job.id)
                    .where(
                        Job.status.in_(["PENDING", "RUNNING"]),
                        Job.id.in_(eligible),
                        Job.id.not_in(blocked),
                    )
                    .order_by(Job.updated_at)
                    .limit(limit)
                )
            )

    async def retry_unknown(self, job_id, step_name):

        async def retry(s):
            await s.execute(update(Job).where(Job.id == job_id).values(updated_at=Job.updated_at))
            steps = list(await s.scalars(select(Step).where(Step.job_id == job_id)))
            candidate_id = ResearchJob.retry_candidate(
                [snapshot(ResearchStep, step) for step in steps],
                step_name,
                self._max_attempts(step_name),
            )
            unfinished = next((step for step in steps if step.id == candidate_id))
            unfinished.status, unfinished.next_retry_at = ("PENDING", None)
            job = await s.get(Job, job_id)
            job.status, job.error_code, job.error_message = ("PENDING", None, None)
            job.updated_at = now()
            event(
                s,
                job_id,
                "MANUAL_RETRY_ACKNOWLEDGED",
                unfinished.id,
                acknowledge_possible_duplicate_charge=True,
            )

        await self.db.write(retry)

    async def cleanup_events(self):

        async def operation_0(s):
            return (
                await s.execute(
                    delete(Event).where(
                        Event.created_at
                        < now() - timedelta(days=self.settings.event_retention_days),
                        Event.job_id.in_(select(Job.id).where(Job.status == "COMPLETED")),
                    )
                )
            ).rowcount

        return await self.db.write(operation_0)

    async def metrics(self):
        async with self.db.session() as s:
            usage_records = list(
                await s.scalars(
                    select(Attempt.collection_usage).where(
                        Attempt.collection_usage.is_not(None),
                        Attempt.collection_started_at.is_not(None),
                    )
                )
            )
            collection_usage = {}
            collection_credits = None
            for record in usage_records:
                if not isinstance(record, dict):
                    continue
                for name, amount in (record.get("usage") or {}).items():
                    collection_usage[name] = collection_usage.get(name, 0) + amount
                if record.get("credits") is not None:
                    collection_credits = (collection_credits or 0) + record["credits"]
            llm_usage = (
                await s.execute(
                    select(
                        Attempt.provider,
                        Attempt.model,
                        Attempt.pricing_kind,
                        func.count(Attempt.id).label("calls"),
                        func.sum(Attempt.tokens_input).label("tokens_input"),
                        func.sum(Attempt.tokens_output).label("tokens_output"),
                        func.sum(Attempt.estimated_cost_usd).label("estimated_cost_usd"),
                    )
                    .where(Attempt.llm_started_at.is_not(None))
                    .group_by(Attempt.provider, Attempt.model, Attempt.pricing_kind)
                    .order_by(Attempt.provider, Attempt.model, Attempt.pricing_kind)
                )
            ).mappings()
            return {
                "jobs": dict(
                    (await s.execute(select(Job.status, func.count()).group_by(Job.status))).all()
                ),
                "steps": dict(
                    (await s.execute(select(Step.status, func.count()).group_by(Step.status))).all()
                ),
                "events": dict(
                    (
                        await s.execute(
                            select(Event.event_type, func.count()).group_by(Event.event_type)
                        )
                    ).all()
                ),
                "llm_calls": await s.scalar(
                    select(func.count(Attempt.id)).where(Attempt.llm_started_at.is_not(None))
                ),
                "llm_usage": [
                    {
                        "provider": record["provider"],
                        "model": record["model"],
                        "pricing_kind": record["pricing_kind"],
                        "calls": record["calls"],
                        "tokens_input": record["tokens_input"],
                        "tokens_output": record["tokens_output"],
                        "estimated_cost_usd": float(record["estimated_cost_usd"] or 0),
                    }
                    for record in llm_usage
                ],
                "collection_calls": await s.scalar(
                    select(func.count(Attempt.id)).where(Attempt.collection_started_at.is_not(None))
                ),
                "collection_usage": collection_usage,
                "collection_credits": collection_credits,
                "tokens_input": await s.scalar(select(func.sum(Attempt.tokens_input))) or 0,
                "tokens_output": await s.scalar(select(func.sum(Attempt.tokens_output))) or 0,
                "estimated_cost_usd": float(
                    await s.scalar(select(func.sum(Job.total_estimated_cost_usd))) or 0
                ),
            }
