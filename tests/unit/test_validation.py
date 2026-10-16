import pytest

from company_ai.domain.contracts import InvalidOutput
from company_ai.domain.policies import validate_evidence
from company_ai.settings import Settings


async def test_evidence_must_be_in_source():
    with pytest.raises(InvalidOutput):
        validate_evidence(
            "RESEARCH",
            {"evidence": ["Invented revenue"]},
            {"company_description": "A company", "website_text": ""},
        )
    with pytest.raises(InvalidOutput):
        validate_evidence(
            "CLASSIFY",
            {"icp_match": True, "matching_signals": ["invented"]},
            {"research": {"evidence": ["A company"]}},
        )


async def test_production_rejects_failure_hooks():
    with pytest.raises(ValueError, match="development-only"):
        Settings(_env_file=None, app_env="production", failure_point="claim")
