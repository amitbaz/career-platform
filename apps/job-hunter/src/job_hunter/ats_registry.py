"""Learn ATS boards from discovered jobs and prioritize them for scanning."""

from __future__ import annotations

from datetime import datetime, timedelta

from job_hunter.canonical import parse_supported_ats_url
from job_hunter.models import AtsReference, AtsRegistryEntry, Job
from job_hunter.store import JobStore

_RECENTLY_ELIGIBLE_WINDOW = timedelta(days=30)


def extract_ats_reference(job: Job) -> AtsReference | None:
    """Return the ATS reference implied by a job's URLs or populated ATS fields.

    Precedence: populated ATS fields, then canonical_url, then url, then
    original_url.
    """
    if job.ats_provider and job.ats_board:
        return AtsReference(
            provider=job.ats_provider, board=job.ats_board, job_id=job.ats_job_id
        )
    for url in (job.canonical_url, job.url, job.original_url):
        if not url:
            continue
        reference = parse_supported_ats_url(url)
        if reference is not None:
            return reference
    return None


def harvest_ats_board(
    store: JobStore, job: Job, market_hint: str | None = None
) -> bool:
    """Learn the ATS board a job references, if it points at a supported one."""
    reference = extract_ats_reference(job)
    if reference is None:
        return False
    return store.upsert_ats_board(
        provider=reference.provider,
        board_identifier=reference.board,
        company_name=job.company,
        market_hint=market_hint or job.market_hint or job.market_id or "",
    )


def select_ats_boards(
    entries: list[AtsRegistryEntry],
    market_order: list[str],
    limit: int,
    now: datetime,
) -> list[AtsRegistryEntry]:
    """Deterministically rank due ATS boards and return the top `limit`.

    Priority: boards eligible within the last 30 days, then configured
    market order, then never-checked boards before checked ones, then oldest
    `last_checked_at` first, with `(provider, board_identifier)` as the final
    tie-breaker.
    """

    def sort_key(entry: AtsRegistryEntry) -> tuple:
        recently_eligible = (
            entry.last_eligible_at is not None
            and now - datetime.fromisoformat(entry.last_eligible_at)
            <= _RECENTLY_ELIGIBLE_WINDOW
        )
        try:
            market_rank = market_order.index(entry.market_hint)
        except ValueError:
            market_rank = len(market_order)
        return (
            0 if recently_eligible else 1,
            market_rank,
            0 if entry.last_checked_at is None else 1,
            entry.last_checked_at or "",
            entry.provider,
            entry.board_identifier,
        )

    return sorted(entries, key=sort_key)[:limit]
