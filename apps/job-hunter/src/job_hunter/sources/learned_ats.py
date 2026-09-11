"""Scan learned ATS boards through their native adapters with per-board isolation."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone

from job_hunter.aggregator_detection import evaluate_board
from job_hunter.ats_registry import select_ats_boards
from job_hunter.models import Job
from job_hunter.normalize import ats_board_key
from job_hunter.postgres_store import PostgresJobStore

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
    boards_rejected: int = 0
    boards_recovered: int = 0


class LearnedAtsSource:
    """Scan due learned ATS boards through their native adapters."""

    source_label = "learned_ats"

    def __init__(
        self,
        store: PostgresJobStore,
        http,
        *,
        limit: int,
        market_order: list[str],
        now: Callable[[], datetime] = utc_now,
        denylist: frozenset[str] = frozenset(),
        allowlist: frozenset[str] = frozenset(),
    ) -> None:
        self._store = store
        self._http = http
        self._limit = limit
        self._market_order = market_order
        self._now = now
        self._denylist = denylist
        self._allowlist = allowlist
        self.stats = LearnedAtsStats()

    def discover(self) -> Iterator[Job]:
        """Yield jobs board by board, isolating per-board failures.

        A whole board stays the unit of work rather than a single posting:
        aggregator detection judges a board on all of its descriptions at
        once, and the health write records the board's job count. Both need
        the board's scan to have finished, so the board's jobs are gathered
        before any of them is handed out -- but a caller that stops between
        boards keeps every job the earlier boards produced.
        """
        checked_at = self._now()
        # Recover before reading the due list, so a board the operator
        # un-rejected is scanned in the same run that recovered it --
        # editing the user's search profile is the whole recovery procedure.
        self._recover_allowlisted_boards()
        due = self._store.list_due_ats_boards(checked_at)

        # Reject denylisted boards before the limit is applied, so a board
        # that is never going to be scanned cannot consume one of the
        # `limit` slots and displace a real board.
        #
        # This is one operator's policy, not evidence about the board, so
        # (since #203) it is never persisted to the shared registry -- only
        # this run's local exclusion, re-applied fresh from config every
        # run. `_reject_board` below writes a rejection to the shared board
        # table and must never be called from this path.
        remaining = []
        denylisted_boards = []
        for entry in due:
            board_key = ats_board_key(entry.provider, entry.board_identifier)
            if board_key in self._denylist and board_key not in self._allowlist:
                self.stats.boards_rejected += 1
                logger.info(
                    "learned ATS board excluded by learned_ats_denylist: %s "
                    "(not written to the shared registry)",
                    board_key,
                )
                denylisted_boards.append((entry.provider, entry.board_identifier))
            else:
                remaining.append(entry)

        # #226: the shared board's `last_checked_at` is never set on this
        # path, so without this every denylisted board sits in
        # `select_ats_boards`' never-checked tier forever and can outrank a
        # genuinely new board on the tie-break. The stamp is this user's own
        # registry row, not the shared board -- see
        # `record_ats_board_denylist_skips`' docstring. One batched call for
        # the whole run, not one per board (#151's pattern for this table).
        if denylisted_boards:
            try:
                self._store.record_ats_board_denylist_skips(
                    denylisted_boards, checked_at
                )
            except Exception:
                logger.warning(
                    "learned ATS denylist-skip stamp failed for %d board(s)",
                    len(denylisted_boards),
                    exc_info=True,
                )

        entries = select_ats_boards(
            remaining, self._market_order, self._limit, checked_at
        )
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

            rejection = self._aggregator_rejection(entry, jobs)
            if rejection is not None:
                logger.info(
                    "learned ATS board rejected as an aggregator: %s:%s (%s)",
                    entry.provider,
                    entry.board_identifier,
                    rejection,
                )
                self._reject_board(entry, checked_at, rejection)
                continue

            # Counted as handed over, and the board's health written only
            # once every posting has been. A caller can stop between the
            # postings of a board -- that is what the per-source time budget
            # does -- and writing either before the drain would stamp
            # `last_checked_at` on a board that was never finished, demoting
            # it in the oldest-first ranking, and record a job count nothing
            # received. On a cut-off the drain never completes, so the board
            # stays due and is scanned again next run, which is correct.
            for job in jobs:
                self.stats.jobs_raw += 1
                yield job

            self.stats.boards_successful += 1
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

    def _scan_board(self, source_type, board_identifier: str) -> list[Job]:
        """Return one board's jobs, re-raising the request failure it hid.

        The adapter fails open, so its request error only surfaces through
        the tracking client; the scan therefore has to be drained here
        before that check means anything.
        """
        tracked_http = _HealthTrackingHttp(self._http)
        jobs = list(source_type(board_identifier, tracked_http).discover())
        if tracked_http.error is not None:
            raise tracked_http.error
        return jobs

    def _aggregator_rejection(self, entry, jobs: list[Job]) -> str | None:
        """Return why this board should be rejected, or None to keep it.

        Fails open: a posting's description can be None (an ATS returning an
        explicit null body), and a detection bug must never cost the whole
        run's discovery, so anything unexpected here keeps the board.
        """
        try:
            verdict = evaluate_board([job.description or "" for job in jobs])
        except Exception:
            logger.warning(
                "learned ATS aggregator detection failed for %s:%s; keeping the board",
                entry.provider,
                entry.board_identifier,
                exc_info=True,
            )
            return None
        if verdict.rejected:
            board_key = ats_board_key(entry.provider, entry.board_identifier)
            if board_key in self._allowlist:
                # Detection still runs, and the overridden verdict is logged:
                # an override nobody can see is an override nobody can ever
                # show to be unnecessary.
                logger.info(
                    "learned ATS board kept by learned_ats_allowlist: %s "
                    "(despite %s)",
                    board_key,
                    verdict.reason,
                )
                return None
            return verdict.reason
        logger.debug(
            "learned ATS board kept: %s:%s (%s)",
            entry.provider,
            entry.board_identifier,
            "; ".join(f"{e.name}: {e.reason}" for e in verdict.evidence),
        )
        return None

    def _recover_allowlisted_boards(self) -> None:
        """Clear the rejection on every allowlisted board that carries one.

        The stored reason is the only record of what the operator overrode
        and the healing write destroys it, so each recovery's reason is
        logged from the pre-clear registry snapshot.
        """
        if not self._allowlist:
            return
        for entry in self._store.list_rejected_ats_boards():
            board_key = ats_board_key(entry.provider, entry.board_identifier)
            if board_key not in self._allowlist:
                continue
            try:
                self._store.clear_ats_board_rejection(
                    entry.provider, entry.board_identifier
                )
            except Exception:
                logger.warning(
                    "learned ATS rejection recovery failed for %s",
                    board_key,
                    exc_info=True,
                )
                continue
            self.stats.boards_recovered += 1
            logger.info(
                "learned ATS board recovered by learned_ats_allowlist: %s "
                "(cleared rejection: %s)",
                board_key,
                entry.rejected_reason,
            )

    def _reject_board(self, entry, checked_at: datetime, reason: str) -> None:
        """Persist an aggregator-detection rejection to the shared registry.

        Aggregator-detection evidence generalizes across users, so (unlike
        the config denylist, which never reaches the store since #203) it
        is written to the shared `job_hunter_ats_boards` table and read
        back by every user's next discovery run.
        """
        self.stats.boards_rejected += 1
        try:
            self._store.reject_ats_board(
                entry.provider, entry.board_identifier, reason, checked_at
            )
        except Exception:
            logger.warning(
                "learned ATS rejection write failed for %s:%s",
                entry.provider,
                entry.board_identifier,
                exc_info=True,
            )
