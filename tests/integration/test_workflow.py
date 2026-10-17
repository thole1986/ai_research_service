import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import update

from company_ai.application.research.execute_workflow import run_workflow
from company_ai.domain.research.contracts import InjectedCrash, InvalidOutput, SafeFailure
from company_ai.infrastructure.research.ai.llm_adapter import CrewAIAdapter
from company_ai.infrastructure.research.persistence.models import Step, now
from tests.fakes import CountingAdapter


async def test_full_job_and_redelivery(repo, payload):
    job, _ = await repo.create_job_idempotently("full", payload)
    adapter = CountingAdapter()
    await run_workflow(job.id, repo, adapter, repo.settings)
    completed, steps = await repo.get_job(job.id)
    assert completed.status == "COMPLETED"
    assert completed.result_json["score"]["score"] == 100
    assert all((step.attempt_count == 1 for step in steps))
    await run_workflow(job.id, repo, adapter, repo.settings)
    assert adapter.calls == ["RESEARCH", "CLASSIFY", "REPORT"]


async def test_full_job_with_actual_crewai_and_mock_provider(repo, payload):
    responses = CountingAdapter()

    async def create(**kwargs):
        step = {
            "ResearchOutput": "RESEARCH",
            "ClassificationOutput": "CLASSIFY",
            "ReportOutput": "REPORT",
        }[kwargs["response_format"]["json_schema"]["name"]]
        data = json.loads(kwargs["messages"][1]["content"])
        result = await responses.execute(step, data)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content=json.dumps(result.output), refusal=None),
                )
            ],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50),
        )

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    job, _ = await repo.create_job_idempotently("actual-crew", payload)
    await run_workflow(job.id, repo, CrewAIAdapter(repo.settings, client), repo.settings)
    state, steps = await repo.get_job(job.id)
    assert state.status == "COMPLETED"
    assert all(step.status == "COMPLETED" for step in steps)
    assert responses.calls == ["RESEARCH", "CLASSIFY", "REPORT"]


async def test_icp_text_is_not_automatically_company_evidence(repo, payload):
    class ParaphrasingAdapter(CountingAdapter):
        async def execute(self, step, data):
            result = await super().execute(step, data)
            if step == "CLASSIFY":
                result.output["matching_signals"] = [payload["icp"]]
            return result

    job, _ = await repo.create_job_idempotently("paraphrase", payload)
    await run_workflow(job.id, repo, ParaphrasingAdapter(), repo.settings)
    state, _ = await repo.get_job(job.id)
    assert state.error_code == "UNSUPPORTED_CLASSIFICATION_SIGNAL"


@pytest.mark.parametrize(
    "failure,category",
    [
        ("UNSUPPORTED_CLASSIFICATION_SIGNAL", "UNSUPPORTED_CLASSIFICATION_SIGNAL"),
        ("OUTPUT_TOKEN_LIMIT_REACHED", "OUTPUT_TOKEN_LIMIT_REACHED"),
        ("PRIVATE SOURCE TEXT", "OUTPUT_OR_CONFIGURATION_INVALID"),
    ],
)
async def test_classify_error_is_actionable_and_sanitized(repo, payload, failure, category):
    class FailingClassification(CountingAdapter):
        async def execute(self, step, data):
            if step == "CLASSIFY":
                raise InvalidOutput(failure)
            return await super().execute(step, data)

    job, _ = await repo.create_job_idempotently("classify-failure", payload)
    await run_workflow(job.id, repo, FailingClassification(), repo.settings)
    state, steps = await repo.get_job(job.id)
    assert state.status == "NEEDS_REVIEW"
    assert state.error_code == category
    assert steps[0].status == "COMPLETED"
    assert steps[1].status == "NEEDS_REVIEW"
    assert all(step.status == "PENDING" for step in steps[2:])
    events = await repo.events(job.id, 0, 100)
    failure_event = next(e for e in events if e.event_type == "STEP_NEEDS_REVIEW")
    assert failure_event.details_json["error_category"] == category
    assert "PRIVATE" not in str(failure_event.details_json)


