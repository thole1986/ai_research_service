import copy
import json

import pytest
from pydantic import SecretStr, ValidationError

from company_ai.domain.research.contracts import InvalidOutput
from company_ai.domain.research.hashing import canonical
from company_ai.domain.research.llm import (
    ANTHROPIC_BASE_URL,
    LLM_STEPS,
    OPENAI_BASE_URL,
    ExecutionConfig,
    estimate_input_tokens,
    legacy_profile,
    normalize_endpoint_url,
    price_usage,
    reserve_profile_cost,
    resolve_execution_config,
    validate_execution_config,
    validate_profile_runtime,
)
from company_ai.settings import Settings


def routing(provider="ollama"):
    return {
        "version": 1,
        "profiles": {
            "selected": {
                "provider": provider,
                "model": "installed-model" if provider == "ollama" else "configured-model",
                "temperature": 0,
                "max_output_tokens": 1000,
                "timeout_seconds": 180,
                "context_window_tokens": 8192,
                "pricing": (
                    {"kind": "local_unmetered"}
                    if provider == "ollama"
                    else {
                        "kind": "estimated_token_rates",
                        "input_usd_per_million": 2,
                        "output_usd_per_million": 8,
                    }
                ),
            }
        },
        "steps": {step: "selected" for step in LLM_STEPS},
    }


def configured(config=None, **kwargs):
    return Settings(
        _env_file=None,
        openai_api_key="",
        anthropic_api_key="",
        llm_routing_json=json.dumps(config if config is not None else routing()),
        **kwargs,
    )


def test_legacy_environment_resolves_three_equivalent_openai_routes():
    settings = Settings(
        _env_file=None,
        openai_model="old-model",
        llm_temperature=0.5,
        max_output_tokens_per_step=600,
        llm_request_timeout_seconds=100,
        input_usd_per_million=1,
        output_usd_per_million=2,
    )
    execution = resolve_execution_config(settings)
    assert set(execution.steps) == set(LLM_STEPS)
    for step, profile in execution.steps.items():
        assert profile == legacy_profile(settings, step)
        assert profile.provider == "openai"
        assert profile.model == "old-model"
        assert profile.temperature == 0.5
        assert profile.max_output_tokens == 600
        assert profile.timeout_seconds == 100
        assert profile.context_window_tokens == 32000
        assert price_usage(profile, 100, 200) == 0.0005
    assert "SCORE" not in execution.steps and "COLLECT" not in execution.steps
    with pytest.raises(ValueError, match="Only LLM"):
        legacy_profile(settings, "SCORE")


def test_mixed_routes_and_unused_profiles_do_not_require_credentials():
    config = routing()
    config["profiles"]["hosted"] = routing("openai")["profiles"]["selected"]
    config["steps"]["RESEARCH"] = config["steps"]["CLASSIFY"] = "hosted"
    settings = configured(config)
    execution = resolve_execution_config(settings)
    assert [p.provider for p in execution.steps.values()] == ["openai", "openai", "ollama"]
    validate_execution_config(execution.model_dump(mode="json"), settings)
    validate_profile_runtime(execution.steps["REPORT"], settings)
    with pytest.raises(InvalidOutput, match="EXECUTION_CONFIG_UNAVAILABLE"):
        validate_profile_runtime(execution.steps["RESEARCH"], settings)


def test_all_anthropic_routes_require_only_anthropic_credential_at_execution():
    settings = configured(routing("anthropic"))
    execution = resolve_execution_config(settings)
    snapshot = execution.model_dump(mode="json")
    assert {profile.provider for profile in execution.steps.values()} == {"anthropic"}
    assert execution.steps["REPORT"].adapter_version == "anthropic-messages-v1"
    assert execution.steps["REPORT"].capability_mode == "anthropic_json_schema"
    assert validate_execution_config(snapshot, settings) == execution
    with pytest.raises(InvalidOutput, match="EXECUTION_CONFIG_UNAVAILABLE"):
        validate_profile_runtime(execution.steps["REPORT"], settings)
    settings.anthropic_api_key = SecretStr("rotated-anthropic-key")
    validate_profile_runtime(execution.steps["REPORT"], settings)
    assert resolve_execution_config(settings) == execution
    assert "rotated-anthropic-key" not in repr(settings)


