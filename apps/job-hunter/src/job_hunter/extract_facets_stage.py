"""The privileged, platform-funded ``extract_facets`` stage (issue #185).

Objective extraction becomes a queue consumer instead of a phase inside a
run. Postings needing facets -- never read, or read at a description that
has since changed -- are enqueued as a side effect of every job persist
(`PostgresJobStore._enqueue_needing_facets_for_job_ids`, called from
`upsert_job`, `upsert_logical_job` and `upsert_logical_jobs`) and drained
here at the rate the platform allowance can sustain. Extraction
remains the only stage that spends provider quota, so the platform key, its
pacing and its fail-closed rule (issue #128) live here and nowhere else: this
module never sees a user, only a posting id, and there is no argument
anything per-user could arrive through -- the same guarantee `facets.py`
makes about `extract_facets` itself.

Failure handling follows the queue's common vocabulary (`stage_queue.py`):

* Unparseable output (`FacetExtractionError`) says nothing usable about the
  posting, so it dead-letters immediately (`PermanentStageFailure`) rather
  than spending another call on the same non-answer.
* A vanished posting (merged away, or never real) also dead-letters
  immediately -- there is nothing left to read.
* An exhausted platform allowance, a paused provider, or a missing
  credential are none of the above: the posting was never read and nothing
  was spent, so the message returns to the queue without counting an attempt
  (`QuotaExhausted`), and a later run drains it.
* Anything else (a timeout, a transient provider 5xx) is an ordinary
  transient failure and retries with backoff -- `StageRunner`'s default for
  an exception that is not one of the above.
"""

from __future__ import annotations

import json
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Protocol

from job_hunter.ai import (
    AIQuotaPaused,
    AITemporaryCapacity,
    CredentialUnavailable,
    PlatformAllowanceExhausted,
)
from job_hunter.facets import FacetExtractionError, PostingFacts, extract_facets
from job_hunter.models import JobFacets
from job_hunter.postgres_stage_queue import PostgresStageQueue
from job_hunter.stage_queue import (
    DeferredToALaterRun,
    PermanentStageFailure,
    QueueDelays,
    QueueMessage,
    QuotaExhausted,
    Stage,
    StageRunner,
    TransientStageFailure,
    utc_now,
)

if TYPE_CHECKING:
    from job_hunter.ai import AIProvider

#: How long a paced-out message waits before this stage is asked again, for
#: the refusals that carry no provider-stated retry-after of their own
#: (an exhausted daily allowance, an active pause, no platform credential).
#: Chosen to roughly match how often a run drains this queue, not any
#: provider signal.
_ALLOWANCE_RETRY_DELAY_SECONDS = 15 * 60

#: How long a message deferred because this run already read its posting waits
#: before being offered again. Longer than the allowance delay because the
#: condition clears when the run ends, not when a provider window reopens.
_ALREADY_ATTEMPTED_RETRY_DELAY_SECONDS = 60 * 60

#: How long a claimed extraction stays invisible to other workers, and so how
#: long a worker run may go without a heartbeat before
#: `job_hunter_worker_health` reports it unfinished (#258).
VISIBILITY_TIMEOUT_SECONDS = 5 * 60


class _ConnectionLease(Protocol):
    def connection(self): ...


@dataclass(frozen=True)
class ExtractedFacets:
    posting_id: str
    facets: JobFacets


@dataclass(frozen=True)
class FacetsAlreadyCurrent:
    """The posting was read before this message was drained.

    A posting is enqueued once per job persist, and one crawl persists in
    three phases -- the raw listings, the unique jobs they dedupe to, and the
    canonical-resolution tail -- so the same advertisement is routinely
    enqueued several times before anything has read it. The run's inline pass
    then reads it once and the queue still holds the rest.

    Without this, each of those messages spent a platform call on an
    advertisement that already had current facets, which is the one cost the
    whole shared-extraction design exists to pay once (#125, #175). Draining
    them is still right -- the message has to leave the queue -- so this is a
    success that spent nothing, not a failure.
    """

    posting_id: str


@dataclass(frozen=True)
class FacetExtractionOutcome:
    """What draining one message from the queue did, for the run log.

    `parse_failure` is set only when the provider answered with something
    `extract_facets` could not read -- the same bucket `pipeline.py`'s inline
    reads count as `extraction_parse_failures`. A vanished posting or a
    storage failure is a failure but not a parse failure.

    `skipped` is a message drained without a provider call, because the
    posting already had current facets. It is neither an attempt nor a
    failure: counting it as an attempt would report a run reading the same
    advertisement three times when it read it once.
    """

    failed: bool
    parse_failure: bool = False
    skipped: bool = False