async def test_checkpoint_resume_and_version_change(repo, payload):
    job, _ = await repo.create_job_idempotently("resume", payload)
    adapter = CountingAdapter()
    repo.settings.failure_point = "checkpoint"
    with pytest.raises(InjectedCrash):
        await run_workflow(job.id, repo, adapter, repo.settings)
    assert adapter.calls == ["RESEARCH"]
    repo.settings.failure_point = ""
    await run_workflow(job.id, repo, adapter, repo.settings)
    assert (await repo.get_job(job.id))[0].status == "COMPLETED"
    assert adapter.calls == ["RESEARCH", "CLASSIFY", "REPORT"]
    second, _ = await repo.create_job_idempotently("version", payload)
    repo.settings.failure_point = "checkpoint"
    with pytest.raises(InjectedCrash):
        await run_workflow(second.id, repo, adapter, repo.settings)
    repo.settings.failure_point = ""
    repo.settings.openai_model = "changed-model"
    before = len(adapter.calls)
    await run_workflow(second.id, repo, adapter, repo.settings)
    assert len(adapter.calls) == before
    assert (await repo.get_job(second.id))[0].error_code == "CHECKPOINT_VERSION_MISMATCH"


@pytest.mark.parametrize("point,calls", [("claim", 0), ("request_started", 0), ("response", 1)])
async def test_ambiguous_crash_never_auto_retried(repo, payload, point, calls):
    job, _ = await repo.create_job_idempotently(point, payload)
    adapter = CountingAdapter()
    repo.settings.failure_point = point
    with pytest.raises(InjectedCrash):
        await run_workflow(job.id, repo, adapter, repo.settings)
    assert len(adapter.calls) == calls

    async def db_operation_3(s):
        return await s.execute(
            update(Step)
            .where(Step.job_id == job.id, Step.status == "RUNNING")
            .values(lease_expires_at=now() - timedelta(seconds=1))
        )

    await repo.db.write(db_operation_3)
    await repo.scan_stale_steps()
    repo.settings.failure_point = ""
    await run_workflow(job.id, repo, adapter, repo.settings)
    assert len(adapter.calls) == calls
    assert (await repo.get_job(job.id))[0].status == "NEEDS_REVIEW"
    assert not await repo.list_resumable_jobs()


async def test_timeout_and_budget(repo, payload):

    class TimeoutAdapter:
        async def execute(self, *_):
            raise TimeoutError("DO NOT LEAK PRIVATE DATA")

    job, _ = await repo.create_job_idempotently("timeout", payload)
    await run_workflow(job.id, repo, TimeoutAdapter(), repo.settings)
    state, steps = await repo.get_job(job.id)
    assert state.status == "NEEDS_REVIEW" and steps[0].status == "UNKNOWN"
    assert "PRIVATE" not in state.error_message
    repo.settings.max_estimated_usd_per_job = 1e-07
    job, _ = await repo.create_job_idempotently("budget", payload)
    adapter = CountingAdapter()
    await run_workflow(job.id, repo, adapter, repo.settings)
    assert adapter.calls == []
    assert (await repo.get_job(job.id))[0].error_code == "BUDGET_EXCEEDED"


async def test_concurrent_deliveries(repo, payload):
    job, _ = await repo.create_job_idempotently("race", payload)
    adapter = CountingAdapter()
    await asyncio.gather(*(run_workflow(job.id, repo, adapter, repo.settings) for _ in range(2)))
    assert (await repo.get_job(job.id))[0].status == "COMPLETED"
    assert adapter.calls == ["RESEARCH", "CLASSIFY", "REPORT"]


async def test_429_bounded_backoff(repo, payload):

    class RateLimited:
        async def execute(self, *_):
            raise SafeFailure("RATE_LIMITED")

    job, _ = await repo.create_job_idempotently("429", payload)
    for attempt in range(repo.settings.max_step_attempts):
        await run_workflow(job.id, repo, RateLimited(), repo.settings)
        state, steps = await repo.get_job(job.id)
        assert steps[0].attempt_count == attempt + 1
        if attempt < repo.settings.max_step_attempts - 1:
            assert steps[0].status == "PENDING"
            await run_workflow(job.id, repo, RateLimited(), repo.settings)
            assert (await repo.get_job(job.id))[1][0].attempt_count == attempt + 1

            async def db_operation_4(s, step_id=steps[0].id):
                return await s.execute(
                    update(Step)
                    .where(Step.id == step_id)
                    .values(next_retry_at=now() - timedelta(seconds=1))
                )

            await repo.db.write(db_operation_4)
    assert state.status == "FAILED"
    assert float(state.total_estimated_cost_usd) == 0
