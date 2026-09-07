from __future__ import annotations

import re

from job_hunter.models import Job
from job_hunter.postgres_store import PostgresJobStore

_LINKEDIN_HIRING_TITLE = re.compile(
    r"^(?P<company>.+?) hiring (?P<title>.+?) in (?P<location>.+?) \| LinkedIn$"
)


def _linkedin_page_title_metadata(
    source_platform: str, title: str, company: str, location: str
) -> tuple[str, str, str]:
    """Split LinkedIn's exact public hiring-title format without guessing."""
    if source_platform.lower() != "linkedin":
        return title, company, location
    match = _LINKEDIN_HIRING_TITLE.fullmatch(title.strip())
    if match is None:
        return title, company, location
    return (
        match.group("title"),
        company or match.group("company"),
        location or match.group("location"),
    )


class GmailStagedSource:
    """Expose staged Gmail candidates to the normal discovery pipeline."""

    source_label = "gmail"

    def __init__(self, store: PostgresJobStore) -> None:
        self._store = store

    def discover(self) -> list[Job]:
        jobs: list[Job] = []
        for row in self._store.list_eligible_inbound_jobs():
            title, company, location = _linkedin_page_title_metadata(
                row["source_platform"] or "",
                row["title"],
                row["company"],
                row["location"],
            )
            jobs.append(
                Job(
                    source=f"gmail:{row['source_platform'] or 'unknown'}",
                    source_job_id=row["source_candidate_key"],
                    title=title,
                    company=company,
                    location=location,
                    url=row["url"],
                    description=row["description"],
                    remote=None if row["remote"] is None else bool(row["remote"]),
                )
            )
        return jobs
