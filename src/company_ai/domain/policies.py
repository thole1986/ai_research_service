"""Business decisions independent of persistence, transport and AI frameworks."""

from company_ai.domain.contracts import InvalidOutput


def budget_allows(calls, estimated_spend, reservation, max_calls, max_usd):
    return calls < max_calls and estimated_spend + reservation <= max_usd


def expired_step_outcome(step_name, attempts, max_attempts):
    if step_name != "SCORE":
        return "UNKNOWN", "NEEDS_REVIEW", "UNKNOWN"
    if attempts < max_attempts:
        return "PENDING", "PENDING", "FAILED"
    return "FAILED", "FAILED", "FAILED"


def failure_job_status(step_status):
    return "NEEDS_REVIEW" if step_status in ("UNKNOWN", "NEEDS_REVIEW") else "FAILED"


def validate_evidence(name, output, payload):
    if name == "RESEARCH":
        source = payload["company_description"] + "\n" + payload["website_text"]
        if any(e not in source for e in output["evidence"]):
            raise InvalidOutput("UNSUPPORTED_EVIDENCE")
    if name == "CLASSIFY":
        evidence = payload["research"]["evidence"]
        if any(not any(signal in e for e in evidence) for signal in output["matching_signals"]):
            raise InvalidOutput("UNSUPPORTED_CLASSIFICATION_SIGNAL")
        if output["icp_match"] and not output["matching_signals"]:
            raise InvalidOutput("ICP_MATCH_WITHOUT_EVIDENCE")
