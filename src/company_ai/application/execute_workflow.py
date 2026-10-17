import time

from pydantic import ValidationError

from company_ai.application.ports import ResearchRepository, WorkflowObserver
from company_ai.application.prompts import (
    INSTRUCTIONS,
    PROMPT_VERSION,
    SCHEMA_VERSION,
    SYSTEM,
    provider_schema,
    reserve_cost,
)
from company_ai.application.step_executor import inject_failure, keep_lease
from company_ai.domain.contracts import (
    AIResult,
    InvalidOutput,
    LLMAdapter,
    SafeFailure,
    output_error_category,
)
from company_ai.domain.hashing import digest
from company_ai.domain.outputs import SCHEMAS
from company_ai.domain.policies import validate_evidence
from company_ai.domain.scoring import score
from company_ai.observability import NoOpObserver, log_event


def step_payload(name, job, outputs):
    if name == "RESEARCH":
        return {
            key: job.input_json[key]
            for key in ("company_name", "company_description", "website_text")
        }
    if name == "CLASSIFY":
        return {"research": outputs["RESEARCH"], "icp": job.input_json["icp"]}
    if name == "SCORE":
        return {"research": outputs["RESEARCH"], "classification": outputs["CLASSIFY"]}
    return {"company": job.input_json, "checkpoints": outputs.copy()}


def input_hash(name, payload, settings):
    identity = {
        "step": name,
        "payload": payload,
        "schema_version": SCHEMA_VERSION,
        "schema": provider_schema(name, payload),
    }
    if name != "SCORE":
        identity.update(
            model=settings.openai_model,
            temperature=settings.llm_temperature,
            prompt_version=PROMPT_VERSION,
            prompt=SYSTEM + INSTRUCTIONS[name],
            max_output_tokens=settings.max_output_tokens_per_step,
            max_agent_iterations=settings.max_agent_iterations,
            timeout=settings.llm_request_timeout_seconds,
            adapter_version="single-call-v1",
        )
    else:
        identity["rules_version"] = "b2b-saas-sme-v1"
    return digest(identity)


async def run_workflow(
    job_id: str,
    repo: ResearchRepository,
    adapter: LLMAdapter,
    settings,
    observer: WorkflowObserver | None = None,
):
    observer = observer or NoOpObserver()
    with observer.span(
        "company-research",
        metadata={"job_id": job_id},
        session_id=job_id,
    ) as workflow:
        await _run_workflow(job_id, repo, adapter, settings, observer, workflow)


def _record_outcome(workflow, job_id, status, outputs, *, error_code=None, level="DEFAULT"):
    """Expose execution state without re-querying the DB or changing workflow decisions."""
    workflow.update(
        status_message=error_code or status,
        level=level,
        output={
            "job_id": job_id,
            "status": status,
            "result": {name.lower(): output for name, output in outputs.items()},
            "error": {"code": error_code} if error_code else None,
        },
    )


