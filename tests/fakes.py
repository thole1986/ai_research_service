from company_ai.domain.contracts import AIResult


class CountingAdapter:
    def __init__(self):
        self.calls = []

    async def execute(self, step, payload):
        self.calls.append(step)
        if step == "RESEARCH":
            output = {
                "company_name": "Acme CRM",
                "industry": "B2B SaaS",
                "products": ["CRM"],
                "customer_segments": ["SMEs"],
                "evidence": [payload["company_description"]],
                "limitations": ["Operator text only"],
            }
        elif step == "CLASSIFY":
            output = {
                "icp_match": True,
                "matching_signals": payload["research"]["evidence"],
                "missing_signals": ["Revenue is unknown"],
                "reasoning": "Supplied evidence fits",
            }
        else:
            output = {
                "executive_summary": "Potential fit from supplied evidence.",
                "strengths": ["B2B SaaS"],
                "risks": ["Unverified"],
                "next_questions": ["Confirm product needs"],
                "sources_note": "Supplied text only; rule-based estimate, unverified.",
            }
        return AIResult(output, 100, 50)
