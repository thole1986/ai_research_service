from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Short = Annotated[str, StringConstraints(min_length=1, max_length=1200)]
Items = Annotated[list[Short], Field(max_length=12)]


class StrictOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ResearchOutput(StrictOutput):
    company_name: Short
    industry: Short
    products: Items
    customer_segments: Items
    evidence: Items
    limitations: Items


class ClassificationOutput(StrictOutput):
    icp_match: bool
    matching_signals: Items
    missing_signals: Items
    reasoning: Short


class Rule(StrictOutput):
    rule: str
    points: int
    supported: bool
    evidence: list[str]


class ScoreOutput(StrictOutput):
    score: int = Field(ge=0, le=100)
    rule_breakdown: list[Rule]
    qualification: Literal["HIGH", "MEDIUM", "LOW"]
    sources_note: str


class ReportOutput(StrictOutput):
    executive_summary: Short
    strengths: Items
    risks: Items
    next_questions: Items
    sources_note: Short


SCHEMAS = {
    "RESEARCH": ResearchOutput,
    "CLASSIFY": ClassificationOutput,
    "SCORE": ScoreOutput,
    "REPORT": ReportOutput,
}
