from __future__ import annotations

from job_hunter.models import Job
from job_hunter.store import JobStore


class GmailStagedSource:
    """Expose staged Gmail candidates to the normal discovery pipeline."""

    def __init__(self, store: JobStore) -> None:
        self._store = store

    def discover(self) -> list[Job]:
        return [
            Job(
                source=f"gmail:{row['source_platform'] or 'unknown'}",
                source_job_id=row["source_candidate_key"],
                title=row["title"],
                company=row["company"],
                location=row["location"],
                url=row["url"],
                description=row["description"],
                remote=None if row["remote"] is None else bool(row["remote"]),
            )
            for row in self._store.list_unmaterialized_inbound_jobs()
        ]
