"""The privileged, user-free ``crawl_source`` stage (issue #184).

One source, one message. A source that is rate-limited or failing stalls
only itself, because it is a message on a queue rather than a step in a
shared run, and its cadence backs off without touching any other source's.

Imports nothing user-scoped -- no store, no matching, no scoring, no
credentials -- so a worker holding only the privileged ingestion connection
can run it, exactly as `resolve_persist.py` can (issue #183, constraint C1).
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Protocol

from .http import NOT_MODIFIED, NotModifiedSignal, Validators
from .normalize import job_fingerprint
from .postgres_stage_queue import PostgresStageQueue
from .stage_queue import (
    PermanentStageFailure,
    QueueDelays,
    QueueMessage,
    QuotaExhausted,
    Stage,
    StageRunner,
    utc_now,
)

logger = logging.getLogger(__name__)

#: How long a claimed crawl stays invisible to other workers. It covers a
#: whole batch at the HTTP client's read budget, so a slow batch is not
#: redelivered while its worker is still on it -- and for the same reason it
#: is how long a worker run may go without a heartbeat before
#: `job_hunter_worker_health` reports it unfinished (#258).
VISIBILITY_TIMEOUT_SECONDS = 15 * 60

#: Why a crawl message was enqueued. `safety` is the one crawl
#: `job_hunter_enqueue_crawl` keeps in a window its evidence says to reduce,
#: so a change in the source's behaviour there is still observed (#258).
CRAWL_PURPOSES = ("scheduled", "safety")


class _ConnectionLease(Protocol):
    def connection(self): ...


@dataclass(frozen=True)
class CrawlOutcome:
    """What one crawl of one source produced, with no user dimension."""

    source_key: str
    outcome: str
    fetched: int = 0
    new_to_corpus: int = 0
    changed: int = 0
    unchanged_by_hash: int = 0
    requests: int = 0
    elapsed_ms: int = 0
    error: str = ""
    # How many of this crawl's postings joined an already-existing variant
    # group (#61), read off resolve_persist's PostingBatch. Written on every
    # crawl, including zero (AGENTS.md rule 5).
    joined_variant_group: int = 0
    # When the crawl began, when its message was enqueued and claimed, and why
    # it ran (#258). `started_at` is taken before the crawl rather than at
    # insert time; `claimed_at - enqueued_at` is the queue-to-worker delay.
    started_at: datetime | None = None
    enqueued_at: datetime | None = None
    claimed_at: datetime | None = None
    purpose: str = "scheduled"


def description_hash(description: str) -> str:
    """Hash a description the way `job_hunter_upsert_posting` does.

    That function computes `encode(sha256(convert_to(description, 'UTF8')),
    'hex')`. Computing the same value here is the whole point of the
    short-circuit: the SQL hash is produced *during* the upsert, which is far
    too late to prevent the work the upsert is doing.
    """
    return hashlib.sha256((description or "").encode("utf-8")).hexdigest()


def _is_rate_limited(error: BaseException) -> bool:
    response = getattr(error, "response", None)
    return getattr(response, "status_code", None) == 429


class CrawlSourceStage:
    """Crawl one source, drop what has not changed, stage the rest."""

    def __init__(
        self,
        database: _ConnectionLease,
        *,
        build_source: Callable[[str], Any],
        persist: Callable[[list], Any],
        probe: Callable[[Any, Validators], Any] | None = None,
        http: Any | None = None,
        worker_run_id: str | None = None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._database = database
        self._build_source = build_source
        self._persist = persist
        self._probe = probe
        # The `job_hunter_worker_runs` row this crawl belongs to, when the
        # worker could record one (#258).
        self._worker_run_id = worker_run_id
        self._now = now
        # The cost half of the yield figure. `HttpClient` counts every attempt
        # it makes, retries included, so bracketing the drain attributes the
        # requests to this source the way `discovery.collect_candidates`
        # already does for the per-run statistics. Optional only because the
        # unit tests construct the stage without one; a real crawl always has
        # a client, and a `requests` column that is always zero would read as
        # measured while telling nobody anything.
        self._http = http

    def _requests_since(self, before: int) -> int:
        if self._http is None:
            return 0
        return max(0, getattr(self._http, "request_count", 0) - before)

    def __call__(self, message: QueueMessage) -> CrawlOutcome:
        source_key, purpose = self._parse_payload(message)
        timing = {
            "started_at": self._now(),
            "enqueued_at": message.enqueued_at,
            "claimed_at": message.claimed_at,
            "purpose": purpose,
        }
        started = time.monotonic()
        requests_before = getattr(self._http, "request_count", 0) if self._http else 0
        try:
            return self._crawl(source_key, timing, started, requests_before)
        # Exception, not BaseException, for the reason given at the
        # discover() clause below: a killed worker must propagate uncaught.
        except Exception as error:
            # A failure anywhere else in the attempt -- the cursor read,
            # building the source, the probe, the unchanged-hash check, the
            # persist -- used to reach the runner with no crawl row, so the
            # window evidence silently lost this source's failure, cost and
            # time. Record it, then re-raise so the queue retries the message
            # exactly as it did before.
            outcome = CrawlOutcome(
                source_key=source_key,
                **timing,
                outcome="failed",
                requests=self._requests_since(requests_before),
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error=str(error)[:500],
            )
            logger.warning(
                "crawl_source %s failed outside the source: %s", source_key, error
            )
            self._record(outcome)
            raise

    def _crawl(
        self,
        source_key: str,
        timing: dict[str, Any],
        started: float,
        requests_before: int,
    ) -> CrawlOutcome:
        self._register_target(source_key)
        validators = self._read_cursor(source_key)
        source = self._build_source(source_key)

        if self._probe is not None and self._probe(source, validators) is NOT_MODIFIED:
            outcome = CrawlOutcome(
                source_key=source_key,
                **timing,
                outcome="not_modified",
                requests=self._requests_since(requests_before),
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
            self._record(outcome)
            return outcome

        try:
            jobs = list(source.discover())
        # Defensive, and currently unreachable: this stage does not open a
        # conditional scope -- it probes separately, above -- so nothing here
        # can raise the signal today. It stays because the cost of being
        # wrong is asymmetric. NotModifiedSignal is a BaseException, so the
        # `except Exception` below cannot catch it; the moment this stage
        # gains a scope, or `_build_source` hands back a source crawled under
        # one, an uncaught signal would escape `__call__`, kill the worker,
        # and leave the message unacknowledged and endlessly redelivered.
        # Must stay ahead of the `except Exception` clause.
        except NotModifiedSignal:
            outcome = CrawlOutcome(
                source_key=source_key,
                **timing,
                outcome="not_modified",
                requests=self._requests_since(requests_before),
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
            self._record(outcome)
            return outcome
        # Exception, not BaseException: stage_queue.StageRunner.run_once
        # depends on a killed or interrupted worker propagating
        # KeyboardInterrupt/SystemExit uncaught, so it never acknowledges its
        # claim and Postgres' visibility timeout redelivers the message.
        # Catching BaseException here would convert that into a recorded
        # "failed" outcome, let __call__ return normally, and let the
        # runner complete the message -- losing the crawl instead of
        # retrying it, and demoting this source's cadence for a reason that
        # has nothing to do with the source. Do not widen this back.
        except Exception as error:
            outcome = CrawlOutcome(
                source_key=source_key,
                **timing,
                outcome="rate_limited" if _is_rate_limited(error) else "failed",
                requests=self._requests_since(requests_before),
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error=str(error)[:500],
            )
            logger.warning(
                "crawl_source %s ended %s: %s", source_key, outcome.outcome, error
            )
            self._record(outcome)
            return outcome

        fresh, unchanged = self._drop_unchanged(jobs)
        # `persist` returns resolve_persist's PostingBatch, whose
        # `newly_discovered` is the only place the count of postings the
        # corpus did not already hold exists. The scheduler bands on exactly
        # that number, so it has to survive the round trip rather than being
        # inferred from `changed` -- a source re-advertising the same job with
        # an edited description is changed but not new, and a source that only
        # ever does that should not earn a faster band.
        batch = self._persist(fresh) if fresh else None

        outcome = CrawlOutcome(
            source_key=source_key,
            **timing,
            outcome="fetched",
            fetched=len(jobs),
            new_to_corpus=getattr(batch, "newly_discovered", 0) or 0,
            changed=len(fresh),
            unchanged_by_hash=unchanged,
            requests=self._requests_since(requests_before),
            elapsed_ms=int((time.monotonic() - started) * 1000),
            joined_variant_group=getattr(batch, "joined_existing_group", 0) or 0,
        )
        self._record(outcome)
        return outcome

    def _drop_unchanged(self, jobs: list) -> tuple[list, int]:
        """Keep only the listings whose description the corpus does not have.

        This is acceptance criterion 3, and it has to happen here rather than
        in SQL: the description hash is computed inside
        `job_hunter_upsert_posting`, by which point the row has already been
        staged, copied and merged.
        """
        if not jobs:
            return [], 0
        by_fingerprint = {job_fingerprint(job): job for job in jobs}
        known = self._known_hashes(list(by_fingerprint))
        fresh = []
        unchanged = 0
        for fingerprint, job in by_fingerprint.items():
            if known.get(fingerprint) == description_hash(job.description):
                unchanged += 1
                continue
            fresh.append(job)
        return fresh, unchanged

    def _known_hashes(self, fingerprints: list[str]) -> dict[str, str]:
        """The stored hash of every open posting among `fingerprints`.

        A closed posting (#186) is left out on purpose, so its listing is
        never "unchanged": its employer's own board listing it again is the
        evidence that reopens it, and that happens in the merge this
        short-circuit would otherwise skip.
        """
        with self._database.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "select fingerprint, description_hash "
                    "from public.job_hunter_postings "
                    "where fingerprint = any(%s) and closed_at is null",
                    (fingerprints,),
                )
                rows = cursor.fetchall()
        return {fingerprint: hashed for fingerprint, hashed in rows}

    def _register_target(self, source_key: str) -> None:
        """Register this fine crawl key the first time it is ever crawled.

        `job_hunter_reschedule_sources` loops over `job_hunter_crawl_targets`,
        not `job_hunter_sources` -- the registry is keyed by the coarse
        posting source, and `build_source` answers only to the fine key on
        this message. `on conflict do nothing` is what makes
        `first_seen_at` mean what its name says: it is written once, on the
        crawl that first proves this key real, and never touched again.
        Same privileged connection as `_record`; nothing user-scoped enters
        this stage.
        """
        try:
            with self._database.connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "insert into public.job_hunter_crawl_targets "
                        "(crawl_key) values (%s) "
                        "on conflict (crawl_key) do nothing",
                        (source_key,),
                    )
        except Exception:
            logger.exception(
                "could not register crawl target %s; its schedule may not "
                "pick it up",
                source_key,
            )

    def _read_cursor(self, source_key: str) -> Validators:
        with self._database.connection() as connection:
            with connection.cursor() as cursor:
                # high_water_at is read but not carried into Validators: it
                # serves the since-style sources recheck_freshness resumes
                # from, not the HTTP validators this stage's probe uses.
                cursor.execute(
                    "select etag, last_modified, high_water_at "
                    "from public.job_hunter_source_cursors where source_key = %s",
                    (source_key,),
                )
                row = cursor.fetchone()
        if row is None:
            return Validators()
        return Validators(etag=row[0] or "", last_modified=row[1] or "")

    def _record(self, outcome: CrawlOutcome) -> None:
        """Write the crawl row unconditionally, including the empty ones.

        An empty result has to carry its reason: a stalled source and a
        schedule that never fired must not both present as "nothing new
        today". Failing to record is logged and never raised -- the crawl
        already happened, and losing its telemetry must not also lose its
        output.
        """
        try:
            with self._database.connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "insert into public.job_hunter_source_crawls "
                        "(source_key, started_at, finished_at, outcome, fetched, "
                        " new_to_corpus, changed, unchanged_by_hash, "
                        " requests, elapsed_ms, error, purpose, enqueued_at, "
                        " claimed_at, worker_run_id, joined_variant_group) "
                        "values (%s, coalesce(%s, now()), now(), %s, %s, %s, %s, "
                        "%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (
                            outcome.source_key,
                            outcome.started_at,
                            outcome.outcome,
                            outcome.fetched,
                            outcome.new_to_corpus,
                            outcome.changed,
                            outcome.unchanged_by_hash,
                            outcome.requests,
                            outcome.elapsed_ms,
                            outcome.error,
                            outcome.purpose,
                            outcome.enqueued_at,
                            outcome.claimed_at,
                            self._worker_run_id,
                            outcome.joined_variant_group,
                        ),
                    )
        except Exception:
            logger.exception(
                "could not record the crawl of %s; its cadence will not move",
                outcome.source_key,
            )

    @staticmethod
    def _parse_payload(message: QueueMessage) -> tuple[str, str]:
        """`(source_key, purpose)` from a crawl message.

        The payload key is "crawl_key", not "source_key": it carries the fine
        string build_source() answers to, which is a different key space from
        job_hunter_sources.source_key (issue #184's defect). Everywhere else
        in this file -- CrawlOutcome, job_hunter_source_crawls,
        job_hunter_source_cursors -- the same fine string is called
        source_key; only the wire format differs.

        `purpose` was added by #258 and is optional, so a message enqueued
        before it existed is still a scheduled crawl.
        """
        if message.stage is not Stage.CRAWL_SOURCE:
            raise PermanentStageFailure("crawl_source received the wrong stage")
        if set(message.payload) not in ({"crawl_key"}, {"crawl_key", "purpose"}):
            raise PermanentStageFailure(
                "crawl_source payload must contain only crawl_key and, "
                "optionally, purpose"
            )
        source_key = message.payload.get("crawl_key")
        if not isinstance(source_key, str) or not source_key:
            raise PermanentStageFailure("crawl_source crawl_key must be a string")
        purpose = message.payload.get("purpose", "scheduled")
        if purpose not in CRAWL_PURPOSES:
            raise PermanentStageFailure(
                "crawl_source purpose must be scheduled or safety"
            )
        return source_key, purpose


# The consumer ---------------------------------------------------------------------


@dataclass
class CrawlDrain:
    """What one drain of the crawl_source queue did, for its log line.

    `stopped_because` says why an empty drain was empty -- a queue with
    nothing due, and a drain cut off by its own time budget, both count zero
    claimed, and AGENTS.md rule 5 is that an empty result must carry its
    reason.
    """

    claimed: int = 0
    outcomes: Counter = field(default_factory=Counter)
    stopped_because: str = ""
    queue_delays: QueueDelays = field(default_factory=QueueDelays)

    def summary(self) -> str:
        counts = " ".join(
            f"{name}={count}" for name, count in sorted(self.outcomes.items())
        )
        return (
            f"claimed={self.claimed} {counts} stopped_because={self.stopped_because}"
        )


def drain_crawl_source(
    database: _ConnectionLease,
    http: Any,
    *,
    build_source: Callable[[str], Any],
    persist: Callable[[list], Any],
    limit: int,
    batch_size: int = 10,
    time_budget_seconds: float = 20 * 60,
    clock: Callable[[], float] = time.monotonic,
    on_batch: Callable[[CrawlDrain], None] | None = None,
    worker_run_id: str | None = None,
    now: Callable[[], datetime] = utc_now,
) -> CrawlDrain:
    """Drain up to `limit` due crawls, in batches, within a time budget.

    Its own process on its own schedule (`python -m engine crawl-source`),
    sharing nothing with any other stage, so a slow or rate-limited source
    cannot delay them. One `CrawlSourceStage` serves the whole drain, so
    `build_source`/`persist` and whatever they close over (a `Settings`, a
    `PostgresJobStore`, a Brave budget) are built once per process and reused
    across every message it claims.

    Modeled directly on `recheck_freshness_stage.drain_recheck_freshness`:
    failures take the queue's common path (`stage_queue.StageRunner`), the
    budget is checked between batches and never mid-request, and a batch's
    visibility timeout covers the whole HTTP read budget so a slow batch is
    not redelivered to a second worker while the first is still on it.

    `on_batch` is called with the drain after every batch, including the one
    that finds the queue empty; `worker_run.WorkerRun.heartbeat` is what the
    CLI passes. `worker_run_id` links each crawl row to that run (#258).
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    runner = StageRunner(
        PostgresStageQueue(database),
        visibility_timeout_seconds=VISIBILITY_TIMEOUT_SECONDS,
    )
    stage = CrawlSourceStage(
        database,
        build_source=build_source,
        persist=persist,
        http=http,
        worker_run_id=worker_run_id,
        now=now,
    )
    drain = CrawlDrain()
    started = clock()

    while True:
        if drain.claimed >= limit:
            drain.stopped_because = "limit"
            break
        if clock() - started >= time_budget_seconds:
            drain.stopped_because = "time_budget"
            break
        seen = 0

        def handler(message: QueueMessage) -> CrawlOutcome:
            nonlocal seen
            seen += 1
            drain.queue_delays.observe(message)
            try:
                outcome = stage(message)
            except QuotaExhausted:
                drain.outcomes["rate_limited"] += 1
                raise
            except Exception:
                drain.outcomes["failed"] += 1
                raise
            drain.outcomes[outcome.outcome] += 1
            return outcome

        runner.run_once(
            Stage.CRAWL_SOURCE,
            handler,
            batch_size=min(batch_size, limit - drain.claimed),
        )
        drain.claimed += seen
        if on_batch is not None:
            on_batch(drain)
        if seen == 0:
            drain.stopped_because = "queue_empty"
            break
    return drain
