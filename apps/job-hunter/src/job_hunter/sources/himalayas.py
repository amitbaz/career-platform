from __future__ import annotations

from job_hunter.models import Job
from job_hunter.normalize import canonicalize_url

from .base import logger, strip_html

_URL = "https://himalayas.app/jobs/api"


class HimalayasSource:
    def __init__(self, http, max_pages: int = 2) -> None:
        self._http = http
        self._max_pages = max_pages

    def discover(self) -> list[Job]:
        jobs: list[Job] = []
        cursor: str | None = None

        for page in range(self._max_pages):
            params = {"cursor": cursor} if cursor else None
            try:
                data = self._http.get_json(_URL, params=params) if params else self._http.get_json(_URL)
            except Exception:
                logger.warning("himalayas discovery failed", exc_info=True)
                break

            for item in data.get("jobs", []):
                job = self._to_job(item)
                if job is not None:
                    jobs.append(job)

            cursor = data.get("nextCursor")
            if not cursor:
                break

        return jobs

    def _to_job(self, item) -> Job | None:
        if not isinstance(item, dict):
            return None

        source_job_id = (item.get("guid") or "").strip()
        title = (item.get("title") or "").strip()
        company = (item.get("companyName") or "").strip()
        url = canonicalize_url((item.get("applicationLink") or "").strip())

        if not source_job_id or not title or not company or not url:
            return None

        locations = item.get("locationRestrictions") or []
        if isinstance(locations, list):
            location = ", ".join(str(value).strip() for value in locations if str(value).strip())
        else:
            location = str(locations).strip()

        return Job(
            source="himalayas",
            source_job_id=source_job_id,
            title=title,
            company=company,
            location=location,
            url=url,
            description=strip_html(item.get("description", "")),
            remote=True,
        )
