import time

from pydantic import ValidationError

from company_ai.application.ports import ResearchRepository
from company_ai.application.prompts import (
    INSTRUCTIONS,
    PROMPT_VERSION,
    SCHEMA_VERSION,
    SYSTEM,
    reserve_cost,
)
from company_ai.application.step_executor import inject_failure, keep_lease
from company_ai.domain.contracts import AIResult, InvalidOutput, LLMAdapter, SafeFailure
from company_ai.domain.hashing import digest
from company_ai.domain.outputs import SCHEMAS
from company_ai.domain.policies import validate_evidence
from company_ai.domain.scoring import score
from company_ai.observability import log_event


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
        "schema": SCHEMAS[name].model_json_schema(),
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


async def run_workflow(job_id: str, repo: ResearchRepository, adapter: LLMAdapter, settings):
    job, steps = await repo.get_job(job_id)
    if not job or not job.can_execute:
        return
    outputs = {}
    for step in steps:
        name = step.step_name
        payload = step_payload(name, job, outputs)
        hashed = input_hash(name, payload, settings)
        if step.status == "COMPLETED":
            if step.input_hash != hashed or step.output_hash != digest(step.output_json):
                await repo.review_job(job_id, "CHECKPOINT_VERSION_MISMATCH")
                return
            outputs[name] = step.output_json
            log_event("CHECKPOINT_HIT", job_id=job_id, step_name=name)
            continue
        if step.status in ("UNKNOWN", "NEEDS_REVIEW"):
            await repo.review_job(job_id, "UNKNOWN_STEP")
            return
        claim = await repo.claim_step(step.id, hashed)
        if not claim:
            return
        fields = {"job_id": job_id, "step_name": name, "attempt_id": claim.attempt_id}
        log_event("STEP_CLAIMED", **fields)
        started = time.perf_counter()
        inject_failure(settings, name, "claim")
        try:
            async with keep_lease(repo, claim, settings):
                if name == "SCORE":
                    result = AIResult(score(outputs["RESEARCH"], outputs["CLASSIFY"]))
                else:
                    reserve = reserve_cost(name, payload, settings)
                    if not await repo.request_started(claim, reserve):
                        return
                    log_event("LLM_REQUEST_STARTED", **fields)
                    inject_failure(settings, name, "request_started")
                    result = await adapter.execute(name, payload)
                    inject_failure(settings, name, "response")
                validated = SCHEMAS[name].model_validate(result.output).model_dump()
                validate_evidence(name, validated, payload)
                cost = None
                if result.tokens_input is not None and result.tokens_output is not None:
                    cost = (
                        result.tokens_input * settings.input_usd_per_million
                        + result.tokens_output * settings.output_usd_per_million
                    ) / 1000000
                if not await repo.complete_step_with_fencing(claim, validated, result, cost):
                    log_event("STALE_RESULT_DISCARDED", **fields)
                    return
                outputs[name] = validated
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
            log_event("SAFE_RETRY", **fields, error_category="PROVIDER_REJECTED")
            return
        except (ValidationError, InvalidOutput):
            await repo.fail(claim, "NEEDS_REVIEW", "OUTPUT_OR_CONFIGURATION_INVALID")
            log_event("STEP_STOPPED", **fields, error_category="OUTPUT_OR_CONFIGURATION_INVALID")
            return
        except Exception:
            status = "FAILED" if name == "SCORE" else "UNKNOWN"
            await repo.fail(
                claim,
                status,
                "DETERMINISTIC_FAILURE" if name == "SCORE" else "AMBIGUOUS_PROVIDER_OUTCOME",
            )
            log_event("STEP_STOPPED", **fields, error_category=status)
            return
        inject_failure(settings, name, "checkpoint")
