import re

from company_ai.domain.research.outputs import ScoreOutput


def score(research, classification):
    # Signals come exclusively from source-validated evidence; no free-text ICP keyword scoring.
    evidence = research["evidence"]
    text = " ".join(evidence).casefold()
    b2b = bool(re.search(r"\bb2b\b|business.to.business", text))
    saas = bool(re.search(r"\bsaas\b|software as a service|subscription", text))
    sme = bool(re.search(r"\bsmes?\b|small and medium|small businesses", text))
    product = any(product.casefold() in text for product in research["products"])
    rules = [
        ("b2b_saas", 40, b2b and saas),
        ("icp_match", 30, classification["icp_match"]),
        ("sme_target", 20, sme),
        ("clear_product", 10, product),
    ]
    total = sum(points for _, points, supported in rules if supported)
    return ScoreOutput(
        score=total,
        qualification="HIGH" if total >= 70 else "MEDIUM" if total >= 40 else "LOW",
        rule_breakdown=[
            {
                "rule": name,
                "points": points if supported else 0,
                "supported": supported,
                "evidence": evidence if supported else [],
            }
            for name, points, supported in rules
        ],
        sources_note="Rule-based estimate from supplied text; no external verification. "
        "Missing signals: " + "; ".join(classification["missing_signals"]),
    ).model_dump()
