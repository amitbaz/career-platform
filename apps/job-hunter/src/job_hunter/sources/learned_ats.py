"""Scan learned ATS boards through their native adapters with per-board isolation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from job_hunter.ats_registry import select_ats_boards
from job_hunter.models import Job
from job_hunter.store import JobStore

from .ashby import AshbySource
from .base import is_stale_board_error, logger
from .greenhouse import GreenhouseSource
from .lever import LeverSource


def utc_now() -> datetime:
    """Return the current timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


_ATS_SOURCE_TYPES = {
    "ashby": AshbySource,
    "lever": LeverSource,
    "greenhouse": GreenhouseSource,
}


class _HealthTrackingHttp:
    """Expose an ATS request failure even when its adapter fails open."""

    def __init__(self, http) -> None:
        self._http = http
        self.error: Exception | None = None

    def get_json(self, url: str, **kwargs):
        try:
            return self._http.get_json(url, **kwargs)
        except Exception as exc:
            self.error = exc
            raise


@dataclass(slots=True)
class LearnedAtsStats:
    boards_scanned: int = 0
    boards_successful: int = 0
    boards_failed: int = 0
    jobs_raw: int = 0


class LearnedAtsSource:
    """Scan due learned ATS boards through their native adapters."""

    def __init__(
        self,
        store: JobStore,
        http,
        *,
        limit: int,
        market_order: list[str],
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._store = store
        self._http = http
        self._limit = limit
        self._market_order = market_order
        self._now = now
        self.stats = LearnedAtsStats()

    def discover(self) -> list[Job]:
        """Return jobs from due learned ATS boards, isolating per-board failures."""
        checked_at = self._now()
        entries = select_ats_boards(
            self._store.list_due_ats_boards(checked_at),
            self._market_order,
            self._limit,
            checked_at,
        )
        discovered: list[Job] = []
        for entry in entries:
            source_type = _ATS_SOURCE_TYPES.get(entry.provider)
            if source_type is None:
                continue
            self.stats.boards_scanned += 1
            try:
                jobs = self._scan_board(source_type, entry.board_identifier)
            except Exception as exc:
                permanent = is_stale_board_error(exc)
                # The adapter already logged the diagnostic for this exact
                # failure (a full traceback for an unexpected error, a
                # compact line for an expected 404) — logging it again here
                # would duplicate that, so this is a compact health-state
                # summary only, never exc_info=True.
                logger.info(
                    "learned ATS board scan failed for %s:%s (%s)",
                    entry.provider,
                    entry.board_identifier,
                    "404, stale board"
                    if permanent
                    else f"unexpected {type(exc).__name__}, see adapter's own warning log",
                )
                self.stats.boards_failed += 1
                try:
                    self._store.record_ats_scan_failure(
                        entry.provider,
                        entry.board_identifier,
                        checked_at,
                        permanent=permanent,
                    )
                except Exception:
                    logger.warning(
                        "learned ATS failure health write failed for %s:%s",
                        entry.provider,
                        entry.board_identifier,
                        exc_info=True,
                    )
                continue

            self.stats.boards_successful += 1
            self.stats.jobs_raw += len(jobs)
            discovered.extend(jobs)
            try:
                self._store.record_ats_scan_success(
                    entry.provider, entry.board_identifier, checked_at, len(jobs)
                )
            except Exception:
                logger.warning(
                    "learned ATS success health write failed for %s:%s",
                    entry.provider,
                    entry.board_identifier,
                    exc_info=True,
                )
        return discovered

    def _scan_board(self, source_type, board_identifier: str) -> list[Job]:
        tracked_http = _HealthTrackingHttp(self._http)
        jobs = source_type(board_identifier, tracked_http).discover()
        if tracked_http.error is not None:
            raise tracked_http.error
        return jobs