class ExtractFacetsStage:
    """Read one posting's objective facts and store them, once, for everyone."""

    def __init__(
        self,
        database: _ConnectionLease,
        ai: "AIProvider",
        *,
        already_attempted: frozenset[str] = frozenset(),
    ) -> None:
        self._database = database
        self._ai = ai
        #: Postings the surrounding run has already spent a read on, whether
        #: that read succeeded or failed. A failed one leaves the posting
        #: uncurrent, so without this the message for it would be drained in
        #: the same run and buy the same non-answer again -- which is what
        #: this module's dead-letter-immediately rule exists to prevent.
        self._already_attempted = already_attempted

    def __call__(self, message: QueueMessage) -> ExtractedFacets | FacetsAlreadyCurrent:
        posting_id = self._posting_id(message)
        exists, posting = self._read_posting_needing_facets(posting_id)
        if not exists:
            raise PermanentStageFailure(f"posting_id={posting_id} no longer exists")
        if posting is None:
            # Read since this message was enqueued -- by the run's inline pass,
            # by an earlier message for the same posting, or by another user's
            # run entirely. Spending a call here would buy the same answer
            # twice against the platform key's one allowance, and the message
            # is finished: it asked for a read that has happened.
            return FacetsAlreadyCurrent(posting_id=posting_id)
        if posting_id in self._already_attempted:
            # Still uncurrent *and* this run already spent a read on it, so
            # that read failed. Checked after the currency question and not
            # before it, because a posting the run read *successfully* is
            # finished rather than deferred -- deferring it would leave a
            # completed message in the queue to consume the next run's drain
            # budget.
            raise DeferredToALaterRun(_ALREADY_ATTEMPTED_RETRY_DELAY_SECONDS)

        try:
            facets = extract_facets(posting, self._ai)
        except PlatformAllowanceExhausted as error:
            raise QuotaExhausted(_ALLOWANCE_RETRY_DELAY_SECONDS) from error
        except AITemporaryCapacity as error:
            raise QuotaExhausted(max(0, int(error.retry_after_seconds))) from error
        except AIQuotaPaused as error:
            raise QuotaExhausted(_ALLOWANCE_RETRY_DELAY_SECONDS) from error
        except CredentialUnavailable as error:
            raise QuotaExhausted(_ALLOWANCE_RETRY_DELAY_SECONDS) from error
        except FacetExtractionError as error:
            raise PermanentStageFailure(str(error)) from error

        self._write_facets(posting_id, facets)
        return ExtractedFacets(posting_id=posting_id, facets=facets)

    @staticmethod
    def _posting_id(message: QueueMessage) -> str:
        if message.stage is not Stage.EXTRACT_FACETS:
            raise PermanentStageFailure("extract_facets received the wrong stage")
        if set(message.payload) != {"posting_id"}:
            raise PermanentStageFailure(
                "extract_facets payload must contain only posting_id"
            )
        posting_id = message.payload.get("posting_id")
        if not isinstance(posting_id, str):
            raise PermanentStageFailure("extract_facets posting_id must be a UUID")
        try:
            return str(uuid.UUID(posting_id))
        except ValueError as error:
            raise PermanentStageFailure(
                "extract_facets posting_id must be a UUID"
            ) from error

    def _read_posting_needing_facets(
        self, posting_id: str
    ) -> tuple[bool, PostingFacts | None]:
        """`(the posting exists, what to read -- or None if already read)`.

        The two answers are separated because they mean opposite things to the
        caller: a posting that is gone is a dead letter, and a posting that has
        already been read is a message to delete quietly.

        Currency is decided by exactly the mechanism that gates re-extraction
        everywhere else -- the posting's `description_hash` against the hash
        the stored facets were read at -- so an edited advertisement is still
        re-read, and there is no second notion of "changed" here.

        A closed posting (#186) counts as needing nothing: it is never
        delivered, so a read of it is platform spend nobody will use. Its
        message is drained like any other that asked for work already done.
        """
        try:
            with self._database.connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "select p.title, p.company, p.location, p.remote, "
                        "       p.description, p.content_confidence, p.source, "
                        "       ((f.posting_id is not null "
                        "         and f.description_hash_at_extraction "
                        "             is not distinct from p.description_hash) "
                        "        or p.closed_at is not null) "
                        "  from public.job_hunter_postings p "
                        "  left join public.job_hunter_job_facets f "
                        "    on f.posting_id = p.id "
                        " where p.id = %s",
                        (posting_id,),
                    )
                    row = cursor.fetchone()
        except Exception as error:
            raise TransientStageFailure("reading the posting failed") from error
        if row is None:
            return False, None
        (
            title,
            company,
            location,
            remote,
            description,
            content_confidence,
            source,
            facets_current,
        ) = row
        if facets_current:
            return True, None
        return True, PostingFacts.from_posting_row(
            {
                "title": title,
                "company": company,
                "location": location,
                "remote": remote,
                "description": description,
                "content_confidence": content_confidence,
                "source": source,
            }
        )

    def _write_facets(self, posting_id: str, facets: JobFacets) -> None:
        compensation = facets.compensation
        try:
            with self._database.connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "insert into public.job_hunter_job_facets "
                        "(posting_id, description_hash_at_extraction, seniority, "
                        "remote_policy, relocation_policy, hiring_regions, stack, "
                        "compensation_disclosed, compensation_currency, "
                        "compensation_min, compensation_max, compensation_period, "
                        "requirements_json, source_supplied, model, extracted_at) "
                        "select %s, description_hash, %s, %s, %s, %s, %s, %s, %s, "
                        "%s, %s, %s, %s::jsonb, %s, %s, now() "
                        "from public.job_hunter_postings where id = %s "
                        "on conflict (posting_id) do update set "
                        "description_hash_at_extraction = excluded.description_hash_at_extraction, "
                        "seniority = excluded.seniority, "
                        "remote_policy = excluded.remote_policy, "
                        "relocation_policy = excluded.relocation_policy, "
                        "hiring_regions = excluded.hiring_regions, "
                        "stack = excluded.stack, "
                        "compensation_disclosed = excluded.compensation_disclosed, "
                        "compensation_currency = excluded.compensation_currency, "
                        "compensation_min = excluded.compensation_min, "
                        "compensation_max = excluded.compensation_max, "
                        "compensation_period = excluded.compensation_period, "
                        "requirements_json = excluded.requirements_json, "
                        "source_supplied = excluded.source_supplied, "
                        "model = excluded.model, "
                        "extracted_at = excluded.extracted_at",
                        (
                            posting_id,
                            facets.seniority,
                            facets.remote_policy,
                            facets.relocation_policy,
                            facets.hiring_regions,
                            facets.stack,
                            compensation.disclosed,
                            compensation.currency,
                            compensation.minimum,
                            compensation.maximum,
                            compensation.period,
                            _to_jsonb(facets.requirements),
                            facets.source_supplied,
                            facets.model,
                            posting_id,
                        ),
                    )
        except Exception as error:
            raise TransientStageFailure("storing facets failed") from error


