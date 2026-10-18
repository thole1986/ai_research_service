import time

from pydantic import ValidationError

from company_ai.application.research.ports import (
    CompanySourceCollector,
    ResearchRepository,
    WorkflowObserver,
)
from company_ai.application.research.prompts import (
    SYSTEM,
    instructions,
    prompt_versions,
    provider_schema,
    reserve_cost,
)
from company_ai.application.research.step_executor import inject_failure, keep_lease
from company_ai.domain.research.collection import CollectionInput, effective_sources
from company_ai.domain.research.contracts import (
    AIResult,
    InvalidOutput,
    LLMAdapter,
    SafeFailure,
    output_error_category,
)
from company_ai.domain.research.hashing import digest
from company_ai.domain.research.outputs import schema_for
from company_ai.domain.research.policies import validate_evidence
from company_ai.domain.research.scoring import score
from company_ai.observability import NoOpObserver, log_event


def step_payload(name, job, outputs, settings=None):
    if name == "COLLECT":
        return CollectionInput(
            company_name=job.input_json["company_name"],
            website=job.input_json.get("website"),
            max_results=settings.tavily_max_results if settings else 5,
        ).as_dict()
    if name == "RESEARCH":
        payload = {
            key: job.input_json.get(key, "")
            for key in ("company_name", "company_description", "website_text")
        }
        if "COLLECT" in outputs:
            payload["sources"] = effective_sources(job.input_json, outputs["COLLECT"])
            payload["collection"] = {
                key: outputs["COLLECT"][key]
                for key in ("collection_version", "provider", "warnings")
            }
        return payload
    if name == "CLASSIFY":
        return {"research": outputs["RESEARCH"], "icp": job.input_json["icp"]}
    if name == "SCORE":
        return {"research": outputs["RESEARCH"], "classification": outputs["CLASSIFY"]}
    payload = {"company": job.input_json, "checkpoints": outputs.copy()}
    if "COLLECT" in outputs:
        payload["sources"] = effective_sources(job.input_json, outputs["COLLECT"])
    return payload


def input_hash(name, payload, settings):
    if name == "COLLECT":
        return digest(
            {
                "step": name,
                "payload": payload,
                "schema": schema_for(name, payload).model_json_schema(),
                "execution_type": "external_collection",
            }
        )
    prompt_version, schema_version = prompt_versions(payload)
    identity = {
        "step": name,
        "payload": payload,
        "schema_version": schema_version,
        "schema": provider_schema(name, payload),
    }
    if name != "SCORE":
        identity.update(
            model=settings.openai_model,
            temperature=settings.llm_temperature,
            prompt_version=prompt_version,
            prompt=SYSTEM + instructions(name, payload),
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
    collector: CompanySourceCollector | None = None,
):
    observer = observer or NoOpObserver()
    with observer.span(
        "company-research",
        metadata={"job_id": job_id},
        session_id=job_id,
    ) as workflow:
        await _run_workflow(job_id, repo, adapter, settings, observer, workflow, collector)


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


async def _run_workflow(job_id, repo, adapter, settings, observer, workflow, collector):
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
        payload = step_payload(name, job, outputs, settings)
        hashed = input_hash(name, payload, settings)
        prompt_version, schema_version = prompt_versions(payload)
        metadata = {
            "job_id": job_id,
            "step_name": name,
            "input_hash": hashed,
            "schema_version": payload["schema_version"] if name == "COLLECT" else schema_version,
            "checkpoint_hit": step.status == "COMPLETED",
            "execution_type": (
                "external_collection"
                if name == "COLLECT"
                else "rule_based"
                if name == "SCORE"
                else "llm"
            ),
        }
        metadata.update(
            {"provider": "tavily"}
            if name == "COLLECT"
            else {"rules_version": "b2b-saas-sme-v1"}
            if name == "SCORE"
            else ({"prompt_version": prompt_version, "model": settings.openai_model})
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
                    if name == "COLLECT":
                        if collector is None:
                            raise InvalidOutput("COLLECT_NOT_CONFIGURED")
                        if not await repo.collection_request_started(claim):
                            observation.update(status_message="COLLECTION_REQUEST_NOT_STARTED")
                            _record_outcome(
                                workflow, job_id, "COLLECTION_REQUEST_NOT_STARTED", outputs
                            )
                            return
                        log_event("COLLECTION_REQUEST_STARTED", **fields)
                        inject_failure(settings, name, "request_started")
                        result = await collector.collect(CollectionInput(**payload))
                        inject_failure(settings, name, "response")
                    elif name == "SCORE":
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
                        validated = (
                            schema_for(name, payload).model_validate(result.output).model_dump()
                        )
                        if name == "COLLECT" and not validated["sources"]:
                            if not effective_sources(job.input_json, validated):
                                raise InvalidOutput("COLLECT_NO_USABLE_SOURCES")
                            if "NO_USABLE_WEB_SOURCES" not in validated["warnings"]:
                                validated["warnings"].append("NO_USABLE_WEB_SOURCES")
                        validate_evidence(name, validated, payload)
                    except (ValidationError, InvalidOutput):
                        # Retain rejected output for diagnosis without checkpointing it.
                        observation.update(output=result.output, status_message="OUTPUT_REJECTED")
                        raise
                    cost = None
                    if (
                        isinstance(result, AIResult)
                        and result.tokens_input is not None
                        and result.tokens_output is not None
                    ):
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
                    usage = (
                        {
                            "source_count": len(validated["sources"]),
                            "collection_usage": validated.get("usage"),
                            "collection_credits": validated.get("credits"),
                        }
                        if name == "COLLECT"
                        else {
                            "tokens_input": result.tokens_input,
                            "tokens_output": result.tokens_output,
                            "estimated_cost_usd": cost,
                        }
                    )
                    log_event(
                        "STEP_CHECKPOINTED",
                        **fields,
                        duration_ms=round((time.perf_counter() - started) * 1000),
                        **usage,
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
                ("COLLECT_INVALID_RESPONSE" if name == "COLLECT" else "INVALID_STRUCTURED_OUTPUT")
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
                "DETERMINISTIC_FAILURE"
                if name == "SCORE"
                else "COLLECT_AMBIGUOUS_PROVIDER_OUTCOME"
                if name == "COLLECT"
                else "AMBIGUOUS_PROVIDER_OUTCOME",
            )
            _record_outcome(
                workflow,
                job_id,
                "FAILED" if name == "SCORE" else "NEEDS_REVIEW",
                outputs,
                error_code="DETERMINISTIC_FAILURE"
                if name == "SCORE"
                else (
                    "COLLECT_AMBIGUOUS_PROVIDER_OUTCOME"
                    if name == "COLLECT"
                    else "AMBIGUOUS_PROVIDER_OUTCOME"
                ),
                level="ERROR",
            )
            log_event("STEP_STOPPED", **fields, error_category=status)
            return
        inject_failure(settings, name, "checkpoint")
    _record_outcome(workflow, job_id, "COMPLETED", outputs)
