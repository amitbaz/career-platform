from __future__ import annotations

from job_hunter.models import Job
from job_hunter.normalize import canonicalize_url

from .base import logger, strip_html

_URL = "https://jobicy.com/api/v2/remote-jobs"


class JobicySource:
    def __init__(self, http, max_pages: int = 1) -> None:
        self._http = http
        self._max_pages = max_pages

    def discover(self) -> list[Job]:
        # Jobicy's v2 feed returns the current public catalogue in one
        # response and rejects pagination parameters. Keep ``max_pages`` in
        # the constructor for a stable source interface, but do not send it
        # to the API or manufacture duplicate requests.
        if self._max_pages <= 0:
            return []

        try:
            data = self._http.get_json(_URL)
        except Exception:
            logger.warning("jobicy discovery failed", exc_info=True)
            return []

        jobs: list[Job] = []
        for item in data.get("jobs", []) if isinstance(data, dict) else []:
            job = self._to_job(item)
            if job is not None:
                jobs.append(job)
        return jobs

    def _to_job(self, item) -> Job | None:
        if not isinstance(item, dict):
            return None

        job_id = item.get("id")
        title = (item.get("jobTitle") or "").strip()
        company = (item.get("companyName") or "").strip()
        url = canonicalize_url((item.get("url") or "").strip())

        if job_id is None or not title or not company or not url:
            return None

        return Job(
            source="jobicy",
            source_job_id=str(job_id),
            title=title,
            company=company,
            location=(item.get("jobGeo") or "").strip(),
            url=url,
            description=strip_html(item.get("jobDescription", "")),
            remote=True,
        )
