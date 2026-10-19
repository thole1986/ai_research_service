import asyncio
import json
import sys
from pathlib import Path

from company_ai.application.research.evidence import materialize_evidence
from company_ai.application.research.prompts import messages, provider_schema, provider_wire_schema
from company_ai.domain.research.collection import CollectionInput, effective_sources
from company_ai.domain.research.llm import resolve_execution_config
from company_ai.domain.research.outputs import ResearchCollectedOutput
from company_ai.domain.research.policies import validate_evidence
from company_ai.infrastructure.research.ai.llm_adapter import CrewAIAdapter
from company_ai.infrastructure.research.ai.transports import LLMRequest, OpenAITransport
from company_ai.infrastructure.research.collection.tavily import TavilySourceCollector
from company_ai.settings import Settings

INPUT = {
    "company_name": "MISA",
    "website": "https://www.misa.vn",
    "company_description": (
        "MISA cung cấp MISA AMIS CRM, giải pháp phần mềm quản lý quan hệ khách hàng "
        "và hoạt động bán hàng cho doanh nghiệp."
    ),
    "website_text": (
        "MISA AMIS CRM hỗ trợ quản lý dữ liệu khách hàng tập trung, theo dõi cơ hội bán hàng, "
        "báo cáo tình hình kinh doanh và tự động hóa quy trình bán hàng. "
        "Giải pháp có phiên bản web và mobile, đồng thời kết nối với các ứng dụng kế toán "
        "và bán hàng đa kênh."
    ),
    "icp": (
        "Doanh nghiệp cung cấp phần mềm CRM và quản lý hoạt động bán hàng "
        "cho khách hàng doanh nghiệp."
    ),
    "collection_mode": "tavily",
}


async def main():
    settings = Settings()
    collector = TavilySourceCollector(settings)
    transport = OpenAITransport(settings)
    path = Path("data/misa-repro.json")
    path.parent.mkdir(exist_ok=True)
    try:
        if path.exists():
            saved = json.loads(path.read_text(encoding="utf-8"))
            payload = saved["payload"]
        else:
            collected = await collector.collect(CollectionInput("MISA", INPUT["website"]))
            payload = {k: INPUT[k] for k in ("company_name", "company_description", "website_text")}
            payload["sources"] = effective_sources(INPUT, collected.output)
            payload["collection"] = {
                k: collected.output[k] for k in ("collection_version", "provider", "warnings")
            }
            # Save collected sources before generation so comparisons do not pay for search again.
            path.write_text(
                json.dumps({"payload": payload}, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        if "--crew" in sys.argv:
            adapter = CrewAIAdapter(settings, transports={"openai": transport})
            result = await adapter.execute(
                "RESEARCH", payload, resolve_execution_config(settings).steps["RESEARCH"]
            )
            validate_evidence("RESEARCH", result.output, payload)
            print(
                json.dumps(
                    {
                        "crew_validated": True,
                        "tokens": result.tokens_output,
                        "evidence_count": len(result.output["evidence"]),
                    }
                )
            )
            return
        response = await transport.generate(
            LLMRequest(
                step="RESEARCH",
                messages=messages("RESEARCH", payload),
                output_schema=provider_schema("RESEARCH", payload),
                wire_schema=provider_wire_schema("RESEARCH", payload, "openai"),
                schema_name="ResearchCollectedOutput",
                profile=resolve_execution_config(settings).steps["RESEARCH"],
            )
        )
        record = {
            "payload": payload,
            "response": response.content,
            "finish": response.finish_category,
        }
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        output = ResearchCollectedOutput.model_validate(
            materialize_evidence(json.loads(response.content), payload)
        ).model_dump()
        by_id = {s["source_id"]: s["content"] for s in payload["sources"]}
        print(
            json.dumps(
                {
                    "finish": response.finish_category,
                    "tokens": response.tokens_output,
                    "evidence_count": len(output["evidence"]),
                    "mapping_matches": [
                        {
                            "source_id": m["source_id"],
                            "exact": m["quote"] in by_id.get(m["source_id"], ""),
                        }
                        for m in output["evidence_sources"]
                    ],
                    "sets_match": set(output["evidence"])
                    == {m["quote"] for m in output["evidence_sources"]},
                },
                ensure_ascii=True,
            )
        )
        validate_evidence("RESEARCH", output, payload)
        print("VALIDATED")
    finally:
        await collector.aclose()
        await transport.aclose()


if __name__ == "__main__":
    asyncio.run(main())
