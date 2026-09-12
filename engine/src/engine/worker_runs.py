"""Every ingestion worker invocation, recorded from start to finish (issue #258).

A Render cron worker that woke, found its queue empty and exited used to leave
only a log line, so a worker that had stopped being invoked and a worker with
nothing to do looked the same. `WorkerRun` writes a `job_hunter_worker_runs`
row before the drain starts, updates it after every batch and when the drain
ends, and `report_worker_health` reads `job_hunter_worker_health()` afterwards
so that an unhealthy worker turns the invocation red instead of scrolling out
of a log (AGENTS.md rule 5).

Recording never raises. Telemetry that cannot be written must not also cost
the drain its work, and a run that was never recorded still surfaces: the next
health read reports its worker missing.

Like the stages it records, this module imports nothing user-scoped.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

logger = logging.getLogger(__name__)

#: The scheduled ingestion workers, as `job_hunter_worker_runs.worker` and
#: `job_hunter_worker_schedules.worker` name them. #259 adds recover_posting.
WORKERS = ("crawl_source", "extract_facets", "recheck_freshness", "recover_posting")

#: Why a drain stops on its own. Anything else is recorded as an error.
STOP_REASONS = ("queue_empty", "limit", "time_budget")


class _ConnectionLease(Protocol):
    def connection(self): ...


class _Drain(Protocol):
    """What every stage drain reports: `CrawlDrain`, `ExtractFacetsDrain`, `FreshnessDrain`."""

    claimed: int
    outcomes: Any
    stopped_because: str
    queue_delays: Any


class WorkerRun:
    """One invocation of one ingestion worker, as a row that outlives the process.

    `start()` before the drain, `heartbeat(drain)` after each batch (pass it as
    the drain's `on_batch`), then exactly one of `finish(drain)` or
    `fail(error)`. A process killed in between never calls either, and its row
    stays unfinished on purpose: `job_hunter_worker_health` reports it once
    its heartbeat is older than `stale_after_seconds`.

    `stale_after_seconds` must be the drain's visibility timeout. A batch that
    outlives it has already been handed to another worker, so a run that has
    not heartbeated for that long is dead or broken by the queue's own
    definition.
    """

    def __init__(
        self,
        database: _ConnectionLease,
        worker: str,
        *,
        stale_after_seconds: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if worker not in WORKERS:
            raise ValueError(f"unknown ingestion worker: {worker!r}")
        if stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")
        self._database = database
        self._worker = worker
        self._stale_after_seconds = stale_after_seconds
        self._clock = clock
        self._started: float | None = None
        #: The row's id, or None when the start could not be recorded. Stages
        #: that write their own rows (the crawl ledger) link them to it.
        self.id: str | None = None

    def start(self) -> None:
        self._started = self._clock()
        try:
            with self._database.connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "insert into public.job_hunter_worker_runs "
                        "(worker, stale_after_seconds) values (%s, %s) "
                        "returning id",
                        (self._worker, self._stale_after_seconds),
                    )
                    row = cursor.fetchone()
            self.id = str(row[0])
        except Exception:
            logger.exception(
                "could not record the start of a %s run; worker health will "
                "report this worker missing",
                self._worker,
            )

    def heartbeat(self, drain: _Drain) -> None:
        self._update(
            "heartbeat_at = now(), claimed = %s, outcomes = %s::jsonb, "
            "queue_delay_total_ms = %s, queue_delay_max_ms = %s",
            _progress(drain),
            action="heartbeat",
        )

    def finish(self, drain: _Drain) -> None:
        if drain.stopped_because in STOP_REASONS:
            stop_reason, error = drain.stopped_because, ""
        else:
            stop_reason = "error"
            error = f"drain stopped without a known reason: {drain.stopped_because!r}"
        self._update(
            "heartbeat_at = now(), claimed = %s, outcomes = %s::jsonb, "
            "queue_delay_total_ms = %s, queue_delay_max_ms = %s, "
            "finished_at = now(), stop_reason = %s, elapsed_ms = %s, error = %s",
            _progress(drain) + (stop_reason, self._elapsed_ms(), error),
            action="finish",
        )

    def fail(self, error: BaseException) -> None:
        """Finish the run as an error. Its progress is what the last heartbeat saw."""
        self._update(
            "heartbeat_at = now(), finished_at = now(), stop_reason = 'error', "
            "elapsed_ms = %s, error = %s",
            (self._elapsed_ms(), f"{type(error).__name__}: {error}"[:2000]),
            action="failure",
        )

    def _elapsed_ms(self) -> int:
        if self._started is None:
            return 0
        return max(0, int((self._clock() - self._started) * 1000))

    def _update(self, assignments: str, params: tuple, *, action: str) -> None:
        if self.id is None:
            return
        try:
            with self._database.connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"update public.job_hunter_worker_runs set {assignments} "
                        "where id = %s",
                        params + (self.id,),
                    )
        except Exception:
            logger.exception(
                "could not record the %s of %s run %s", action, self._worker, self.id
            )


def _progress(drain: _Drain) -> tuple:
    return (
        drain.claimed,
        json.dumps(dict(drain.outcomes)),
        drain.queue_delays.total_ms,
        drain.queue_delays.max_ms,
    )


@dataclass(frozen=True)
class WorkerHealth:
    worker: str
    status: str
    detail: str

    @property
    def healthy(self) -> bool:
        return self.status == "ok"


def read_worker_health(database: _ConnectionLease) -> list[WorkerHealth]:
    with database.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "select worker, status, detail from public.job_hunter_worker_health()"
            )
            rows = cursor.fetchall()
    return [WorkerHealth(worker, status, detail) for worker, status, detail in rows]


def report_worker_health(database: _ConnectionLease) -> bool:
    """Log every worker's health. False when any is unhealthy, or when it cannot be read.

    Health that cannot be read is not reported as healthy: that would be the
    quiet gap this module exists to close.
    """
    try:
        health = read_worker_health(database)
    except Exception:
        logger.exception("ingestion_health: worker health could not be read")
        return False
    if not health:
        logger.error(
            "ingestion_health: job_hunter_worker_schedules is empty, so no "
            "worker's health can be judged"
        )
        return False
    healthy = True
    for row in health:
        if row.healthy:
            logger.info("ingestion_health: worker=%s status=ok", row.worker)
        else:
            healthy = False
            logger.error(
                "ingestion_health: worker=%s status=%s %s",
                row.worker,
                row.status,
                row.detail,
            )
    return healthy
