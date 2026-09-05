from __future__ import annotations

from job_hunter.models import Job
from job_hunter.normalize import canonicalize_url

from .base import is_stale_board_error, logger, strip_html

_URL_TEMPLATE = "https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true"


class AshbySource:
    def __init__(self, board: str, http) -> None:
        self._board = board
        self._http = http

    def discover(self) -> list[Job]:
        try:
            data = self._http.get_json(_URL_TEMPLATE.format(board=self._board))
        except Exception as exc:
            if is_stale_board_error(exc):
                logger.info("ashby board not found (404) for board %s", self._board)
            else:
                logger.warning(
                    "ashby discovery failed for board %s", self._board, exc_info=True
                )
            return []

        jobs = []
        for item in data.get("jobs", []):
            job_id = item.get("id")
            description = item.get("descriptionPlain") or item.get("descriptionHtml", "")
            jobs.append(
                Job(
                    source="ashby",
                    source_job_id=str(job_id) if job_id is not None else None,
                    title=item.get("title", ""),
                    company=self._board,
                    location=item.get("location", ""),
                    url=item.get("jobUrl", ""),
                    description=strip_html(description),
                    remote=item.get("isRemote"),
                )
            )
        return jobs


def fetch_description(board: str, target_url: str, http) -> str | None:
    """Return the full description for the job at target_url on this board.

    Reuses the same board-listing endpoint discover() already hits, since
    it returns every job's full description in one response — no separate
    per-job endpoint needed.
    """
    data = http.get_json(_URL_TEMPLATE.format(board=board))
    target = canonicalize_url(target_url)
    for item in data.get("jobs", []):
        if canonicalize_url(item.get("jobUrl", "")) == target:
            description = item.get("descriptionPlain") or item.get("descriptionHtml", "")
            return strip_html(description) or None
    return None
