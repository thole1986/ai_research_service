import json
import logging
from contextlib import contextmanager
from datetime import UTC, datetime


def log_event(event, **fields):
    # Only call with identifiers, fixed categories, and numeric metadata.
    logging.getLogger("company_ai").info(
        json.dumps(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "service": "company-ai",
                "event": event,
                **fields,
            }
        )
    )


def configure_logging(level="INFO"):
    logging.basicConfig(level=level, format="%(message)s")
    for name in ("httpx", "httpcore", "openai", "crewai", "LiteLLM"):
        logging.getLogger(name).setLevel(logging.CRITICAL)


class NoOpObservation:
    def update(self, **fields):
        pass


class NoOpObserver:
    """Null object used when optional tracing is unavailable or disabled."""

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
        yield NoOpObservation()

    def flush(self):
        pass