def _to_jsonb(value: object) -> str:
    return json.dumps(value)


# The consumer ---------------------------------------------------------------------


@dataclass
class ExtractFacetsDrain:
    """What one drain of the extract_facets queue did, for its log line.

    Distinct from `PostgresJobStore.drain_extract_facets_queue`: that method
    exists for the monolith's inline pass, which tracks `already_attempted`
    across the same run's own reads. A standalone drain process never makes
    an inline read of its own, so that bookkeeping does not apply here -- this
    is a plain queue consumer, on its own schedule, like
    `recheck_freshness_stage.drain_recheck_freshness`.
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


def drain_extract_facets(
    database: Any,
    ai: "AIProvider",
    *,
    limit: int,
    batch_size: int = 25,
    time_budget_seconds: float = 20 * 60,
    clock: Callable[[], float] = time.monotonic,
    on_batch: Callable[[ExtractFacetsDrain], None] | None = None,
    now: Callable[[], datetime] = utc_now,
) -> ExtractFacetsDrain:
    """Drain up to `limit` due extractions, in batches, within a time budget.

    Its own process on its own schedule (`python -m job_hunter
    extract-facets`), sharing nothing with the daily digest or a crawl.
    `ai` is trusted to already be resolved to the platform key (#128) --
    this module never sees a user's credential, the same guarantee
    `ExtractFacetsStage` itself makes.

    `on_batch` is called with the drain after every batch, including the one
    that finds the queue empty (#258).
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    runner = StageRunner(
        PostgresStageQueue(database),
        visibility_timeout_seconds=VISIBILITY_TIMEOUT_SECONDS,
    )
    stage = ExtractFacetsStage(database, ai)
    drain = ExtractFacetsDrain()
    started = clock()

    while True:
        if drain.claimed >= limit:
            drain.stopped_because = "limit"
            break
        if clock() - started >= time_budget_seconds:
            drain.stopped_because = "time_budget"
            break
        seen = 0

        def handler(message: QueueMessage):
            nonlocal seen
            seen += 1
            drain.queue_delays.observe(message, now())
            try:
                result = stage(message)
            except QuotaExhausted:
                drain.outcomes["quota_exhausted"] += 1
                raise
            except DeferredToALaterRun:
                drain.outcomes["deferred"] += 1
                raise
            except Exception:
                drain.outcomes["failed"] += 1
                raise
            if isinstance(result, FacetsAlreadyCurrent):
                drain.outcomes["already_current"] += 1
            else:
                drain.outcomes["extracted"] += 1
            return result

        runner.run_once(
            Stage.EXTRACT_FACETS,
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
