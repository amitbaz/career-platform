from __future__ import annotations

from job_hunter.models import Job

from .base import logger, strip_html

_URL = "https://remotive.com/api/remote-jobs"


class RemotiveSource:
    def __init__(self, http, query: str | None = None) -> None:
        self._http = http
        self._query = query

    def discover(self) -> list[Job]:
        params = {"search": self._query} if self._query else None
        try:
            data = self._http.get_json(_URL, params=params)
        except Exception:
            logger.warning("remotive discovery failed", exc_info=True)
            return []

        jobs = []
        for item in data.get("jobs", []):
            job_id = item.get("id")
            jobs.append(
                Job(
                    source="remotive",
                    source_job_id=str(job_id) if job_id is not None else None,
                    title=item.get("title", ""),
                    company=item.get("company_name", ""),
                    location=item.get("candidate_required_location", ""),
                    url=item.get("url", ""),
                    description=strip_html(item.get("description", "")),
                    remote=True,
                )
            )
        return jobs
