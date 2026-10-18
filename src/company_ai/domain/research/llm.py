"""Versioned, credential-free LLM execution configuration and pricing policy."""

import json
import math
from typing import TYPE_CHECKING, Annotated, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from company_ai.domain.research.contracts import InvalidOutput
from company_ai.domain.research.hashing import canonical, digest

if TYPE_CHECKING:
    from company_ai.settings import Settings

LLMStep = Literal["RESEARCH", "CLASSIFY", "REPORT"]
LLM_STEPS = ("RESEARCH", "CLASSIFY", "REPORT")
OPENAI_BASE_URL = "https://api.openai.com/v1"
ANTHROPIC_BASE_URL = "https://api.anthropic.com"
CONTEXT_GUARD_VERSION = "utf8-bytes-v1"
ADAPTER_VERSIONS = {
    "openai": "openai-chat-v1",
    "anthropic": "anthropic-messages-v1",
    "ollama": "ollama-native-v1",
}
CAPABILITY_MODES = {
    "openai": "json_schema",
    "anthropic": "anthropic_json_schema",
    "ollama": "native_json_schema",
}
CONNECTION_REFS = {
    "openai": "openai_default",
    "anthropic": "anthropic_default",
    "ollama": "ollama_default",
}


class FrozenConfig(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, hide_input_in_errors=True, allow_inf_nan=False
    )


class EstimatedTokenRates(FrozenConfig):
    kind: Literal["estimated_token_rates"] = "estimated_token_rates"
    input_usd_per_million: float = Field(ge=0)
    output_usd_per_million: float = Field(ge=0)


class LocalUnmetered(FrozenConfig):
    """Estimated external API charge only; excludes hardware and operating costs."""

    kind: Literal["local_unmetered"] = "local_unmetered"


Pricing = Annotated[EstimatedTokenRates | LocalUnmetered, Field(discriminator="kind")]


class LLMProfile(FrozenConfig):
    provider: Literal["openai", "anthropic", "ollama"]
    model: str = Field(min_length=1, max_length=256)
    temperature: float = Field(ge=0, le=2)
    max_output_tokens: int = Field(gt=0)
    timeout_seconds: float = Field(gt=0)
    context_window_tokens: int = Field(gt=0)
    pricing: Pricing

    @field_validator("model")
    @classmethod
    def valid_model(cls, value):
        if not value.strip() or value != value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("Model must be a nonempty model identifier")
        return value

    @model_validator(mode="after")
    def supported_limits(self):
        if self.max_output_tokens >= self.context_window_tokens:
            raise ValueError("Output token limit must be smaller than the context window")
        if self.provider in ("openai", "anthropic") and self.pricing.kind == "local_unmetered":
            raise ValueError("Hosted providers require estimated token rates")
        if self.provider == "anthropic" and self.temperature > 1:
            raise ValueError("Anthropic temperature must be between 0 and 1")
        return self


class ResolvedProfile(LLMProfile):
    profile_id: str = Field(min_length=1, max_length=128)
    connection_ref: str = Field(min_length=1)
    endpoint_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    adapter_version: str = Field(min_length=1)
    capability_mode: str = Field(min_length=1)
    context_guard_version: str = Field(min_length=1)

    @property
    def config_fingerprint(self) -> str:
        # Credentials are never present; pricing deliberately participates in identity.
        return digest(self.model_dump(mode="json"))


class ExecutionConfig(FrozenConfig):
    version: int = Field(default=1, strict=True)
    steps: dict[LLMStep, ResolvedProfile]

    @field_validator("version")
    @classmethod
    def supported_version(cls, value):
        if value != 1:
            raise ValueError("Unsupported execution configuration version")
        return value

    @field_validator("steps")
    @classmethod
    def complete_steps(cls, value):
        if set(value) != set(LLM_STEPS):
            raise ValueError("Exactly RESEARCH, CLASSIFY and REPORT routes are required")
        return value

    @property
    def config_fingerprint(self) -> str:
        return digest(self.model_dump(mode="json"))


