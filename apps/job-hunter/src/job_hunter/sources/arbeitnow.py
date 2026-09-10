from __future__ import annotations

from collections.abc import Iterator

from job_hunter.models import Job

from .base import logger, strip_html

_URL = "https://www.arbeitnow.com/api/job-board-api"


class ArbeitnowSource:

    # One board, one URL -- paginated from the same URL where it pages at
    # all -- so a 304 answers for the whole source (issue #184).
    crawl_is_one_resource = True
    source_label = "arbeitnow"

    def __init__(self, http, max_pages: int = 2) -> None:
        self._http = http
        self._max_pages = max_pages

    def discover(self) -> Iterator[Job]:
        """Yield each page's jobs before requesting the page after it."""
        url = _URL
        page = 0
        while url and page < self._max_pages:
            try:
                data = self._http.get_json(url)
            except Exception:
                logger.warning("arbeitnow discovery failed", exc_info=True)
                return

            for item in data.get("data", []):
                yield Job(
                    source="arbeitnow",
                    source_job_id=item.get("slug"),
                    title=item.get("title", ""),
                    company=item.get("company_name", ""),
                    location=item.get("location", ""),
                    url=item.get("url", ""),
                    description=strip_html(item.get("description", "")),
                    remote=item.get("remote"),
                )

            url = (data.get("links") or {}).get("next")
            page += 1
