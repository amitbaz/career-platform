"""Discover public job-card links from configured Y Combinator job pages."""

from __future__ import annotations

from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from job_hunter.models import Job

from .base import logger

_BASE_URL = "https://www.ycombinator.com"


class YCSource:
    """Map public YC job-card links into jobs without using authenticated endpoints."""

    def __init__(self, http, urls: list[str]) -> None:
        self._http = http
        self._urls = urls

    def discover(self) -> list[Job]:
        """Return jobs found on configured pages, continuing after a page failure."""
        jobs: list[Job] = []
        seen_urls: set[str] = set()
        for page_url in self._urls:
            try:
                response = self._http.get(page_url)
                response.raise_for_status()
            except Exception:
                logger.warning("YC job page failed: %s", page_url, exc_info=True)
                continue

            soup = BeautifulSoup(response.text, "html.parser")
            for anchor in soup.select('a.ycdc-card[href], a[href*="/companies/"][href*="/jobs/"]'):
                job = self._job_from_anchor(anchor)
                if job is not None and job.url not in seen_urls:
                    seen_urls.add(job.url)
                    jobs.append(job)
        return jobs

    @staticmethod
    def _job_from_anchor(anchor) -> Job | None:
        href = anchor.get("href")
        if not href:
            return None

        title = (anchor.get("data-title") or anchor.get_text(" ", strip=True)).strip()
        if not title:
            return None

        url = urljoin(_BASE_URL, href)
        location = (anchor.get("data-location") or "").strip()
        company = (anchor.get("data-company") or "").strip()
        return Job(
            source="yc",
            source_job_id=_job_id_from_url(url),
            title=title,
            company=company,
            location=location,
            url=url,
            remote="remote" in location.lower(),
        )


def _job_id_from_url(url: str) -> str | None:
    """Extract the public YC job identifier from a company job URL when present."""
    parts = [part for part in urlparse(url).path.split("/") if part]
    try:
        return parts[parts.index("jobs") + 1]
    except (ValueError, IndexError):
        return None
