"""Business decisions independent of persistence, transport and AI frameworks."""

from company_ai.domain.research.contracts import InvalidOutput


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
        if "sources" in payload:
            _validate_collected_evidence(output, payload["sources"])
        else:
            source = payload["company_description"] + "\n" + payload["website_text"]
            if any(e not in source for e in output["evidence"]):
                raise InvalidOutput("UNSUPPORTED_EVIDENCE")
    if name == "CLASSIFY":
        evidence = payload["research"]["evidence"]
        if any(not any(signal in e for e in evidence) for signal in output["matching_signals"]):
            raise InvalidOutput("UNSUPPORTED_CLASSIFICATION_SIGNAL")
        if output["icp_match"] and not output["matching_signals"]:
            raise InvalidOutput("ICP_MATCH_WITHOUT_EVIDENCE")
    if name == "REPORT" and "sources" in payload:
        _validate_report_sources(output, payload)


def _validate_collected_evidence(output, sources):
    by_id = {source["source_id"]: source for source in sources}
    evidence = output.get("evidence", [])
    mappings = output.get("evidence_sources", [])
    if len(by_id) != len(sources) or {item.get("quote") for item in mappings} != set(evidence):
        raise InvalidOutput("UNSUPPORTED_EVIDENCE")
    for item in mappings:
        source = by_id.get(item.get("source_id"))
        quote = item.get("quote")
        if not source or not quote or quote not in source["content"]:
            raise InvalidOutput("UNSUPPORTED_EVIDENCE")


def _validate_report_sources(output, payload):
    by_id = {source["source_id"]: source for source in payload["sources"]}
    citations = output.get("sources", [])
    cited_ids = [source.get("source_id") for source in citations]
    research = payload.get("research", payload.get("checkpoints", {}).get("RESEARCH", {}))
    required = {item["source_id"] for item in research.get("evidence_sources", [])}
    if len(by_id) != len(payload["sources"]) or len(set(cited_ids)) != len(cited_ids):
        raise InvalidOutput("UNSUPPORTED_REPORT_SOURCE")
    if not required.issubset(cited_ids):
        raise InvalidOutput("UNSUPPORTED_REPORT_SOURCE")
    for citation in citations:
        source = by_id.get(citation.get("source_id"))
        if not source or citation.get("url") != source.get("url"):
            raise InvalidOutput("UNSUPPORTED_REPORT_SOURCE")
    if not output.get("limitations"):
        raise InvalidOutput("MISSING_REPORT_LIMITATIONS")
