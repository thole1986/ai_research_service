import builtins
import sys
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import SecretStr

from company_ai.domain.research.contracts import InvalidOutput
from company_ai.infrastructure.research.ai.llm_adapter import configure_crewai
from company_ai.infrastructure.telemetry.langfuse import LangfuseObserver, create_observer
from company_ai.observability import NoOpObserver


def test_crewai_does_not_disable_langfuse_tracing(monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
    configure_crewai()
    import os

    assert os.environ["OTEL_SDK_DISABLED"] == "false"
    assert os.environ["CREWAI_TELEMETRY_DISABLED"] == "true"


@pytest.mark.parametrize(
    "option", ["disabled", "missing_public", "missing_secret", "otel_disabled"]
)
def test_optional_tracing_does_not_import_sdk(settings, monkeypatch, option):
    settings.langfuse_public_key = SecretStr("pk-test")
    settings.langfuse_secret_key = SecretStr("sk-test")
    settings.langfuse_tracing_enabled = option != "disabled"
    settings.otel_sdk_disabled = option == "otel_disabled"
    if option == "missing_public":
        settings.langfuse_public_key = SecretStr("")
    if option == "missing_secret":
        settings.langfuse_secret_key = SecretStr("")
    original = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        assert not name.startswith(("langfuse", "opentelemetry"))
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    assert isinstance(create_observer(settings), NoOpObserver)


@pytest.mark.parametrize("failure", [ImportError, RuntimeError])
def test_sdk_unavailable_is_a_noop(settings, monkeypatch, failure, caplog):
    settings.langfuse_public_key = SecretStr("pk-test")
    settings.langfuse_secret_key = SecretStr("sk-test")
    settings.otel_sdk_disabled = False
    original = builtins.__import__

    def failed_import(name, *args, **kwargs):
        if name == "langfuse":
            raise failure("PRIVATE CREDENTIALS")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", failed_import)
    assert isinstance(create_observer(settings), NoOpObserver)
    assert "PRIVATE" not in caplog.text


@pytest.mark.parametrize("initialization_fails", [False, True])
def test_configured_factory_uses_private_provider(settings, monkeypatch, initialization_fails):
    settings.langfuse_public_key = SecretStr("pk-test")
    settings.langfuse_secret_key = SecretStr("sk-test")
    settings.otel_sdk_disabled = False
    settings.langfuse_base_url = "http://localhost:3000"
    provider = Mock()
    provider_factory = Mock(return_value=provider)
    client_factory = Mock()
    if initialization_fails:
        client_factory.side_effect = RuntimeError("PRIVATE")
    monkeypatch.setitem(
        sys.modules,
        "langfuse",
        SimpleNamespace(
            Langfuse=client_factory,
            propagate_attributes=Mock(),
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "opentelemetry.sdk.trace",
        SimpleNamespace(
            TracerProvider=provider_factory,
        ),
    )
    observer = create_observer(settings)
    if initialization_fails:
        assert isinstance(observer, NoOpObserver)
        provider.shutdown.assert_called_once()
    else:
        assert isinstance(observer, LangfuseObserver)
        assert client_factory.call_args.kwargs["tracer_provider"] is provider
        assert client_factory.call_args.kwargs["base_url"] == "http://localhost:3000"
        assert client_factory.call_args.kwargs["timeout"] == 2


@pytest.mark.parametrize("stage", ["start", "enter", "update", "exit", "flush"])
def test_telemetry_failure_never_changes_business_execution(stage, caplog):
    raw = Mock()
    manager = Mock()
    manager.__enter__ = Mock(return_value=raw)
    manager.__exit__ = Mock()
    client = SimpleNamespace(start_as_current_observation=Mock(return_value=manager))
    provider = SimpleNamespace(force_flush=Mock(return_value=True))
    operations = {
        "start": client.start_as_current_observation,
        "enter": manager.__enter__,
        "update": raw.update,
        "exit": manager.__exit__,
        "flush": provider.force_flush,
    }
    operations[stage].side_effect = RuntimeError("PRIVATE TELEMETRY ERROR")
    observer = LangfuseObserver(client, provider)
    calls = []
    with observer.span("test") as observation:
        calls.append("business")
        observation.update(output={"private": "text"})
    observer.flush()
    assert calls == ["business"]
    assert "PRIVATE" not in caplog.text


def test_exception_is_not_suppressed_or_sent_raw():
    raw = Mock()
    manager = Mock()
    manager.__enter__ = Mock(return_value=raw)
    manager.__exit__ = Mock(return_value=True)
    observer = LangfuseObserver(
        SimpleNamespace(start_as_current_observation=Mock(return_value=manager)), Mock()
    )
    error = InvalidOutput("UNSUPPORTED_CLASSIFICATION_SIGNAL")
    with pytest.raises(InvalidOutput) as raised, observer.span("CLASSIFY"):
        raise error
    assert raised.value is error
    manager.__exit__.assert_called_once_with(None, None, None)
    raw.update.assert_called_once_with(
        level="ERROR", status_message="UNSUPPORTED_CLASSIFICATION_SIGNAL"
    )


@pytest.mark.parametrize("capture", [False, True])
def test_content_capture_is_opt_in(capture):
    raw = Mock()
    calls = []

    @contextmanager
    def start(**kwargs):
        calls.append(kwargs)
        yield raw

    observer = LangfuseObserver(
        SimpleNamespace(start_as_current_observation=start), Mock(), capture_content=capture
    )
    with observer.span("test", input="private prompt") as observation:
        observation.update(output="private output", usage_details={"input": 10})
    assert ("input" in calls[0]) is capture
    assert ("output" in raw.update.call_args.kwargs) is capture
    assert raw.update.call_args.kwargs["usage_details"] == {"input": 10}


@pytest.mark.parametrize("stage", ["enter", "exit"])
def test_attribute_propagation_failures_preserve_span_and_business_error(stage):
    raw = Mock()
    manager = Mock()
    manager.__enter__ = Mock(return_value=raw)
    manager.__exit__ = Mock()
    attributes = Mock()
    attributes.__enter__ = Mock()
    attributes.__exit__ = Mock()
    getattr(attributes, f"__{stage}__").side_effect = RuntimeError("PRIVATE ATTRIBUTES")
    observer = LangfuseObserver(
        SimpleNamespace(start_as_current_observation=Mock(return_value=manager)),
        None,
        propagate_attributes=Mock(return_value=attributes),
    )
    error = InvalidOutput("UNSUPPORTED_CLASSIFICATION_SIGNAL")
    with pytest.raises(InvalidOutput) as raised, observer.span("CLASSIFY"):
        raise error
    assert raised.value is error
    manager.__exit__.assert_called_once_with(None, None, None)
