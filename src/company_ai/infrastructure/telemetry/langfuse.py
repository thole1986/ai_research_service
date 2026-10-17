"""Best-effort Langfuse adapter, isolated from workflow and provider semantics."""

from contextlib import contextmanager

from company_ai.domain.research.contracts import InvalidOutput, SafeFailure, output_error_category
from company_ai.observability import NoOpObservation, NoOpObserver, log_event


class LangfuseObservation:
    def __init__(self, observation, capture_content):
        self.observation = observation
        self.capture_content = capture_content

    def update(self, **fields):
        if not self.capture_content:
            fields.pop("input", None)
            fields.pop("output", None)
        try:
            self.observation.update(**fields)
        except Exception:
            log_event("TELEMETRY_UNAVAILABLE", error_category="LANGFUSE_UPDATE_FAILED")


class LangfuseObserver:
    def __init__(
        self,
        client,
        provider,
        *,
        capture_content=False,
        timeout_seconds=2,
        propagate_attributes=None,
    ):
        self.client, self.provider = client, provider
        self.capture_content = capture_content
        self.timeout_seconds = timeout_seconds
        self.propagate_attributes = propagate_attributes

    @contextmanager
    def span(
        self,
        name,
        *,
        metadata=None,
        input=None,
        generation=False,
        model=None,
        model_parameters=None,
        session_id=None,
    ):
        manager = None
        attributes_manager = None
        observation = NoOpObservation()
        try:
            fields = {
                "name": name,
                "as_type": "generation" if generation else "span",
                "metadata": metadata,
            }
            if self.capture_content and input is not None:
                fields["input"] = input
            if generation:
                fields["model"] = model
                fields["model_parameters"] = model_parameters
            candidate = self.client.start_as_current_observation(**fields)
            raw = candidate.__enter__()
            manager = candidate
            observation = LangfuseObservation(raw, self.capture_content)
            if self.propagate_attributes is not None:
                try:
                    attributes = self.propagate_attributes(
                        metadata=metadata,
                        session_id=session_id,
                    )
                    attributes.__enter__()
                    attributes_manager = attributes
                except Exception:
                    log_event("TELEMETRY_UNAVAILABLE", error_category="LANGFUSE_ATTRIBUTES_FAILED")
        except Exception:
            log_event("TELEMETRY_UNAVAILABLE", error_category="LANGFUSE_SPAN_FAILED")
        try:
            yield observation
        except BaseException as exc:
            category = (
                output_error_category(exc)
                if isinstance(exc, InvalidOutput)
                else "PROVIDER_REJECTED"
                if isinstance(exc, SafeFailure)
                else "OPERATION_FAILED"
            )
            observation.update(level="ERROR", status_message=category)
            raise
        finally:
            for context in (attributes_manager, manager):
                if context is None:
                    continue
                try:
                    # Do not let SDK auto-record raw business/provider exceptions or
                    # suppress them. Only fixed sanitized categories are exported.
                    context.__exit__(None, None, None)
                except Exception:
                    log_event("TELEMETRY_UNAVAILABLE", error_category="LANGFUSE_SPAN_END_FAILED")

    def flush(self):
        try:
            # Only spans are emitted by this adapter. Bound draining rather than
            # flushing unrelated SDK media/score queues on the worker event loop.
            if not self.provider.force_flush(timeout_millis=self.timeout_seconds * 1000):
                log_event("TELEMETRY_UNAVAILABLE", error_category="LANGFUSE_FLUSH_TIMEOUT")
        except Exception:
            log_event("TELEMETRY_UNAVAILABLE", error_category="LANGFUSE_FLUSH_FAILED")


def create_observer(settings):
    if not settings.langfuse_tracing_enabled:
        return NoOpObserver()
    if not (
        settings.langfuse_public_key.get_secret_value()
        and settings.langfuse_secret_key.get_secret_value()
    ):
        return NoOpObserver()
    if settings.otel_sdk_disabled:
        log_event("TELEMETRY_DISABLED", error_category="OTEL_SDK_DISABLED")
        return NoOpObserver()
    provider = None
    try:
        # Lazy optional dependencies. Never register a global provider or patch OpenAI.
        from langfuse import Langfuse, propagate_attributes
        from opentelemetry.sdk.trace import TracerProvider

        provider = TracerProvider()
        client = Langfuse(
            public_key=settings.langfuse_public_key.get_secret_value(),
            secret_key=settings.langfuse_secret_key.get_secret_value(),
            base_url=settings.langfuse_base_url
            or settings.langfuse_host
            or ("https://cloud.langfuse.com"),
            environment=settings.langfuse_tracing_environment or settings.app_env,
            timeout=settings.langfuse_timeout_seconds,
            tracer_provider=provider,
            should_export_span=lambda span: span.instrumentation_scope.name == "langfuse-sdk",
        )
        log_event(
            "TELEMETRY_ENABLED",
            provider="langfuse",
            capture_content=settings.langfuse_capture_content,
        )
        return LangfuseObserver(
            client,
            provider,
            capture_content=settings.langfuse_capture_content,
            timeout_seconds=settings.langfuse_timeout_seconds,
            propagate_attributes=propagate_attributes,
        )
    except ImportError:
        log_event("TELEMETRY_DISABLED", error_category="LANGFUSE_NOT_INSTALLED")
    except Exception:
        log_event("TELEMETRY_UNAVAILABLE", error_category="LANGFUSE_INITIALIZATION_FAILED")
    if provider is not None:
        try:
            provider.shutdown()
        except Exception:
            pass
    return NoOpObserver()
