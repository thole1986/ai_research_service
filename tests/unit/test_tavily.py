import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from company_ai.domain.research.collection import CollectionInput, CollectOutput
from company_ai.domain.research.contracts import InvalidOutput, SafeFailure
from company_ai.infrastructure.research.collection.tavily import (
    SEARCH_ENDPOINT,
    TavilySourceCollector,
    build_query,
    normalize_url,
)
from company_ai.settings import Settings


def source(url="https://example.com/", content="Acme CRM provides software.", title="Acme CRM"):
    return {"url": url, "title": title, "content": content}


async def collect_response(settings, body, *, input=None, status=200):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(status, json=body)

    settings.tavily_api_key = type(settings.tavily_api_key)("test-only-tavily")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        collector = TavilySourceCollector(settings, client=client)
        result = await collector.collect(
            input or CollectionInput("Acme CRM", "https://example.com")
        )
    return result, requests


async def test_one_basic_search_request_without_answers_and_typed_checkpoint(settings):
    result, requests = await collect_response(
        settings,
        {
            "results": [source()],
            "request_id": "provider-123",
            "usage": {"credits": 1, "untrusted_metadata": "must not persist"},
            "answer": "Generated answers must not become source evidence.",
        },
    )
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST" and str(request.url) == SEARCH_ENDPOINT
    assert request.headers["authorization"] == "Bearer test-only-tavily"
    payload = json.loads(request.content)
    assert payload == {
        "query": build_query(CollectionInput("Acme CRM", "https://example.com")),
        "search_depth": "basic",
        "max_results": 5,
        "topic": "general",
        "include_answer": False,
        "include_raw_content": False,
        "include_images": False,
        "auto_parameters": False,
        "include_usage": True,
    }
    validated = CollectOutput.model_validate(result.output)
    assert len(validated.sources) == 1
    assert result.output["usage"] == {"credits": 1}
    assert result.output["credits"] == 1
    assert result.provider_request_id == "provider-123"
    assert "Generated answers" not in json.dumps(result.output)
    assert "untrusted_metadata" not in json.dumps(result.output)


async def test_normalization_dedup_exact_hostname_priority_and_safe_warnings(settings):
    body = {
        "results": [
            source("https://example.com.evil.test/", "Other identity", "Other company"),
            source("https://www.EXAMPLE.com:443/about#fragment"),
            source("https://www.example.com/about#different"),
            source("https://docs.example.com/"),
            source("javascript:alert(1)"),
            source("https://empty.test/", "  "),
        ]
    }
    result, requests = await collect_response(settings, body)
    urls = [item["url"] for item in result.output["sources"]]
    assert urls == [
        "https://www.example.com/about",
        "https://docs.example.com/",
        "https://example.com.evil.test/",
    ]
    assert len(requests) == 1  # Source URLs are never fetched.
    assert "INVALID_EMPTY_OR_DUPLICATE_SOURCES_DISCARDED" in result.output["warnings"]
    assert "COMPANY_NAME_MATCH_UNCERTAIN" in result.output["warnings"]
    assert "OFFICIAL_DOMAIN_NOT_FOUND" not in result.output["warnings"]
    assert not any("https://" in warning for warning in result.output["warnings"])


async def test_domain_suffix_confusion_does_not_count_as_official(settings):
    result, _ = await collect_response(
        settings,
        {"results": [source("https://example.com.evil.test/"), source("https://badexample.com/")]},
    )
    assert "OFFICIAL_DOMAIN_NOT_FOUND" in result.output["warnings"]


async def test_canonical_unicode_urls_deduplicate_and_title_fits_checkpoint_schema(settings):
    result, _ = await collect_response(
        settings,
        {
            "results": [
                source("https://example.com/công", title="Acme CRM " + "x" * 1500),
                source("https://example.com/c%C3%B4ng"),
            ]
        },
    )
    checkpoint = CollectOutput.model_validate(result.output)
    assert len(checkpoint.sources) == 1
    assert len(checkpoint.sources[0].title) == 1000
    assert checkpoint.sources[0].url == "https://example.com/c%C3%B4ng"


async def test_company_name_substring_is_not_treated_as_an_identity_match(settings):
    result, _ = await collect_response(
        settings, {"results": [source(content="Other company", title="NotAcme CRM")]}
    )
    assert "COMPANY_NAME_MATCH_UNCERTAIN" in result.output["warnings"]


