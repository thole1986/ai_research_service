"""One SQL aggregation implementation shared by worker/research and admin reads."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from company_ai.application.research.admin_queries import AdminMetrics
from company_ai.infrastructure.research.persistence.admin_mapping import safe_collection_usage
from company_ai.infrastructure.research.persistence.models import Attempt, Event, Job, Step


async def read_metrics(s: AsyncSession) -> AdminMetrics:
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
    for raw_record in usage_records:
        record = safe_collection_usage(raw_record)
        if record is None:
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
                await s.execute(select(Event.event_type, func.count()).group_by(Event.event_type))
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
