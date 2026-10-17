from dataclasses import dataclass
from typing import Any, Protocol

STEPS = ("RESEARCH", "CLASSIFY", "SCORE", "REPORT")


@dataclass(frozen=True)
class Claim:
    step_id: str
    job_id: str
    step_name: str
    token: str
    attempt_id: str
    input_hash: str


@dataclass
class AIResult:
    output: dict[str, Any]
    tokens_input: int | None = None
    tokens_output: int | None = None
    provider_request_id: str | None = None


class LLMAdapter(Protocol):
    async def execute(self, step: str, payload: dict) -> AIResult: ...


class SafeFailure(Exception):
    """Confirmed rejection, with no completed generation (e.g. HTTP 429)."""


class InvalidOutput(Exception):
    """Provider returned data which does not satisfy our contract."""


def output_error_category(exc):
    # Never persist arbitrary exception messages, provider bodies or source text.
    categories = {
        "ADDITIONAL_PROVIDER_CALL_BLOCKED",
        "INCOMPLETE_OR_REFUSED_OUTPUT",
        "OUTPUT_TOKEN_LIMIT_REACHED",
        "PROVIDER_REFUSED_OUTPUT",
        "PROVIDER_CONTENT_FILTERED",
        "INVALID_STRUCTURED_OUTPUT",
        "PROVIDER_NOT_CONFIGURED",
        "CREW_OUTPUT_REJECTED",
        "NO_STRUCTURED_RESULT",
        "SYNC_CALL_ON_EVENT_LOOP_BLOCKED",
        "UNSUPPORTED_EVIDENCE",
        "UNSUPPORTED_CLASSIFICATION_SIGNAL",
        "ICP_MATCH_WITHOUT_EVIDENCE",
        "PROVIDER_AUTHENTICATION_FAILED",
        "PROVIDER_PERMISSION_DENIED",
        "PROVIDER_REQUEST_INVALID",
        "PROVIDER_MODEL_NOT_FOUND",
    }
    if isinstance(exc, InvalidOutput) and str(exc) in categories:
        return str(exc)
    return "OUTPUT_OR_CONFIGURATION_INVALID"


class InjectedCrash(BaseException):
    """Intentionally bypass ordinary error handlers, like a lost worker."""
