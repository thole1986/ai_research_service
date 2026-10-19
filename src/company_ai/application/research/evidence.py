"""Ground research citations in deterministic source spans, without fuzzy repair."""

import re

from company_ai.domain.research.contracts import InvalidOutput


def evidence_candidates(payload):
    candidates = {}
    for index, source in enumerate(payload.get("sources", [])):
        # Split at sentence/line boundaries; long sentences use bounded contiguous spans.
        content = source["content"]
        spans = [
            (match.start(), match.group())
            for match in re.finditer(r"[^\n.!?]+(?:[.!?]+|(?=\n)|$)", content)
        ]
        # Keep schema enums bounded even for thousands of tiny sentences. The fallback
        # chunks the entire source rather than dropping the tail of the evidence.
        if not spans or len(spans) > 64:
            spans = [(0, content)]
        for offset, span in spans:
            text = span.strip()
            start = 0
            while start < len(text):
                end = min(start + 600, len(text))
                if end < len(text):
                    boundary = text.rfind(" ", start, end)
                    if boundary > start:
                        end = boundary
                quote = text[start:end].strip()
                if quote:
                    candidate_id = f"s{index}:{offset}:{start}"
                    candidates[candidate_id] = {"quote": quote, "source_id": source["source_id"]}
                start = end
    return candidates


def evidence_payload(payload):
    candidates = evidence_candidates(payload)
    if not candidates:
        return payload
    sources = []
    for source in payload["sources"]:
        sources.append(
            {
                **{key: value for key, value in source.items() if key != "content"},
                "excerpts": [
                    {"evidence_id": key, "quote": value["quote"]}
                    for key, value in candidates.items()
                    if value["source_id"] == source["source_id"]
                ],
            }
        )
    return {
        **{k: v for k, v in payload.items() if k not in ("company_description", "website_text")},
        "sources": sources,
    }


def materialize_evidence(output, payload):
    if "evidence_ids" not in output:
        # Legacy/domain-shaped responses still pass the existing exact validator.
        return output
    candidates = evidence_candidates(payload)
    ids = output["evidence_ids"]
    if (
        not isinstance(ids, list)
        or len(ids) > 12
        or any(not isinstance(key, str) or key not in candidates for key in ids)
    ):
        raise InvalidOutput("UNSUPPORTED_EVIDENCE", evidence_reason="UNKNOWN_EVIDENCE_ID")
    if "evidence" in output or "evidence_sources" in output:
        raise InvalidOutput("INVALID_STRUCTURED_OUTPUT")
    mappings = [candidates[key] for key in dict.fromkeys(ids)]
    return {
        **{key: value for key, value in output.items() if key != "evidence_ids"},
        "evidence": list(dict.fromkeys(item["quote"] for item in mappings)),
        "evidence_sources": mappings,
    }