class RoutingConfig(FrozenConfig):
    version: int = Field(strict=True)
    profiles: dict[str, LLMProfile]
    steps: dict[LLMStep, str]

    @model_validator(mode="after")
    def valid_routes(self):
        if self.version != 1:
            raise ValueError("Unsupported LLM routing version")
        if set(self.steps) != set(LLM_STEPS):
            raise ValueError("Exactly RESEARCH, CLASSIFY and REPORT routes are required")
        if not self.profiles or any(
            not key.strip() or key != key.strip() or len(key) > 128 for key in self.profiles
        ):
            raise ValueError("Profile IDs must be nonempty identifiers of at most 128 characters")
        if any(profile_id not in self.profiles for profile_id in self.steps.values()):
            raise ValueError("Every route must reference an existing profile")
        return self


def normalize_endpoint_url(value: str) -> str:
    """Normalize operator endpoints without allowing credentials or request modifiers."""
    try:
        parsed = urlsplit(value)
        if (
            not value
            or value != value.strip()
            or any(ord(c) <= 32 for c in value)
            or parsed.scheme.lower() not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or "?" in value
            or "#" in value
            or "\\" in value
        ):
            raise ValueError
        port = parsed.port
        host = parsed.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        scheme = parsed.scheme.lower()
        if port is not None and port != {"http": 80, "https": 443}[scheme]:
            host = f"{host}:{port}"
        return urlunsplit((scheme, host, parsed.path.rstrip("/"), "", ""))
    except (TypeError, ValueError):
        raise ValueError(
            "Provider endpoint must be an HTTP(S) URL without credentials or query"
        ) from None


def normalize_ollama_endpoint_url(value: str) -> str:
    normalized = normalize_endpoint_url(value)
    hostname = urlsplit(normalized).hostname.rstrip(".")
    if hostname == "ollama.com" or hostname.endswith(".ollama.com"):
        raise ValueError("Ollama Cloud endpoints are not supported by the local adapter")
    return normalized


def endpoint_fingerprint(provider: str, settings: "Settings") -> str:
    urls = {
        "openai": OPENAI_BASE_URL,
        "anthropic": ANTHROPIC_BASE_URL,
        "ollama": settings.ollama_base_url,
    }
    url = urls[provider]
    normalized = (
        normalize_ollama_endpoint_url(url) if provider == "ollama" else normalize_endpoint_url(url)
    )
    return digest(normalized)


def _resolved(profile_id: str, profile: LLMProfile, settings: "Settings") -> ResolvedProfile:
    return ResolvedProfile(
        **profile.model_dump(),
        profile_id=profile_id,
        connection_ref=CONNECTION_REFS[profile.provider],
        endpoint_fingerprint=endpoint_fingerprint(profile.provider, settings),
        adapter_version=ADAPTER_VERSIONS[profile.provider],
        capability_mode=CAPABILITY_MODES[profile.provider],
        context_guard_version=CONTEXT_GUARD_VERSION,
    )


def legacy_profile(settings: "Settings", step: str) -> ResolvedProfile:
    if step not in LLM_STEPS:
        raise ValueError("Only LLM steps have a profile")
    profile = LLMProfile(
        provider="openai",
        model=settings.openai_model,
        temperature=settings.llm_temperature,
        max_output_tokens=settings.max_output_tokens_per_step,
        timeout_seconds=float(settings.llm_request_timeout_seconds),
        context_window_tokens=32000,
        pricing=EstimatedTokenRates(
            input_usd_per_million=settings.input_usd_per_million,
            output_usd_per_million=settings.output_usd_per_million,
        ),
    )
    return _resolved(f"legacy_{step.lower()}", profile, settings)


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate configuration key")
        result[key] = value
    return result


def _invalid_json_constant(value):
    raise ValueError("Nonfinite JSON number")


