import pytest

from company_ai.application.research.evidence import (
    evidence_candidates,
    evidence_payload,
    materialize_evidence,
)
from company_ai.application.research.prompts import provider_schema, provider_wire_schema
from company_ai.domain.research.contracts import InvalidOutput
from company_ai.domain.research.policies import validate_evidence

QUOTE = "MISA AMIS CRM hỗ trợ quản lý dữ liệu khách hàng tập trung"
SOURCE = {"source_id": "operator:website_text", "content": QUOTE + ", theo dõi cơ hội bán hàng."}


def test_collected_schema_limits_source_ids_without_leaking_between_jobs():
    payload = {"sources": [SOURCE]}
    schema = provider_schema("RESEARCH", payload)
    field = schema["$defs"]["EvidenceSource"]["properties"]["source_id"]
    assert field["enum"] == [SOURCE["source_id"]]
    wire = provider_wire_schema("RESEARCH", payload, "openai")
    assert "evidence_sources" not in wire["properties"]
    assert wire["properties"]["evidence_ids"]["items"]["enum"]
    other = provider_schema("RESEARCH", {"sources": [{**SOURCE, "source_id": "web:other"}]})
    assert other["$defs"]["EvidenceSource"]["properties"]["source_id"]["enum"] == ["web:other"]


@pytest.mark.parametrize(
    "quote,source_id,mapped,reason",
    [
        (QUOTE, SOURCE["source_id"], True, None),
        (
            "MISA AMIS CRM giúp quản lý tập trung dữ liệu khách hàng",
            SOURCE["source_id"],
            True,
            "QUOTE_NOT_IN_SOURCE",
        ),
        (QUOTE.lower(), SOURCE["source_id"], True, "QUOTE_NOT_IN_SOURCE"),
        (QUOTE, "web:invented", True, "UNKNOWN_SOURCE_ID"),
        (QUOTE, SOURCE["source_id"], False, "EVIDENCE_MAPPING_MISMATCH"),
    ],
)
def test_vietnamese_evidence_remains_exact_and_reports_safe_reason(
    quote, source_id, mapped, reason
):
    output = {
        "evidence": [quote],
        "evidence_sources": [{"quote": quote, "source_id": source_id}] if mapped else [],
    }
    if reason is None:
        validate_evidence("RESEARCH", output, {"sources": [SOURCE]})
    else:
        with pytest.raises(InvalidOutput, match="UNSUPPORTED_EVIDENCE") as caught:
            validate_evidence("RESEARCH", output, {"sources": [SOURCE]})
        assert caught.value.evidence_reason == reason
        assert quote not in str(caught.value)


def test_duplicate_source_identity_is_rejected():
    with pytest.raises(InvalidOutput) as caught:
        validate_evidence(
            "RESEARCH", {"evidence": [], "evidence_sources": []}, {"sources": [SOURCE, SOURCE]}
        )
    assert caught.value.evidence_reason == "DUPLICATE_SOURCE_ID"


def test_candidates_preserve_vietnamese_lines_long_spans_and_source_identity():
    content = "Dòng không dấu chấm\n" + QUOTE + ".\n" + "khách hàng " * 150
    payload = {"sources": [{**SOURCE, "content": content}]}
    candidates = evidence_candidates(payload)
    assert next(iter(candidates.values()))["quote"] == "Dòng không dấu chấm"
    assert all(item["quote"] in content for item in candidates.values())
    assert all(0 < len(item["quote"]) <= 600 for item in candidates.values())
    assert all(item["source_id"] == SOURCE["source_id"] for item in candidates.values())
    assert candidates == evidence_candidates(payload)
    presented = evidence_payload(payload)
    assert "content" not in presented["sources"][0]
    assert payload["sources"][0]["content"] == content
    for candidate_id in candidates:
        resolved = materialize_evidence({"evidence_ids": [candidate_id]}, payload)
        validate_evidence("RESEARCH", resolved, payload)


@pytest.mark.parametrize("ids", [["invented"], [1], "s0", None, [[]]])
def test_materialization_rejects_invalid_ids_without_fuzzy_matching(ids):
    with pytest.raises(InvalidOutput, match="UNSUPPORTED_EVIDENCE"):
        materialize_evidence({"evidence_ids": ids}, {"sources": [SOURCE]})


def test_tiny_sentences_do_not_overflow_schema_enum_or_drop_the_source_tail():
    content = "A. " * 1300 + "FINAL EVIDENCE"
    candidates = evidence_candidates({"sources": [{**SOURCE, "content": content}]})
    assert len(candidates) < 64
    assert all(item["quote"] in content for item in candidates.values())
    assert any("FINAL EVIDENCE" in item["quote"] for item in candidates.values())


async def test_adapter_materializes_selected_ids_before_domain_validation(settings):
    import json

    from company_ai.infrastructure.research.ai.llm_adapter import bounded_llm
    from company_ai.infrastructure.research.ai.transports import LLMResponse

    payload = {"sources": [SOURCE]}
    candidate_id = next(iter(evidence_candidates(payload)))
    selected = {
        "company_name": "MISA",
        "industry": "Software",
        "products": ["CRM"],
        "customer_segments": [],
        "limitations": [],
        "evidence_ids": [candidate_id],
    }

    class Transport:
        async def generate(self, request):
            assert "evidence_ids" in request.wire_schema["properties"]
            return LLMResponse(
                content=json.dumps(selected), provider="openai", model="gpt-4.1-mini"
            )

    llm = bounded_llm(settings, "RESEARCH", payload, transport=Transport())
    await llm.acall()
    result = llm.result.output
    assert "evidence_ids" not in result
    assert result["evidence"] == [SOURCE["content"]]
    validate_evidence("RESEARCH", result, payload)
