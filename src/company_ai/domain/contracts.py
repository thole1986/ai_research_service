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


class InjectedCrash(BaseException):
    """Intentionally bypass ordinary error handlers, like a lost worker."""
