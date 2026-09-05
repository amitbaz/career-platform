from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Protocol

from bs4 import BeautifulSoup

from job_hunter.normalize import canonicalize_url

logger = logging.getLogger(__name__)

_DDG_URL = "https://duckduckgo.com/html/"
_BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"


class SearchBudgetExhausted(RuntimeError):
    """Raised before provider I/O when no metered search capacity remains."""


@dataclass(frozen=True, slots=True)
class SearchHit:
    title: str
    url: str


@dataclass(frozen=True, slots=True)
class SearchResponse:
    hits: list[SearchHit]
    backend: str


class SearchBackend(Protocol):
    name: str

    def search(self, query: str) -> SearchResponse: ...


def _safe_provider_text(value: object, *, max_length: int = 180) -> str:
    """Compact provider-owned diagnostic text without ever including raw bodies."""
    text = " ".join(str(value or "").split())
    return text[:max_length]


def _brave_error_summary(response) -> tuple[str, str, str]:
    """Extract only Brave's structured validation metadata for safe logging."""
    try:
        data = response.json()
    except (TypeError, ValueError):
        return "", "", ""
    if not isinstance(data, dict):
        return "", "", ""

    error = data.get("error")
    if not isinstance(error, dict):
        return "", "", ""

    code = _safe_provider_text(error.get("code"))
    detail = _safe_provider_text(error.get("detail"))
    validation_parts: list[str] = []
    meta = error.get("meta")
    if isinstance(meta, dict):
        errors = meta.get("errors")
        if isinstance(errors, list):
            for item in errors[:3]:
                if not isinstance(item, dict):
                    continue
                loc = item.get("loc")
                if isinstance(loc, list):
                    location = ".".join(_safe_provider_text(part, max_length=60) for part in loc)
                else:
                    location = ""
                message = _safe_provider_text(item.get("msg"))
                if location and message:
                    validation_parts.append(f"{location}: {message}")
                elif message:
                    validation_parts.append(message)

    return code, detail, "; ".join(validation_parts)


class BraveSearchBackend:
    name = "brave"

    def __init__(
        self,
        http,
        api_key: str,
        *,
        on_attempt: Callable[[], bool | None] | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("Brave Search API key must be non-empty")
        self._http = http
        self._api_key = api_key.strip()
        self._on_attempt = on_attempt

    def search(self, query: str) -> SearchResponse:
        # Count conservatively before the request: if the network outcome is
        # ambiguous, treating the attempt as billable is safer than risking the
        # configured monthly free-tier ceiling.
        if self._on_attempt is not None and self._on_attempt() is False:
            raise SearchBudgetExhausted("Brave Search budget exhausted")
        response = self._http.get(
            _BRAVE_URL,
            params={"q": query, "count": 20, "safesearch": "moderate"},
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "Cache-Control": "no-cache",
                "X-Subscription-Token": self._api_key,
            },
            retry=False,
        )
        if response.status_code >= 400:
            code, detail, validation = _brave_error_summary(response)
            logger.warning(
                "Brave Search request failed: status=%s code=%s detail=%s validation=%s",
                response.status_code,
                code or "unknown",
                detail or "unknown",
                validation or "unknown",
            )
        response.raise_for_status()
        data = response.json()
        web = data.get("web") if isinstance(data, dict) else None
        results = web.get("results", []) if isinstance(web, dict) else []
        hits: list[SearchHit] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            url = canonicalize_url(str(item.get("url") or ""))
            if title and url:
                hits.append(SearchHit(title=title, url=url))
        return SearchResponse(hits=hits, backend=self.name)


class DuckDuckGoSearchBackend:
    name = "duckduckgo"

    def __init__(self, http) -> None:
        self._http = http

    def search(self, query: str) -> SearchResponse:
        response = self._http.get(_DDG_URL, params={"q": query})
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        hits: list[SearchHit] = []
        for anchor in soup.select("a.result__a"):
            href = anchor.get("href")
            title = anchor.get_text(strip=True)
            if not href or not title or "duckduckgo.com" in href:
                continue
            hits.append(SearchHit(title=title, url=canonicalize_url(href)))
        return SearchResponse(hits=hits, backend=self.name)


class FallbackSearchBackend:
    """Use a preferred search backend and fall back per query on failure."""

    name = "fallback"

    def __init__(self, primary: SearchBackend | None, secondary: SearchBackend) -> None:
        self._primary = primary
        self._secondary = secondary

    def search(self, query: str) -> SearchResponse:
        if self._primary is not None:
            try:
                return self._primary.search(query)
            except SearchBudgetExhausted:
                logger.info(
                    "targeted search budget exhausted; falling back: backend=%s",
                    self._primary.name,
                )
            except Exception:
                logger.warning(
                    "targeted search backend failed; falling back: backend=%s",
                    self._primary.name,
                    exc_info=True,
                )
        return self._secondary.search(query)


def build_search_backend(
    http,
    brave_api_key: str | None = None,
    *,
    enable_brave: bool = False,
    on_brave_attempt: Callable[[], bool | None] | None = None,
) -> SearchBackend:
    """Build the search chain.

    Brave is opt-in and callers that enable it should supply shared persisted
    budget accounting so auxiliary lookups cannot silently consume capacity.
    """
    secondary = DuckDuckGoSearchBackend(http)
    primary = (
        BraveSearchBackend(http, brave_api_key, on_attempt=on_brave_attempt)
        if brave_api_key and enable_brave
        else None
    )
    return FallbackSearchBackend(primary, secondary)
