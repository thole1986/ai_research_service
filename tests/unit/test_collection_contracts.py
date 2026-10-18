from copy import deepcopy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from company_ai.api.research.schemas import JobInput, RetryInput
from company_ai.domain.research.collection import (
    CollectionInput,
    CollectOutput,
    effective_sources,
)
from company_ai.domain.research.contracts import ALL_STEPS, STEPS, InvalidOutput, steps_for_mode
from company_ai.domain.research.entities import ResearchJob
from company_ai.domain.research.errors import Conflict
from company_ai.domain.research.hashing import digest
from company_ai.domain.research.outputs import (
    SCHEMAS,
    ReportCollectedOutput,
    ResearchCollectedOutput,
    schema_for,
)
from company_ai.domain.research.policies import validate_evidence


def checkpoint():
    return {
        "collection_version": "tavily-v1",
        "provider": "tavily",
        "query": "Acme products customers",
        "collected_at": "2026-10-18T00:00:00Z",
        "sources": [
            {
                "source_id": "web:1",
                "source_type": "web",
                "url": "https://example.com/",
                "title": "Acme",
                "content": "Acme provides CRM software for SMEs.",
                "retrieved_at": "2026-10-18T00:00:00Z",
            }
        ],
        "warnings": [],
    }


def research():
    return {
        "company_name": "Acme",
        "industry": "SaaS",
        "products": ["CRM"],
        "customer_segments": ["SMEs"],
        "evidence": ["CRM software for SMEs"],
        "limitations": ["Search snippets may be incomplete."],
        "evidence_sources": [{"quote": "CRM software for SMEs", "source_id": "web:1"}],
    }


def report():
    return {
        "executive_summary": "Acme may fit the supplied ICP.",
        "strengths": ["CRM for SMEs"],
        "risks": ["Incomplete source information"],
        "next_questions": ["Confirm current product offering."],
        "sources_note": "Tavily snippets; no independent verification.",
        "sources": [{"source_id": "web:1", "url": "https://example.com/"}],
        "limitations": ["Search snippets may be incomplete or outdated."],
    }


def test_api_manual_is_default_and_requires_description():
    common = {"company_name": "Acme", "icp": "B2B SaaS"}
    assert JobInput(**common, company_description=" CRM ").collection_mode == "manual"
    for value in (None, "", "   "):
        with pytest.raises(ValidationError, match="company_description"):
            JobInput(**common, company_description=value)
    with pytest.raises(ValidationError, match="company_description"):
        JobInput(**common)
    collected = JobInput(**common, collection_mode="tavily")
    assert collected.company_description == ""
    assert collected.website_text == ""
    assert RetryInput(step_name="COLLECT", acknowledge_possible_duplicate_charge=True)
    with pytest.raises(ValidationError, match="credentials"):
        JobInput(**common, collection_mode="tavily", website="https://user:secret@example.com")


def test_collection_input_is_deterministic_normalized_and_has_no_icp():
    first = CollectionInput("  Acme  CRM ", "https://EXAMPLE.com#about")
    second = CollectionInput("Acme CRM", "https://example.com/")
    assert first.as_dict() == second.as_dict()
    assert digest(first.as_dict()) == digest(second.as_dict())
    assert "icp" not in first.as_dict()
    assert digest(first.as_dict()) != digest(CollectionInput("Acme CRM", max_results=2).as_dict())
    assert digest(first.as_dict()) != digest(
        CollectionInput("Acme CRM", max_source_chars=2000).as_dict()
    )
    with pytest.raises(ValueError):
        CollectionInput("Acme", max_results=6)
    with pytest.raises(ValueError):
        CollectionInput("Acme", "file:///etc/passwd")
    with pytest.raises(ValueError):
        CollectionInput("Acme", "https://user:password@example.com")


def test_collection_checkpoint_preserves_text_and_unknown_cost():
    output = CollectOutput.model_validate(checkpoint()).model_dump()
    assert output["sources"][0]["content"] == checkpoint()["sources"][0]["content"]
    assert output["usage"] is None
    assert output["credits"] is None
    assert CollectOutput.model_validate({**checkpoint(), "usage": {"credits": 1}})


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data.update(collected_at="2026-10-18T00:00:00"),
        lambda data: data["sources"][0].update(url="javascript:alert(1)"),
        lambda data: data["sources"][0].update(source_id="operator:company_description"),
        lambda data: data["sources"][0].update(content="a" * 4001),
        lambda data: data["sources"].append(deepcopy(data["sources"][0])),
        lambda data: data.update(usage={"credits": -1}),
        lambda data: data.update(usage={"query": 1}),
    ],
)
def test_invalid_collection_checkpoints_rejected(mutate):
    output = checkpoint()
    mutate(output)
    with pytest.raises(ValidationError):
        CollectOutput.model_validate(output)


def test_operator_sources_keep_separate_identity_and_no_assumed_url():
    payload = {
        "company_description": "Operator statement",
        "website_text": "Pasted text",
        "website": "https://example.com/",
        "icp": "Secret ICP criterion",
    }
    before = deepcopy(payload)
    sources = effective_sources(payload, checkpoint())
    assert [source["source_id"] for source in sources] == [
        "operator:company_description",
        "operator:website_text",
        "web:1",
    ]
    assert all(source["url"] is None for source in sources[:2])
    assert all("Secret ICP criterion" not in source["content"] for source in sources)
    assert payload == before


