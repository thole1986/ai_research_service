"""Explicit, detached projections for administrative reads of stored research data."""

import math
import re
from uuid import UUID

from company_ai.application.research.admin_queries import ExecutionMetadata
from company_ai.domain.research.contracts import ALL_STEPS, InvalidOutput, output_error_category
from company_ai.domain.research.llm import ExecutionConfig

_RUNTIME_ERRORS = {
    "AMBIGUOUS_PROVIDER_OUTCOME",
    "BUDGET_EXCEEDED",
    "CHECKPOINT_VERSION_MISMATCH",
    "COLLECT_AMBIGUOUS_PROVIDER_OUTCOME",
    "COLLECT_ATTEMPT_LIMIT_EXCEEDED",
    "DETERMINISTIC_FAILURE",
    "LEASE_EXPIRED",
    "OUTPUT_OR_CONFIGURATION_INVALID",
    "PROVIDER_REJECTED",
    "UNKNOWN_STEP",
}
_PROVIDERS = {"openai", "anthropic", "ollama", "tavily"}
_PRICING_KINDS = {"estimated_token_rates", "local_unmetered"}
_AUDIT_FIELDS = {"provider", "model", "profile_id", "pricing_kind", "adapter_version"}
_EVENT_FIELDS = {
    "STEP_CLAIMED": {"attempt_id", "step_name"},
    "LLM_REQUEST_STARTED": {"attempt_id", *_AUDIT_FIELDS},
    "LLM_USAGE_RECORDED": {"attempt_id", "usage_known", "cost_reconciled", *_AUDIT_FIELDS},
    "COLLECTION_REQUEST_STARTED": {"attempt_id"},
    "MODEL_IDENTITY_PINNED": {"attempt_id", "identity_verified"},
    "STEP_CHECKPOINTED": {"attempt_id", "step_name", *_AUDIT_FIELDS},
    "STEP_UNKNOWN": {"attempt_id", "error_category", *_AUDIT_FIELDS},
    "STEP_NEEDS_REVIEW": {"attempt_id", "error_category", *_AUDIT_FIELDS},
    "STEP_FAILED": {"attempt_id", "error_category", *_AUDIT_FIELDS},
    "JOB_NEEDS_REVIEW": {"error_category"},
    "MANUAL_RETRY_ACKNOWLEDGED": {"acknowledge_possible_duplicate_charge"},
}
_BOOL_FIELDS = {
    "usage_known",
    "cost_reconciled",
    "identity_verified",
    "acknowledge_possible_duplicate_charge",
}


def safe_error_code(value: str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and (
        value in _RUNTIME_ERRORS or output_error_category(InvalidOutput(value)) == value
    ):
        return value
    return "UNKNOWN_ERROR"


def safe_text(value, limit: int) -> str | None:
    if (
        isinstance(value, str)
        and 0 < len(value) <= limit
        and value == value.strip()
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        return value
    return None


def safe_request_id(value) -> str | None:
    text = safe_text(value, 200)
    return text if text and re.fullmatch(r"[A-Za-z0-9_.:-]+", text) else None


def execution_metadata(value) -> dict[str, ExecutionMetadata] | None:
    if value is None:
        return None
    try:
        config = ExecutionConfig.model_validate(value)
    except (TypeError, ValueError):
        # Historical/malformed snapshots must never be replaced by today's settings.
        return None
    return {
        name: ExecutionMetadata(
            provider=profile.provider,
            model=profile.model,
            profile_id=profile.profile_id,
            pricing_kind=profile.pricing.kind,
        )
        for name, profile in config.steps.items()
    }


def safe_event_details(event_type: str, value) -> dict:
    if not isinstance(value, dict):
        return {}
    result = {}
    for name in sorted(_EVENT_FIELDS.get(event_type, ())):
        candidate = value.get(name)
        if name in _BOOL_FIELDS:
            if type(candidate) is bool:
                result[name] = candidate
        elif name == "attempt_id":
            try:
                if isinstance(candidate, str) and str(UUID(candidate)) == candidate:
                    result[name] = candidate
            except ValueError:
                pass
        elif name == "step_name":
            if isinstance(candidate, str) and candidate in ALL_STEPS:
                result[name] = candidate
        elif name == "error_category":
            if isinstance(candidate, str):
                result[name] = safe_error_code(candidate)
        elif name == "provider":
            if isinstance(candidate, str) and candidate in _PROVIDERS:
                result[name] = candidate
        elif name == "pricing_kind":
            if isinstance(candidate, str) and candidate in _PRICING_KINDS:
                result[name] = candidate
        else:
            limit = {"model": 256, "profile_id": 128, "adapter_version": 80}[name]
            if (text := safe_text(candidate, limit)) is not None:
                result[name] = text
    return result


def _safe_amount(value) -> bool:
    if type(value) not in (int, float) or value < 0:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def safe_collection_usage(value) -> dict | None:
    """Collection currently defines only numeric usage.credits and top-level credits.

    Preserve unknown measurements as null, and retain a measured zero as zero.
    Never echo arbitrary nested historical provider content through a known key.
    """
    if not isinstance(value, dict):
        return None
    result = {}
    usage = value.get("usage")
    if isinstance(usage, dict):
        result["usage"] = (
            {"credits": usage["credits"]} if _safe_amount(usage.get("credits")) else {}
        )
    elif usage is None and "usage" in value:
        result["usage"] = None
    credits = value.get("credits")
    if _safe_amount(credits) or (credits is None and "credits" in value):
        result["credits"] = credits
    return result or None
