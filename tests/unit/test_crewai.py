import json
from types import SimpleNamespace

import httpx
import pytest
from openai import APIStatusError, RateLimitError

from company_ai.application.research.prompts import provider_schema
from company_ai.domain.research.contracts import InvalidOutput, SafeFailure
from company_ai.infrastructure.research.ai.llm_adapter import CrewAIAdapter


async def test_actual_crewai_with_mock_sdk(settings, payload):
    output = {
        "company_name": "Acme CRM",
        "industry": "SaaS",
        "products": ["CRM"],
        "customer_segments": ["SMEs"],
        "evidence": [payload["company_description"]],
        "limitations": ["Unverified"],
    }
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content=json.dumps(output), refusal=None),
                )
            ],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50),
            _request_id="req-test",
        )

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    result = await CrewAIAdapter(settings, client).execute("RESEARCH", payload)
    assert result.output == output
    assert result.tokens_input == 100
    assert result.provider_request_id == "req-test"
    assert len(calls) == 1
    assert "tools" not in calls[0]
    assert calls[0]["response_format"]["type"] == "json_schema"


async def test_actual_crewai_429_never_repeats_sdk_request(settings, payload):
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        raise RateLimitError(
            "rate limited",
            response=httpx.Response(
                429, request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
            ),
            body={"error": {"code": "rate_limit_exceeded"}},
        )

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with pytest.raises(SafeFailure):
        await CrewAIAdapter(settings, client).execute("RESEARCH", payload)
    assert len(calls) == 1


@pytest.mark.parametrize("evidence", [["B2B SaaS", "SMEs in Vietnam"], []])
async def test_classify_actual_crewai_with_evidence_schema(settings, evidence):
    payload = {"research": {"evidence": evidence}, "icp": "B2B SaaS for SMEs in Vietnam"}
    output = {
        "icp_match": bool(evidence),
        "matching_signals": evidence,
        "missing_signals": [],
        "reasoning": "Based on supplied evidence only.",
    }
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content=json.dumps(output), refusal=None),
                )
            ],
            usage=None,
        )

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    result = await CrewAIAdapter(settings, client).execute("CLASSIFY", payload)
    assert result.output == output
    assert len(calls) == 1
    signals = calls[0]["response_format"]["json_schema"]["schema"]["properties"]["matching_signals"]
    if evidence:
        assert signals["items"]["enum"] == evidence
    else:
        assert signals["maxItems"] == 0
        assert "enum" not in signals["items"]
    # Schema construction must not leak one job's evidence into another request.
    assert "enum" not in provider_schema("RESEARCH", {})["properties"]["evidence"]["items"]


@pytest.mark.parametrize(
    "finish_reason,refusal,content,category",
    [
        ("length", None, '{"icp_match":', "OUTPUT_TOKEN_LIMIT_REACHED"),
        ("stop", "private refusal", "", "PROVIDER_REFUSED_OUTPUT"),
        ("content_filter", None, "", "PROVIDER_CONTENT_FILTERED"),
        ("stop", None, '{"icp_match": "yes"}', "INVALID_STRUCTURED_OUTPUT"),
    ],
)
async def test_actual_crewai_output_failure_reason(
    settings, finish_reason, refusal, content, category
):
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason=finish_reason,
                    message=SimpleNamespace(content=content, refusal=refusal),
                )
            ]
        )

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with pytest.raises(InvalidOutput, match=f"^{category}$"):
        await CrewAIAdapter(settings, client).execute(
            "CLASSIFY", {"research": {"evidence": []}, "icp": "B2B"}
        )
    assert len(calls) == 1


@pytest.mark.parametrize(
    "status,category",
    [
        (400, "PROVIDER_REQUEST_INVALID"),
        (401, "PROVIDER_AUTHENTICATION_FAILED"),
        (403, "PROVIDER_PERMISSION_DENIED"),
        (404, "PROVIDER_MODEL_NOT_FOUND"),
        (422, "PROVIDER_REQUEST_INVALID"),
    ],
)
async def test_actual_crewai_explicit_provider_rejection(settings, payload, status, category):
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        raise APIStatusError(
            "PRIVATE PROVIDER RESPONSE",
            response=httpx.Response(
                status, request=httpx.Request("POST", "https://api.openai.com")
            ),
            body={},
        )

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with pytest.raises(InvalidOutput, match=f"^{category}$"):
        await CrewAIAdapter(settings, client).execute("RESEARCH", payload)
    assert len(calls) == 1