def test_collected_schema_selection_leaves_legacy_contracts_unchanged():
    assert schema_for("RESEARCH", {}) is SCHEMAS["RESEARCH"]
    assert "evidence_sources" not in SCHEMAS["RESEARCH"].model_fields
    assert "sources" not in SCHEMAS["REPORT"].model_fields
    assert schema_for("RESEARCH", {"sources": []}) is ResearchCollectedOutput
    assert schema_for("REPORT", {"sources": []}) is ReportCollectedOutput
    assert schema_for("COLLECT", {}) is CollectOutput
    assert ResearchCollectedOutput.model_validate(research())
    assert ReportCollectedOutput.model_validate(report())


@pytest.mark.parametrize("model", [ResearchCollectedOutput, ReportCollectedOutput])
def test_collected_llm_contract_objects_require_all_fields_and_forbid_extras(model):
    schema = model.model_json_schema()

    def inspect(value):
        if isinstance(value, dict):
            if value.get("type") == "object":
                assert value.get("additionalProperties") is False
                assert set(value.get("required", [])) == set(value.get("properties", {}))
            assert "default" not in value
            for nested in value.values():
                inspect(nested)
        elif isinstance(value, list):
            for nested in value:
                inspect(nested)

    inspect(schema)


def test_collected_evidence_requires_real_source_exact_quote_and_complete_mapping():
    payload = {"sources": effective_sources({}, checkpoint())}
    validate_evidence("RESEARCH", research(), payload)
    invalid = research()
    invalid["evidence_sources"][0]["source_id"] = "invented"
    with pytest.raises(InvalidOutput, match="UNSUPPORTED_EVIDENCE"):
        validate_evidence("RESEARCH", invalid, payload)
    invalid = research()
    invalid["evidence"] = ["CRM Software for SMEs"]
    invalid["evidence_sources"][0]["quote"] = "CRM Software for SMEs"
    with pytest.raises(InvalidOutput, match="UNSUPPORTED_EVIDENCE"):
        validate_evidence("RESEARCH", invalid, payload)
    invalid = research()
    invalid["evidence_sources"] = []
    with pytest.raises(InvalidOutput, match="UNSUPPORTED_EVIDENCE"):
        validate_evidence("RESEARCH", invalid, payload)


def test_evidence_can_map_to_multiple_real_sources_but_each_mapping_is_checked():
    sources = effective_sources({"company_description": "CRM software for SMEs"}, checkpoint())
    output = research()
    output["evidence_sources"].append(
        {"quote": "CRM software for SMEs", "source_id": "operator:company_description"}
    )
    validate_evidence("RESEARCH", output, {"sources": sources})
    sources[0]["content"] = "An unrelated statement"
    with pytest.raises(InvalidOutput, match="UNSUPPORTED_EVIDENCE"):
        validate_evidence("RESEARCH", output, {"sources": sources})


def test_report_requires_exact_urls_evidence_attribution_and_limitations():
    payload = {
        "sources": effective_sources({}, checkpoint()),
        "checkpoints": {"RESEARCH": research()},
    }
    validate_evidence("REPORT", report(), payload)
    for source in (
        {"source_id": "web:1", "url": "https://invented.example/"},
        {"source_id": "invented", "url": "https://example.com/"},
    ):
        invalid = {**report(), "sources": [source]}
        with pytest.raises(InvalidOutput, match="UNSUPPORTED_REPORT_SOURCE"):
            validate_evidence("REPORT", invalid, payload)
    with pytest.raises(InvalidOutput, match="UNSUPPORTED_REPORT_SOURCE"):
        validate_evidence("REPORT", {**report(), "sources": []}, payload)
    with pytest.raises(InvalidOutput, match="MISSING_REPORT_LIMITATIONS"):
        validate_evidence("REPORT", {**report(), "limitations": []}, payload)


def test_report_cannot_attribute_operator_text_to_supplied_website():
    source = effective_sources({"company_description": "CRM software for SMEs"}, {"sources": []})
    facts = research()
    facts["evidence_sources"][0]["source_id"] = source[0]["source_id"]
    output = report()
    output["sources"] = [{"source_id": source[0]["source_id"], "url": None}]
    payload = {"sources": source, "checkpoints": {"RESEARCH": facts}}
    validate_evidence("REPORT", output, payload)
    output["sources"][0]["url"] = "https://example.com/"
    with pytest.raises(InvalidOutput, match="UNSUPPORTED_REPORT_SOURCE"):
        validate_evidence("REPORT", output, payload)


def test_step_order_and_retry_use_actual_job_steps():
    assert steps_for_mode("manual") == STEPS
    assert steps_for_mode("tavily") == ALL_STEPS
    assert steps_for_mode("manual-v1") == STEPS
    assert steps_for_mode("tavily-v1") == ALL_STEPS
    steps = [
        SimpleNamespace(id=name, step_name=name, status="PENDING", attempt_count=0)
        for name in reversed(ALL_STEPS)
    ]
    collect = next(step for step in steps if step.step_name == "COLLECT")
    collect.status = "UNKNOWN"
    collect.attempt_count = 1
    assert ResearchJob.retry_candidate(steps, "COLLECT", 2) == "COLLECT"
    with pytest.raises(Conflict):
        ResearchJob.retry_candidate(steps, "COLLECT", 1)
    manual_steps = [step for step in steps if step.step_name != "COLLECT"]
    research_step = next(step for step in manual_steps if step.step_name == "RESEARCH")
    research_step.status = "NEEDS_REVIEW"
    assert ResearchJob.retry_candidate(manual_steps, "RESEARCH", 2) == "RESEARCH"
