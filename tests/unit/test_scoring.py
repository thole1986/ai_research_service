from company_ai.domain.research.scoring import score


async def test_scoring_missing_signals():
    result = score(
        {"products": [], "evidence": []},
        {"icp_match": False, "missing_signals": ["Unknown product"]},
    )
    assert result["score"] == 0
    assert result["qualification"] == "LOW"
    assert "Unknown product" in result["sources_note"]


async def test_scoring_ignores_unsubstantiated_industry():
    result = score(
        {"industry": "B2B SaaS", "products": [], "evidence": ["A bakery"]},
        {"icp_match": False, "missing_signals": []},
    )
    assert result["score"] == 0
