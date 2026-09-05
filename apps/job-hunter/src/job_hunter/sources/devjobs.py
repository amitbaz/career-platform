from __future__ import annotations

import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from job_hunter.models import Job

from .base import logger

_LISTING_URL = "https://www.devjobs.co.il/jobs-grid"
_DETAIL_URL = "https://www.devjobs.co.il/job-details/{job_id}"
_CATEGORIES = ["Frontend", "Full Stack"]
_WORK_MODES = {
    "Remote": True,
    "On-site": False,
    "Hybrid": False,
}


class DevJobsSource:
    """Discover Israel-market postings from devjobs.co.il listing/detail pages."""

    def __init__(self, http, *, max_jobs_per_category: int = 30) -> None:
        self._http = http
        self._max_jobs_per_category = max_jobs_per_category

    def discover(self) -> list[Job]:
        jobs: list[Job] = []
        seen_job_ids: set[str] = set()
        for category in _CATEGORIES:
            try:
                response = self._http.get(
                    _LISTING_URL, params={"developerTypes": category}
                )
                response.raise_for_status()
            except Exception:
                logger.warning(
                    "devjobs listing failed for category %s", category, exc_info=True
                )
                continue

            job_ids = _extract_job_ids(response.text)
            new_job_ids = [job_id for job_id in job_ids if job_id not in seen_job_ids]
            for job_id in new_job_ids[: self._max_jobs_per_category]:
                seen_job_ids.add(job_id)
                job = self._fetch_job(job_id)
                if job is not None:
                    jobs.append(job)
        return jobs

    def _fetch_job(self, job_id: str) -> Job | None:
        detail_url = _DETAIL_URL.format(job_id=job_id)
        try:
            response = self._http.get(detail_url)
            response.raise_for_status()
        except Exception:
            logger.warning("devjobs detail fetch failed for id %s", job_id, exc_info=True)
            return None

        return _parse_detail(response.text, job_id, detail_url)


def _extract_job_ids(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    ids: list[str] = []
    seen: set[str] = set()
    for anchor in soup.select('a[href^="/job-details/"]'):
        path = urlparse(anchor["href"]).path
        match = re.fullmatch(r"/job-details/(\d+)", path)
        if not match:
            continue
        job_id = match.group(1)
        if job_id in seen:
            continue
        seen.add(job_id)
        ids.append(job_id)
    return ids


def _parse_detail(html: str, job_id: str, detail_url: str) -> Job | None:
    soup = BeautifulSoup(html, "html.parser")
    title_tag = soup.find("title")
    if title_tag is None:
        logger.warning("devjobs detail missing title tag for id %s", job_id)
        return None

    raw = title_tag.get_text(" ", strip=True).removesuffix(" | DevJobs")
    parts = [part.strip() for part in raw.rsplit(" - ", 2)]
    if len(parts) != 3 or not all(parts):
        logger.warning("devjobs detail title did not split into 3 parts for id %s", job_id)
        return None
    job_title, company, location = parts

    body = soup.find("body")
    description = " ".join(body.get_text(" ", strip=True).split()) if body else ""

    remote = _parse_work_mode(description)

    return Job(
        source="devjobs",
        source_job_id=job_id,
        title=job_title,
        company=company,
        location=location,
        url=detail_url,
        description=description,
        remote=remote,
        market_hint="israel_remote",
    )


def _parse_work_mode(text: str) -> bool | None:
    match = re.search(r"Job Type\s+(Remote|On-site|Hybrid)", text)
    if not match:
        return None
    return _WORK_MODES[match.group(1)]