def test_local_routes_and_zero_external_api_charge_need_no_hosted_keys():
    settings = configured()
    execution = resolve_execution_config(settings)
    for profile in execution.steps.values():
        validate_profile_runtime(profile, settings)
        assert price_usage(profile, None, None) == 0
        assert reserve_profile_cost(profile, [], {}) == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider", "unknown"),
        ("model", ""),
        ("model", " "),
        ("model", " name "),
        ("temperature", -0.1),
        ("temperature", 2.1),
        ("temperature", float("nan")),
        ("temperature", float("inf")),
        ("timeout_seconds", 0),
        ("timeout_seconds", float("inf")),
        ("context_window_tokens", 0),
        ("max_output_tokens", 0),
        ("max_output_tokens", 8192),
        ("max_output_tokens", True),
        ("max_output_tokens", "1000"),
        ("unsupported_option", True),
    ],
)
def test_invalid_profile_is_rejected_before_dispatch(field, value):
    config = routing()
    config["profiles"]["selected"][field] = value
    with pytest.raises(ValidationError, match="LLM_ROUTING_JSON"):
        configured(config)


@pytest.mark.parametrize(
    "pricing",
    [
        {"kind": "unrecognized"},
        {"kind": "local_unmetered", "rate": 1},
        {"kind": "estimated_token_rates", "input_usd_per_million": -1, "output_usd_per_million": 1},
        {
            "kind": "estimated_token_rates",
            "input_usd_per_million": 1,
            "output_usd_per_million": float("nan"),
        },
        {"kind": "estimated_token_rates", "input_usd_per_million": 1},
    ],
)
def test_invalid_pricing_is_rejected(pricing):
    config = routing()
    config["profiles"]["selected"]["pricing"] = pricing
    with pytest.raises(ValidationError, match="LLM_ROUTING_JSON"):
        configured(config)


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_hosted_provider_cannot_claim_unmetered_pricing(provider):
    config = routing(provider)
    config["profiles"]["selected"]["pricing"] = {"kind": "local_unmetered"}
    with pytest.raises(ValidationError, match="LLM_ROUTING_JSON"):
        configured(config)


def test_anthropic_temperature_limit_is_provider_specific():
    config = routing("anthropic")
    config["profiles"]["selected"]["temperature"] = 1.1
    with pytest.raises(ValidationError, match="LLM_ROUTING_JSON"):
        configured(config)
    config["profiles"]["selected"]["provider"] = "openai"
    configured(config)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda c: c.pop("version"),
        lambda c: c.update(version=2),
        lambda c: c.update(version=True),
        lambda c: c.update(unsupported=True),
        lambda c: c["steps"].pop("REPORT"),
        lambda c: c["steps"].update(SCORE="selected"),
        lambda c: c["steps"].update(RESEARCH="missing"),
        lambda c: c["profiles"].update({" ": c["profiles"]["selected"]}),
    ],
)
def test_explicit_config_requires_exact_complete_supported_routes(mutation):
    config = routing()
    mutation(config)
    with pytest.raises(ValidationError, match="LLM_ROUTING_JSON"):
        configured(config)


@pytest.mark.parametrize("raw", ["{", "[]", "null", '{"version":1,"version":1}'])
def test_invalid_json_and_duplicate_keys_are_rejected_without_raw_input(raw):
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None, llm_routing_json=raw)
    assert raw not in str(exc.value)


@pytest.mark.parametrize(
    "url",
    [
        "file:///private.sock",
        "ftp://localhost",
        "http://user:secret@localhost:11434",
        "http://@localhost:11434",
        "http://localhost/?token=secret",
        "http://localhost/?",
        "http://localhost/#private",
        "http://localhost:99999",
        " http://localhost:11434",
        "http://localhost\\other",
        "http://local host:11434",
    ],
)
def test_endpoint_rejects_credentials_queries_and_non_http_urls(url):
    with pytest.raises(ValidationError) as exc:
        configured(ollama_base_url=url)
    assert url not in str(exc.value)


def test_endpoint_is_normalized_and_not_exposed_by_configuration_snapshot():
    settings = configured(ollama_base_url="HTTP://LOCALHOST:80/path///")
    equivalent = configured(ollama_base_url="http://localhost/path")
    assert settings.ollama_base_url == "http://localhost/path"
    assert resolve_execution_config(settings) == resolve_execution_config(equivalent)
    assert normalize_endpoint_url("https://[::1]:443/") == "https://[::1]"
    snapshot = resolve_execution_config(settings).model_dump_json()
    assert "localhost" not in snapshot and "http" not in snapshot
    assert settings.ollama_base_url not in repr(settings)
    assert OPENAI_BASE_URL != ANTHROPIC_BASE_URL


@pytest.mark.parametrize(
    "endpoint", ["https://ollama.com", "https://API.OLLAMA.COM/api", "https://ollama.com./api"]
)
def test_paid_ollama_cloud_endpoint_is_rejected_before_dispatch(endpoint):
    with pytest.raises(ValidationError, match="Ollama Cloud endpoints") as exc:
        configured(ollama_base_url=endpoint)
    assert endpoint not in str(exc.value)
    configured(ollama_base_url="http://self-managed-ollama.internal:11434")