async def _run_workflow(job_id, repo, adapter, settings, observer, workflow):
    job, steps = await repo.get_job(job_id)
    if not job:
        _record_outcome(workflow, job_id, "JOB_NOT_FOUND", {}, level="WARNING")
        return
    workflow.update(input=job.input_json)
    if not job.can_execute:
        workflow.update(
            output={
                "job_id": job_id,
                "status": job.status,
                "result": job.result_json,
                "error": {"code": job.error_code} if job.error_code else None,
            },
            status_message="JOB_NOT_EXECUTABLE",
        )
        return
    outputs = {}
    for step in steps:
        name = step.step_name
        payload = step_payload(name, job, outputs)
        hashed = input_hash(name, payload, settings)
        metadata = {
            "job_id": job_id,
            "step_name": name,
            "input_hash": hashed,
            "schema_version": SCHEMA_VERSION,
            "checkpoint_hit": step.status == "COMPLETED",
            "execution_type": "rule_based" if name == "SCORE" else "llm",
        }
        metadata.update(
            {"rules_version": "b2b-saas-sme-v1"}
            if name == "SCORE"
            else ({"prompt_version": PROMPT_VERSION, "model": settings.openai_model})
        )
        if step.status == "COMPLETED":
            if step.input_hash != hashed or step.output_hash != digest(step.output_json):
                await repo.review_job(job_id, "CHECKPOINT_VERSION_MISMATCH")
                _record_outcome(
                    workflow,
                    job_id,
                    "NEEDS_REVIEW",
                    outputs,
                    error_code="CHECKPOINT_VERSION_MISMATCH",
                    level="ERROR",
                )
                return
            outputs[name] = step.output_json
            with observer.span(name, metadata=metadata, input=payload) as cached:
                cached.update(output=step.output_json, status_message="CHECKPOINT_HIT")
            log_event("CHECKPOINT_HIT", job_id=job_id, step_name=name)
            continue
        if step.status in ("UNKNOWN", "NEEDS_REVIEW"):
            await repo.review_job(job_id, "UNKNOWN_STEP")
            _record_outcome(
                workflow, job_id, "NEEDS_REVIEW", outputs, error_code="UNKNOWN_STEP", level="ERROR"
            )
            return
        claim = await repo.claim_step(step.id, hashed)
        if not claim:
            _record_outcome(workflow, job_id, "STEP_NOT_CLAIMED", outputs)
            return
        fields = {"job_id": job_id, "step_name": name, "attempt_id": claim.attempt_id}
        metadata["attempt_id"] = claim.attempt_id
        log_event("STEP_CLAIMED", **fields)
        started = time.perf_counter()
        inject_failure(settings, name, "claim")
        try:
            with observer.span(name, metadata=metadata, input=payload) as observation:
                async with keep_lease(repo, claim, settings):
                    if name == "SCORE":
                        result = AIResult(score(outputs["RESEARCH"], outputs["CLASSIFY"]))
                    else:
                        reserve = reserve_cost(name, payload, settings)
                        if not await repo.request_started(claim, reserve):
                            observation.update(status_message="LLM_REQUEST_NOT_STARTED")
                            _record_outcome(workflow, job_id, "LLM_REQUEST_NOT_STARTED", outputs)
                            return
                        log_event("LLM_REQUEST_STARTED", **fields)
                        inject_failure(settings, name, "request_started")
                        result = await adapter.execute(name, payload)
                        inject_failure(settings, name, "response")
                    try:
                        validated = SCHEMAS[name].model_validate(result.output).model_dump()
                        validate_evidence(name, validated, payload)
                    except (ValidationError, InvalidOutput):
                        # Retain rejected output for diagnosis without checkpointing it.
                        observation.update(output=result.output, status_message="OUTPUT_REJECTED")
                        raise
                    cost = None
                    if result.tokens_input is not None and result.tokens_output is not None:
                        cost = (
                            result.tokens_input * settings.input_usd_per_million
                            + result.tokens_output * settings.output_usd_per_million
                        ) / 1000000
                    if not await repo.complete_step_with_fencing(claim, validated, result, cost):
                        observation.update(
                            output=validated, status_message="STALE_RESULT_DISCARDED"
                        )
                        _record_outcome(workflow, job_id, "STALE_RESULT_DISCARDED", outputs)
                        log_event("STALE_RESULT_DISCARDED", **fields)
                        return
                    outputs[name] = validated
                    observation.update(output=validated, status_message="CHECKPOINTED")
                    log_event(
                        "STEP_CHECKPOINTED",
                        **fields,
                        duration_ms=round((time.perf_counter() - started) * 1000),
                        tokens_input=result.tokens_input,
                        tokens_output=result.tokens_output,
                        estimated_cost_usd=cost,
                    )
        except SafeFailure:
            await repo.safe_retry(claim)
            _record_outcome(
                workflow,
                job_id,
                "PROVIDER_REJECTED",
                outputs,
                error_code="PROVIDER_REJECTED",
                level="WARNING",
            )
            log_event("SAFE_RETRY", **fields, error_category="PROVIDER_REJECTED")
            return
        except (ValidationError, InvalidOutput) as exc:
            category = (
                "INVALID_STRUCTURED_OUTPUT"
                if isinstance(exc, ValidationError)
                else output_error_category(exc)
            )
            await repo.fail(claim, "NEEDS_REVIEW", category)
            _record_outcome(
                workflow, job_id, "NEEDS_REVIEW", outputs, error_code=category, level="ERROR"
            )
            log_event("STEP_STOPPED", **fields, error_category=category)
            return
        except Exception:
            status = "FAILED" if name == "SCORE" else "UNKNOWN"
            await repo.fail(
                claim,
                status,
                "DETERMINISTIC_FAILURE" if name == "SCORE" else "AMBIGUOUS_PROVIDER_OUTCOME",
            )
            _record_outcome(
                workflow,
                job_id,
                "FAILED" if name == "SCORE" else "NEEDS_REVIEW",
                outputs,
                error_code="DETERMINISTIC_FAILURE"
                if name == "SCORE"
                else ("AMBIGUOUS_PROVIDER_OUTCOME"),
                level="ERROR",
            )
            log_event("STEP_STOPPED", **fields, error_category=status)
            return
        inject_failure(settings, name, "checkpoint")
    _record_outcome(workflow, job_id, "COMPLETED", outputs)