async def test_text_and_result_limits_content_and_source_identity_are_stable(settings):
    body = {
        "results": [source(f"https://example.com/{i}", "Acme CRM " + "x" * 6000) for i in range(9)]
    }
    first, _ = await collect_response(settings, body)
    second, _ = await collect_response(settings, body)
    sources = first.output["sources"]
    assert len(sources) == 5
    assert all(len(item["content"]) == 4000 for item in sources)
    assert sum(len(item["content"]) for item in sources) == 20000
    assert "WEB_SOURCE_LIMIT_APPLIED" in first.output["warnings"]
    assert [item["source_id"] for item in sources] == [
        item["source_id"] for item in second.output["sources"]
    ]
    limited, requests = await collect_response(
        settings, body, input=CollectionInput("Acme CRM", max_results=2)
    )
    assert len(limited.output["sources"]) == 2
    assert json.loads(requests[0].content)["max_results"] == 2


async def test_empty_collection_is_a_checkpoint_and_usage_is_unknown(settings):
    result, _ = await collect_response(settings, {"results": []})
    assert result.output["sources"] == []
    assert "NO_USABLE_WEB_SOURCES" in result.output["warnings"]
    assert result.output["usage"] is None
    assert result.output["credits"] is None


@pytest.mark.parametrize(
    "status,error,category",
    [
        (401, InvalidOutput, "COLLECT_AUTHENTICATION_FAILED"),
        (403, InvalidOutput, "COLLECT_PERMISSION_DENIED"),
        (400, InvalidOutput, "COLLECT_REQUEST_INVALID"),
        (422, InvalidOutput, "COLLECT_REQUEST_INVALID"),
        (432, InvalidOutput, "COLLECT_USAGE_LIMIT_REACHED"),
        (433, InvalidOutput, "COLLECT_USAGE_LIMIT_REACHED"),
        (429, SafeFailure, "COLLECT_RATE_LIMITED"),
        (500, RuntimeError, "COLLECT_RESULT_UNKNOWN"),
        (503, RuntimeError, "COLLECT_RESULT_UNKNOWN"),
        (302, InvalidOutput, "COLLECT_INVALID_RESPONSE"),
    ],
)
async def test_http_failure_categories_do_not_include_provider_body(
    settings, status, error, category
):
    with pytest.raises(error, match=f"^{category}$"):
        await collect_response(settings, {"detail": "private query and api key"}, status=status)


@pytest.mark.parametrize("exception_type", [httpx.ReadTimeout, httpx.ConnectError])
async def test_uncertain_network_failure_is_not_retried_and_is_sanitized(settings, exception_type):
    requests = []

    def handle(request):
        requests.append(request)
        raise exception_type("private query and key", request=request)

    settings.tavily_api_key = type(settings.tavily_api_key)("test-only")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        collector = TavilySourceCollector(settings, client=client)
        with pytest.raises(RuntimeError, match="^COLLECT_RESULT_UNKNOWN$") as caught:
            await collector.collect(CollectionInput("Acme CRM"))
    assert len(requests) == 1
    assert caught.value.__suppress_context__


@pytest.mark.parametrize(
    "body",
    [
        [],
        {},
        {"results": "invalid"},
        {"results": [None]},
        {"results": [{"url": "https://example.com", "content": None}]},
        {"results": [], "usage": {"credits": -1}},
        {"results": [], "usage": {"credits": "unknown"}},
        {"results": [], "usage": {"credits": True}},
        {"results": [], "usage": "invalid"},
        {"results": [], "request_id": "private response body\n"},
    ],
)
async def test_invalid_response_is_rejected(settings, body):
    with pytest.raises(InvalidOutput, match="^COLLECT_INVALID_RESPONSE$"):
        await collect_response(settings, body)


async def test_malformed_json_is_rejected(settings):
    settings.tavily_api_key = type(settings.tavily_api_key)("test-only")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text="not json"))
    ) as client:
        with pytest.raises(InvalidOutput, match="^COLLECT_INVALID_RESPONSE$"):
            await TavilySourceCollector(settings, client=client).collect(CollectionInput("Acme"))