def resolve_execution_config(settings: "Settings") -> ExecutionConfig:
    raw = settings.llm_routing_json
    if not raw or not raw.strip():
        config = ExecutionConfig(steps={step: legacy_profile(settings, step) for step in LLM_STEPS})
    else:
        try:
            data = json.loads(
                raw, object_pairs_hook=_unique_json_object, parse_constant=_invalid_json_constant
            )
            routing = RoutingConfig.model_validate(data)
        except (TypeError, ValueError):
            # Never include the operator's JSON (which may contain secrets) in errors.
            raise ValueError(
                "LLM_ROUTING_JSON must contain a valid version 1 routing config"
            ) from None
        config = ExecutionConfig(
            steps={
                step: _resolved(profile_id, routing.profiles[profile_id], settings)
                for step, profile_id in routing.steps.items()
            }
        )
    _validate_lease(config, settings)
    return config


def _validate_lease(config: ExecutionConfig, settings: "Settings") -> None:
    timeout = max(profile.timeout_seconds for profile in config.steps.values())
    timeout = max(timeout, settings.tavily_request_timeout_seconds)
    if settings.lease_ttl_seconds <= timeout + 10:
        raise ValueError("Lease TTL must exceed provider timeout by more than 10 seconds")


def _validate_profile_identity(profile: ResolvedProfile, settings: "Settings") -> None:
    if (
        profile.connection_ref != CONNECTION_REFS[profile.provider]
        or profile.endpoint_fingerprint != endpoint_fingerprint(profile.provider, settings)
        or profile.adapter_version != ADAPTER_VERSIONS[profile.provider]
        or profile.capability_mode != CAPABILITY_MODES[profile.provider]
        or profile.context_guard_version != CONTEXT_GUARD_VERSION
    ):
        raise ValueError("Runtime does not support the saved execution profile")


def validate_execution_config(snapshot: dict, settings: "Settings") -> ExecutionConfig:
    """Validate a saved snapshot without resolving current routes or inspecting credentials."""
    try:
        config = ExecutionConfig.model_validate(snapshot)
        _validate_lease(config, settings)
        for profile in config.steps.values():
            _validate_profile_identity(profile, settings)
        return config
    except (TypeError, ValueError):
        raise InvalidOutput("EXECUTION_CONFIG_UNAVAILABLE") from None


def validate_profile_runtime(
    profile: ResolvedProfile, settings: "Settings", *, require_credentials: bool = True
) -> None:
    """Check only the route about to execute, allowing all-local jobs without an API key."""
    try:
        _validate_profile_identity(profile, settings)
        if settings.lease_ttl_seconds <= profile.timeout_seconds + 10:
            raise ValueError
        if require_credentials and profile.provider in ("openai", "anthropic"):
            credential = getattr(settings, f"{profile.provider}_api_key")
            if not credential.get_secret_value().strip():
                raise ValueError
    except (TypeError, ValueError):
        raise InvalidOutput("EXECUTION_CONFIG_UNAVAILABLE") from None


def estimate_input_tokens(messages: list[dict], schema: dict) -> int:
    # Deliberately conservative, versioned heuristic, not a provider tokenizer guarantee.
    return len(canonical(messages).encode()) + len(canonical(schema).encode()) + 1024


def price_usage(
    profile: LLMProfile, input_tokens: int | None, output_tokens: int | None
) -> float | None:
    if profile.pricing.kind == "local_unmetered":
        return 0.0
    if input_tokens is None or output_tokens is None:
        return None
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in (input_tokens, output_tokens)
    ):
        raise ValueError("Token usage must be nonnegative integers")
    cost = (
        input_tokens * profile.pricing.input_usd_per_million
        + output_tokens * profile.pricing.output_usd_per_million
    ) / 1_000_000
    if not math.isfinite(cost):
        raise ValueError("Estimated API charge must be finite")
    return cost


def reserve_profile_cost(profile: LLMProfile, messages: list[dict], schema: dict) -> float:
    return price_usage(profile, estimate_input_tokens(messages, schema), profile.max_output_tokens)
