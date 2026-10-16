import json
from types import SimpleNamespace

import httpx
import pytest
from openai import RateLimitError

from company_ai.domain.contracts import SafeFailure
from company_ai.infrastructure.ai.llm_adapter import CrewAIAdapter


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