async def test_missing_key_never_constructs_http_client(settings, monkeypatch):
    settings.tavily_api_key = type(settings.tavily_api_key)("  ")

    def unexpected_client(*args, **kwargs):
        raise AssertionError("Missing key must not instantiate an HTTP client")

    monkeypatch.setattr(httpx, "AsyncClient", unexpected_client)
    collector = TavilySourceCollector(settings)
    with pytest.raises(InvalidOutput, match="^COLLECT_NOT_CONFIGURED$"):
        await collector.collect(CollectionInput("Acme CRM"))
    await collector.aclose()


async def test_owned_client_closes_on_owning_event_loop(settings, monkeypatch):
    settings.tavily_api_key = type(settings.tavily_api_key)("test-only")
    clients, transports = [], []
    original_client = httpx.AsyncClient
    original_transport = httpx.AsyncHTTPTransport

    def client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(
            lambda _: httpx.Response(200, json={"results": []})
        )
        value = original_client(**kwargs)
        clients.append(value)
        return value

    def transport(**kwargs):
        transports.append(kwargs)
        return original_transport(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", transport)
    collector = TavilySourceCollector(settings)
    assert clients == []
    await collector.collect(CollectionInput("Acme CRM"))
    assert transports == [{"retries": 0}]
    assert not clients[0].is_closed
    await collector.aclose()
    assert clients[0].is_closed


@pytest.mark.parametrize(
    "value,normalized",
    [
        ("HTTPS://EXAMPLE.com:443#fragment", "https://example.com/"),
        ("http://example.com:80/a?b=1#c", "http://example.com/a?b=1"),
        ("https://example.com:8443/", "https://example.com:8443/"),
        ("https://[::1]/", "https://[::1]/"),
        ("https://user:password@example.com/", None),
        ("https://example.com:invalid/", None),
        ("https://example.com/a b", None),
        ("javascript:alert(1)", None),
        ("//example.com/", None),
    ],
)
def test_url_normalization(value, normalized):
    assert normalize_url(value) == normalized


def test_query_only_uses_identity_and_normalizes_website():
    assert build_query(CollectionInput("Acme CRM", "https://www.EXAMPLE.com/path")) == (
        '"Acme CRM" example.com company about products services target customers'
    )
    assert "icp" not in CollectionInput("Acme CRM").as_dict()


def test_collection_settings_are_optional_masked_and_bounded():
    settings = Settings(_env_file=None, tavily_api_key="test-private", max_step_attempts=1)
    assert "test-private" not in repr(settings)
    assert settings.collect_max_attempts == 1
    assert Settings(_env_file=None).tavily_request_timeout_seconds == 30
    for option, value in [
        ("tavily_max_results", 6),
        ("tavily_max_results", 0),
        ("tavily_request_timeout_seconds", 0),
        ("collect_max_attempts", 0),
        ("tavily_request_timeout_seconds", 300),
    ]:
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **{option: value})


@pytest.mark.parametrize("mode", [None, "manual", "tavily"])
@pytest.mark.parametrize("workflow_fails", [False, True])
def test_worker_only_constructs_collector_for_tavily_and_always_closes(
    mode, workflow_fails, monkeypatch
):
    from company_ai.tasks import ai_tasks

    called = []
    fake_repo = SimpleNamespace(settings=SimpleNamespace())

    async def get_job(job_id):
        return SimpleNamespace(input_json={"collection_mode": mode} if mode else {}), []

    fake_repo.get_job = get_job

    class Collector:
        def __init__(self, settings):
            called.append("construct")

        async def aclose(self):
            called.append("close")

    async def workflow(*args, collector=None, **kwargs):
        assert bool(collector) == (mode == "tavily")
        if workflow_fails:
            raise RuntimeError("task failed")

    def run_async(operation):
        import asyncio

        return asyncio.run(operation(fake_repo))

    observer = SimpleNamespace(flush=lambda: called.append("flush"))
    monkeypatch.setattr(ai_tasks, "run_async", run_async)
    monkeypatch.setattr(ai_tasks, "get_worker_observer", lambda: observer)
    monkeypatch.setattr(ai_tasks, "CrewAIAdapter", lambda *args, **kwargs: None)
    monkeypatch.setattr(ai_tasks, "TavilySourceCollector", Collector)
    monkeypatch.setattr(ai_tasks, "run_workflow", workflow)
    if workflow_fails:
        with pytest.raises(RuntimeError, match="task failed"):
            ai_tasks.run_job.run("test-job")
    else:
        ai_tasks.run_job.run("test-job")
    assert called == (["construct", "close", "flush"] if mode == "tavily" else ["flush"])
