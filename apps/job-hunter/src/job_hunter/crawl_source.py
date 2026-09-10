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
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .http import NOT_MODIFIED, Validators
from .normalize import job_fingerprint
from .stage_queue import PermanentStageFailure, QueueMessage, Stage

logger = logging.getLogger(__name__)


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
    ) -> None:
        self._database = database
        self._build_source = build_source
        self._persist = persist
        self._probe = probe
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
        source_key = self._source_key(message)
        started = time.monotonic()
        requests_before = getattr(self._http, "request_count", 0) if self._http else 0

        self._register_target(source_key)
        validators = self._read_cursor(source_key)
        source = self._build_source(source_key)

        if self._probe is not None and self._probe(source, validators) is NOT_MODIFIED:
            outcome = CrawlOutcome(
                source_key=source_key,
                outcome="not_modified",
                requests=self._requests_since(requests_before),
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
            self._record(outcome)
            return outcome

        try:
            jobs = list(source.discover())
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
            outcome="fetched",
            fetched=len(jobs),
            new_to_corpus=getattr(batch, "newly_discovered", 0) or 0,
            changed=len(fresh),
            unchanged_by_hash=unchanged,
            requests=self._requests_since(requests_before),
            elapsed_ms=int((time.monotonic() - started) * 1000),
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
        with self._database.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "select fingerprint, description_hash "
                    "from public.job_hunter_postings "
                    "where fingerprint = any(%s)",
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
                        "(source_key, finished_at, outcome, fetched, "
                        " new_to_corpus, changed, unchanged_by_hash, "
                        " requests, elapsed_ms, error) "
                        "values (%s, now(), %s, %s, %s, %s, %s, %s, %s, %s)",
                        (
                            outcome.source_key,
                            outcome.outcome,
                            outcome.fetched,
                            outcome.new_to_corpus,
                            outcome.changed,
                            outcome.unchanged_by_hash,
                            outcome.requests,
                            outcome.elapsed_ms,
                            outcome.error,
                        ),
                    )
        except Exception:
            logger.exception(
                "could not record the crawl of %s; its cadence will not move",
                outcome.source_key,
            )

    @staticmethod
    def _source_key(message: QueueMessage) -> str:
        # The payload key is "crawl_key", not "source_key": it carries the
        # fine string build_source() answers to, which is a different key
        # space from job_hunter_sources.source_key (issue #184's defect).
        # This method's own name still says source_key because everywhere
        # else in this file -- CrawlOutcome, job_hunter_source_crawls,
        # job_hunter_source_cursors -- already uses that name for the same
        # fine string; only the wire format changes here.
        if message.stage is not Stage.CRAWL_SOURCE:
            raise PermanentStageFailure("crawl_source received the wrong stage")
        if set(message.payload) != {"crawl_key"}:
            raise PermanentStageFailure(
                "crawl_source payload must contain only crawl_key"
            )
        source_key = message.payload.get("crawl_key")
        if not isinstance(source_key, str) or not source_key:
            raise PermanentStageFailure("crawl_source crawl_key must be a string")
        return source_key
