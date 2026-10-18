"""Versioned collection inputs, persisted source checkpoints and provenance."""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import (
    Field,
    HttpUrl,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_validator,
)

from company_ai.domain.research.contracts import COLLECTION_WORKFLOW_VERSION
from company_ai.domain.research.outputs import StrictOutput

QUERY_POLICY_VERSION = "company-v1"
COLLECTION_SCHEMA_VERSION = COLLECTION_WORKFLOW_VERSION
MAX_SOURCE_CHARS = 4000
MAX_TOTAL_SOURCE_CHARS = 20000
_http_url = TypeAdapter(HttpUrl)
SourceId = Annotated[str, StringConstraints(min_length=1, max_length=200)]
SourceText = Annotated[str, StringConstraints(min_length=1, max_length=MAX_SOURCE_CHARS)]


def normalize_website(value: str | None) -> str | None:
    if value is None:
        return None
    parsed = urlsplit(str(_http_url.validate_python(value)))
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Source URLs must not contain credentials")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


@dataclass(frozen=True)
class CollectionInput:
    company_name: str
    website: str | None = None
    max_results: int = 5
    query_policy_version: str = QUERY_POLICY_VERSION
    schema_version: str = COLLECTION_SCHEMA_VERSION
    max_source_chars: int = MAX_SOURCE_CHARS
    max_total_chars: int = MAX_TOTAL_SOURCE_CHARS

    def __post_init__(self):
        name = " ".join(self.company_name.split())
        if not name or len(name) > 200:
            raise ValueError("Invalid company name")
        if type(self.max_results) is not int or not 1 <= self.max_results <= 5:
            raise ValueError("max_results must be between 1 and 5")
        if not 1 <= self.max_source_chars <= MAX_SOURCE_CHARS:
            raise ValueError("Invalid per-source text limit")
        if not 1 <= self.max_total_chars <= MAX_TOTAL_SOURCE_CHARS:
            raise ValueError("Invalid total text limit")
        if not self.query_policy_version or not self.schema_version:
            raise ValueError("Collection versions are required")
        object.__setattr__(self, "company_name", name)
        object.__setattr__(self, "website", normalize_website(self.website))

    def as_dict(self) -> dict:
        """Only deterministic, non-secret values which affect collection output."""
        return asdict(self)


@dataclass
class CollectionResult:
    output: dict[str, Any]
    provider_request_id: str | None = None


def _utc_timestamp(value: str) -> str:
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Invalid collection timestamp") from exc
    if timestamp.utcoffset() != timedelta(0):
        raise ValueError("Collection timestamps must include UTC timezone")
    return value


class WebSource(StrictOutput):
    source_id: SourceId
    url: str
    title: str = Field(max_length=1000)
    content: SourceText
    retrieved_at: str
    source_type: Literal["web"] = "web"

    @field_validator("source_id")
    @classmethod
    def web_identity(cls, value):
        if value.startswith("operator:"):
            raise ValueError("Web sources cannot use operator source IDs")
        return value

    @field_validator("url")
    @classmethod
    def valid_url(cls, value):
        return normalize_website(value)

    @field_validator("retrieved_at")
    @classmethod
    def utc_time(cls, value):
        return _utc_timestamp(value)


class CollectOutput(StrictOutput):
    collection_version: str
    provider: Literal["tavily"] = "tavily"
    query: str = Field(min_length=1, max_length=1000)
    collected_at: str
    sources: list[WebSource] = Field(max_length=5)
    warnings: list[str] = Field(max_length=20)
    provider_request_id: str | None = None
    usage: dict[str, int | float] | None = None
    credits: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @field_validator("collected_at")
    @classmethod
    def utc_time(cls, value):
        return _utc_timestamp(value)

    @field_validator("usage")
    @classmethod
    def valid_usage(cls, value):
        if value is not None:
            if set(value) - {"credits"}:
                raise ValueError("Unknown collection usage field")
            for count in value.values():
                if isinstance(count, bool) or count < 0 or not float("-inf") < count < float("inf"):
                    raise ValueError("Collection usage must be known finite nonnegative numbers")
        return value

    @model_validator(mode="after")
    def bounded_unique_sources(self):
        if len({source.source_id for source in self.sources}) != len(self.sources):
            raise ValueError("Duplicate source IDs")
        if len({source.url for source in self.sources}) != len(self.sources):
            raise ValueError("Duplicate source URLs")
        if sum(len(source.content) for source in self.sources) > MAX_TOTAL_SOURCE_CHARS:
            raise ValueError("Total source content limit exceeded")
        return self


def effective_sources(input_json: dict, collect: dict | CollectOutput) -> list[dict]:
    """Operator text has its own identity; a supplied website is never its attribution."""
    sources = []
    for name in ("company_description", "website_text"):
        text = input_json.get(name) or ""
        if text.strip():
            sources.append(
                {
                    "source_id": f"operator:{name}",
                    "source_type": "operator",
                    "url": None,
                    "title": name,
                    "content": text,
                }
            )
    checkpoint = collect.model_dump(mode="json") if isinstance(collect, CollectOutput) else collect
    for source in checkpoint.get("sources", []):
        sources.append(dict(source))
    return sources