@pytest.mark.parametrize("lease", [180, 190])
def test_lease_must_exceed_longest_active_timeout_by_more_than_ten_seconds(lease):
    with pytest.raises(ValidationError, match="Lease TTL"):
        configured(lease_ttl_seconds=lease)


def test_explicit_routes_validate_only_active_profile_timeouts_and_tavily():
    config = routing()
    config["profiles"]["inactive"] = copy.deepcopy(config["profiles"]["selected"])
    config["profiles"]["inactive"]["timeout_seconds"] = 1000
    configured(config, lease_ttl_seconds=191, llm_request_timeout_seconds=1000)
    with pytest.raises(ValidationError, match="Lease TTL"):
        configured(config, lease_ttl_seconds=191, tavily_request_timeout_seconds=181)


def test_snapshot_is_independent_of_live_routes_rates_and_key_rotation():
    settings = configured()
    config = resolve_execution_config(settings)
    settings.llm_routing_json = json.dumps(routing("openai"))
    assert resolve_execution_config(settings) != config
    assert validate_execution_config(config.model_dump(mode="json"), settings) == config
    with pytest.raises(ValidationError, match="frozen"):
        config.steps["REPORT"].model = "changed"
    assert ExecutionConfig.model_validate_json(config.model_dump_json()) == config


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("connection_ref", "missing"),
        ("endpoint_fingerprint", "f" * 64),
        ("adapter_version", "future-v2"),
        ("capability_mode", "future"),
        ("context_guard_version", "future-v2"),
    ],
)
def test_snapshot_runtime_identity_mismatch_is_sanitized(field, value):
    settings = configured()
    snapshot = resolve_execution_config(settings).model_dump(mode="json")
    snapshot["steps"]["REPORT"][field] = value
    with pytest.raises(InvalidOutput, match="^EXECUTION_CONFIG_UNAVAILABLE$"):
        validate_execution_config(snapshot, settings)


def test_saved_snapshot_rechecks_version_endpoint_and_lease_without_credentials():
    settings = configured()
    config = resolve_execution_config(settings)
    snapshot = config.model_dump(mode="json")
    snapshot["version"] = 2
    with pytest.raises(InvalidOutput, match="EXECUTION_CONFIG_UNAVAILABLE"):
        validate_execution_config(snapshot, settings)
    settings.ollama_base_url = "http://different-internal-host:11434"
    with pytest.raises(InvalidOutput, match="EXECUTION_CONFIG_UNAVAILABLE"):
        validate_execution_config(config.model_dump(mode="json"), settings)
    settings.ollama_base_url = "http://localhost:11434"
    settings.lease_ttl_seconds = 190
    with pytest.raises(InvalidOutput, match="EXECUTION_CONFIG_UNAVAILABLE"):
        validate_execution_config(config.model_dump(mode="json"), settings)


def test_pricing_snapshot_changes_fingerprint_and_has_one_shared_policy():
    settings = configured(routing("openai"))
    profile = resolve_execution_config(settings).steps["REPORT"]
    assert price_usage(profile, 1_000_000, 2_000_000) == 18
    assert price_usage(profile, None, 20) is None
    msgs, schema = [{"role": "user", "content": "Tiếng Việt"}], {"type": "object"}
    count = len(canonical(msgs).encode()) + len(canonical(schema).encode()) + 1024
    assert estimate_input_tokens(msgs, schema) == count
    assert reserve_profile_cost(profile, msgs, schema) == price_usage(profile, count, 1000)
    changed = profile.model_dump()
    changed["pricing"]["input_usd_per_million"] = 4
    assert type(profile).model_validate(changed).config_fingerprint != profile.config_fingerprint
    for tokens in (-1, 1.5, True):
        with pytest.raises(ValueError, match="nonnegative integers"):
            price_usage(profile, tokens, 20)


def test_injected_transport_skips_only_credential_check():
    settings = configured(routing("openai"))
    profile = resolve_execution_config(settings).steps["REPORT"]
    validate_profile_runtime(profile, settings, require_credentials=False)
    settings.lease_ttl_seconds = 190
    with pytest.raises(InvalidOutput, match="EXECUTION_CONFIG_UNAVAILABLE"):
        validate_profile_runtime(profile, settings, require_credentials=False)


def test_settings_validation_performs_no_provider_io(monkeypatch):
    import httpx

    def unexpected_call(*args, **kwargs):
        raise AssertionError("Settings validation must perform no provider I/O")

    monkeypatch.setattr(httpx.AsyncClient, "request", unexpected_call)
    configured()
    configured(routing("openai"))
    configured(routing("anthropic"))
