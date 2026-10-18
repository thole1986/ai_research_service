"""One paid Tavily Search request per collection attempt, without hidden retries.

REST contract verified against Tavily's official Search reference on 2026-10-18:
https://docs.tavily.com/documentation/api-reference/endpoint/search
"""

import hashlib
import math
import re
import unicodedata
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

import httpx

from company_ai.domain.research.collection import (
    CollectionInput,
    CollectionResult,
    normalize_website,
)
from company_ai.domain.research.contracts import InvalidOutput, SafeFailure

SEARCH_ENDPOINT = "https://api.tavily.com/search"


def normalize_url(value: str) -> str | None:
    """Normalize HTTP URLs for source identity without contacting their hosts."""
    if not isinstance(value, str) or any(char.isspace() or ord(char) < 32 for char in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        if not host or "\\" in host or "/" in host:
            return None
        # urlsplit removes IPv6 brackets; put them back for a valid URL.
        netloc = f"[{host}]" if ":" in host else host
        port = parsed.port
        scheme = parsed.scheme.lower()
        if port is not None and (scheme, port) not in {("http", 80), ("https", 443)}:
            netloc += f":{port}"
        # Use the checkpoint's canonical form too (including Unicode path escaping),
        # so equivalent source URLs cannot become duplicates during schema validation.
        return normalize_website(urlunsplit((scheme, netloc, parsed.path or "/", parsed.query, "")))
    except (ValueError, UnicodeError):
        return None


def _hostname(url: str | None) -> str | None:
    normalized = normalize_url(url) if url else None
    if not normalized:
        return None
    hostname = urlsplit(normalized).hostname
    return hostname.removeprefix("www.") if hostname else None


def _official(url: str, hostname: str | None) -> bool:
    candidate = _hostname(url)
    return bool(
        hostname and candidate and (candidate == hostname or candidate.endswith("." + hostname))
    )


def build_query(input: CollectionInput) -> str:
    """The v1 query depends only on company identity, never on the requested ICP."""
    company = " ".join(input.company_name.replace('"', " ").split())
    hostname = _hostname(input.website)
    identity = f'"{company}"' + (f" {hostname}" if hostname else "")
    return f"{identity} company about products services target customers"


def _name_text(value: str) -> str:
    folded = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.findall(r"\w+", folded))


