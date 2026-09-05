from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from job_hunter.models import Job

from .base import logger

_BASE_URL = "https://wellfound.com"
_JOB_PATH_RE = re.compile(r"^/jobs/(\d+)-")
_TITLE_SUFFIX = " | Wellfound"
_WELLFOUND_RETRY_STATUS_CODES = {500, 502, 503, 504}
_WORK_MODES = {
    "Remote only": True,
    "In office": False,
    "Hybrid": None,
}
_WORK_MODE_RE = re.compile(r"Remote Work Policy\s+(Remote only|In office|Hybrid)")

_WELLFOUND_LISTINGS = {
    "germany_eu": [
        "https://wellfound.com/role/l/frontend-engineer/europe",
        "https://wellfound.com/role/l/full-stack-engineer/europe",
    ],
    "london": [
        "https://wellfound.com/role/l/frontend-engineer/london",
        "https://wellfound.com/role/l/full-stack-engineer/london",
    ],
    "us_nyc_sf": [
        "https://wellfound.com/role/l/frontend-engineer/new-york",
        "https://wellfound.com/role/l/frontend-engineer/san-francisco",
        "https://wellfound.com/role/l/full-stack-engineer/new-york",
        "https://wellfound.com/role/l/full-stack-engineer/san-francisco",
    ],
}


@dataclass(frozen=True, slots=True)
class WellfoundListing:
    url: str
    market_id: str


class WellfoundSource:
    """Discover Europe/London/US startup postings from Wellfound listing/detail pages."""

    def __init__(
        self,
        http,
        listings: list[WellfoundListing],
        *,
        max_jobs_per_listing: int = 12,
    ) -> None:
        self._http = http
        self.listings = listings
        self._max_jobs_per_listing = max_jobs_per_listing

    def discover(self) -> list[Job]:
        jobs: list[Job] = []
        seen_job_ids: set[str] = set()
        for listing in self.listings:
            response = self._get_response(
                listing.url,
                kind="listing",
                target=listing.url,
            )
            if response is None:
                continue

            links = _extract_job_links(response.text)
            new_links = [link for link in links if link[0] not in seen_job_ids]
            for job_id, detail_url in new_links[: self._max_jobs_per_listing]:
                seen_job_ids.add(job_id)
                job = self._fetch_job(job_id, detail_url, listing.market_id)
                if job is not None:
                    jobs.append(job)
        return jobs

    def _get_response(self, url: str, *, kind: str, target: str):
        try:
            response = self._http.get(
                url,
                retry_status_codes=_WELLFOUND_RETRY_STATUS_CODES,
            )
        except Exception:
            logger.warning(
                "wellfound %s fetch failed for %s",
                kind,
                target,
                exc_info=True,
            )
            return None

        if response.status_code == 429:
            logger.warning(
                "wellfound rate limited: kind=%s status=429 target=%s",
                kind,
                target,
            )
            return None

        try:
            response.raise_for_status()
        except Exception:
            logger.warning(
                "wellfound %s fetch failed for %s",
                kind,
                target,
                exc_info=True,
            )
            return None
        return response

    def _fetch_job(self, job_id: str, detail_url: str, market_id: str) -> Job | None:
        response = self._get_response(
            detail_url,
            kind="detail",
            target=job_id,
        )
        if response is None:
            return None
        return _parse_detail(response.text, job_id, detail_url, market_id)


def _extract_job_links(html: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    links: list[tuple[str, str]] = []
    seen: set[str] = set()
    for anchor in soup.select('a[href^="/jobs/"]'):
        href = anchor["href"]
        match = _JOB_PATH_RE.match(urlparse(href).path)
        if not match:
            continue
        job_id = match.group(1)
        if job_id in seen:
            continue
        seen.add(job_id)
        links.append((job_id, urljoin(_BASE_URL, href)))
    return links


def _parse_detail(html: str, job_id: str, detail_url: str, market_id: str) -> Job:
    soup = BeautifulSoup(html, "html.parser")

    title_tag = soup.find("title")
    raw_title = title_tag.get_text(" ", strip=True) if title_tag else ""
    raw_title = raw_title.removesuffix(_TITLE_SUFFIX).strip()

    company = ""
    location = ""
    if " at " in raw_title and " • " in raw_title:
        _, rest = raw_title.split(" at ", 1)
        company_part, location_part = rest.split(" • ", 1)
        company = company_part.strip()
        location = location_part.strip()

    h1 = soup.find("h1")
    title = h1.get_text(" ", strip=True) if h1 is not None else raw_title.split(" at ", 1)[0].strip()

    body = soup.find("body")
    description = " ".join(body.get_text(" ", strip=True).split()) if body else ""

    return Job(
        source="wellfound",
        source_job_id=job_id,
        title=title,
        company=company,
        location=location,
        url=detail_url,
        description=description,
        remote=_parse_work_mode(description),
        market_hint=market_id,
        source_page_html=html,
    )


def _parse_work_mode(text: str) -> bool | None:
    match = _WORK_MODE_RE.search(text)
    if not match:
        return None
    return _WORK_MODES[match.group(1)]
