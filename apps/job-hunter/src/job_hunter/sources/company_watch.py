"""Discover jobs from active company-watch targets with isolated health updates."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from job_hunter.fetching import extract_job_from_html, extract_job_page_links
from job_hunter.models import Job
from job_hunter.store import JobStore

from .ashby import AshbySource
from .base import logger
from .greenhouse import GreenhouseSource
from .lever import LeverSource


def utc_now() -> datetime:
    """Return the current timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


_ATS_SOURCE_TYPES = {
    "ashby": AshbySource,
    "greenhouse": GreenhouseSource,
    "lever": LeverSource,
}

_JOB_POSTING_TYPES = frozenset(
    {
        "JobPosting",
        "https://schema.org/JobPosting",
        "http://schema.org/JobPosting",
    }
)


class _HealthTrackingHttp:
    """Expose an ATS request failure even when its adapter fails open."""

    def __init__(self, http) -> None:
        self._http = http
        self.error: Exception | None = None

    def get_json(self, url: str, **kwargs):
        try:
            return self._http.get_json(url, **kwargs)
        except Exception as exc:
            self.error = exc
            raise


def _has_endpoint(watch) -> bool:
    """Return whether a watch names an endpoint that can actually be checked."""
    provider = (watch["ats_provider"] or "").strip().lower()
    identifier = (watch["ats_identifier"] or "").strip()
    if provider in _ATS_SOURCE_TYPES and identifier:
        return True
    return bool((watch["careers_url"] or "").strip())


class CompanyWatchSource:
    """Check each due watch independently and persist endpoint health."""

    def __init__(
        self,
        store: JobStore,
        http,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._store = store
        self._http = http
        self._now = now

    def discover(self) -> list[Job]:
        """Return jobs from due watches while isolating per-company failures."""
        checked_at = self._now()
        discovered: list[Job] = []
        for watch in self._store.list_due_company_watches(checked_at):
            if not _has_endpoint(watch):
                # A company-only watch is a placeholder awaiting endpoint
                # repair, not an unhealthy endpoint. Checking it would record
                # a failure for a check that never happened.
                continue
            try:
                jobs = self._discover_watch(watch)
            except Exception:
                logger.warning(
                    "company watch check failed for %s",
                    watch["company_name"],
                    exc_info=True,
                )
                try:
                    self._store.record_watch_failure(watch["id"], checked_at)
                except Exception:
                    logger.warning(
                        "company watch failure health write failed for %s",
                        watch["company_name"],
                        exc_info=True,
                    )
                continue

            discovered.extend(jobs)
            try:
                self._store.record_watch_success(watch["id"], checked_at)
            except Exception:
                logger.warning(
                    "company watch success health write failed for %s",
                    watch["company_name"],
                    exc_info=True,
                )
        return discovered

    def _discover_watch(self, watch) -> list[Job]:
        provider = watch["ats_provider"]
        identifier = watch["ats_identifier"]
        source_type = _ATS_SOURCE_TYPES.get(provider)
        if source_type is not None and identifier:
            tracked_http = _HealthTrackingHttp(self._http)
            jobs = source_type(identifier, tracked_http).discover()
            if tracked_http.error is not None:
                raise tracked_http.error
            for job in jobs:
                job.source = f"watch:{provider}"
            return jobs

        careers_url = watch["careers_url"]
        if careers_url:
            return self._discover_generic_page(watch["company_name"], careers_url)

        raise ValueError("watch does not have a usable endpoint")

    def _discover_generic_page(
        self, company_name: str, careers_url: str
    ) -> list[Job]:
        response = self._http.get(careers_url)
        response.raise_for_status()
        html = response.text
        jobs: list[Job] = []
        seen_urls: set[str] = set()

        for posting in _iter_json_ld_job_postings(html):
            parser_posting = {**posting, "@type": "JobPosting"}
            metadata = extract_job_from_html(
                '<script type="application/ld+json">'
                f"{json.dumps(parser_posting)}"
                "</script>"
            )
            raw_url = posting.get("url")
            url = urljoin(careers_url, raw_url) if isinstance(raw_url, str) else ""
            if url:
                seen_urls.add(url)
            jobs.append(
                Job(
                    source="watch:generic",
                    title=metadata.get("title", ""),
                    company=metadata.get("company") or company_name,
                    location=metadata.get("location", ""),
                    url=url,
                    description=metadata.get("description", ""),
                    remote=metadata.get("remote"),
                )
            )

        for url in extract_job_page_links(html, careers_url):
            if url in seen_urls:
                continue
            seen_urls.add(url)
            jobs.append(
                Job(
                    source="watch:generic",
                    title="",
                    company=company_name,
                    url=url,
                )
            )
        return jobs


def _iter_json_ld_job_postings(html: str):
    """Yield every structured JobPosting from one already-fetched page."""
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        yield from _iter_job_postings(data)


def _iter_job_postings(data):
    if isinstance(data, dict):
        posting_types = data.get("@type")
        if isinstance(posting_types, str):
            posting_types = [posting_types]
        if isinstance(posting_types, list) and any(
            isinstance(posting_type, str)
            and posting_type in _JOB_POSTING_TYPES
            for posting_type in posting_types
        ):
            yield data
        for value in data.values():
            yield from _iter_job_postings(value)
    elif isinstance(data, list):
        for item in data:
            yield from _iter_job_postings(item)
