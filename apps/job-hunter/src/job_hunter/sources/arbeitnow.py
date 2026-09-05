from __future__ import annotations

from job_hunter.models import Job

from .base import logger, strip_html

_URL = "https://www.arbeitnow.com/api/job-board-api"


class ArbeitnowSource:
    def __init__(self, http, max_pages: int = 2) -> None:
        self._http = http
        self._max_pages = max_pages

    def discover(self) -> list[Job]:
        jobs: list[Job] = []
        url = _URL
        page = 0
        while url and page < self._max_pages:
            try:
                data = self._http.get_json(url)
            except Exception:
                logger.warning("arbeitnow discovery failed", exc_info=True)
                break

            for item in data.get("data", []):
                jobs.append(
                    Job(
                        source="arbeitnow",
                        source_job_id=item.get("slug"),
                        title=item.get("title", ""),
                        company=item.get("company_name", ""),
                        location=item.get("location", ""),
                        url=item.get("url", ""),
                        description=strip_html(item.get("description", "")),
                        remote=item.get("remote"),
                    )
                )

            url = (data.get("links") or {}).get("next")
            page += 1
        return jobs