class TavilySourceCollector:
    def __init__(self, settings, *, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self._client = client
        self._owns_client = client is None

    def validate_configuration(self) -> None:
        if not self.settings.tavily_api_key.get_secret_value().strip():
            raise InvalidOutput("COLLECT_NOT_CONFIGURED")

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def collect(self, input: CollectionInput) -> CollectionResult:
        self.validate_configuration()
        # Construct on the worker's owning event loop, only when dispatch is needed.
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.settings.tavily_request_timeout_seconds,
                transport=httpx.AsyncHTTPTransport(retries=0),
                follow_redirects=False,
            )
        query = build_query(input)
        try:
            response = await self._client.post(
                SEARCH_ENDPOINT,
                headers={
                    "Authorization": "Bearer "
                    + self.settings.tavily_api_key.get_secret_value().strip(),
                },
                json={
                    "query": query,
                    "search_depth": "basic",
                    "max_results": min(input.max_results, self.settings.tavily_max_results, 5),
                    "topic": "general",
                    "include_answer": False,
                    "include_raw_content": False,
                    "include_images": False,
                    "auto_parameters": False,
                    "include_usage": True,
                },
                timeout=self.settings.tavily_request_timeout_seconds,
            )
        except httpx.RequestError:
            # The provider may have processed a paid request. Persist only this category.
            raise RuntimeError("COLLECT_RESULT_UNKNOWN") from None
        if response.status_code == 429:
            raise SafeFailure("COLLECT_RATE_LIMITED")
        categories = {
            401: "COLLECT_AUTHENTICATION_FAILED",
            403: "COLLECT_PERMISSION_DENIED",
            400: "COLLECT_REQUEST_INVALID",
            422: "COLLECT_REQUEST_INVALID",
            432: "COLLECT_USAGE_LIMIT_REACHED",
            433: "COLLECT_USAGE_LIMIT_REACHED",
        }
        if response.status_code in categories:
            raise InvalidOutput(categories[response.status_code])
        if response.status_code >= 500:
            raise RuntimeError("COLLECT_RESULT_UNKNOWN")
        if response.status_code != 200:
            raise InvalidOutput("COLLECT_INVALID_RESPONSE")
        try:
            body = response.json()
        except (ValueError, UnicodeError):
            raise InvalidOutput("COLLECT_INVALID_RESPONSE") from None
        if not isinstance(body, dict) or not isinstance(body.get("results"), list):
            raise InvalidOutput("COLLECT_INVALID_RESPONSE")
        return self._result(input, query, body)

    def _result(self, input: CollectionInput, query: str, body: dict) -> CollectionResult:
        timestamp = datetime.now(UTC).isoformat()
        warnings = ["WEB_SNIPPETS_MAY_BE_INCOMPLETE_OR_OUTDATED"]
        candidates, seen = [], set()
        discarded = False
        for item in body["results"]:
            if not isinstance(item, dict):
                raise InvalidOutput("COLLECT_INVALID_RESPONSE")
            if not isinstance(item.get("url"), str) or not isinstance(item.get("content"), str):
                raise InvalidOutput("COLLECT_INVALID_RESPONSE")
            if "title" in item and not isinstance(item["title"], str):
                raise InvalidOutput("COLLECT_INVALID_RESPONSE")
            url = normalize_url(item["url"])
            content = item["content"].replace("\r\n", "\n").replace("\r", "\n").strip()
            if not url or not content or url in seen:
                discarded = True
                continue
            seen.add(url)
            candidates.append((url, item.get("title", "").strip()[:1000], content))
        hostname = _hostname(input.website)
        candidates.sort(key=lambda source: not _official(source[0], hostname))
        sources, total = [], 0
        result_limit = min(input.max_results, self.settings.tavily_max_results, 5)
        source_limit = min(input.max_source_chars, 4000)
        total_limit = min(input.max_total_chars, 20000)
        truncated = False
        mismatch = False
        company_name = _name_text(input.company_name)
        for url, title, raw_content in candidates[:result_limit]:
            content = raw_content[: min(source_limit, max(total_limit - total, 0))]
            truncated |= len(content) < len(raw_content)
            if not content:
                continue
            total += len(content)
            mismatch |= bool(
                company_name and f" {company_name} " not in f" {_name_text(title + ' ' + content)} "
            )
            sources.append(
                {
                    "source_id": "web_" + hashlib.sha256(url.encode()).hexdigest()[:16],
                    "url": url,
                    "title": title,
                    "content": content,
                    "retrieved_at": timestamp,
                    "source_type": "web",
                }
            )
        if discarded:
            warnings.append("INVALID_EMPTY_OR_DUPLICATE_SOURCES_DISCARDED")
        if truncated or len(candidates) > result_limit:
            warnings.append("WEB_SOURCE_LIMIT_APPLIED")
        if hostname and not any(_official(source["url"], hostname) for source in sources):
            warnings.append("OFFICIAL_DOMAIN_NOT_FOUND")
        if mismatch:
            warnings.append("COMPANY_NAME_MATCH_UNCERTAIN")
        if not sources:
            warnings.append("NO_USABLE_WEB_SOURCES")
        request_id = body.get("request_id")
        if request_id is not None and (
            not isinstance(request_id, str)
            or not re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", request_id)
        ):
            raise InvalidOutput("COLLECT_INVALID_RESPONSE")
        usage = body.get("usage")
        credits = None
        if usage is not None:
            if not isinstance(usage, dict):
                raise InvalidOutput("COLLECT_INVALID_RESPONSE")
            credits = usage.get("credits")
            if credits is not None and (
                isinstance(credits, bool)
                or not isinstance(credits, (float, int))
                or not math.isfinite(credits)
                or credits < 0
            ):
                raise InvalidOutput("COLLECT_INVALID_RESPONSE")
        # Persist reported numeric usage only, never untrusted provider metadata or estimates.
        output = {
            "collection_version": input.schema_version,
            "provider": "tavily",
            "query": query,
            "collected_at": timestamp,
            "sources": sources,
            "warnings": warnings,
            "provider_request_id": request_id,
            "usage": {"credits": credits} if credits is not None else None,
            "credits": credits,
        }
        return CollectionResult(output=output, provider_request_id=request_id)
