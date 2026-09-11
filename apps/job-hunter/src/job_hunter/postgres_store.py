"""Postgres-backed persistence layer for the job hunter bot.

Replaces the SQLite-backed `JobStore` in `store.py`, porting it table by
table over the course of issue #70's task series onto `SupabaseClient`
and `Supabase`'s row-level security. This module currently holds only the
class shell: the constructor and the context-manager/`close()` surface
existing call sites already depend on. Every read/write method arrives in
a later task, built on the row-mapping helpers in `store_mapping.py`.

There is no `read_only` mode. The SQLite original had one so the webhook
could open a snapshot without creating tables; there is no snapshot
concept against Postgres, and `__init__` does no schema work at all --
migrations own the schema now (see `supabase/migrations/`).
"""

from __future__ import annotations

import json
import logging
import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, TypeVar

from job_hunter.ats_hosts import SUPPORTED_ATS_HOSTS
from job_hunter.canonical import parse_supported_ats_url
from job_hunter.extract_facets_stage import (
    ExtractFacetsStage,
    FacetExtractionOutcome,
    FacetsAlreadyCurrent,
)
from job_hunter.facets import FacetExtractionError
from job_hunter.gmail_models import AUTO_CONFIDENCE_THRESHOLD, ExtractedJob
from job_hunter.job_identity import normalize_company_name
from job_hunter.models import (
    AtsRegistryEntry,
    CandidateContextCacheEntry,
    CompanyFacets,
    Evaluation,
    Job,
    JobFacets,
    Material,
    NavigationSession,
    ProviderCredentials,
)
from job_hunter.normalize import job_fingerprint
from job_hunter.pg import IngestionDatabase
from job_hunter.postgres_stage_queue import PostgresStageQueue
from job_hunter.resolve_persist import PostingBatch, ResolvePersistStage
from job_hunter.search_profile import SearchProfile
from job_hunter.stage_queue import (
    DeferredToALaterRun,
    PermanentStageFailure,
    QuotaExhausted,
    Stage,
    StageRunner,
)
from job_hunter.store_mapping import (
    ats_entry_from_row,
    company_facets_from_row,
    evaluation_from_row,
    job_facets_from_row,
    job_from_row,
    material_from_row,
    navigation_session_from_row,
    posting_facts,
    to_iso,
    touch,
)
from job_hunter.supabase_client import SupabaseClient, SupabaseRequestError

if TYPE_CHECKING:
    from job_hunter.ai import AIProvider

logger = logging.getLogger(__name__)


class SharedWriteUnavailable(RuntimeError):
    """A shared-table write was attempted with no privileged connection.

    Since #179 the tables with no user dimension are writable only by the
    ingestion role, over the direct Postgres connection. A deployment
    without one is supported and does not crash: it skips ingestion and
    enrichment entirely and delivers from the postings it already has. This
    is raised when something tries to write anyway, because at that point
    the run has already spent the work that produced the row and the only
    honest outcome is a loud failure rather than a silently discarded write.
    """


# Postgres SQLSTATE for foreign_key_violation, which PostgREST reports in the
# body of a 409. A write against a job id that `merge_jobs` has already
# deleted fails with exactly this, and is the one case the store retries
# somewhere else rather than giving up -- see `_write_following_merges`.
_FOREIGN_KEY_VIOLATION = "23503"

# How long a company's facts are treated as current (issue #198). Company
# attributes change slowly -- an employer's industry and business model do not
# move at the cadence its job adverts do -- so this is a long interval and the
# only thing that invalidates a row. Six months is short enough that a company
# that raises a round or is acquired is re-read within a hiring cycle, and long
# enough that the extraction cost stays amortised over every posting the
# employer publishes in between, which is the whole argument for the table.
COMPANY_FACET_REFRESH = timedelta(days=180)

# The tie-break `pending_delivery_job_ids`'s SQL function and the two
# "latest row" reads below share: newest `evaluated_at`/`generated_at` wins,
# with `created_at` then `id` as a deterministic (if practically unreachable
# -- see the two methods' docstrings) fallback.
_LATEST_EVALUATION_ORDER = "evaluated_at.desc,created_at.desc,id.desc"
_LATEST_MATERIAL_ORDER = "generated_at.desc,created_at.desc,id.desc"

# What the advertisement itself says, and where it is read from (issues #177,
# #178).
#
# A job row is a membership of a posting and carries no fact about the
# advertisement any more, so every one of these columns is selected from the
# posting and there is no second copy to fall back to.
#
# The embed is a PostgREST resource embedding over the `posting_id` foreign
# key, so it costs no extra round trip: the posting arrives inside the row
# it belongs to. It is `to-one`, so PostgREST returns an object; `posting_id`
# is `not null` since #178, so it is never null.
#
# `url`, `canonical_url` and the `ats_*` triple are in the list now. They were
# left off while they still lived on the job row -- a merged job row was the
# only row that had seen every posting behind it, so its `url` was the only
# resolved one. Merging is a posting-level decision since #176: the survivor
# carries the folded identity columns and the resolved link, which is what
# makes reading them here the same answer the job row used to give.
_ADVERTISEMENT_COLUMNS = (
    "source,title,company,location,description,source_job_id,remote,"
    "content_confidence,url,canonical_url,ats_provider,ats_board,ats_job_id"
)
_MEMBERSHIP_COLUMNS = "market_id"
_POSTING_FACT_EMBED = f"posting:job_hunter_postings({_ADVERTISEMENT_COLUMNS})"

# The pair that decides whether work done against a description is still
# current: `needs_evaluation` compares both against what the evaluation
# recorded. They describe the advertisement, so they are read from the
# posting -- the same text the pipeline is handed by `get_job`, which is
# what makes "the evaluation is current" mean the same thing on both sides.
_DESCRIPTION_STATE_COLUMNS = "description_hash,content_confidence"
_DESCRIPTION_STATE_EMBED = f"posting:job_hunter_postings({_DESCRIPTION_STATE_COLUMNS})"

# The columns `list_jobs_for_matching` hands the Gmail matcher, which reads
# them flat. Everything but the timestamps is the advertisement's.
_MATCHING_POSTING_COLUMNS = "source_job_id,url,company,title"

# Every id list that travels in a query-string filter (`id=in.(...)`,
# `message_id=in.(...)`) is chunked at this many ids per request. A message
# id/uuid is short, but a few hundred ids strung into one query string can
# approach the ~8 KB URL limit typical of proxies/load balancers in front of
# PostgREST, which fails as a 414 rather than on any condition the code
# checks. 200 ids keeps every request's URL comfortably under that regardless
# of id length. Use this -- not _ID_ARRAY_CHUNK_SIZE -- whenever the ids end
# up in `params`, however large the body-carried batches around it are.
_URL_FILTER_CHUNK_SIZE = 200

# upsert_logical_jobs sends this many jobs per request. Payload size is not
# the binding limit -- two server-side budgets are, and both are per-call:
#   * Supabase's default `statement_timeout` on the `authenticated` role
#     (8 s). One `job_hunter_upsert_jobs` call is a *single* statement that
#     runs a full identity resolution (canonical URL, ATS triple, normalized
#     company/title/location, fingerprint, plus any merges) for every job in
#     the chunk, inside one transaction.
#   * `HttpClient._timeout`'s 25 s read timeout.
# A chunk that exceeds either comes back as 500/504, which _RETRY_STATUS_CODES
# treats as retryable, so an oversized chunk burns three attempts and their
# backoff before falling back to per-job replay -- turning a "faster" run into
# a slower one. 100 keeps a chunk's server-side work well inside 8 s.
# The tradeoff: a 19,000-job run (raw and unique passes together) now spends
# about 318 upsert round trips instead of the 64 that 500 would give, against
# a ~70,000-request per-job baseline. Trading ~250 requests for headroom
# against the timeouts is the right side of that trade.
_JOB_UPSERT_CHUNK_SIZE = 100

# Bulk id arrays go in the request body, not the query string, so the 200-id
# URL-length limit that constrains _URL_FILTER_CHUNK_SIZE does not apply.
_ID_ARRAY_CHUNK_SIZE = 1000

# How many consecutive failed job-upsert chunks upsert_logical_jobs tolerates
# before it stops falling back to per-job replay and raises instead. Three,
# because the two failure modes need separating and three is where they stop
# overlapping: a poison posting is a property of one row, so it fails one
# chunk and the next chunk succeeds -- three chunks failing back to back
# would need three independently poisoned rows to land in three adjacent
# chunks, which a batch built from arbitrary discovery order does not
# produce. A broken batch path (timeout, missing function, dead PostgREST)
# fails the first three immediately. Three also bounds the wasted work:
# 300 jobs replayed individually, plus at most three chunks' retry budget
# (~81 s each), before the run stops with a message that names the real
# problem.
_CONSECUTIVE_CHUNK_FAILURE_LIMIT = 3

# The staging columns `merge_posting_batch` COPYs into, in the order the rows
# it builds are written. Named here rather than inline so the COPY header and
# the row builder cannot drift apart -- COPY reports neither a wrong order nor
# a wrong width as anything but a type error somewhere down the batch.
_POSTING_STAGING_COLUMNS = (
    "batch_id",
    "ordinal",
    "fingerprint",
    "source",
    "source_job_id",
    "url",
    "canonical_url",
    "company",
    "title",
    "location",
    "remote",
    "description",
    "content_confidence",
    "ats_provider",
    "ats_board",
    "ats_job_id",
)

_T = TypeVar("_T")


def _chunked(items: list[_T], size: int) -> list[list[_T]]:
    """Split ``items`` into consecutive chunks of at most ``size`` elements."""
    return [items[i : i + size] for i in range(0, len(items), size)]


def _quoted_in_list(values: list[str]) -> str:
    """Render text values for a PostgREST ``in.(...)`` filter.

    The uuid lists elsewhere in this file interpolate bare, because a uuid
    cannot contain a comma or a space. A company identity can contain a space
    ("acme labs"), which an unquoted list would leave to PostgREST's own
    tokenizer, so each value is double-quoted. Embedded double quotes are
    doubled per PostgREST's escaping; `normalize_company_name` cannot produce
    one, and this does not rely on that staying true.
    """
    return ",".join('"' + value.replace('"', '""') + '"' for value in values)


def _is_legacy_poisoned_linkedin_job(company: str, title: str) -> bool:
    """Translates `gmail_linkedin_cleanup.py`'s (deleted) same-named helper.

    A job is safe to release only if it is entirely blank or carries the
    known ``Sign in`` poison title left by historical LinkedIn login-page
    scraping.
    """
    if company.strip():
        return False
    normalized_title = title.strip().casefold()
    return normalized_title in {"", "sign in"}


# Translates store.py's `_SUPPORTED_ATS_PROVIDERS` and
# `_STALE_BOARD_DEACTIVATION_THRESHOLD`. Redefined here rather than imported
# because `store.py` is deleted at the end of this port.
_SUPPORTED_ATS_PROVIDERS = frozenset({"ashby", "greenhouse", "lever"})
_STALE_BOARD_DEACTIVATION_THRESHOLD = 3

# A company watch pauses on its third consecutive failure; an ATS board
# pauses on every failure. Both pause for the same fixed span.
_WATCH_PAUSE_THRESHOLD = 3
_HEALTH_PAUSE = timedelta(hours=24)


def _require_aware(now: datetime) -> datetime:
    """Return an aware datetime as UTC, or reject an ambiguous naive one.

    Translates store.py's `_normalize_utc`. `store_mapping.to_iso` treats a
    naive datetime as already being UTC, which is right for an instant the
    store stamped itself but wrong for one a caller supplied -- a caller's
    naive local time would be silently recorded as UTC. Every method that
    takes `now` from its caller passes it through here first.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(timezone.utc)


def _watch_endpoint_strength(
    careers_url: str, ats_provider: str | None, ats_identifier: str | None
) -> int:
    """Rank a watch endpoint: supported ATS > generic URL > company only."""
    if ats_provider in _SUPPORTED_ATS_PROVIDERS and ats_identifier:
        return 3
    if careers_url:
        return 2
    return 1


class PostgresJobStore:
    """Persistence layer backed by a user-scoped `SupabaseClient`.

    `__init__` stores the client and nothing else: no schema creation, no
    migration machinery, no lazy table setup. `close()`, `__enter__`, and
    `__exit__` are no-ops beyond `close()` itself -- kept only so call
    sites written against the SQLite store's context-manager usage don't
    need to change.
    """

    def __init__(
        self,
        client: SupabaseClient,
        ingestion: IngestionDatabase | None = None,
        *,
        stage_queue_names: Mapping[Stage, str] | None = None,
    ) -> None:
        self._client = client
        self._ingestion = ingestion
        self._stage_queue_names = stage_queue_names
        #: Postings this store has already asked for facet extraction on, so
        #: one crawl's three persist phases enqueue an advertisement once
        #: rather than three times. See `_enqueue_needing_facets`.
        self._enqueued_postings: set[str] = set()
        #: Postings this run has already spent a read on, successful or not.
        #: See `note_facet_read_attempt`.
        self._facet_read_attempts: set[str] = set()

    @property
    def client(self) -> SupabaseClient:
        """Expose the underlying `SupabaseClient`.

        `run_pipeline` needs this to build a `SearchUsageLedger`/
        `BraveRequestBudget` for Brave source-discovery -- see
        `build_brave_budget`'s docstring. Deriving the client from the store
        it is already given (rather than adding a separate
        `supabase_client` parameter to `run_pipeline` that every caller
        would have to remember to pass) is what keeps Brave from silently
        going dark again the way issue #70 task 12 left it.
        """
        return self._client

    def close(self) -> None:
        """Release the ingestion pool, if this store was given one.

        The PostgREST client holds nothing to release; a connection pool
        does, and the store is what owns it for the length of a run.
        """
        if self._ingestion is not None:
            self._ingestion.close()

    def __enter__(self) -> "PostgresJobStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ------------------------------------------------------------------
    # The privileged writer (#179)
    # ------------------------------------------------------------------

    @property
    def platform_ingestion(self):
        """The privileged lease, for platform tables with no store method.

        Exposed deliberately rather than reached for as `_ingestion`, so that
        `DryRunStore` can withhold it by name. A dry run that reached the raw
        lease would write platform rows while reporting that it wrote nothing.
        """
        return self._ingestion

    @property
    def can_write_shared_rows(self) -> bool:
        """Whether this store can write the tables that have no user.

        Since #179 `job_hunter_postings`, `job_hunter_job_facets`,
        `job_hunter_companies`, `job_hunter_ats_boards` and
        `job_hunter_posting_merges` are writable only by the privileged
        ingestion role. Callers use this to *skip* work rather than to attempt
        a write and handle the refusal: a run with no connection has nothing
        to ingest and nothing to enrich, and pretending otherwise would spend
        a crawl's worth of network -- and the platform key's allowance -- on
        rows the database will not accept.

        "Configured" is not enough to answer with, which is why this consults
        the pool as well. `IngestionDatabase` opens lazily and latches
        unreachable on its first failed lease, so a wrong or dead
        `SUPABASE_DB_URL` produces a store that would otherwise report itself
        able to write right up until the first write. Asked again between
        phases, this turns that into the same skip a missing DSN gets.
        """
        if self._ingestion is None:
            return False
        return not self._ingestion.unavailable

    def _shared_write(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple]:
        """Run one shared-table write over the direct connection.

        Every write to a table with no user dimension goes through here, so
        "which transport carried this write" has one answer and one place to
        read it. Returns whatever the statement returns, so a caller can use
        `returning`; a statement with no result set gives an empty list.

        Raises `SharedWriteUnavailable` when there is no connection. That is
        a programming error rather than a deployment one by the time it is
        reached: `can_write_shared_rows` is what a caller is expected to ask
        first, and this is what makes forgetting to ask fail loudly instead
        of half-writing a run.
        """
        if self._ingestion is None:
            raise SharedWriteUnavailable(
                "this run has no direct Postgres connection, so it cannot write "
                "shared rows; check store.can_write_shared_rows before ingesting "
                "or enriching"
            )
        with self._ingestion.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, params)
                if cursor.description is None:
                    return []
                return cursor.fetchall()

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------

    def _canonicalize_url(self, url: str) -> str:
        """Canonicalize a URL through the SQL authority, not Python's own.

        `job_hunter_canonicalize_url` is the sole authority for canonical
        URLs and identity keys (migration 202609060004): it disagrees with
        `normalize.canonicalize_url` both on percent-encoding (Python's
        `parse_qsl`/`urlencode` round trip turns `%20` into `+`; SQL leaves
        the raw text alone) and on tracking-param ordering (SQL sorts
        `"k=v"` strings under `collate "C"`; Python sorts `(k, v)` tuples).
        Every RPC-backed write (`job_hunter_upsert_job`) computes
        `canonical_url` and `job_sources.identity_key` with the SQL
        function, so a Python-side computation here would silently diverge
        from what's already stored -- a URL differing only in encoding
        would miss a job that exists, or `record_job_source` would write a
        second provenance row for an identity `job_hunter_upsert_job`
        already recorded under a differently-encoded key.
        """
        return self._client.rpc("job_hunter_canonicalize_url", {"p_url": url})[0]

    @staticmethod
    def _job_payload(job: Job) -> dict[str, Any]:
        """Build the jsonb payload `job_hunter_upsert_job` expects.

        Every recognised key is included, `match_mode` excepted -- callers
        add that themselves so the shared helper stays agnostic to which of
        the two upsert behaviours is being invoked. `fingerprint` is
        computed here, in Python, and never recomputed in SQL: see the
        migration's note on `job_hunter_upsert_job` for why.
        """
        return {
            "fingerprint": job_fingerprint(job),
            "source": job.source or "",
            "source_job_id": job.source_job_id,
            "url": job.url or "",
            "canonical_url": job.canonical_url or "",
            "company": job.company or "",
            "title": job.title or "",
            "location": job.location or "",
            "remote": job.remote,
            "description": job.description or "",
            "content_confidence": job.content_confidence or "",
            "ats_provider": job.ats_provider,
            "ats_board": job.ats_board,
            "ats_job_id": job.ats_job_id,
            "original_url": job.original_url or "",
        }

    def _upsert_job_rpc(self, payload: dict[str, Any]) -> tuple[str, bool, bool]:
        """Persist one job payload over the privileged connection (#179).

        `job_hunter_upsert_job` writes `job_hunter_postings` and may merge
        two of them, which is a write every other user sees, so it is
        reachable only by the ingestion role and takes the user it acts for
        as an argument rather than reading `auth.uid()`. The user is this
        store's own -- the store is per-user for everything else, and
        nothing here may write a row for anybody else.
        """
        rows = self._shared_write(
            "select id, is_new, description_changed "
            "from public.job_hunter_upsert_job(%s::jsonb, %s::uuid)",
            (json.dumps(payload), self._client.user_id),
        )
        job_id, is_new, description_changed = rows[0]
        return str(job_id), bool(is_new), bool(description_changed)

    def upsert_job(self, job: Job) -> tuple[str, bool, bool]:
        """Insert or update a job record, matched by fingerprint alone.

        Translates store.py:649-743. The narrow upsert never merges
        duplicates -- see `upsert_logical_job` for the identity-resolving
        path. `match_mode: "fingerprint"` selects that branch inside
        `job_hunter_upsert_job`; omitting it (as `upsert_logical_job` does)
        defaults to the merging 'logical' branch.
        """
        payload = self._job_payload(job)
        payload["match_mode"] = "fingerprint"
        job_id, is_new, description_changed = self._upsert_job_rpc(payload)
        self._enqueue_needing_facets_for_job_ids([job_id])
        return job_id, is_new, description_changed

    def upsert_logical_job(
        self, job: Job, *, posting_batch: PostingBatch | None = None
    ) -> tuple[str, bool, bool]:
        """Persist a source-independent logical job and its provenance.

        Translates store.py:744-905. Identity is resolved from strongest to
        weakest exact evidence (canonical URL, ATS triple, normalized
        company/title/location, fingerprint) and every duplicate found is
        merged into one survivor. The return shape matches `upsert_job`.

        `posting_batch` is the batch this job's advertisement was already
        merged in, where there was one (#182). Without it the call resolves
        its own posting, which is what a caller that does not stage a batch
        first -- the Gmail paths, the webhook -- gets. Since #179 there is
        no longer a "no direct connection" case here: without one there is
        no job upsert at all, because the posting it would write is a
        shared row.
        """
        payload = self._batch_job_payload(job, posting_batch)
        job_id, is_new, description_changed = self._upsert_job_rpc(payload)
        self._enqueue_needing_facets_for_job_ids([job_id])
        return job_id, is_new, description_changed

    def merge_posting_batch(self, jobs: list[Job]) -> PostingBatch:
        """Stage, enqueue, and consume one crawl batch of postings.

        The COPY and queue send commit together before the worker claims
        anything. A process killed after that point leaves a durable message;
        pgmq makes it visible to a later call after the visibility timeout.
        The bounded worker also drains older abandoned batches before this
        call falls back to per-listing persistence.

        Needs the direct Postgres connection: `COPY` and a set-based statement
        are the two things PostgREST cannot express, which is the whole reason
        ingestion holds one (#182). Without it -- no `SUPABASE_DB_URL`, or a
        connection that will not open -- this returns an empty batch, and the
        caller then lets `job_hunter_upsert_job` resolve each posting inside
        its own upsert, exactly as it did before. A failure is logged rather
        than raised for the same reason: a run that cannot take the fast path
        should still deliver, and the phase breakdown will show it did not.

        `resolve_persist` is idempotent at the posting boundary. If a worker is
        killed after the merge commits but before its queue acknowledgement,
        the replay sees an empty staging batch and completes harmlessly; the
        postings already produced remain the same.
        """
        if self._ingestion is None or not jobs:
            return PostingBatch()

        batch_id = str(uuid.uuid4())
        columns = ", ".join(_POSTING_STAGING_COLUMNS)
        queue = PostgresStageQueue(self._ingestion, self._stage_queue_names)
        try:
            with self._ingestion.connection() as connection:
                with connection.cursor() as cursor:
                    with cursor.copy(
                        f"copy public.job_hunter_posting_staging ({columns}) from stdin"
                    ) as copy:
                        for ordinal, job in enumerate(jobs):
                            copy.write_row(self._staging_row(batch_id, ordinal, job))
                    message_id = queue.enqueue(
                        Stage.RESOLVE_PERSIST,
                        {"batch_id": batch_id},
                        connection=connection,
                    )

            runner = StageRunner(queue, visibility_timeout_seconds=5 * 60)
            outcomes = runner.run_once(
                Stage.RESOLVE_PERSIST,
                ResolvePersistStage(self._ingestion),
                batch_size=100,
            )
        except Exception:
            logger.exception(
                "staged resolve_persist queue failed for %s listing(s); falling "
                "back to resolving each posting inside its own job upsert",
                len(jobs),
            )
            return PostingBatch()

        for outcome in outcomes:
            if outcome.message.message_id == message_id:
                posting_batch = outcome.result
                # crawl_source's own persist path never calls
                # upsert_logical_jobs (that writes job_hunter_jobs, a
                # per-user table this user-free stage must not touch), so
                # this is the only place a Render crawl-only deployment
                # enqueues extraction for what it just merged. Safe to call
                # unconditionally: _enqueue_needing_facets already filters to
                # postings actually missing current facets and dedupes
                # against this store's own _enqueued_postings set, so a
                # caller that also enqueues via upsert_logical_jobs (the
                # monolith, today) just no-ops here on the second call.
                self._enqueue_needing_facets(posting_batch.posting_ids.values())
                return posting_batch
        logger.warning(
            "resolve_persist batch remains queued; falling back to per-listing "
            "persistence for this run: batch_id=%s",
            batch_id,
        )
        return PostingBatch()

    def _enqueue_needing_facets_for_job_ids(self, job_ids: Iterable[str]) -> int:
        """Resolve `job_ids` to their postings and enqueue extraction (#185).

        Called from every job-persist path -- `upsert_job`,
        `upsert_logical_job`, and `upsert_logical_jobs` -- rather than only
        from the `merge_posting_batch` fast path. `job_hunter_upsert_job`
        resolves a posting internally whenever the caller has not already
        merged one, including both fallbacks `merge_posting_batch` itself
        takes (no direct connection, or the staged batch left queued), and
        does not return which posting it chose. Asking again here, once per
        persist call, is what keeps those fallbacks from being a second
        "postings that never get extracted" path -- there is no separate
        backfill step, so a posting nothing ever enqueues extraction for
        stays unread forever.
        """
        ids = sorted({job_id for job_id in job_ids if job_id})
        if not ids or self._ingestion is None:
            return 0
        posting_ids: set[str] = set()
        for chunk in _chunked(ids, _URL_FILTER_CHUNK_SIZE):
            try:
                rows = self._client.select(
                    "job_hunter_jobs",
                    params={"id": f"in.({','.join(chunk)})", "select": "posting_id"},
                )
            except Exception:
                logger.exception("could not resolve posting ids for facet enqueue")
                continue
            posting_ids.update(
                row["posting_id"] for row in rows if row.get("posting_id")
            )
        return self._enqueue_needing_facets(posting_ids)

    def _enqueue_needing_facets(self, posting_ids: Iterable[str]) -> int:
        """Enqueue every posting in `posting_ids` with no current facets (#185).

        A posting already current costs one join and no queue send, and a
        posting this store has already enqueued costs neither.

        That second filter matters more than it looks. One crawl persists in
        three phases -- the raw listings, the unique jobs they dedupe to, and
        the canonical-resolution tail -- and every one of them enqueues, so
        without it the same advertisement arrives on the queue three times
        before anything has read it once. The consumer skips a posting that
        has since been read, which handles the case where one of those
        messages succeeded; this handles the case where none of them has run
        yet, and the case the consumer cannot: an extraction that *failed*
        leaves the posting uncurrent, so its two siblings would each spend
        another call on the same non-answer -- exactly what
        `extract_facets_stage`'s dead-letter-immediately rule exists to
        prevent, defeated by there being three messages rather than one.

        The set lives for the length of this store, which is the length of a
        run. A posting whose description improves later in the same run is
        therefore not re-enqueued until the next one, which is the right
        trade: the alternative is paying to read an advertisement twice in
        one day.

        Failing to enqueue is logged but never raised: the caller's own
        outcome must not be held hostage to a queue send for work nobody is
        waiting on this run.
        """
        ids = sorted(
            {
                posting_id
                for posting_id in posting_ids
                if posting_id and posting_id not in self._enqueued_postings
            }
        )
        if not ids or self._ingestion is None:
            return 0
        queue = PostgresStageQueue(self._ingestion, self._stage_queue_names)
        enqueued = 0
        try:
            with self._ingestion.connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "select p.id from public.job_hunter_postings p "
                        "left join public.job_hunter_job_facets f "
                        "on f.posting_id = p.id "
                        # A closed posting (#186) is never delivered, so
                        # reading it would be platform spend nobody uses.
                        "where p.id = any(%s) and p.closed_at is null and ("
                        "f.posting_id is null or "
                        "f.description_hash_at_extraction is distinct from p.description_hash"
                        ")",
                        (ids,),
                    )
                    needing = [str(row[0]) for row in cursor.fetchall()]
                for posting_id in needing:
                    queue.enqueue(
                        Stage.EXTRACT_FACETS,
                        {"posting_id": posting_id},
                        connection=connection,
                    )
                    # Recorded per send rather than for the whole batch up
                    # front: a send that throws must leave its posting
                    # unmarked, or the crawl's later phases skip it and the
                    # advertisement is never enqueued at all this run.
                    self._enqueued_postings.add(posting_id)
                    enqueued += 1
        except Exception:
            logger.exception(
                "could not enqueue facet extraction for %s posting(s)", len(ids)
            )
            return enqueued
        return enqueued

    def note_facet_read_attempt(self, job_id: str) -> None:
        """Record that this run has spent a facet read on `job_id`'s posting.

        The run's inline pass and the durable queue read the same postings
        from different directions, and a read that *failed* leaves the posting
        uncurrent -- so without this, the message for it is drained moments
        later in the same run and buys the same non-answer a second time.
        `extract_facets_stage` refuses those messages and leaves them in the
        queue for a later run, which is where a retry belongs.

        Costs one read to resolve the posting, against a provider call that
        costs seconds. A job with no posting records nothing, because there is
        no shared row for the queue to hold a message about either.
        """
        posting_id = self._posting_for_job(job_id)
        if posting_id:
            self._facet_read_attempts.add(posting_id)

    def drain_extract_facets_queue(
        self, ai: "AIProvider", *, limit: int
    ) -> list[FacetExtractionOutcome]:
        """Drain up to `limit` messages from the durable extract_facets queue.

        This is the backfill half of objective extraction (#185): postings
        the run's crawl enqueued because nothing in the corpus had current
        facets for them. Unlike the inline pass it replaces, a failure here
        retries or dead-letters durably through `stage_queue.py` instead of
        vanishing with a cancelled run.

        Extraction spends only the platform key (`CallClass.SHARED_EXTRACTION`
        inside `extract_facets`), so this must never be handed a user's
        credential; the caller passing `ai` is trusted to have already
        resolved the platform one.
        """
        if self._ingestion is None or limit <= 0:
            return []
        queue = PostgresStageQueue(self._ingestion, self._stage_queue_names)
        runner = StageRunner(queue, visibility_timeout_seconds=5 * 60)
        stage = ExtractFacetsStage(
            self._ingestion, ai, already_attempted=frozenset(self._facet_read_attempts)
        )
        outcomes: list[FacetExtractionOutcome] = []

        def handler(message):
            try:
                result = stage(message)
            except PermanentStageFailure as error:
                outcomes.append(
                    FacetExtractionOutcome(
                        failed=True,
                        parse_failure=isinstance(error.__cause__, FacetExtractionError),
                    )
                )
                raise
            except (QuotaExhausted, DeferredToALaterRun):
                # Never spent, never a failure -- exactly like
                # PlatformAllowanceExhausted in the inline pass. A deferral is
                # the same shape: the message goes back to the queue and this
                # run reports nothing about it.
                raise
            except Exception:
                outcomes.append(FacetExtractionOutcome(failed=True))
                raise
            else:
                outcomes.append(
                    FacetExtractionOutcome(
                        failed=False,
                        skipped=isinstance(result, FacetsAlreadyCurrent),
                    )
                )
                return result

        try:
            runner.run_once(Stage.EXTRACT_FACETS, handler, batch_size=limit)
        except Exception:
            logger.exception("draining the extract_facets queue failed")
        return outcomes

    @staticmethod
    def _staging_row(batch_id: str, ordinal: int, job: Job) -> tuple[Any, ...]:
        """One staging row, in `_POSTING_STAGING_COLUMNS` order.

        Deliberately the same values `_job_payload` sends for the same job:
        the merge and the per-listing upsert must not be able to disagree
        about what was fetched.
        """
        return (
            batch_id,
            ordinal,
            job_fingerprint(job),
            job.source or "",
            job.source_job_id,
            job.url or "",
            job.canonical_url or "",
            job.company or "",
            job.title or "",
            job.location or "",
            job.remote,
            job.description or "",
            job.content_confidence or "",
            job.ats_provider,
            job.ats_board,
            job.ats_job_id,
        )

    def upsert_logical_jobs(
        self, jobs: list[Job], *, posting_batch: PostingBatch | None = None
    ) -> list[tuple[str, bool, bool] | None]:
        """Persist many logical jobs in as few round trips as possible.

        Returns one entry per input job, in input order, so a caller can zip
        the results back onto the list it passed. An entry is ``None`` when
        that job could not be persisted and was skipped -- callers must
        handle it.

        `posting_batch` is what `merge_posting_batch` returned for these same
        jobs, if anything. Each payload then carries the posting it resolved
        to, and the per-element loop inside `job_hunter_upsert_jobs` no longer
        pays an insert, a read and an update against `job_hunter_postings` for
        every listing -- including the many listings of one advertisement that
        the batch already collapsed. Omitting it is safe: every payload then
        resolves its own posting, as before #182.

        Each chunk is one transaction on the server, so a failure rolls the
        whole chunk back. Rather than paying for a savepoint per row inside
        plpgsql to guard against that, a failed chunk is replayed here one
        job at a time: clean runs cost nothing, and one malformed posting
        costs its own row instead of the run.

        That fallback only makes sense for an *isolated* bad chunk. If the
        batch path itself is broken -- the function missing, the role's
        `statement_timeout` cutting every call, PostgREST unreachable -- then
        every chunk fails, and replaying them all one job at a time would
        issue tens of thousands of requests to produce a run slower than the
        unbatched code this replaced, while looking in the log like a handful
        of unlucky postings. After
        `_CONSECUTIVE_CHUNK_FAILURE_LIMIT` chunks fail back to back, stop
        pretending and raise.
        """
        results: list[tuple[str, bool, bool] | None] = []
        consecutive_failures = 0
        for chunk in _chunked(jobs, _JOB_UPSERT_CHUNK_SIZE):
            try:
                results.extend(self._upsert_job_chunk(chunk, posting_batch))
            except Exception as error:
                consecutive_failures += 1
                if consecutive_failures >= _CONSECUTIVE_CHUNK_FAILURE_LIMIT:
                    raise SupabaseRequestError(
                        f"{consecutive_failures} consecutive batch job upserts failed; "
                        "the batch path is broken, not the postings -- refusing to "
                        f"replay {len(jobs)} jobs one at a time"
                    ) from error
                logger.exception(
                    "batch job upsert failed for %s jobs; retrying them one at a time",
                    len(chunk),
                )
                results.extend(self._upsert_jobs_individually(chunk, posting_batch))
            else:
                consecutive_failures = 0
        self._enqueue_needing_facets_for_job_ids(
            result[0] for result in results if result is not None
        )
        return results

    def _batch_job_payload(
        self, job: Job, posting_batch: PostingBatch | None
    ) -> dict[str, Any]:
        payload = self._job_payload(job)
        posting_id = (
            posting_batch.posting_ids.get(payload["fingerprint"])
            if posting_batch is not None
            else None
        )
        if posting_id:
            payload["posting_id"] = posting_id
        return payload

    def _upsert_job_chunk(
        self, chunk: list[Job], posting_batch: PostingBatch | None = None
    ) -> list[tuple[str, bool, bool]]:
        payloads = [self._batch_job_payload(job, posting_batch) for job in chunk]
        rows = self._shared_write(
            "select input_index, id, is_new, description_changed "
            "from public.job_hunter_upsert_jobs(%s::jsonb, %s::uuid)",
            (json.dumps(payloads), self._client.user_id),
        )
        if len(rows) != len(chunk):
            raise SupabaseRequestError(
                f"job_hunter_upsert_jobs returned {len(rows)} rows for {len(chunk)} jobs"
            )
        ordered = sorted(rows, key=lambda row: row[0])
        return [
            (str(job_id), bool(is_new), bool(description_changed))
            for _input_index, job_id, is_new, description_changed in ordered
        ]

    def _upsert_jobs_individually(
        self, chunk: list[Job], posting_batch: PostingBatch | None = None
    ) -> list[tuple[str, bool, bool] | None]:
        results: list[tuple[str, bool, bool] | None] = []
        for job in chunk:
            try:
                results.append(
                    self.upsert_logical_job(job, posting_batch=posting_batch)
                )
            except Exception:
                logger.exception(
                    "dropping a job that could not be persisted: source=%s url=%s",
                    job.source,
                    job.url,
                )
                results.append(None)
        return results

    def merge_jobs(self, survivor_id: str, duplicate_id: str) -> str | None:
        """Transactionally merge a duplicate job and all attached records.

        Translates store.py:906-1038 (`merge_jobs`/`_merge_jobs`) into a
        single call to `job_hunter_merge_jobs`.

        Since #178 this merges the *postings* behind two of one user's rows,
        which re-points every other affected user's row as well, so since
        #179 it runs over the privileged connection with the user supplied
        rather than through PostgREST as that user. The retry hazard that
        made the old PostgREST call pass `retry=False` goes with it: this
        connection issues the statement once and does not replay it.
        """
        rows = self._shared_write(
            "select public.job_hunter_merge_jobs(%s::uuid, %s::uuid, %s::uuid)",
            (survivor_id, duplicate_id, self._client.user_id),
        )
        survivor = rows[0][0] if rows else None
        # `job_hunter_merge_jobs` returns NULL when this user holds no row on
        # the surviving posting. `str()` would turn that into the string
        # "None", which reads as a job id everywhere downstream -- the
        # PostgREST call this replaced returned None, and so does this.
        return None if survivor is None else str(survivor)

    def resolve_merged_job_id(self, job_id: str) -> str | None:
        """Where a merged-away job's records belong now, or None if it still exists.

        `merge_jobs` deletes the duplicate row, so an id captured before a
        merge names nothing afterwards and the jobs table cannot answer for
        it. `job_hunter_job_merges` is written inside the merge transaction
        and keeps that answer. Redirects are flattened when they are written
        (a row pointing at a job that later becomes a duplicate itself is
        repointed), so one lookup is always enough -- there is no chain to
        walk.
        """
        rows = self._client.select(
            "job_hunter_job_merges",
            params={
                "duplicate_id": f"eq.{job_id}",
                "select": "survivor_id",
                "limit": "1",
            },
        )
        return rows[0]["survivor_id"] if rows else None

    def _write_following_merges(self, job_id: str, write) -> str:
        """Run a per-job write, retrying against the survivor if the job was merged.

        Returns the job id the write actually landed on, so a caller can keep
        using an id that still exists. Resolution happens only on the failure,
        which keeps the ordinary case -- nothing merged -- at exactly one
        request; a run merges hundreds of jobs, but almost never one the
        pipeline is still holding.

        Any other failure, including a foreign key violation with no redirect
        behind it, is left to the caller: retrying it here would only turn a
        clear error into a confusing one.
        """
        try:
            write(job_id)
        except SupabaseRequestError as error:
            if error.code != _FOREIGN_KEY_VIOLATION:
                raise
            survivor_id = self.resolve_merged_job_id(job_id)
            if survivor_id is None:
                raise
            logger.info(
                "job_id=%s was merged away mid-run; writing against survivor_id=%s",
                job_id,
                survivor_id,
            )
            write(survivor_id)
            return survivor_id
        return job_id

    def record_job_source(
        self,
        job_id: str,
        *,
        source: str,
        source_job_id: str | None,
        source_url: str,
    ) -> None:
        """Record a discovery source once while refreshing its last-seen time.

        Translates store.py:1090-1140. On a repeat call for the same
        identity key, `source`, `source_job_id`, `source_url`, and
        `first_seen_at` must not move -- the SQLite original's
        `ON CONFLICT ... DO UPDATE SET last_seen_at = excluded.last_seen_at`
        leaves all four untouched, but `SupabaseClient.upsert` issues a
        merge-duplicates PATCH-via-POST that would overwrite every column.
        Reading back any existing row's four columns first and carrying
        them forward reproduces the original's selective-column behaviour;
        only a genuinely new identity uses this call's arguments and `now`.
        """
        identity_key = (
            f"id:{source}:{source_job_id}"
            if source_job_id
            else f"url:{self._canonicalize_url(source_url)}"
        )
        now = to_iso(datetime.now(timezone.utc))
        existing = self._client.select(
            "job_hunter_job_sources",
            params={
                "job_id": f"eq.{job_id}",
                "identity_key": f"eq.{identity_key}",
                "limit": "1",
                "select": "source,source_job_id,source_url,first_seen_at",
            },
        )
        if existing:
            row = existing[0]
            source = row["source"]
            source_job_id = row["source_job_id"]
            source_url = row["source_url"]
            first_seen_at = row["first_seen_at"]
        else:
            first_seen_at = now
        self._client.upsert(
            "job_hunter_job_sources",
            [
                {
                    "user_id": self._client.user_id,
                    "job_id": job_id,
                    "source": source,
                    "source_job_id": source_job_id,
                    "source_url": source_url,
                    "identity_key": identity_key,
                    "first_seen_at": first_seen_at,
                    "last_seen_at": now,
                }
            ],
            on_conflict="job_id,identity_key",
        )

    def list_job_sources(self, job_id: str) -> list[dict[str, Any]]:
        """Return source provenance for a job in insertion order.

        Translates store.py:1142-1146. Ids are random uuids now, so
        `created_at` (with `select`'s `id.asc` tie-breaker) replaces
        `ORDER BY id` as the insertion-order proxy.
        """
        return self._client.select(
            "job_hunter_job_sources",
            params={"job_id": f"eq.{job_id}", "order": "created_at.asc"},
        )

    def find_job_by_canonical_url(self, url: str) -> str | None:
        """Return a job ID only when a canonical URL identifies one job.

        Translates store.py:1148-1157. The canonical URL is the
        advertisement's, so the filter is on the embedded posting (#178);
        `!inner` makes the embed a join rather than a nullable side, which is
        what lets the filter select rows instead of blanking the embed. The
        rows themselves are still the caller's own -- row-level security on
        `job_hunter_jobs` sees to that -- so the answer is a job id the caller
        holds, exactly as before.
        """
        rows = self._client.select(
            "job_hunter_jobs",
            params={
                "posting.canonical_url": f"eq.{self._canonicalize_url(url)}",
                "select": "id,posting:job_hunter_postings!inner(id)",
            },
        )
        return rows[0]["id"] if len(rows) == 1 else None

    def find_job_by_ats(
        self, provider: str, board: str, job_id: str | None
    ) -> str | None:
        """Return a job ID only when an ATS tuple identifies one job.

        Translates store.py:1159-1178. Filtered on the posting for the same
        reason as `find_job_by_canonical_url`.
        """
        if not job_id:
            return None
        rows = self._client.select(
            "job_hunter_jobs",
            params={
                "posting.ats_provider": f"eq.{provider}",
                "posting.ats_board": f"eq.{board}",
                "posting.ats_job_id": f"eq.{job_id}",
                "select": "id,posting:job_hunter_postings!inner(id)",
            },
        )
        return rows[0]["id"] if len(rows) == 1 else None

    def find_job_by_identity(
        self, company: str, title: str, location: str
    ) -> str | None:
        """Return a job ID only for one normalized company/title/location match.

        Translates store.py:1180-1222. `job_hunter_find_job_by_identity`
        returns `setof uuid` -- a plain list of id strings, not row dicts --
        and the row is taken only when exactly one comes back. `limit 1` (or
        indexing `result[0]` unconditionally) would silently accept an
        ambiguous match as a confident one.
        """
        result = self._client.rpc(
            "job_hunter_find_job_by_identity",
            {"p_company": company, "p_title": title, "p_location": location},
        )
        return result[0] if len(result) == 1 else None

    def set_job_market(self, job_id: str, market_id: str | None) -> None:
        """Persist the primary market a job has been attributed to.

        Translates store.py:1223-1229.
        """
        self._client.update(
            "job_hunter_jobs",
            {"market_id": market_id or ""},
            params={"id": f"eq.{job_id}"},
        )

    def set_job_markets(self, pairs: list[tuple[str, str | None]]) -> None:
        """Attribute many jobs to their markets, chunked into few requests.

        ``None`` is stored as ``''``, exactly as the single-job
        `set_job_market` does.
        """
        if not pairs:
            return
        for chunk in _chunked(pairs, _ID_ARRAY_CHUNK_SIZE):
            self._client.rpc(
                "job_hunter_set_job_markets",
                {
                    "p_rows": [
                        {"id": job_id, "market_id": market_id or ""}
                        for job_id, market_id in chunk
                    ]
                },
            )

    def count_jobs(self) -> int:
        """Translates store.py:1662-1664."""
        rows = self._client.select("job_hunter_jobs", params={"select": "id"})
        return len(rows)

    def list_jobs_for_matching(self) -> list[dict[str, Any]]:
        """Translates store.py:1666-1673.

        Ids are random uuids now, so `created_at` (with `select`'s
        `id.asc` tie-breaker) replaces `ORDER BY id` as the insertion-order
        proxy.

        Four of the seven keys are the advertisement's and come from the
        posting (#178); the rows are flattened here so `gmail_matching` keeps
        reading one mapping per job rather than learning the shape of the
        embed.
        """
        rows = self._client.select(
            "job_hunter_jobs",
            params={
                "select": (
                    "id,first_seen_at,last_seen_at,"
                    f"posting:job_hunter_postings({_MATCHING_POSTING_COLUMNS})"
                ),
                "order": "created_at.asc",
            },
        )
        flattened = []
        for row in rows:
            facts = dict(posting_facts(row))
            facts.pop("posting", None)
            flattened.append(facts)
        return flattened

    def get_job(self, job_id: str) -> Job | None:
        """Translates store.py:2147-2169."""
        rows = self._client.select(
            "job_hunter_jobs",
            params={
                "id": f"eq.{job_id}",
                "select": f"{_MEMBERSHIP_COLUMNS},{_POSTING_FACT_EMBED}",
            },
        )
        if not rows:
            return None
        return job_from_row(rows[0])

    def backfill_ats_identity(self) -> int:
        """Attribute stored postings that have a supported ATS URL but no identity.

        Translates store.py:399-457. SQLite's `LIKE` is case-insensitive;
        Postgres's is not, so the host-match filter below uses `ilike`
        (PostgREST's `*` wildcard alias) instead. Never overwrites a field
        that is already set -- only an empty/missing `ats_provider`,
        `ats_board`, or `ats_job_id` is backfilled from the parsed
        reference. Returns how many rows were updated.

        The identity is the advertisement's, so this reads and writes
        `job_hunter_postings` since #178. One run's repair therefore benefits
        every user holding that posting, which is the same reason the columns
        moved: an identity established once is established for everyone.

        What it reads is still bounded by the caller's own corpus -- the
        postings this user holds a membership row for, not every posting in
        the table. A user's run should cost work proportional to what that
        user discovered; scanning the whole shared corpus would make one
        user's repair pass grow with everybody else's crawling, which is rule
        2 ("cost scales with jobs, not with users") read backwards.
        """
        missing_identity = "or(" + ",".join(
            [
                "ats_provider.is.null",
                "ats_provider.eq.",
                "ats_board.is.null",
                "ats_board.eq.",
                "ats_job_id.is.null",
                "ats_job_id.eq.",
            ]
        ) + ")"
        host_terms = [
            f"{column}.ilike.*{host}/*"
            for column in ("url", "canonical_url")
            for host in SUPPORTED_ATS_HOSTS
        ]
        host_match = "or(" + ",".join(host_terms) + ")"

        membership_rows = self._client.select(
            "job_hunter_jobs",
            params={
                "posting.and": f"({missing_identity},{host_match})",
                "select": (
                    "posting:job_hunter_postings!inner"
                    "(id,url,canonical_url,ats_provider,ats_board,ats_job_id)"
                ),
            },
        )
        # One posting can be held by several of this user's rows only through
        # a bug, but de-duplicating by id costs nothing and keeps the returned
        # count a count of postings repaired rather than of rows scanned.
        rows = list({row["posting"]["id"]: row["posting"] for row in membership_rows}.values())

        updated = 0
        for row in rows:
            reference = None
            for url in (row.get("canonical_url"), row.get("url")):
                if not url:
                    continue
                reference = parse_supported_ats_url(url)
                if reference is not None:
                    break
            if reference is None:
                continue
            # The posting is a shared row, so the repair is a privileged
            # write since #179. The read above stays on PostgREST: it is
            # bounded by this user's own memberships, which is the point.
            self._shared_write(
                "update public.job_hunter_postings set "
                "  ats_provider = %s, ats_board = %s, ats_job_id = %s "
                " where id = %s::uuid",
                (
                    row.get("ats_provider") or reference.provider,
                    row.get("ats_board") or reference.board,
                    row.get("ats_job_id") or reference.job_id,
                    row["id"],
                ),
            )
            updated += 1
        return updated

    # ------------------------------------------------------------------
    # Evaluations
    # ------------------------------------------------------------------

    def _posting_facts_of(self, job_id: str) -> dict[str, Any]:
        """Read one job's description state from the posting, in one request.

        Returns an empty mapping for an id the caller cannot read, so every
        comparison against it answers the same way it did when the read
        returned no row.
        """
        rows = self._client.select(
            "job_hunter_jobs",
            params={
                "id": f"eq.{job_id}",
                "select": _DESCRIPTION_STATE_EMBED,
            },
        )
        return posting_facts(rows[0]) if rows else {}

    def needs_evaluation(self, job_id: str) -> bool:
        """Translates store.py:1999-2034.

        The SQLite original joined `evaluations` to `jobs` in one query and
        ordered by `e.id DESC` to find the most recent evaluation. Ids are
        random uuids now, so `id DESC` is meaningless; this orders by
        `evaluated_at` (with the usual `created_at`/`id` fallback -- see
        `get_evaluation`) instead, in two requests rather than one PostgREST
        embed, matching this module's existing two-step pattern (e.g.
        `record_job_source`).
        """
        evaluations = self._client.select(
            "job_hunter_evaluations",
            params={
                "job_id": f"eq.{job_id}",
                "select": "status,description_hash_at_eval,content_confidence_at_eval",
                "order": _LATEST_EVALUATION_ORDER,
                "limit": "1",
            },
        )
        if not evaluations:
            return True
        evaluation = evaluations[0]
        if evaluation["status"] == "failed":
            return True

        facts = self._posting_facts_of(job_id)
        if evaluation["description_hash_at_eval"] != (facts.get("description_hash") or ""):
            return True
        if evaluation["content_confidence_at_eval"] != (facts.get("content_confidence") or ""):
            return True
        return False

    def needs_evaluation_bulk(self, job_ids: list[str]) -> dict[str, bool]:
        """Answer `needs_evaluation` for many jobs in one request per chunk.

        Duplicate ids are asked once and answered for every occurrence. An id
        the caller cannot read comes back from Postgres as no row at all --
        row-level security filters it before the function sees it -- and is
        reported as ``True`` here, matching what the per-job method does with
        a job whose evaluations it cannot see.
        """
        unique_ids = list(dict.fromkeys(job_ids))
        if not unique_ids:
            return {}
        answered: dict[str, bool] = {}
        for chunk in _chunked(unique_ids, _ID_ARRAY_CHUNK_SIZE):
            rows = self._client.rpc("job_hunter_needs_evaluation", {"p_job_ids": chunk})
            for row in rows:
                answered[row["job_id"]] = row["needs"]
        return {job_id: answered.get(job_id, True) for job_id in unique_ids}

    def save_evaluation(self, job_id: str, evaluation: Evaluation) -> str:
        """Translates store.py:2036-2075.

        Upserts against `job_hunter_evaluations`'s
        `(user_id, job_id, evaluated_at)` constraint rather than inserting --
        `HttpClient` retries POST on 5xx, so a plain insert here would
        double-write on a transient error. `evaluated_at` is stamped now,
        same as the original's `_now_iso()`; nothing about the evaluation
        itself carries a caller-supplied timestamp to preserve.

        Returns the job id the evaluation was written against. That is the id
        passed in unless the job was merged away since the caller captured it,
        in which case the evaluation follows the merge to the surviving job
        and the caller gets that id back -- everything it does next with the
        evaluation (delivering it, marking it delivered, promoting the
        company) has to name a row that still exists.
        """
        return self._write_following_merges(
            job_id, lambda target_id: self._write_evaluation(target_id, evaluation)
        )

    def _write_evaluation(self, job_id: str, evaluation: Evaluation) -> None:
        facts = self._posting_facts_of(job_id)
        description_hash = facts.get("description_hash") or ""
        content_confidence_value = (
            evaluation.content_confidence or facts.get("content_confidence") or ""
        )
        self._client.upsert(
            "job_hunter_evaluations",
            [
                {
                    "user_id": self._client.user_id,
                    "job_id": job_id,
                    "total_score": evaluation.total_score,
                    "scores_json": evaluation.scores,
                    "decision": evaluation.decision,
                    "hard_blockers_json": evaluation.hard_blockers,
                    "strengths_json": evaluation.strengths,
                    "gaps_json": evaluation.gaps,
                    "salary_note": evaluation.salary_note,
                    "location_note": evaluation.location_note,
                    "rationale": evaluation.rationale,
                    "model": evaluation.model,
                    "status": evaluation.status,
                    "market_id": evaluation.market_id,
                    "description_hash_at_eval": description_hash,
                    "content_confidence_at_eval": content_confidence_value,
                    "requirements_json": evaluation.requirements,
                    "raw_model_score": evaluation.raw_model_score,
                    "evaluated_at": to_iso(datetime.now(timezone.utc)),
                }
            ],
            on_conflict="user_id,job_id,evaluated_at",
        )

    def get_evaluation(self, job_id: str) -> Evaluation | None:
        """Translates store.py:2171-2204.

        "Latest evaluation" means newest `evaluated_at` now, not highest
        `id` -- ids are random uuids, so max-id is meaningless. Ties are
        broken by `created_at` then `id`, matching
        `job_hunter_pending_delivery_jobs`'s per-job ordering. In practice a
        tie on `evaluated_at` for one job can't arise: `save_evaluation`
        always upserts against `(user_id, job_id, evaluated_at)`, so two
        saves that land on the same instant converge into one row instead
        of leaving two to choose between (see the dedicated test for this).
        The tie-break stays here anyway, matching the SQL function's
        pattern, as a defensive-in-depth safeguard.
        """
        rows = self._client.select(
            "job_hunter_evaluations",
            params={
                "job_id": f"eq.{job_id}",
                "select": (
                    "job_id,total_score,scores_json,decision,hard_blockers_json,"
                    "strengths_json,gaps_json,salary_note,location_note,rationale,"
                    "model,status,market_id,content_confidence_at_eval,"
                    "requirements_json,raw_model_score"
                ),
                "order": _LATEST_EVALUATION_ORDER,
                "limit": "1",
            },
        )
        if not rows:
            return None
        return evaluation_from_row(rows[0])

    def get_evaluations_bulk(self, job_ids: list[str]) -> dict[str, Evaluation]:
        """The latest evaluation for many jobs at once (#188).

        `matching.match_jobs` needs this for every row its SQL ranking
        returns, to decide "already delivered", "evaluated but not
        delivered" or "neither" without one `get_evaluation` round trip per
        row. Ordered `job_id, then latest-first` so the first row seen per
        `job_id` in the loop below is that job's most recent evaluation --
        the same ordering `get_evaluation` applies per job, just requested
        for the whole chunk in one call instead of one call per job.
        """
        unique_ids = list(dict.fromkeys(job_ids))
        if not unique_ids:
            return {}
        found: dict[str, Evaluation] = {}
        for chunk in _chunked(unique_ids, _URL_FILTER_CHUNK_SIZE):
            rows = self._client.select(
                "job_hunter_evaluations",
                params={
                    "job_id": f"in.({','.join(chunk)})",
                    "select": (
                        "job_id,total_score,scores_json,decision,hard_blockers_json,"
                        "strengths_json,gaps_json,salary_note,location_note,rationale,"
                        "model,status,market_id,content_confidence_at_eval,"
                        "requirements_json,raw_model_score"
                    ),
                    "order": f"job_id.asc,{_LATEST_EVALUATION_ORDER}",
                },
            )
            for row in rows:
                job_id = row["job_id"]
                if job_id not in found:
                    found[job_id] = evaluation_from_row(row)
        return found

    # ------------------------------------------------------------------
    # Objective facets
    # ------------------------------------------------------------------

    def save_job_facets(self, job_id: str, facets: JobFacets) -> None:
        """Persist the facets of the posting `job_id` is a copy of (#175).

        Facets belong to the advertisement, not to the user who found it, so
        the row is keyed on `job_hunter_postings` and replaces whatever was
        there before -- including a set another user's run wrote. The caller
        keeps passing job ids because that is what the pipeline holds; the
        translation to a posting belongs here.

        The description hash is read off the posting rather than taken from
        the caller, exactly as `_write_evaluation` reads it off the job:
        there is one notion of "the description this was computed at", and
        for a shared extraction it is the posting's. That is what makes a
        changed description cost one re-extraction rather than one per user.

        A job with no posting is left unenriched. That covers the job merged
        away mid-run -- its row is gone, so it resolves to no posting, and
        the facets are discarded rather than stamped on the survivor, which
        would pin one posting's facts to another posting's text as
        permanently current. It also covers a job row written without going
        through `job_hunter_upsert_job`, which is the only other way a job
        can lack one.
        """
        posting_id = self._posting_for_job(job_id)
        if posting_id is None:
            logger.info(
                "job_id=%s resolves to no posting (merged away mid-run, or "
                "never pointed at one); discarding its facets",
                job_id,
            )
            return
        try:
            self._write_posting_facets(posting_id, facets)
        except SupabaseRequestError as error:
            if error.code != _FOREIGN_KEY_VIOLATION:
                raise
            logger.info(
                "posting_id=%s disappeared mid-run; discarding the facets read "
                "from it",
                posting_id,
            )

    def _posting_for_job(self, job_id: str) -> str | None:
        """The posting a job is one user's copy of, or None when it has none.

        None is also what an unreadable job id gives, which is the same
        answer for the purposes of every caller here: there is no shared row
        to read facets from or write them to.
        """
        rows = self._client.select(
            "job_hunter_jobs",
            params={"id": f"eq.{job_id}", "select": "posting_id", "limit": "1"},
        )
        if not rows:
            return None
        return rows[0].get("posting_id")

    def _write_posting_facets(self, posting_id: str, facets: JobFacets) -> None:
        """Store one posting's facets, over the privileged connection (#179).

        The hash the extraction was made against is read inside the same
        statement rather than in a round trip of its own. That is not only a
        request saved: it removes the window in which the posting's
        description changed between the read and the write, which would have
        stamped facets read from the old text as current against the new.

        A posting that disappeared between the extraction and this write --
        merged away, its facets discarded by the merge (#125's rule at the
        posting level) -- writes nothing at all rather than failing: the
        select supplying the row finds nothing, so the insert has no row to
        insert. The caller's foreign-key branch stays for the PostgREST-era
        shape of that failure and costs nothing when it never fires.
        """
        compensation = facets.compensation
        self._shared_write(
            """
            insert into public.job_hunter_job_facets (
              posting_id, description_hash_at_extraction, seniority,
              remote_policy, relocation_policy, hiring_regions, stack,
              compensation_disclosed, compensation_currency, compensation_min,
              compensation_max, compensation_period, requirements_json,
              source_supplied, model, extracted_at)
            select
              p.id, p.description_hash, %s, %s, %s, %s::text[], %s::text[],
              %s, %s, %s, %s, %s, %s::jsonb, %s::text[], %s, %s::timestamptz
              from public.job_hunter_postings p where p.id = %s::uuid
            on conflict (posting_id) do update set
              description_hash_at_extraction = excluded.description_hash_at_extraction,
              seniority = excluded.seniority,
              remote_policy = excluded.remote_policy,
              relocation_policy = excluded.relocation_policy,
              hiring_regions = excluded.hiring_regions,
              stack = excluded.stack,
              compensation_disclosed = excluded.compensation_disclosed,
              compensation_currency = excluded.compensation_currency,
              compensation_min = excluded.compensation_min,
              compensation_max = excluded.compensation_max,
              compensation_period = excluded.compensation_period,
              requirements_json = excluded.requirements_json,
              source_supplied = excluded.source_supplied,
              model = excluded.model,
              extracted_at = excluded.extracted_at
            """,
            (
                facets.seniority,
                facets.remote_policy,
                facets.relocation_policy,
                list(facets.hiring_regions),
                list(facets.stack),
                compensation.disclosed,
                compensation.currency,
                compensation.minimum,
                compensation.maximum,
                compensation.period,
                json.dumps(facets.requirements),
                list(facets.source_supplied),
                facets.model,
                to_iso(datetime.now(timezone.utc)),
                posting_id,
            ),
        )

    def get_job_facets(self, job_id: str) -> JobFacets | None:
        """Return the posting's stored facets, or None when never extracted.

        Whoever extracted them: the row is keyed on the posting and readable
        by every authenticated user, so a run reads what another user's run
        paid for rather than paying again.

        None means "not extracted yet", never "this posting states nothing":
        a posting that states nothing is stored with every facet at its
        unknown/empty value, which is a fact about the posting and worth
        keeping.
        """
        posting_id = self._posting_for_job(job_id)
        if posting_id is None:
            return None
        rows = self._client.select(
            "job_hunter_job_facets",
            params={
                "posting_id": f"eq.{posting_id}",
                "select": (
                    "seniority,remote_policy,relocation_policy,hiring_regions,stack,"
                    "compensation_disclosed,compensation_currency,compensation_min,"
                    "compensation_max,compensation_period,requirements_json,"
                    "source_supplied,description_hash_at_extraction,model"
                ),
                "limit": "1",
            },
        )
        if not rows:
            return None
        return job_facets_from_row(rows[0])

    def posting_display_credit(self, posting_id: str) -> dict[str, Any] | None:
        """What a surface must display alongside this posting, or None (#184, read by #188).

        Calls `job_hunter_posting_display_credit`, which is `security
        definer` with no user parameter: the obligation belongs to the
        posting's source, never to whoever is reading it, so nothing here
        takes or forwards a `user_id`. `None` means the source imposes
        nothing -- the SQL function's own `null` for an empty
        `display_credit` object -- never "unknown".
        """
        rows = self._client.rpc(
            "job_hunter_posting_display_credit",
            {"p_posting_id": posting_id},
        )
        # A bare-scalar function always yields one element (`rpc`'s own
        # contract) -- `[None]` for "no obligation", `[{...}]` for one.
        return (rows[0] if rows else None) or None

    def match_jobs(
        self,
        *,
        preferred_roles: list[str],
        preferred_seniority: list[str],
        must_have_signals: list[str],
        nice_to_have_signals: list[str],
        preferred_locations: list[str],
        avoid_signals: list[str],
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Rank and flag every open posting in one SQL call (#187, #243).

        `job_hunter_match_jobs` is `security invoker`, so RLS still scopes
        this to what the acting user may see -- but since #243 that is no
        longer only the membership rows they already hold: every open
        posting is ranked, `job_id` is `None` for one the caller has never
        been matched against before, and every row the caller already holds
        a membership row for comes back in full regardless of state. A
        never-discovered posting is capped at `limit`, ranked -- see the SQL
        function's own comment for why that bound falls only on that new
        surface. Each row is `{"job_id", "posting_id", "market_id", "score",
        "hard_blockers", "has_facets", "locations"}`; `matching.match_jobs`
        is what turns this into scored `Evaluation`s, and what creates a
        membership row for a `job_id`-less row it decides to act on.
        """
        return self._client.rpc(
            "job_hunter_match_jobs",
            {
                "p_preferred_roles": preferred_roles,
                "p_preferred_seniority": preferred_seniority,
                "p_must_have_signals": must_have_signals,
                "p_nice_to_have_signals": nice_to_have_signals,
                "p_preferred_locations": preferred_locations,
                "p_avoid_signals": avoid_signals,
                "p_limit": limit,
            },
        )

    def match_state_counts(
        self,
        *,
        preferred_roles: list[str],
        preferred_seniority: list[str],
        must_have_signals: list[str],
        nice_to_have_signals: list[str],
        preferred_locations: list[str],
        avoid_signals: list[str],
    ) -> list[dict[str, Any]]:
        """Aggregate ineligible/qualified/unresolved counts and reasons (#243).

        `job_hunter_match_state_counts` classifies every open posting the
        same way `match_jobs` does, but returns counts only -- no row
        bodies -- so a caller can report why a bounded ranked result was
        short or empty (AC9) without paging through the whole corpus. Each
        row is `{"state", "reason", "count"}`.
        """
        return self._client.rpc(
            "job_hunter_match_state_counts",
            {
                "p_preferred_roles": preferred_roles,
                "p_preferred_seniority": preferred_seniority,
                "p_must_have_signals": must_have_signals,
                "p_nice_to_have_signals": nice_to_have_signals,
                "p_preferred_locations": preferred_locations,
                "p_avoid_signals": avoid_signals,
            },
        )

    def ensure_membership(self, posting_id: str, market_id: str = "") -> str:
        """Per-user membership as an output of matching, not a precondition (#243).

        Thin wrapper around `job_hunter_ensure_job_membership`: idempotent,
        `security invoker`, so RLS confines it to a row the acting user owns
        or is creating for themselves. Not a re-sighting -- a second call for
        a posting matching already acted on returns the existing id rather
        than touching `last_seen_at`.
        """
        return self._client.rpc(
            "job_hunter_ensure_job_membership",
            {"p_posting_id": posting_id, "p_market_id": market_id},
        )[0]

    def jobs_needing_facets(self, job_ids: list[str]) -> set[str]:
        """Which of `job_ids` sit on a posting nobody has current facets for.

        A job needs extraction when its posting has no facet row at all, or
        when the description that row was extracted at is not the
        description the *posting* carries now -- the same comparison
        `needs_evaluation` makes against `description_hash_at_eval`,
        deliberately reusing the one mechanism rather than adding a second
        notion of a changed posting. Reading the hash off the posting rather
        than off each user's job row is what makes an edited advertisement
        cost one re-extraction instead of one per user.

        Three requests per chunk: the jobs, their postings, and the facets on
        those postings.

        An id with no readable job row, or a job row with no posting, is left
        out entirely. Row-level security filters the first before this sees
        it, and neither has a shared row to read or write: reporting one as
        needing work would send the pipeline into a call whose result it
        could not store.
        """
        unique_ids = list(dict.fromkeys(job_ids))
        if not unique_ids:
            return set()

        needing: set[str] = set()
        for chunk in _chunked(unique_ids, _URL_FILTER_CHUNK_SIZE):
            job_rows = self._client.select(
                "job_hunter_jobs",
                params={"id": f"in.({','.join(chunk)})", "select": "id,posting_id"},
            )
            posting_ids = sorted(
                {row["posting_id"] for row in job_rows if row.get("posting_id")}
            )
            if not posting_ids:
                continue
            posting_filter = f"in.({','.join(posting_ids)})"
            posting_rows = self._client.select(
                "job_hunter_postings",
                params={"id": posting_filter, "select": "id,description_hash"},
            )
            facet_rows = self._client.select(
                "job_hunter_job_facets",
                params={
                    "posting_id": posting_filter,
                    "select": "posting_id,description_hash_at_extraction",
                },
            )
            current_hash = {
                row["id"]: row.get("description_hash") or "" for row in posting_rows
            }
            extracted_at_hash = {
                row["posting_id"]: row.get("description_hash_at_extraction") or ""
                for row in facet_rows
            }
            for row in job_rows:
                posting_id = row.get("posting_id")
                if not posting_id:
                    continue
                if posting_id not in extracted_at_hash:
                    needing.add(row["id"])
                elif extracted_at_hash[posting_id] != current_hash.get(posting_id, ""):
                    needing.add(row["id"])
        return needing

    # ------------------------------------------------------------------
    # Company facets
    # ------------------------------------------------------------------

    def get_company_facets(self, company: str) -> CompanyFacets | None:
        """Return the employer's stored facets, or None when never read.

        Whoever read them: the row is keyed on the company's normalized
        identity and readable by every authenticated user, so a run reads
        what another user's run paid for rather than paying again.

        None means "not established yet", never "this company is nothing":
        a company read as entirely unknown is still stored, because "we
        looked and could not tell" is worth keeping and worth not paying for
        twice.
        """
        identity = normalize_company_name(company)
        if not identity:
            return None
        found = self.get_company_facets_bulk([company])
        return found.get(identity)

    def get_company_facets_bulk(
        self, companies: list[str]
    ) -> dict[str, CompanyFacets]:
        """The stored facets for each of `companies`, keyed by identity.

        One request per chunk rather than one per company: a run asks about
        every employer in its eligible set at once, and that set is the whole
        point -- reading 300 employers one row at a time would cost more in
        round trips than the extraction it is saving.

        A company with no row is simply absent from the result. Callers must
        read that as "nothing established", never as a negative fact.
        """
        identities = [
            identity
            for identity in dict.fromkeys(
                normalize_company_name(company) for company in companies
            )
            if identity
        ]
        if not identities:
            return {}

        found: dict[str, CompanyFacets] = {}
        for chunk in _chunked(identities, _URL_FILTER_CHUNK_SIZE):
            rows = self._client.select(
                "job_hunter_companies",
                params={
                    "identity": f"in.({_quoted_in_list(chunk)})",
                    "select": (
                        "identity,display_name,industry,business_model,stage,"
                        "size_band,headquarters_region,source_supplied,model"
                    ),
                },
            )
            for row in rows:
                facets = company_facets_from_row(row)
                if facets.identity:
                    found[facets.identity] = facets
        return found

    def companies_needing_facets(self, companies: list[str]) -> set[str]:
        """Which of `companies` have no current facts, by normalized identity.

        A company needs reading when it has no row at all, or when the row it
        has was extracted longer ago than `COMPANY_FACET_REFRESH`.

        Refresh is **time-based, and deliberately not tied to any posting's
        description hash** (#198). A posting's facets invalidate when the
        advertisement's text moves, because the text is the thing being
        described. A company's attributes are not tied to any one advert: an
        employer does not stop being a B2B marketplace because it edited a
        job description, and attaching this to the posting mechanism would
        re-derive stable facts at posting cadence, which is exactly the cost
        this table exists to avoid.
        """
        identities = [
            identity
            for identity in dict.fromkeys(
                normalize_company_name(company) for company in companies
            )
            if identity
        ]
        if not identities:
            return set()

        stale_before = to_iso(datetime.now(timezone.utc) - COMPANY_FACET_REFRESH)
        needing = set(identities)
        for chunk in _chunked(identities, _URL_FILTER_CHUNK_SIZE):
            rows = self._client.select(
                "job_hunter_companies",
                params={
                    "identity": f"in.({_quoted_in_list(chunk)})",
                    "extracted_at": f"gte.{stale_before}",
                    "select": "identity",
                },
            )
            needing -= {row["identity"] for row in rows if row.get("identity")}
        return needing

    def save_company_facets(self, facets: CompanyFacets) -> None:
        """Persist what was read about one employer, replacing what was there.

        Keyed on the identity rather than on a user, so a second user's run
        never writes a second row for the same employer -- that duplication
        is precisely the per-user cost this is here to remove. A row written
        by another user's run is overwritten rather than merged: the prompt
        could not see who asked, so the two answers describe the same company
        and the newer one was read against more recent advertisements.

        A company with no identity is dropped. `normalize_company_name`
        returns "" for a name that is entirely punctuation or a bare legal
        suffix, and there is nothing to key such a row on.
        """
        if not facets.identity:
            logger.info(
                "company %r normalizes to no identity; discarding its facets",
                facets.display_name,
            )
            return
        now = to_iso(datetime.now(timezone.utc))
        # Over the privileged connection since #179: an employer's facts feed
        # every user's ranking, so they are ingestion's to write.
        self._shared_write(
            """
            insert into public.job_hunter_companies (
              identity, display_name, industry, business_model, stage,
              size_band, headquarters_region, source_supplied, model,
              extracted_at, updated_at)
            values (%s, %s, %s, %s, %s, %s, %s, %s::text[], %s,
                    %s::timestamptz, %s::timestamptz)
            on conflict (identity) do update set
              display_name = excluded.display_name,
              industry = excluded.industry,
              business_model = excluded.business_model,
              stage = excluded.stage,
              size_band = excluded.size_band,
              headquarters_region = excluded.headquarters_region,
              source_supplied = excluded.source_supplied,
              model = excluded.model,
              extracted_at = excluded.extracted_at,
              updated_at = excluded.updated_at
            """,
            (
                facets.identity,
                facets.display_name,
                facets.industry,
                facets.business_model,
                facets.stage,
                facets.size_band,
                facets.headquarters_region,
                list(facets.source_supplied),
                facets.model,
                now,
                now,
            ),
        )

    # ------------------------------------------------------------------
    # Materials
    # ------------------------------------------------------------------

    def save_material(self, job_id: str, material: Material) -> None:
        """Translates store.py:2081-2090.

        Upserts against `job_hunter_materials`'s `(user_id, job_id,
        generated_at)` constraint, never inserts -- same retry-safety
        reasoning as `save_evaluation`.
        """
        self._client.upsert(
            "job_hunter_materials",
            [
                {
                    "user_id": self._client.user_id,
                    "job_id": job_id,
                    "cover_letter_text": material.cover_letter_text,
                    "generated_at": to_iso(datetime.now(timezone.utc)),
                }
            ],
            on_conflict="user_id,job_id,generated_at",
        )

    def get_material(self, job_id: str) -> Material | None:
        """Translates store.py:2206-2218.

        Same "latest" reasoning as `get_evaluation`: newest `generated_at`
        replaces highest `id`, with the same `created_at`/`id` fallback.
        """
        rows = self._client.select(
            "job_hunter_materials",
            params={
                "job_id": f"eq.{job_id}",
                "select": "job_id,cover_letter_text",
                "order": _LATEST_MATERIAL_ORDER,
                "limit": "1",
            },
        )
        if not rows:
            return None
        return material_from_row(rows[0])

    # ------------------------------------------------------------------
    # Deliveries
    # ------------------------------------------------------------------

    def mark_delivered(
        self,
        job_id: str,
        delivery_type: str,
        telegram_id: str | None = None,
    ) -> str:
        """Translates store.py:2096-2111.

        Upserts against `job_hunter_deliveries`'s `(user_id, job_id,
        delivery_type, delivered_at)` constraint, never inserts -- same
        retry-safety reasoning as `save_evaluation`. It follows a merge the
        same way too: a job merged away between delivery and this call is
        recorded as delivered against the surviving job rather than failing.
        Returns the id it wrote against, so a caller that reads the job back
        afterwards reads a row that exists.
        """
        return self._write_following_merges(
            job_id,
            lambda target_id: self._write_delivery(target_id, delivery_type, telegram_id),
        )

    def _write_delivery(
        self, job_id: str, delivery_type: str, telegram_id: str | None
    ) -> None:
        self._client.upsert(
            "job_hunter_deliveries",
            [
                {
                    "user_id": self._client.user_id,
                    "job_id": job_id,
                    "delivery_type": delivery_type,
                    "status": "sent",
                    "delivered_at": to_iso(datetime.now(timezone.utc)),
                    "telegram_message_id": telegram_id,
                }
            ],
            on_conflict="user_id,job_id,delivery_type,delivered_at",
        )

    def has_delivery(self, job_id: str, delivery_type: str | None = None) -> bool:
        """Translates store.py:2113-2124."""
        params: dict[str, str] = {"job_id": f"eq.{job_id}", "select": "id", "limit": "1"}
        if delivery_type is not None:
            params["delivery_type"] = f"eq.{delivery_type}"
        rows = self._client.select("job_hunter_deliveries", params=params)
        return len(rows) > 0

    def delivered_job_ids(self, job_ids: list[str], delivery_type: str) -> set[str]:
        """Which of `job_ids` already have a delivery of `delivery_type` (#188).

        The bulk sibling of `has_delivery`: `matching.match_jobs` asks this
        once for every row its SQL ranking returned, so a corpus of
        already-delivered history costs one request per chunk to skip
        rather than one `has_delivery` round trip per row.
        """
        unique_ids = list(dict.fromkeys(job_ids))
        if not unique_ids:
            return set()
        delivered: set[str] = set()
        for chunk in _chunked(unique_ids, _URL_FILTER_CHUNK_SIZE):
            rows = self._client.select(
                "job_hunter_deliveries",
                params={
                    "job_id": f"in.({','.join(chunk)})",
                    "delivery_type": f"eq.{delivery_type}",
                    "select": "job_id",
                },
            )
            delivered.update(row["job_id"] for row in rows)
        return delivered

    def pending_delivery_job_ids(
        self,
        match_score_floor: int,
    ) -> list[str]:
        """Translates store.py:2126-2141.

        `job_hunter_pending_delivery_jobs` (migration
        202609060004) reimplements the whole query -- the per-job "latest
        evaluation" join, the score floor, the decision filter, and the
        anti-join against a sent `telegram_message` delivery -- as one SQL
        function, rather than fetching every job/evaluation pair into
        Python and filtering there. The SQL predicate is `>=`, matching the
        profile's inclusive floor, so the value passes through unchanged
        (migration 20260908160000). The parameter is still named
        `p_score_floor`: PostgREST resolves `rpc` by parameter name, so
        renaming it would break whichever side of a deploy is not yet
        updated. It `returns table (job_id uuid)`, so
        `rpc` hands back `[{'job_id': '...'}, ...]`; unwrap the single key.
        """
        rows = self._client.rpc(
            "job_hunter_pending_delivery_jobs",
            {"p_score_floor": match_score_floor},
        )
        return [row["job_id"] for row in rows]

    # ------------------------------------------------------------------
    # Company watch
    #
    # Since #204 a watch lives in one of two places. A manual watch --
    # `promotion_source == "manual"` -- is one user's own intent and stays
    # on the per-user `job_hunter_company_watch`, exactly as before. An
    # automatic promotion writes the shared `job_hunter_company_watch_health`
    # instead, keyed on #198's company entity rather than a normalized name
    # of its own, so a second user's promotion of the same employer
    # converges on one row rather than duplicating it. The two pools are
    # never merged: a company that is both manually watched by one user and
    # automatically discovered is checked from both, which is an accepted,
    # rare duplicate rather than a reason to let one user's manual intent be
    # silently upgraded by another user's automatic discovery. See the
    # migration's note for the full reasoning.
    # ------------------------------------------------------------------

    def upsert_company_watch(
        self,
        *,
        company_name: str,
        careers_url: str,
        ats_provider: str | None,
        ats_identifier: str | None,
        discovered_from_job_id: str | None,
        promotion_source: str,
        confidence: float,
    ) -> str | None:
        """Insert or safely upgrade one company watch target.

        Dispatches on `promotion_source`, the only thing that decides which
        of the two pools above this call belongs to. The signature is
        unchanged since before #204 -- `watchlist.py`'s two callers,
        `sync_manual_watch_seeds` and `promote_company`, need no change.
        `promote_company` already returns `str | None` (None for a
        non-promotable evaluation), so a skipped automatic promotion
        returning None costs neither caller a change.

        An automatic promotion returns None, skipping the write entirely,
        when `can_write_shared_rows` is False. Before #204 this call wrote
        the per-user `job_hunter_company_watch` over PostgREST, which needs
        no privileged connection and so kept working in the degraded,
        no-`SUPABASE_DB_URL` mode `run_pipeline` otherwise still scores and
        delivers in. Since the automatic pool is now the shared,
        privileged-connection-only `job_hunter_company_watch_health`,
        attempting it there would raise `SharedWriteUnavailable` on every
        promotable evaluation -- caught by `pipeline.py`'s broad `except
        Exception` around this call, but counted as a run error on every
        one, which is a regression from a silent, working write. A manual
        promotion is unaffected: it never touches the privileged
        connection.
        """
        if promotion_source == "manual":
            return self._upsert_manual_company_watch(
                company_name=company_name,
                careers_url=careers_url,
                ats_provider=ats_provider,
                ats_identifier=ats_identifier,
                confidence=confidence,
            )
        if promotion_source == "automatic":
            if not self.can_write_shared_rows:
                logger.info(
                    "company watch promotion for %r skipped: no privileged "
                    "connection to write the shared endpoint",
                    company_name,
                )
                return None
            return self._promote_shared_company_watch(
                company_name=company_name,
                careers_url=careers_url,
                ats_provider=ats_provider,
                ats_identifier=ats_identifier,
                discovered_from_job_id=discovered_from_job_id,
                confidence=confidence,
            )
        raise ValueError(f"unsupported promotion_source: {promotion_source!r}")

    def _upsert_manual_company_watch(
        self,
        *,
        company_name: str,
        careers_url: str,
        ats_provider: str | None,
        ats_identifier: str | None,
        confidence: float,
    ) -> str:
        """Insert or upgrade one user's manual company watch.

        Translates store.py:1235-1329, minus the cross-source merge that
        used to live here: since #204 this table holds manual watches only,
        so the strength/confidence ranking below only ever compares a
        manual watch against a later manual watch for the same company --
        the one case left that can actually happen on this table.

        The SQLite original did INSERT-OR-NOTHING, then re-read and UPDATEd
        the losing row. That is two statements in one transaction; over
        PostgREST it would be two requests with no transaction around them,
        so this reads first and then writes the fully-merged row through a
        single merge-duplicates upsert. A row written by anyone between the
        read and the write is not lost: the upsert collides on
        `unique (user_id, normalized_company_name)` and updates that row
        instead of adding a second one (see the dedicated test). What such a
        race can cost is the strength comparison, which was computed against
        the row we read -- acceptable because both workflows that write here
        share `concurrency: group: job-hunter-state`, so there is one writer.
        """
        normalized_name = normalize_company_name(company_name)
        if not normalized_name:
            raise ValueError("company_name must normalize to a non-empty value")

        provider = (ats_provider or "").strip().lower() or None
        identifier = (ats_identifier or "").strip() or None
        careers_url = (careers_url or "").strip()
        now = to_iso(datetime.now(timezone.utc))

        existing = self._client.select(
            "job_hunter_company_watch",
            params={
                "normalized_company_name": f"eq.{normalized_name}",
                "select": (
                    "company_name,careers_url,ats_provider,ats_identifier,"
                    "confidence,first_seen_at"
                ),
                "limit": "1",
            },
        )

        if existing:
            row = existing[0]
            replace_target = self._replaces_watch_target(
                row, careers_url, provider, identifier, confidence
            )
            values = {
                # The original's UPDATE never touched company_name, so the
                # display name stays as first discovered even when a later
                # call spells it differently.
                "company_name": row["company_name"],
                "careers_url": careers_url if replace_target else row["careers_url"],
                "ats_provider": provider if replace_target else row["ats_provider"],
                "ats_identifier": (
                    identifier if replace_target else row["ats_identifier"]
                ),
                "confidence": confidence if replace_target else row["confidence"],
                "first_seen_at": row["first_seen_at"],
            }
        else:
            values = {
                "company_name": company_name,
                "careers_url": careers_url,
                "ats_provider": provider,
                "ats_identifier": identifier,
                "confidence": confidence,
                "first_seen_at": now,
            }

        values["user_id"] = self._client.user_id
        values["normalized_company_name"] = normalized_name
        written = self._client.upsert(
            "job_hunter_company_watch",
            [touch(values)],
            on_conflict="user_id,normalized_company_name",
        )
        return written[0]["id"]

    def _promote_shared_company_watch(
        self,
        *,
        company_name: str,
        careers_url: str,
        ats_provider: str | None,
        ats_identifier: str | None,
        discovered_from_job_id: str | None,
        confidence: float,
    ) -> str:
        """Promote a discovered endpoint onto the shared watch-health row.

        Since #204 every automatic promotion writes here instead of the
        per-user table: the endpoint and its health are properties of the
        company, not of whichever user's evaluation triggered the
        promotion. The same strength/confidence ranking
        `_upsert_manual_company_watch` applies decides whether this
        candidate replaces what is already known -- applied here across
        every automatic promotion for the company, from any user, rather
        than one user's own repeated writes.

        Requires the privileged connection (#179): the shared row is not
        reachable over PostgREST at all. Read-then-write, same
        single-writer reasoning as the manual path -- both are called only
        from within one pipeline run under `concurrency: group:
        job-hunter-state`.
        """
        company_id = self._ensure_company_id(company_name)
        if company_id is None:
            raise ValueError("company_name must normalize to a non-empty value")

        provider = (ats_provider or "").strip().lower() or None
        identifier = (ats_identifier or "").strip() or None
        careers_url = (careers_url or "").strip()
        now = to_iso(datetime.now(timezone.utc))

        existing = self._shared_write(
            "select careers_url, ats_provider, ats_identifier, confidence, first_seen_at "
            "  from public.job_hunter_company_watch_health where company_id = %s::uuid",
            (company_id,),
        )

        if existing:
            existing_url, existing_provider, existing_identifier, existing_confidence, first_seen_at = existing[0]
            replace_target = self._replaces_watch_target(
                {
                    "careers_url": existing_url,
                    "ats_provider": existing_provider,
                    "ats_identifier": existing_identifier,
                    "confidence": existing_confidence,
                },
                careers_url,
                provider,
                identifier,
                confidence,
            )
            write_url = careers_url if replace_target else existing_url
            write_provider = provider if replace_target else existing_provider
            write_identifier = identifier if replace_target else existing_identifier
            write_confidence = confidence if replace_target else existing_confidence
        else:
            first_seen_at = now
            write_url, write_provider, write_identifier, write_confidence = (
                careers_url,
                provider,
                identifier,
                confidence,
            )

        rows = self._shared_write(
            """
            insert into public.job_hunter_company_watch_health
              (company_id, careers_url, ats_provider, ats_identifier, confidence,
               discovered_from_job_id, first_seen_at, updated_at)
            values (%s::uuid, %s, %s, %s, %s, %s::uuid, %s::timestamptz, %s::timestamptz)
            on conflict (company_id) do update set
              careers_url = excluded.careers_url,
              ats_provider = excluded.ats_provider,
              ats_identifier = excluded.ats_identifier,
              confidence = excluded.confidence,
              discovered_from_job_id = coalesce(
                excluded.discovered_from_job_id,
                public.job_hunter_company_watch_health.discovered_from_job_id),
              updated_at = excluded.updated_at
            returning id
            """,
            (
                company_id,
                write_url,
                write_provider,
                write_identifier,
                write_confidence,
                discovered_from_job_id,
                first_seen_at,
                now,
            ),
        )
        return str(rows[0][0])

    def _ensure_company_id(self, company_name: str) -> str | None:
        """Return company_name's entity id, creating a bare stub if needed.

        An automatic watch can be discovered before #198's facet extraction
        has ever read this employer, so this cannot assume a row already
        exists. `on conflict (identity) do update set identity =
        excluded.identity` is a no-op write on an existing row -- it exists
        only to make `returning id` fire on a conflict too, so an existing
        company's real facets (industry, business model, ...) are never
        touched or replaced by this.

        A freshly-inserted stub must not look already read: the column
        defaults `extracted_at` to `now()`, which is right for
        `save_company_facets` (a real read just happened) and wrong here --
        a stub has no facets at all, and `companies_needing_facets` decides
        staleness from this column. Left at the default, the epoch this
        watch was promoted at would make `companies_needing_facets` believe
        the company's facts were already established and skip it forever.
        The epoch below is always older than any refresh interval, so the
        stub reads as never-extracted until a real extraction overwrites it.
        """
        identity = normalize_company_name(company_name)
        if not identity:
            return None
        rows = self._shared_write(
            """
            insert into public.job_hunter_companies
              (identity, display_name, extracted_at)
            values (%s, %s, to_timestamp(0))
            on conflict (identity) do update set identity = excluded.identity
            returning id
            """,
            (identity, company_name),
        )
        return str(rows[0][0])

    @staticmethod
    def _replaces_watch_target(
        row: dict[str, Any],
        careers_url: str,
        provider: str | None,
        identifier: str | None,
        confidence: float,
    ) -> bool:
        """Whether the candidate endpoint outranks the stored one."""
        existing_strength = _watch_endpoint_strength(
            row["careers_url"], row["ats_provider"], row["ats_identifier"]
        )
        candidate_strength = _watch_endpoint_strength(careers_url, provider, identifier)
        return candidate_strength > existing_strength or (
            candidate_strength == existing_strength and confidence > row["confidence"]
        )

    def get_company_watch(self, company_name: str) -> dict[str, Any] | None:
        """Return the effective company watch row, manual first then shared.

        A manual row wins when both exist: it is this user's stated
        intent, and callers such as `_persisted_watch_target` want that
        over an automatically-guessed endpoint. Neither table stores
        `promotion_source` any more, so it is synthesized on both paths --
        'manual' or 'automatic' -- for callers that still read it (e.g.
        `pipeline._watch_promotion_state`). `company_name` on the shared
        path comes from the joined company entity's `display_name`.
        """
        normalized_name = normalize_company_name(company_name)
        if not normalized_name:
            return None

        manual_rows = self._client.select(
            "job_hunter_company_watch",
            params={
                "normalized_company_name": f"eq.{normalized_name}",
                "limit": "1",
            },
        )
        if manual_rows:
            row = dict(manual_rows[0])
            row["promotion_source"] = "manual"
            return row

        shared_rows = self._client.select(
            "job_hunter_company_watch_health",
            params={
                "select": "*,company:job_hunter_companies!inner(display_name)",
                "company.identity": f"eq.{normalized_name}",
                "limit": "1",
            },
        )
        if not shared_rows:
            return None
        return self._shared_watch_row(shared_rows[0])

    @staticmethod
    def _shared_watch_row(row: dict[str, Any]) -> dict[str, Any]:
        """Flatten one embedded `job_hunter_company_watch_health` row.

        The joined company entity arrives nested under its `company` alias;
        every caller of `get_company_watch`/`list_due_company_watches`
        wants `company_name` alongside the endpoint columns, the same flat
        shape the per-user manual row already has.
        """
        row = dict(row)
        company = row.pop("company", None) or {}
        row["company_name"] = company.get("display_name", "")
        row["promotion_source"] = "automatic"
        return row

    def list_due_company_watches(self, now: datetime) -> list[dict[str, Any]]:
        """Return every active watch target whose health pause has expired.

        Since #204 this unions two pools: this user's own manual watches
        and every user's automatically-promoted endpoints, shared on
        `job_hunter_company_watch_health`. A company watched both ways is
        checked from both -- see the migration's note on why that is
        accepted rather than merged. `paused_until` is `timestamptz` in
        both tables, so PostgREST's `lte` comparison is already an instant
        comparison -- a pause stored at `+02:00` and a `now` given at
        `-04:00` compare as the instants they name. The combined list is
        sorted by `created_at`, which sorts correctly as a string because
        `to_iso` always renders a UTC-normalised offset.
        """
        timestamp = to_iso(_require_aware(now))
        due_filter = {
            "active": "eq.true",
            "or": f"(paused_until.is.null,paused_until.lte.{timestamp})",
        }

        manual_rows = self._client.select(
            "job_hunter_company_watch",
            params={**due_filter, "order": "created_at.asc"},
        )
        for row in manual_rows:
            row["promotion_source"] = "manual"

        shared_rows = self._client.select(
            "job_hunter_company_watch_health",
            params={
                **due_filter,
                "select": "*,company:job_hunter_companies!inner(display_name)",
                "order": "created_at.asc",
            },
        )
        normalized_shared = [self._shared_watch_row(row) for row in shared_rows]

        return sorted(
            [*manual_rows, *normalized_shared], key=lambda row: row["created_at"]
        )

    def record_watch_success(self, watch_id: str, now: datetime) -> None:
        """Record a verified endpoint check and clear its failure backoff.

        Translates store.py:1360-1375. Tries this user's own manual watch
        first; an id that matches nothing there is either unknown or names
        a shared automatic watch, which lives on
        `job_hunter_company_watch_health` and is written over the
        privileged connection (#179) since it has no per-user owner to
        write it as. A watch id that matches neither is a no-op, as the
        original's UPDATE always was for an unknown id.
        """
        timestamp = to_iso(_require_aware(now))
        updated = self._client.update(
            "job_hunter_company_watch",
            touch(
                {
                    "last_successful_check_at": timestamp,
                    "last_verified_at": timestamp,
                    "consecutive_failures": 0,
                    "paused_until": None,
                }
            ),
            params={"id": f"eq.{watch_id}"},
        )
        if updated:
            return
        self._shared_write(
            """
            update public.job_hunter_company_watch_health set
              last_successful_check_at = %s::timestamptz,
              last_verified_at = %s::timestamptz,
              consecutive_failures = 0,
              paused_until = null,
              updated_at = %s::timestamptz
            where id = %s::uuid
            """,
            (timestamp, timestamp, timestamp, watch_id),
        )

    def record_watch_failure(self, watch_id: str, now: datetime) -> None:
        """Increment endpoint failures and apply the deterministic 24h pause.

        Translates store.py:1377-1397. The original incremented and tested
        the counter inside one `UPDATE ... CASE WHEN`; PostgREST cannot
        express a self-referential update, so the counter is read first and
        the new value written back. Same single-writer reasoning as
        `_upsert_manual_company_watch`. Tries the manual watch first, same
        as `record_watch_success`, and falls back to the shared table over
        the privileged connection when the id names one of those instead.
        """
        normalized_now = _require_aware(now)
        manual_rows = self._client.select(
            "job_hunter_company_watch",
            params={
                "id": f"eq.{watch_id}",
                "select": "consecutive_failures",
                "limit": "1",
            },
        )
        if manual_rows:
            failures = manual_rows[0]["consecutive_failures"] + 1
            paused_until = (
                to_iso(normalized_now + _HEALTH_PAUSE)
                if failures >= _WATCH_PAUSE_THRESHOLD
                else None
            )
            self._client.update(
                "job_hunter_company_watch",
                touch({"consecutive_failures": failures, "paused_until": paused_until}),
                params={"id": f"eq.{watch_id}"},
            )
            return

        shared_rows = self._shared_write(
            "select consecutive_failures from public.job_hunter_company_watch_health "
            "where id = %s::uuid",
            (watch_id,),
        )
        if not shared_rows:
            return
        failures = shared_rows[0][0] + 1
        paused_until = (
            to_iso(normalized_now + _HEALTH_PAUSE)
            if failures >= _WATCH_PAUSE_THRESHOLD
            else None
        )
        self._shared_write(
            """
            update public.job_hunter_company_watch_health set
              consecutive_failures = %s, paused_until = %s::timestamptz,
              updated_at = %s::timestamptz
            where id = %s::uuid
            """,
            (failures, paused_until, to_iso(datetime.now(timezone.utc)), watch_id),
        )

    # ------------------------------------------------------------------
    # ATS registry
    # ------------------------------------------------------------------

    def upsert_ats_board(
        self,
        provider: str,
        board_identifier: str,
        company_name: str = "",
        market_hint: str = "",
    ) -> bool:
        """Insert or refresh one learned ATS board's discovery metadata.

        Since #203, board identity and health (`company_name`,
        `market_hint`, `first_seen_at`, `last_seen_at`, `active`,
        `rejected_reason`, ...) live on the shared `job_hunter_ats_boards`
        table, upserted here exactly as `job_hunter_ats_registry` used to
        be: on an existing board, updates display metadata and
        `last_seen_at` and reactivates it, but leaves `paused_until` and
        `consecutive_failures` untouched -- ordinary rediscovery must not
        bypass an unexpired pause; the board becomes due naturally once
        `paused_until` elapses. A board with a `rejected_reason` is never
        reactivated by rediscovery, so a board rejected once stays rejected
        (see `reject_ats_board`).

        `job_hunter_ats_registry` now only tracks that *this* user has seen
        the board, for `record_ats_eligible_jobs` to join against -- a bare
        `(user_id, provider, board_identifier)` row, written once and never
        updated again here.

        Returns True only when this user's own registry row was newly
        created -- i.e. the board is new to this user's crawl, whether or
        not another user already discovered it. `job_hunter_ats_boards` has
        no `updated_at` column, so no `touch` here.

        Since #179 the board half runs over the privileged connection, which
        is also what collapses the old read-then-write into one statement:
        PostgREST could not express "keep the stored value when the new one
        is blank" or "reactivate only an unrejected board", so both were
        decided in Python from a row read a moment earlier. In SQL they are
        the `on conflict` clause, and the window in which another run's
        write landed between the read and the write closes with them.
        """
        provider = provider.strip().lower()
        if provider not in _SUPPORTED_ATS_PROVIDERS:
            raise ValueError(f"unsupported ATS provider: {provider!r}")
        board_identifier = board_identifier.strip()
        now = to_iso(datetime.now(timezone.utc))

        self._shared_write(
            """
            insert into public.job_hunter_ats_boards
              (provider, board_identifier, company_name, market_hint,
               first_seen_at, last_seen_at)
            values (%s, %s, %s, %s, %s::timestamptz, %s::timestamptz)
            on conflict (provider, board_identifier) do update set
              -- A blank argument means "no new information", not "clear
              -- what is stored".
              company_name = coalesce(nullif(excluded.company_name, ''),
                                      public.job_hunter_ats_boards.company_name),
              market_hint = coalesce(nullif(excluded.market_hint, ''),
                                     public.job_hunter_ats_boards.market_hint),
              last_seen_at = excluded.last_seen_at,
              -- Ordinary rediscovery reactivates a board paused by health
              -- backoff, but never one rejected as an aggregator: that
              -- verdict is undone only by clear_ats_board_rejection.
              -- paused_until and consecutive_failures are left alone, so a
              -- board becomes due naturally rather than by being seen again.
              active = case
                when public.job_hunter_ats_boards.rejected_reason is null then true
                else public.job_hunter_ats_boards.active
              end
            """,
            (provider, board_identifier, company_name, market_hint, now, now),
        )

        existing_registry = self._client.select(
            "job_hunter_ats_registry",
            params={
                "provider": f"eq.{provider}",
                "board_identifier": f"eq.{board_identifier}",
                "select": "id",
                "limit": "1",
            },
        )
        if existing_registry:
            return False
        self._client.upsert(
            "job_hunter_ats_registry",
            [
                {
                    "user_id": self._client.user_id,
                    "provider": provider,
                    "board_identifier": board_identifier,
                }
            ],
            on_conflict="user_id,provider,board_identifier",
        )
        return True

    def upsert_ats_boards(self, references: list[tuple[str, str, str, str]]) -> int:
        """Register a run's distinct ATS boards, returning how many were new.

        Takes ``(provider, board_identifier, company_name, market_hint)``
        tuples, one per sighting, and asks the registry once per distinct
        ``(provider, board_identifier)``. Discovery sees a board once per job
        that references it -- thousands of sightings resolving to dozens of
        boards -- so collapsing here is what keeps the request count off the
        job count.

        Sightings of one board are *merged*, first non-empty value wins per
        field, rather than frozen at the first sighting. The per-job loop
        this replaced called `upsert_ats_board` once per sighting, and that
        method's `company_name or row["company_name"]` meant a later sighting
        backfilled a field an earlier one left blank. Keeping only the first
        sighting would drop that backfill for the whole run -- a board first
        seen with a blank company would stay blank, and a blank
        ``market_hint`` would cost it its place in `select_ats_boards`'
        market ranking.

        A board that fails to register is logged and skipped. Learning the
        registry is opportunistic; losing one board must not cost the run.
        """
        merged: dict[tuple[str, str], tuple[str, str]] = {}
        for provider, board_identifier, company_name, market_hint in references:
            key = (provider, board_identifier)
            seen_company, seen_market = merged.get(key, ("", ""))
            merged[key] = (
                seen_company or company_name,
                seen_market or market_hint,
            )

        newly_registered = 0
        for (provider, board_identifier), (company_name, market_hint) in merged.items():
            try:
                if self.upsert_ats_board(
                    provider=provider,
                    board_identifier=board_identifier,
                    company_name=company_name,
                    market_hint=market_hint,
                ):
                    newly_registered += 1
            except Exception:
                logger.exception(
                    "ATS board registration failed: provider=%s board=%s",
                    provider,
                    board_identifier,
                )
        return newly_registered

    def reject_ats_board(
        self, provider: str, board_identifier: str, reason: str, now: datetime
    ) -> None:
        """Deactivate a shared board and persist why, so rediscovery can't
        resurrect it.

        Since #203 this writes to the shared `job_hunter_ats_boards` table,
        and must only ever be called with a reason that generalizes across
        users -- an aggregator-detection verdict. The config denylist's
        "instant kill" is one user's policy and must never reach this
        method (see `sources/learned_ats.py`'s per-run exclusion).
        """
        timestamp = to_iso(_require_aware(now))
        self._shared_write(
            "update public.job_hunter_ats_boards "
            "   set active = false, rejected_reason = %s, last_checked_at = %s::timestamptz "
            " where provider = %s and board_identifier = %s",
            (reason, timestamp, provider, board_identifier),
        )

    def clear_ats_board_rejection(self, provider: str, board_identifier: str) -> None:
        """Reverse a shared rejection, putting the board back in the due rotation.

        The inverse of `reject_ats_board`, and the only code path that
        clears `rejected_reason`. Used when an operator names a board in
        `learned_ats_allowlist`, having judged its aggregator rejection
        wrong -- clearing it here recovers the board for every user, not
        just the one running this recovery.

        Scoped to rejected rows on purpose: a board deactivated by repeated
        404s carries no `rejected_reason`, and reviving it here would
        confuse "wrongly judged" with "broken", which health backoff owns.
        Clearing a board that was never rejected is a no-op.

        The original matched `lower(provider) = lower(?)`. PostgREST's
        case-insensitive operator is `ilike`, which would also read `_` and
        `%` in a board identifier as wildcards and could clear a
        neighbouring board's rejection, so the rejected rows are fetched and
        compared in Python instead. There are only ever a handful of them.
        """
        wanted = (provider.strip().lower(), board_identifier.strip().lower())
        for row in self._client.select(
            "job_hunter_ats_boards",
            params={
                "rejected_reason": "not.is.null",
                "select": "id,provider,board_identifier",
            },
        ):
            if (
                row["provider"].lower(),
                row["board_identifier"].lower(),
            ) != wanted:
                continue
            self._shared_write(
                "update public.job_hunter_ats_boards "
                "   set active = true, rejected_reason = null "
                " where id = %s::uuid",
                (row["id"],),
            )

    def list_due_ats_boards(self, now: datetime) -> list[AtsRegistryEntry]:
        """Return active shared ATS boards whose health pause has expired.

        Same `timestamptz` comparison as `list_due_company_watches`. Board
        health is shared since #203, so the board list itself is every
        user's answer, not just the caller's -- but `select_ats_boards`
        ranks that list by each board's *recent eligible yield*, which is
        per-user, so this merges in the caller's own
        `job_hunter_ats_registry` row for every board returned.
        """
        timestamp = to_iso(_require_aware(now))
        rows = self._client.select(
            "job_hunter_ats_boards",
            params={
                "active": "eq.true",
                "or": f"(paused_until.is.null,paused_until.lte.{timestamp})",
                "order": "provider.asc,board_identifier.asc",
            },
        )
        yield_by_board = {
            (yield_row["provider"], yield_row["board_identifier"]): yield_row
            for yield_row in self._client.select(
                "job_hunter_ats_registry",
                params={"select": "provider,board_identifier,eligible_jobs_seen,last_eligible_at"},
            )
        }
        entries = []
        for row in rows:
            yield_row = yield_by_board.get((row["provider"], row["board_identifier"]), {})
            entries.append(
                ats_entry_from_row(
                    row,
                    eligible_jobs_seen=yield_row.get("eligible_jobs_seen", 0),
                    last_eligible_at=yield_row.get("last_eligible_at"),
                )
            )
        return entries

    def list_rejected_ats_boards(self) -> list[AtsRegistryEntry]:
        """Return shared boards rejected as aggregators.

        `list_due_ats_boards` only returns active boards, so this is the
        only way to read a rejection (and its reason) back after the run
        that made it. Since #203 a config-denylist exclusion is never
        written here at all -- only an aggregator-detection verdict is.
        """
        rows = self._client.select(
            "job_hunter_ats_boards",
            params={
                "rejected_reason": "not.is.null",
                "order": "provider.asc,board_identifier.asc",
            },
        )
        return [ats_entry_from_row(row) for row in rows]

    def record_ats_scan_success(
        self, provider: str, board_identifier: str, now: datetime, job_count: int
    ) -> None:
        """Record a successful scan and clear the shared board's failure backoff."""
        timestamp = to_iso(_require_aware(now))
        self._shared_write(
            "update public.job_hunter_ats_boards set "
            "   last_checked_at = %s::timestamptz, last_success_at = %s::timestamptz, "
            "   last_job_count = %s, consecutive_failures = 0, paused_until = null "
            " where provider = %s and board_identifier = %s",
            (timestamp, timestamp, job_count, provider, board_identifier),
        )

    def record_ats_scan_failure(
        self,
        provider: str,
        board_identifier: str,
        now: datetime,
        *,
        permanent: bool = False,
    ) -> None:
        """Increment scan failures and back off the board.

        Translates store.py:1555-1620. Every failure is isolated per board
        and gets the same 24h pause so it retries soon. `consecutive_failures`
        is a single shared counter incremented by every failure, transient or
        permanent alike -- it does not track which kind of failure contributed
        to it. `permanent=True` (a stale-looking 404) additionally escalates:
        once that shared counter reaches `_STALE_BOARD_DEACTIVATION_THRESHOLD`,
        a permanent failure deactivates the board instead of just pausing it.
        Transient failures accrue toward the same counter but never deactivate
        a board on their own -- only a `permanent=True` call checks the
        threshold. So a mixed sequence such as [transient, transient,
        permanent] deactivates the board on that single 404, not only after
        three permanent failures in a row.

        Deactivation preserves registry history -- it is not a delete -- and
        `upsert_ats_board` already reactivates any inactive board the next
        time it is rediscovered. That reactivation only sets `active`; it
        does not reset `consecutive_failures`, so a rediscovered board that
        immediately fails again resumes from its prior strike count and can
        re-deactivate right away, not after three fresh strikes.

        The self-referential counter update was a read followed by a write
        only because PostgREST cannot express `col = col + 1`. Over the
        privileged connection (#179) it is one statement, so the increment
        and the threshold comparison see the same value and two runs failing
        the same board concurrently cannot both read the same count and both
        write it back as one more.

        A board that is not there is left alone, exactly as before: the
        `where` matches nothing and nothing is written.
        """
        normalized_now = _require_aware(now)
        self._shared_write(
            """
            update public.job_hunter_ats_boards set
              last_checked_at = %s::timestamptz,
              consecutive_failures = consecutive_failures + 1,
              paused_until = %s::timestamptz,
              active = case
                when %s and consecutive_failures + 1 >= %s then false
                else active
              end
             where provider = %s and board_identifier = %s
            """,
            (
                to_iso(normalized_now),
                to_iso(normalized_now + _HEALTH_PAUSE),
                permanent,
                _STALE_BOARD_DEACTIVATION_THRESHOLD,
                provider,
                board_identifier,
            ),
        )

    def record_ats_eligible_jobs(
        self, sightings: list[tuple[str, str]], now: datetime
    ) -> int:
        """Record a run's eligible jobs against their boards, in one round trip.

        Takes ``(provider, board_identifier)`` tuples, one per eligible job,
        and collapses them to one entry per distinct board before sending --
        the same shape and the same collapse as `upsert_ats_boards` above,
        for the same reason: discovery sees a board once per job, and the
        request count must follow the board count rather than the job count.

        The per-job version this replaces did a select followed by an update
        for every eligible job, which put roughly 2,700 serial round trips
        inside the daily run to increment a counter on a few dozen rows
        (issue #151). Returns how many registry rows were updated.

        A board absent from the registry is left alone rather than created,
        exactly as before. Learning the registry is opportunistic: a failure
        here is the caller's to log and skip, and must not cost the run.
        """
        timestamp = to_iso(_require_aware(now))
        counts: dict[tuple[str, str], int] = {}
        for provider, board_identifier in sightings:
            key = (provider, board_identifier)
            counts[key] = counts.get(key, 0) + 1
        if not counts:
            return 0
        rows = self._client.rpc(
            "job_hunter_record_ats_eligible_jobs",
            {
                "p_boards": [
                    {
                        "provider": provider,
                        "board_identifier": board_identifier,
                        "eligible_jobs": eligible_jobs,
                    }
                    for (provider, board_identifier), eligible_jobs in counts.items()
                ],
                "p_now": timestamp,
            },
            # This adds to a counter, so it is not idempotent: a retried call
            # would count the same run's eligible jobs twice. Same reasoning
            # as `merge_jobs`, the other non-idempotent RPC here.
            retry=False,
        )
        return int(rows[0]) if rows else 0

    def count_ats_boards(self) -> int:
        """Count every board the shared registry has ever learned, not just
        this user's."""
        return len(
            self._client.select("job_hunter_ats_boards", params={"select": "id"})
        )

    # ------------------------------------------------------------------
    # AI-accounting persistence
    # ------------------------------------------------------------------

    def record_ai_usage(
        self,
        *,
        occurred_at: str,
        provider: str,
        model: str,
        purpose: str,
        status: str,
        estimated_input_tokens: int,
        prompt_tokens: int | None = None,
        output_tokens: int | None = None,
        thinking_tokens: int | None = None,
        cached_tokens: int | None = None,
        total_tokens: int | None = None,
        http_status: int | None = None,
        error_code: str | None = None,
    ) -> None:
        """Record one provider attempt without persisting request or response content.

        Translates store.py:463-507 onto `job_hunter_ai_usage` (renamed from
        `gemini_usage`). `provider` is supplied by the adapter that made the
        call rather than left to the column's `'gemini'` default, so a second
        adapter writes its own rows correctly on the day it is added.

        `run_id` is NOT NULL after migration 202609060003 and is written as the
        migration's own backfill sentinel `'unknown'`. It survives as an inert
        annotation: nothing quota-related reads it, and issue #73 removed the
        `GEMINI_RUN_ID` plumbing that used to set it, because a per-run
        discriminator in a per-user ledger let one run's budget hide behind
        another's.

        That leaves the upsert conflict target -- `(user_id, run_id, model,
        purpose, occurred_at)`, the unique constraint the schema actually has
        -- effectively `(user_id, model, purpose, occurred_at)`, since `run_id`
        is now constant. Two attempts sharing a microsecond timestamp would
        collapse into one row; `occurred_at` comes from `datetime.now()`, so
        that is a theoretical loss rather than an observed one. `provider` is
        deliberately *not* in the target because it is not in the constraint:
        the second adapter this port exists to enable needs a migration that
        widens the unique index to include it, or two providers on the same
        model id would overwrite each other's ledger rows. That migration is a
        schema change with its own plan, not a side effect of this one.
        """
        self._client.upsert(
            "job_hunter_ai_usage",
            [
                {
                    "user_id": self._client.user_id,
                    "provider": provider,
                    "occurred_at": occurred_at,
                    "run_id": "unknown",
                    "model": model,
                    "purpose": purpose,
                    "status": status,
                    "estimated_input_tokens": estimated_input_tokens,
                    "prompt_tokens": prompt_tokens,
                    "output_tokens": output_tokens,
                    "thinking_tokens": thinking_tokens,
                    "cached_tokens": cached_tokens,
                    "total_tokens": total_tokens,
                    "http_status": http_status,
                    "error_code": error_code,
                }
            ],
            on_conflict="user_id,run_id,model,purpose,occurred_at",
        )

    def ai_usage_rows(
        self,
        start_at: str,
        end_at: str,
        *,
        provider: str,
        model: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return one provider's ledger rows in the half-open range [start_at, end_at).

        Translates store.py:509-530. `select` is scoped to exactly the columns
        the SQLite `SELECT *` returned (`gemini_usage` never had `prompt`/
        `response` columns to begin with -- see `record_ai_usage`'s
        docstring), so `id` is a random uuid, not the callers' former ordering
        proxy; `occurred_at` (with `select`'s `id.asc` tie-breaker) replaces it.
        """
        params: dict[str, str] = {
            "provider": f"eq.{provider}",
            "and": f"(occurred_at.gte.{start_at},occurred_at.lt.{end_at})",
            "select": (
                "id,occurred_at,run_id,model,purpose,status,estimated_input_tokens,"
                "prompt_tokens,output_tokens,thinking_tokens,cached_tokens,"
                "total_tokens,http_status,error_code"
            ),
            "order": "occurred_at.asc",
        }
        if model is not None:
            params["model"] = f"eq.{model}"
        return self._client.select("job_hunter_ai_usage", params=params)

    def set_ai_pause(
        self, provider: str, model: str, paused_until: str | None, reason: str
    ) -> None:
        """Persist the active quota pause for one provider model.

        Translates store.py:532-547 onto `job_hunter_ai_quota_state` (renamed
        from `ai_quota_state`). Upserts against `(user_id, provider,
        model)`, with `touch()` maintaining `updated_at` -- there is no
        trigger for it (see `store_mapping.touch`). `created_at` is
        deliberately omitted from the payload so an existing row's insertion
        time survives a later pause update, matching the original's
        `ON CONFLICT ... DO UPDATE SET` column list.
        """
        self._client.upsert(
            "job_hunter_ai_quota_state",
            [
                touch(
                    {
                        "user_id": self._client.user_id,
                        "provider": provider,
                        "model": model,
                        "paused_until": paused_until,
                        "reason": reason,
                    }
                )
            ],
            on_conflict="user_id,provider,model",
        )

    def get_ai_pause(self, provider: str, model: str) -> dict[str, Any] | None:
        """Return the persisted quota pause for a provider model, if present.

        Translates store.py:549-553.
        """
        rows = self._client.select(
            "job_hunter_ai_quota_state",
            params={
                "provider": f"eq.{provider}",
                "model": f"eq.{model}",
                "limit": "1",
            },
        )
        return rows[0] if rows else None

    def clear_ai_pause(self, provider: str, model: str) -> None:
        """Remove a provider model's persisted quota pause.

        Translates store.py:555-560.
        """
        self._client.delete(
            "job_hunter_ai_quota_state",
            params={"provider": f"eq.{provider}", "model": f"eq.{model}"},
        )

    # --- The platform key's own ledger (issue #128) ---------------------------
    #
    # Objective facet extraction is funded by a platform-owned key, and the
    # four methods below are the same four the per-user ledger offers, against
    # tables that carry no `user_id`. They are separate methods rather than an
    # `account=` argument on the ones above for the reason the tables are
    # separate: a shared allowance whose rows were filtered by the acting user
    # would read as untouched to every run, and the one thing this ledger
    # exists to answer is how close the shared key is to its ceiling.
    #
    # There is no `clear_platform_ai_pause`. `set_platform_ai_pause` with
    # `paused_until=None` is how a pause is lifted, and the tables carry no
    # delete policy at all -- see the migration.

    def record_platform_ai_usage(
        self,
        *,
        occurred_at: str,
        provider: str,
        model: str,
        purpose: str,
        status: str,
        estimated_input_tokens: int,
        prompt_tokens: int | None = None,
        output_tokens: int | None = None,
        thinking_tokens: int | None = None,
        cached_tokens: int | None = None,
        total_tokens: int | None = None,
        http_status: int | None = None,
        error_code: str | None = None,
    ) -> None:
        """Record one attempt made on the platform key.

        The upsert target is the table's own unique constraint, `(provider,
        model, purpose, occurred_at)`, so a retried POST converges instead of
        double-counting a call. No prompt or response content is written here,
        exactly as `record_ai_usage` writes none.
        """
        self._client.upsert(
            "job_hunter_platform_ai_usage",
            [
                {
                    "provider": provider,
                    "occurred_at": occurred_at,
                    "model": model,
                    "purpose": purpose,
                    "status": status,
                    "estimated_input_tokens": estimated_input_tokens,
                    "prompt_tokens": prompt_tokens,
                    "output_tokens": output_tokens,
                    "thinking_tokens": thinking_tokens,
                    "cached_tokens": cached_tokens,
                    "total_tokens": total_tokens,
                    "http_status": http_status,
                    "error_code": error_code,
                }
            ],
            on_conflict="provider,model,purpose,occurred_at",
        )

    def platform_ai_usage_rows(
        self,
        start_at: str,
        end_at: str,
        *,
        provider: str,
        model: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return the platform ledger's rows in the half-open range [start_at, end_at).

        The selected columns are exactly those `ai_usage_rows` returns minus
        `run_id`, which the platform ledger never had: the tracker reads
        `occurred_at`, the token columns and `status`, and nothing else.
        """
        params: dict[str, str] = {
            "provider": f"eq.{provider}",
            "and": f"(occurred_at.gte.{start_at},occurred_at.lt.{end_at})",
            "select": (
                "id,occurred_at,model,purpose,status,estimated_input_tokens,"
                "prompt_tokens,output_tokens,thinking_tokens,cached_tokens,"
                "total_tokens,http_status,error_code"
            ),
            "order": "occurred_at.asc",
        }
        if model is not None:
            params["model"] = f"eq.{model}"
        return self._client.select("job_hunter_platform_ai_usage", params=params)

    def set_platform_ai_pause(
        self, provider: str, model: str, paused_until: str | None, reason: str
    ) -> None:
        """Persist the platform key's active quota pause for one provider model."""
        self._client.upsert(
            "job_hunter_platform_ai_quota_state",
            [
                touch(
                    {
                        "provider": provider,
                        "model": model,
                        "paused_until": paused_until,
                        "reason": reason,
                    }
                )
            ],
            on_conflict="provider,model",
        )

    def get_platform_ai_pause(self, provider: str, model: str) -> dict[str, Any] | None:
        """Return the platform key's persisted pause for a provider model, if present."""
        rows = self._client.select(
            "job_hunter_platform_ai_quota_state",
            params={
                "provider": f"eq.{provider}",
                "model": f"eq.{model}",
                "limit": "1",
            },
        )
        return rows[0] if rows else None

    def get_candidate_context(self, cache_key: str) -> CandidateContextCacheEntry | None:
        """Return a cached candidate context, decoding its stored JSON payload.

        Translates store.py:562-576. `context_json` is jsonb; PostgREST hands
        it back already decoded, so no `json.loads` is needed here.
        """
        rows = self._client.select(
            "job_hunter_candidate_context_cache",
            params={"cache_key": f"eq.{cache_key}", "limit": "1"},
        )
        if not rows:
            return None
        row = rows[0]
        return CandidateContextCacheEntry(
            cache_key=row["cache_key"],
            profile_hash=row["profile_hash"],
            model=row["model"],
            schema_version=row["schema_version"],
            context=row["context_json"],
            created_at=row["created_at"],
        )

    def save_candidate_context(
        self,
        *,
        cache_key: str,
        profile_hash: str,
        model: str,
        schema_version: str,
        context: dict,
    ) -> None:
        """Persist a structured candidate context under its cache identity.

        Translates store.py:578-609. Upserts against `(user_id, cache_key)`.
        Unlike `set_ai_pause`, `created_at` is included in the payload:
        the SQLite original's own `ON CONFLICT ... DO UPDATE SET` refreshed
        `created_at = excluded.created_at` on every save, so this does too.
        """
        self._client.upsert(
            "job_hunter_candidate_context_cache",
            [
                {
                    "user_id": self._client.user_id,
                    "cache_key": cache_key,
                    "profile_hash": profile_hash,
                    "model": model,
                    "schema_version": schema_version,
                    "context_json": context,
                    "created_at": to_iso(datetime.now(timezone.utc)),
                }
            ],
            on_conflict="user_id,cache_key",
        )

    def enqueue_ai_work(self, work_type: str, job_id: str) -> None:
        """Idempotently enqueue deferred AI work and refresh its retry timestamp.

        Translates store.py:611-624. Upserts against `(user_id, work_type,
        job_id)`; `created_at` is left out of the payload so it keeps the
        table default on first insert and is never touched on a repeat call,
        matching the original's `DO UPDATE SET updated_at = excluded.updated_at`
        only. `touch()` sets `updated_at` -- there is no trigger for it.

        Follows a merge like `save_evaluation` does, and for the same reason:
        the queue's `(job_id, user_id)` foreign key means deferring a job that
        was merged away mid-run would otherwise raise, and a deferral that
        raises is a job dropped rather than retried tomorrow.
        """
        # The id it lands on is not returned: no caller needs it, and the
        # queue is re-read by id from the store on the next run anyway.
        self._write_following_merges(
            job_id, lambda target_id: self._write_pending_ai_work(work_type, target_id)
        )

    def _write_pending_ai_work(self, work_type: str, job_id: str) -> None:
        self._client.upsert(
            "job_hunter_pending_ai_work",
            [
                touch(
                    {
                        "user_id": self._client.user_id,
                        "work_type": work_type,
                        "job_id": job_id,
                    }
                )
            ],
            on_conflict="user_id,work_type,job_id",
        )

    def list_pending_ai_work(self, work_type: str) -> list[dict[str, Any]]:
        """Return pending rows for one AI-work category in stable retry order.

        Translates store.py:626-635. Ids are random uuids now, so `created_at`
        (with `select`'s `id.asc` tie-breaker) replaces `ORDER BY ..., job_id`
        as the retry-order proxy. This changes what a tie means: the original
        broke ties by `job_id`, a stable value, so two same-instant rows sorted
        the same way every run. Breaking ties by `id` instead orders same-instant
        rows deterministically within a single run, but that order is a random
        uuid comparison -- it is not stable across a reseed of the table.
        """
        return self._client.select(
            "job_hunter_pending_ai_work",
            params={"work_type": f"eq.{work_type}", "order": "created_at.asc"},
        )

    def closed_job_ids(self, job_ids: Iterable[str]) -> set[str]:
        """Which of `job_ids` are memberships of a posting found gone (#186).

        A freshness re-check closes a posting for everyone; this is how one
        user's run learns which of its candidates that happened to. Read
        through PostgREST like every other per-user read, so an id the caller
        does not hold simply never comes back.
        """
        ids = sorted({job_id for job_id in job_ids if job_id})
        closed: set[str] = set()
        for chunk in _chunked(ids, _URL_FILTER_CHUNK_SIZE):
            rows = self._client.select(
                "job_hunter_jobs",
                params={
                    "id": f"in.({','.join(chunk)})",
                    "select": "id,posting:job_hunter_postings!inner(closed_at)",
                    "posting.closed_at": "not.is.null",
                },
            )
            closed.update(row["id"] for row in rows)
        return closed

    def get_posting_description_hash(self, posting_id: str) -> str | None:
        """The advertisement's current content hash, by posting id (#257).

        Engine Lab's `posting_version` is this value: it already changes
        exactly when the posting's description does (`needs_evaluation`
        already keys re-evaluation off it), so it needs no parallel
        versioning scheme of its own.
        """
        rows = self._client.select(
            "job_hunter_postings",
            params={"id": f"eq.{posting_id}", "select": "description_hash"},
        )
        if not rows:
            return None
        return rows[0].get("description_hash")

    def complete_ai_work(self, work_type: str, job_id: str) -> None:
        """Remove a completed deferred AI-work item.

        Translates store.py:637-643.
        """
        self._client.delete(
            "job_hunter_pending_ai_work",
            params={"work_type": f"eq.{work_type}", "job_id": f"eq.{job_id}"},
        )

    # ------------------------------------------------------------------
    # Search profile
    # ------------------------------------------------------------------

    def get_provider_credentials(self) -> ProviderCredentials:
        """Return provider secrets exposed to this trusted Job Hunter runner.

        The RPC is the audited runner-claim security-definer exception. Its
        response is treated as untrusted at this boundary: malformed, unknown,
        or duplicate rows fail closed without including row values in errors,
        and response-bearing client failures are replaced without a chain.
        """
        request_error: SupabaseRequestError | None = None
        try:
            rows = self._client.rpc("job_hunter_get_provider_credentials")
        except SupabaseRequestError:
            request_error = SupabaseRequestError("provider credential request failed")
        # Raise after leaving the handler so the response-bearing original is
        # not retained as this value-free exception's implicit context.
        if request_error is not None:
            raise request_error from None
        if not isinstance(rows, list):
            raise ValueError("invalid provider credential response")

        credentials: dict[str, str] = {}
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("invalid provider credential response")
            provider = row.get("provider")
            secret = row.get("secret")
            if (
                not isinstance(provider, str)
                or provider not in {"gemini", "brave"}
                or provider in credentials
                or not isinstance(secret, str)
                or not secret.strip()
            ):
                raise ValueError("invalid provider credential response")
            credentials[provider] = secret

        return ProviderCredentials(
            gemini_api_key=credentials.get("gemini"),
            brave_search_api_key=credentials.get("brave"),
        )

    def get_source_documents(self) -> dict[str, str]:
        """Return the newest non-null CV and cover letter visible through RLS.

        Unknown, duplicate, or malformed material fails closed with a
        value-free error. Supabase response bodies and their exception chains
        are also removed at this sensitive boundary.
        """
        request_error: SupabaseRequestError | None = None
        try:
            rows = self._client.select(
                "source_documents",
                params={
                    "select": "kind,content,updated_at",
                    "order": "updated_at.desc",
                },
            )
        except SupabaseRequestError:
            request_error = SupabaseRequestError("source document request failed")
        # See the credential reader above: raising outside the handler avoids
        # retaining the original response body through ``__context__``.
        if request_error is not None:
            raise request_error from None
        if not isinstance(rows, list):
            raise ValueError("invalid source document response")

        documents: dict[str, str] = {}
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("invalid source document response")
            kind = row.get("kind")
            if not isinstance(kind, str) or kind not in {"cv", "cover_letter"}:
                raise ValueError("invalid source document response")
            if "content" not in row:
                raise ValueError("invalid source document response")
            content = row["content"]
            if content is None:
                continue
            if not isinstance(content, str) or kind in documents:
                raise ValueError("invalid source document response")
            documents[kind] = content
        return documents

    def get_search_profile(self) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        """Return the caller's search profile row and its market rows, if any.

        RLS scopes both selects to the acting user; no explicit user_id
        filter is needed.
        """
        profiles = self._client.select("job_hunter_search_profiles", params={"limit": "1"})
        if not profiles:
            return None
        profile_row = profiles[0]
        market_rows = self._client.select(
            "job_hunter_search_profile_markets",
            params={"profile_id": f"eq.{profile_row['id']}", "order": "position.asc"},
        )
        return profile_row, market_rows

    def save_search_profile(self, profile: SearchProfile) -> str:
        """Upsert the caller's one search profile and replace its market rows.

        Markets have no natural per-row update semantics from the caller's
        point of view (the profile is edited as a whole) -- existing market
        rows for this profile are deleted and replaced, matching the "one
        active search profile" model rather than trying to diff old and new
        market lists.
        """
        profile_row = touch({**profile.to_profile_row(), "user_id": self._client.user_id})
        written = self._client.upsert(
            "job_hunter_search_profiles", [profile_row], on_conflict="user_id"
        )
        profile_id = written[0]["id"]

        self._client.delete(
            "job_hunter_search_profile_markets", params={"profile_id": f"eq.{profile_id}"}
        )
        market_rows = profile.to_market_rows(profile_id, self._client.user_id)
        if market_rows:
            self._client.upsert(
                "job_hunter_search_profile_markets",
                market_rows,
                on_conflict="profile_id,market_id",
            )
        return profile_id

    # ------------------------------------------------------------------
    # Gmail sync and staging operations
    # ------------------------------------------------------------------

    def has_processed_gmail_message(self, message_id: str) -> bool:
        """Translates store.py:1679-1684."""
        rows = self._client.select(
            "job_hunter_gmail_messages",
            params={"message_id": f"eq.{message_id}", "select": "id", "limit": "1"},
        )
        return len(rows) > 0

    def record_gmail_message(
        self,
        *,
        message_id: str,
        thread_id: str | None,
        sender: str,
        subject: str,
        occurred_at: str,
        classification: str,
        confidence: float,
        rationale: str,
    ) -> None:
        """Record a classified Gmail message once, never reclassifying it.

        Translates store.py:1686-1717. The original's `INSERT OR IGNORE`
        leaves an already-recorded message untouched by a later call with a
        different classification; `SupabaseClient.upsert` always overwrites on
        conflict, so this checks for an existing row first and returns without
        writing when one is found, same pattern as `record_job_source`. A
        genuinely new message is inserted through `upsert` (rather than plain
        `insert`) so a retried POST on a transient 5xx converges instead of
        duplicating.
        """
        existing = self._client.select(
            "job_hunter_gmail_messages",
            params={"message_id": f"eq.{message_id}", "select": "id", "limit": "1"},
        )
        if existing:
            return
        self._client.upsert(
            "job_hunter_gmail_messages",
            [
                {
                    "user_id": self._client.user_id,
                    "message_id": message_id,
                    "thread_id": thread_id,
                    "sender": sender,
                    "subject": subject,
                    "occurred_at": occurred_at,
                    "classification": classification,
                    "confidence": confidence,
                    "rationale": rationale,
                    "processed_at": to_iso(datetime.now(timezone.utc)),
                }
            ],
            on_conflict="user_id,message_id",
        )

    def get_gmail_sync_state(self, account_id: str) -> dict[str, Any] | None:
        """Translates store.py:1719-1728.

        The SQLite original checked `sqlite_master` first because the table
        could be missing on an old database file; `job_hunter_gmail_sync_state`
        always exists under Postgres (migrations own the schema now), so that
        guard has no equivalent here.
        """
        rows = self._client.select(
            "job_hunter_gmail_sync_state",
            params={"account_id": f"eq.{account_id}", "limit": "1"},
        )
        return rows[0] if rows else None

    def save_gmail_sync_state(
        self,
        account_id: str,
        history_id: str | None,
        last_successful_sync_at: str | None,
        backfill_completed_at: str | None,
    ) -> None:
        """Translates store.py:1730-1759.

        Upserts against `(user_id, account_id)`. `created_at` is left out of
        the payload, same reasoning as `enqueue_ai_work`. `touch()` sets
        `updated_at`.
        """
        self._client.upsert(
            "job_hunter_gmail_sync_state",
            [
                touch(
                    {
                        "user_id": self._client.user_id,
                        "account_id": account_id,
                        "history_id": history_id,
                        "last_successful_sync_at": last_successful_sync_at,
                        "backfill_completed_at": backfill_completed_at,
                    }
                )
            ],
            on_conflict="user_id,account_id",
        )

    def stage_inbound_job(
        self,
        source_message_id: str,
        source_candidate_key: str,
        job: ExtractedJob,
    ) -> str:
        """Insert or refresh the last-seen time of one staged inbound candidate.

        Translates store.py:1761-1803. Upserts against `(user_id, origin,
        source_message_id, source_candidate_key)`. `created_at` is left out of
        the payload -- the original's `ON CONFLICT ... DO UPDATE SET` only
        ever touched `last_seen_at`, and omitting the column here reproduces
        that: the table default fills it on first insert, and a repeat call's
        merge-duplicates upsert leaves it alone. Returns the row's id directly
        from the upsert response rather than a follow-up `SELECT`.

        This is a deliberate divergence from the SQLite original beyond
        `created_at`: this `resolution=merge-duplicates` upsert overwrites
        every other column too -- `source_platform`, `source_job_id`, `url`,
        `company`, `title`, `location`, `remote`, and `description` -- on
        conflict, where the original's `DO UPDATE SET` only ever wrote
        `last_seen_at`. This is accepted because a restage of the same
        message and candidate key always carries the same extraction, so
        overwriting those columns with identical values is a no-op in
        practice.
        """
        written = self._client.upsert(
            "job_hunter_inbound_job_candidates",
            [
                {
                    "user_id": self._client.user_id,
                    "origin": "gmail",
                    "source_message_id": source_message_id,
                    "source_candidate_key": source_candidate_key,
                    "source_platform": job.source_platform,
                    "source_job_id": job.source_job_id,
                    "url": job.url or "",
                    "company": job.company or "",
                    "title": job.title or "",
                    "location": job.location or "",
                    "remote": job.remote,
                    "description": "",
                    "last_seen_at": to_iso(datetime.now(timezone.utc)),
                }
            ],
            on_conflict="user_id,origin,source_message_id,source_candidate_key",
        )
        return written[0]["id"]

    def list_eligible_inbound_jobs(self) -> list[dict[str, Any]]:
        """Return recent Gmail candidates whose matching job still needs work."""
        return self._client.rpc("job_hunter_eligible_inbound_jobs", {})

    def set_job_status(self, job_id: str, status: str) -> None:
        """Persist a terminal discovery status for a caller-owned logical job."""
        if status not in {"rejected", "closed"}:
            raise ValueError("status must be rejected or closed")
        self._client.update("job_hunter_jobs", {"status": status}, params={"id": f"eq.{job_id}"})

    def set_job_statuses(self, pairs: list[tuple[str, str]]) -> None:
        """Persist many terminal discovery statuses in as few requests as possible.

        Every status must be ``"rejected"`` or ``"closed"``, same as
        `set_job_status`. There are only ever those two values, so this
        groups ids by status and issues one PATCH per status per chunk
        (``id=in.(...)``) rather than a per-row RPC like `set_job_markets`
        needs for its arbitrary per-row values.

        Those ids ride in the query string, so the chunk size is
        `_URL_FILTER_CHUNK_SIZE`, not the larger body-carried
        `_ID_ARRAY_CHUNK_SIZE`: a normal run rejects thousands of jobs, and
        1,000 uuids in one filter is a ~36 KB request line -- a 414 that no
        retry rule covers.
        """
        by_status: dict[str, list[str]] = {}
        for job_id, status in pairs:
            if status not in {"rejected", "closed"}:
                raise ValueError("status must be rejected or closed")
            by_status.setdefault(status, []).append(job_id)
        for status, job_ids in by_status.items():
            for chunk in _chunked(job_ids, _URL_FILTER_CHUNK_SIZE):
                self._client.update(
                    "job_hunter_jobs",
                    {"status": status},
                    params={"id": f"in.({','.join(chunk)})"},
                )

    def save_application_event(
        self,
        *,
        job_id: str | None,
        event_type: str,
        occurred_at: str,
        source_message_id: str,
        source_thread_id: str | None,
        confidence: float,
        company: str,
        role_title: str,
        rationale: str,
        source: str = "gmail",
    ) -> str:
        """Record an application-lifecycle event once per source message.

        Translates store.py:1848-1890. `job_hunter_application_events` is
        unique on `(user_id, source_message_id)`; the original's
        `INSERT OR IGNORE` then re-`SELECT`ed by `source_message_id` regardless
        of whether the insert happened, always returning whichever row (new or
        pre-existing) owns that identity. This checks for an existing row
        first and returns its id unchanged rather than overwriting it, same
        pattern as `record_gmail_message`.
        """
        existing = self._client.select(
            "job_hunter_application_events",
            params={
                "source_message_id": f"eq.{source_message_id}",
                "select": "id",
                "limit": "1",
            },
        )
        if existing:
            return existing[0]["id"]
        written = self._client.upsert(
            "job_hunter_application_events",
            [
                {
                    "user_id": self._client.user_id,
                    "job_id": job_id,
                    "event_type": event_type,
                    "occurred_at": occurred_at,
                    "source": source,
                    "source_message_id": source_message_id,
                    "source_thread_id": source_thread_id,
                    "confidence": confidence,
                    "company": company,
                    "role_title": role_title,
                    "rationale": rationale,
                }
            ],
            on_conflict="user_id,source_message_id",
        )
        return written[0]["id"]

    def list_application_events(self, job_id: str) -> list[dict[str, Any]]:
        """Translates store.py:1892-1900.

        Ids are random uuids now, so `occurred_at` with `created_at` and
        `select`'s `id.asc` tie-breaker replaces `ORDER BY occurred_at, id`.
        """
        return self._client.select(
            "job_hunter_application_events",
            params={"job_id": f"eq.{job_id}", "order": "occurred_at.asc,created_at.asc"},
        )

    def current_application_state(self, job_id: str) -> str | None:
        """Translates store.py:1902-1905.

        Imported lazily, same as the original, to avoid a module-level import
        cycle between this module and `gmail_matching`.
        """
        from job_hunter.gmail_matching import derive_application_state

        return derive_application_state(self.list_application_events(job_id))

    def pending_review_events(self) -> list[dict[str, Any]]:
        """Return undelivered events needing human review, with their subject.

        Translates store.py:1907-1931 into a single call to
        `job_hunter_pending_review_events`, which reimplements the join
        against `job_hunter_gmail_messages` (for `subject`) and the anti-join
        against `job_hunter_review_deliveries` entirely in SQL. It `returns
        setof jsonb`, so `rpc` hands back a plain list of decoded event dicts
        (each already carrying `subject`) -- no key to unwrap.
        """
        return self._client.rpc(
            "job_hunter_pending_review_events",
            {"p_confidence_threshold": AUTO_CONFIDENCE_THRESHOLD},
        )

    def mark_review_delivered(
        self, event_ids: list[str], telegram_message_id: str
    ) -> None:
        """Translates store.py:1933-1946.

        Upserts against `(user_id, event_id)` rather than the original's
        `INSERT OR IGNORE` -- a repeat delivery of the same event with a
        different `telegram_message_id` overwrites it, but a retried POST on
        one call converges instead of duplicating, and no caller ever marks
        the same event delivered twice with different arguments in practice.
        """
        if not event_ids:
            return
        now = to_iso(datetime.now(timezone.utc))
        self._client.upsert(
            "job_hunter_review_deliveries",
            [
                {
                    "user_id": self._client.user_id,
                    "event_id": event_id,
                    "delivered_at": now,
                    "telegram_message_id": telegram_message_id,
                }
                for event_id in event_ids
            ],
            on_conflict="user_id,event_id",
        )

    def release_legacy_gmail_semantic_failures(self) -> int:
        """Remove only legacy synthetic technical reviews so Gmail can retry them.

        Translates store.py:1948-1993. Three deletes replace the original's
        single transaction (no cross-statement transaction exists over
        PostgREST): review deliveries and application events for the affected
        message ids are removed first (children before the parent), then the
        gmail messages themselves. Because these are three separate requests
        rather than one transaction, an intermediate state is observable --
        if the second delete fails after the first succeeded, legacy
        application events survive with their review-delivery records
        already gone, and a concurrent run of the review-delivery CLI could
        re-send a Telegram review card for an event that is about to be
        deleted here. Every step is individually idempotent (re-running this
        method again converges), so a subsequent run repairs the gap; there
        is no data-loss window, only a re-notify window.

        `LEGACY_SEMANTIC_FAILURE_RATIONALE` message ids are threaded through
        a PostgREST `in.(...)` filter; there being no matching messages
        short-circuits before any delete runs. The id lists are chunked at
        `_URL_FILTER_CHUNK_SIZE` ids per request -- see that constant's
        comment for why an unchunked `in.(...)` list is unsafe for a legacy
        backlog.
        """
        from job_hunter.gmail_models import LEGACY_SEMANTIC_FAILURE_RATIONALE

        rationale = LEGACY_SEMANTIC_FAILURE_RATIONALE
        messages = self._client.select(
            "job_hunter_gmail_messages",
            params={
                "classification": "eq.REVIEW_NEEDED",
                "rationale": f"eq.{rationale}",
                "select": "message_id",
            },
        )
        message_ids = [row["message_id"] for row in messages]
        if not message_ids:
            return 0

        event_ids: list[str] = []
        for chunk in _chunked(message_ids, _URL_FILTER_CHUNK_SIZE):
            events = self._client.select(
                "job_hunter_application_events",
                params={
                    "source": "eq.gmail",
                    "event_type": "eq.REVIEW_NEEDED",
                    "rationale": f"eq.{rationale}",
                    "source_message_id": f"in.({','.join(chunk)})",
                    "select": "id",
                },
            )
            event_ids.extend(row["id"] for row in events)

        for chunk in _chunked(event_ids, _URL_FILTER_CHUNK_SIZE):
            self._client.delete(
                "job_hunter_review_deliveries",
                params={"event_id": f"in.({','.join(chunk)})"},
            )
            self._client.delete(
                "job_hunter_application_events",
                params={"id": f"in.({','.join(chunk)})"},
            )

        for chunk in _chunked(message_ids, _URL_FILTER_CHUNK_SIZE):
            self._client.delete(
                "job_hunter_gmail_messages",
                params={"message_id": f"in.({','.join(chunk)})"},
            )
        return len(message_ids)

    def _job_has_dependencies(self, job_id: str) -> bool:
        """Translates `gmail_linkedin_cleanup.py`'s (deleted) `_job_has_dependencies`.

        One `select ... limit 1` per dependent table replaces the original's
        single-connection loop over the same four tables.

        `job_hunter_company_watch.discovered_from_job_id` used to belong on
        this list: that foreign key (migration 202609060002:128) had no `on
        delete cascade`, so a job that seeded a watch row would otherwise
        pass every check here and then fail the DELETE with a 409 mid-loop,
        after that message's other candidate rows were already deleted.
        Since #204 there is nothing left to protect against: the column is
        dropped from the per-user table entirely (only manual watches live
        there now, and a manual watch never carries a discovered-from job),
        and the shared `job_hunter_company_watch_health` keeps the column
        only as unenforced provenance -- no foreign key at all -- so a job
        delete can never fail on it.
        """
        for table, column in (
            ("job_hunter_evaluations", "job_id"),
            ("job_hunter_materials", "job_id"),
            ("job_hunter_deliveries", "job_id"),
            ("job_hunter_application_events", "job_id"),
        ):
            rows = self._client.select(
                table, params={column: f"eq.{job_id}", "select": "id", "limit": "1"}
            )
            if rows:
                return True
        return False

    def release_legacy_blank_linkedin_jobs(self) -> int:
        """Release only safe blank/poisoned LinkedIn Gmail artifacts for reprocessing.

        Translates `gmail_linkedin_cleanup.py`'s (deleted)
        `release_legacy_blank_linkedin_jobs`. The original ran one SQL query
        with a correlated `NOT EXISTS` anti-join to find messages whose
        LinkedIn candidates are *all* blank; PostgREST cannot express a
        correlated anti-join in one request, so this fetches every gmail/
        LinkedIn candidate row once and does the blank/populated split in
        Python instead (`trim(...) = ''` becomes `.strip()`, `lower(...) =
        'linkedin'` becomes an `ilike` exact-match filter: without a `*`
        wildcard it's case-insensitive exact-match for the literal
        `linkedin`, true here because that string has no `_` in it -- `_`
        is still a single-character LIKE wildcard even with no `*` present,
        as Task 10 already ruled on for `clear_ats_board_rejection`).

        A message is still only released when every step confirms safety:
        its gmail message exists and is classified `JOB_ALERT`, every
        matching `job_hunter_jobs` row is blank or carries the known
        ``Sign in`` poison title, and none of those jobs has a dependent
        evaluation, material, delivery, or application event. As with
        `release_legacy_gmail_semantic_failures`, there is no cross-request
        transaction: each message's deletes (candidates, then jobs, then the
        gmail message) run as separate idempotent requests, so a failure
        partway through is repaired by a subsequent run rather than left
        half-applied indefinitely.
        """
        candidates = self._client.select(
            "job_hunter_inbound_job_candidates",
            params={
                "origin": "eq.gmail",
                "source_platform": "ilike.linkedin",
                "select": "id,source_message_id,source_candidate_key,company,title",
            },
        )

        blank_by_message: dict[str, list[dict[str, Any]]] = defaultdict(list)
        populated_messages: set[str] = set()
        for row in candidates:
            message_id = row["source_message_id"]
            if (row.get("company") or "").strip() or (row.get("title") or "").strip():
                populated_messages.add(message_id)
            else:
                blank_by_message[message_id].append(row)

        candidate_message_ids = [
            message_id
            for message_id in blank_by_message
            if message_id not in populated_messages
        ]
        if not candidate_message_ids:
            return 0

        job_alert_messages: set[str] = set()
        for chunk in _chunked(candidate_message_ids, _URL_FILTER_CHUNK_SIZE):
            rows = self._client.select(
                "job_hunter_gmail_messages",
                params={
                    "classification": "eq.JOB_ALERT",
                    "message_id": f"in.({','.join(chunk)})",
                    "select": "message_id",
                },
            )
            job_alert_messages.update(row["message_id"] for row in rows)

        released = 0
        for message_id in candidate_message_ids:
            if message_id not in job_alert_messages:
                continue

            message_candidates = blank_by_message[message_id]
            job_ids: list[str] = []
            safe = True
            for candidate in message_candidates:
                jobs = self._client.select(
                    "job_hunter_jobs",
                    params={
                        "posting.source": "eq.gmail:linkedin",
                        "posting.source_job_id": f"eq.{candidate['source_candidate_key']}",
                        "select": "id,posting:job_hunter_postings!inner(company,title)",
                    },
                )
                for job in jobs:
                    facts = posting_facts(job)
                    if not _is_legacy_poisoned_linkedin_job(
                        facts.get("company") or "", facts.get("title") or ""
                    ):
                        safe = False
                        break
                    if self._job_has_dependencies(job["id"]):
                        safe = False
                        break
                    job_ids.append(job["id"])
                if not safe:
                    break

            if not safe:
                continue

            self._client.delete(
                "job_hunter_inbound_job_candidates",
                params={
                    "id": f"in.({','.join(candidate['id'] for candidate in message_candidates)})"
                },
            )
            for chunk in _chunked(job_ids, _URL_FILTER_CHUNK_SIZE):
                self._client.delete("job_hunter_jobs", params={"id": f"in.({','.join(chunk)})"})
            self._client.delete(
                "job_hunter_gmail_messages",
                params={"message_id": f"eq.{message_id}", "classification": "eq.JOB_ALERT"},
            )
            released += 1

        return released

    # ------------------------------------------------------------------
    # Navigation sessions
    # ------------------------------------------------------------------

    def create_navigation_session(self, session: NavigationSession) -> None:
        """Persist a Telegram navigation session's card list.

        Translates `navigation_store.py`'s (deleted) `create_navigation_session`.
        Upserts on `(user_id, session_id)` -- the table's unique constraint
        (migration 202609060002) -- so a retried POST converges instead of
        duplicating, and re-creating an existing session_id overwrites its
        cards cleanly. `cards_json` is a jsonb column; a plain list of
        `asdict(card)` dicts serializes correctly without a manual
        `json.dumps` -- PostgREST/`requests` encode it as a JSON array.
        `ensure_navigation_schema` (the SQLite original's lazy `CREATE TABLE
        IF NOT EXISTS`) has no equivalent here: migrations own the schema.
        """
        self._client.upsert(
            "job_hunter_telegram_navigation_sessions",
            [
                {
                    "user_id": self._client.user_id,
                    "session_id": session.session_id,
                    "cards_json": [asdict(card) for card in session.cards],
                    "telegram_message_id": session.telegram_message_id,
                    "created_at": session.created_at,
                    "expires_at": session.expires_at,
                }
            ],
            on_conflict="user_id,session_id",
        )

    def attach_navigation_message_id(self, session_id: str, message_id: str) -> bool:
        """Record the Telegram message id a navigation session was sent under.

        Translates `navigation_store.py`'s (deleted)
        `attach_navigation_message_id`. The SQLite original reported success
        via `cursor.rowcount`; PostgREST's `update` (default
        `return=representation`) hands back the updated rows instead, so
        "did a session with this id exist" becomes "is the list non-empty".
        """
        rows = self._client.update(
            "job_hunter_telegram_navigation_sessions",
            {"telegram_message_id": message_id},
            params={"session_id": f"eq.{session_id}"},
        )
        return len(rows) > 0

    def get_navigation_session(self, session_id: str) -> NavigationSession | None:
        """Translates `navigation_store.py`'s (deleted) `get_navigation_session`.

        The SQLite original caught "no such table" as "no session yet"
        because its schema was created lazily on first write. There is no
        such case here -- migrations always create the table -- so a
        missing session is simply an empty result set.
        """
        rows = self._client.select(
            "job_hunter_telegram_navigation_sessions",
            params={"session_id": f"eq.{session_id}", "limit": "1"},
        )
        if not rows:
            return None
        return navigation_session_from_row(rows[0])

    def prune_navigation_sessions(self, now_iso: str) -> int:
        """Delete expired navigation sessions and report how many were removed.

        Translates `navigation_store.py`'s (deleted)
        `prune_navigation_sessions`. `delete`'s default
        `return=representation` hands back the deleted rows, so the count is
        their length rather than a driver-level `rowcount`. `HttpClient`
        retries a DELETE on 5xx, so a first attempt that commits and then
        returns a 502 makes the retry see nothing left to delete and report
        0 instead of the true count. Harmless today -- every caller discards
        the return value -- but worth knowing if that ever changes.
        """
        rows = self._client.delete(
            "job_hunter_telegram_navigation_sessions",
            params={"expires_at": f"lt.{now_iso}"},
        )
        return len(rows)


# ----------------------------------------------------------------------------
# DryRunStore
# ----------------------------------------------------------------------------

# Every write method `PostgresJobStore` defines, mapped to the *shape* of the
# synthetic value `DryRunStore` fabricates in place of actually writing:
#
#   None            -- the real method returns None; so does the dry-run one.
#   "id"            -- the real method returns a new row's id; the dry-run
#                       one returns a fresh `uuid4()` string, never derived
#                       from any real row.
#   "bool"          -- the real method returns a boolean write outcome
#                       (e.g. "was this newly inserted"); the dry-run one
#                       always returns False, since nothing was inserted.
#   "count"         -- the real method returns how many rows were touched;
#                       the dry-run one always returns 0, since none were.
#   "job_upsert_results" -- the real batch method returns one
#                       (job id, inserted, description changed) tuple per
#                       input job; the dry-run one preserves that cardinality
#                       with a fresh synthetic id and two False outcomes.
#   a tuple of the above -- the real method returns a tuple; the dry-run one
#                       returns a tuple of the corresponding synthetic values.
#
# This is the enforcement mechanism for "a method added to PostgresJobStore
# later cannot silently become a writing method on DryRunStore": every
# public method PostgresJobStore defines must appear in either this mapping
# or `_POSTGRES_JOB_STORE_READ_METHODS` below, and
# `test_postgres_store_dry_run.py::test_every_public_method_is_classified`
# asserts that partition is exhaustive against `PostgresJobStore.__dict__`
# by name, not by re-deriving read/write from behaviour. A new method that
# is neither listed fails that test immediately -- including a new *write*
# method nobody remembered to add here, which is the failure mode that
# matters: without this check, `DryRunStore.__getattr__` (see below) would
# delegate it straight to the real store, and a "dry" run would mutate the
# live database.
_POSTGRES_JOB_STORE_WRITE_METHODS: dict[str, str | tuple[str, ...] | None] = {
    "upsert_job": ("id", "bool", "bool"),
    "upsert_logical_job": ("id", "bool", "bool"),
    "upsert_logical_jobs": "job_upsert_results",
    "merge_posting_batch": "posting_batch",
    "drain_extract_facets_queue": "empty_list",
    "merge_jobs": "id",
    "record_job_source": None,
    "set_job_market": None,
    "set_job_markets": None,
    "set_job_status": None,
    "set_job_statuses": None,
    "upsert_ats_boards": "count",
    "backfill_ats_identity": "count",
    "save_evaluation": "echo_job_id",
    "save_job_facets": None,
    "save_company_facets": None,
    "save_material": None,
    "mark_delivered": "echo_job_id",
    "upsert_company_watch": "id",
    "record_watch_success": None,
    "record_watch_failure": None,
    "upsert_ats_board": "bool",
    "reject_ats_board": None,
    "clear_ats_board_rejection": None,
    "record_ats_scan_success": None,
    "record_ats_scan_failure": None,
    "record_ats_eligible_jobs": "count",
    "record_ai_usage": None,
    "set_ai_pause": None,
    "clear_ai_pause": None,
    "record_platform_ai_usage": None,
    "set_platform_ai_pause": None,
    "save_candidate_context": None,
    "enqueue_ai_work": None,
    "complete_ai_work": None,
    "record_gmail_message": None,
    "save_gmail_sync_state": None,
    "stage_inbound_job": "id",
    "save_application_event": "id",
    "mark_review_delivered": None,
    "release_legacy_gmail_semantic_failures": "count",
    "release_legacy_blank_linkedin_jobs": "count",
    "create_navigation_session": None,
    "attach_navigation_message_id": "bool",
    "prune_navigation_sessions": "count",
    "save_search_profile": "id",
    # Per-user membership as an output of matching, not a precondition
    # (#243): idempotent insert-or-touch of one job_hunter_jobs row.
    "ensure_membership": "id",
}

# Every public method that only reads, plus `close`/`__enter__`/`__exit__`
# (no-ops beyond `close()` on the real class, safe to delegate unchanged).
# `client` is a property, not a method, and is exempted separately -- see
# `test_every_public_method_is_classified`.
_POSTGRES_JOB_STORE_READ_METHODS: frozenset[str] = frozenset(
    {
        "client",
        "close",
        # A property, and a question rather than an action: whether this store
        # holds the privileged connection the shared tables need (#179). A dry
        # run answers it truthfully, because the pipeline uses it to decide
        # what to skip and a dry run should skip exactly what a real run would.
        "can_write_shared_rows",
        # Classified here because it performs no write itself -- it is the
        # accessor for the privileged lease. Note that `DryRunStore` does NOT
        # delegate it: it names `platform_ingestion = None` directly, because
        # handing a dry run the raw lease would let a caller write platform
        # tables without passing through any store method, which is exactly
        # the fail-open this registry exists to close (#184).
        "platform_ingestion",
        # Classified as a read because it persists nothing: it resolves a job
        # to its posting and remembers, in this store's own memory, that the
        # run has spent a facet call on it. A dry run wants that bookkeeping
        # to happen exactly as a real run does -- suppressing it would let the
        # queue re-read a posting the run already read -- and letting it
        # happen writes no row.
        "note_facet_read_attempt",
        "get_job_facets",
        "jobs_needing_facets",
        "get_company_facets",
        "get_company_facets_bulk",
        "companies_needing_facets",
        "list_job_sources",
        "find_job_by_canonical_url",
        "find_job_by_ats",
        "find_job_by_identity",
        "count_jobs",
        "list_jobs_for_matching",
        # SQL ranking and hard blocking over the caller's whole corpus (#187):
        # no write, so a dry run answers it exactly as a real run would.
        "match_jobs",
        # Aggregate ineligible/qualified/unresolved counts (#243): the same
        # classification as match_jobs, no row bodies, no write.
        "match_state_counts",
        # The obligation a source imposes on a display surface (#184, read
        # by #188): resolved from the posting's source, writes nothing.
        "posting_display_credit",
        "get_job",
        "needs_evaluation",
        "needs_evaluation_bulk",
        "get_evaluation",
        "get_evaluations_bulk",
        "get_material",
        "resolve_merged_job_id",
        "has_delivery",
        "delivered_job_ids",
        "pending_delivery_job_ids",
        "closed_job_ids",
        # A posting's content hash, read for Engine Lab's `posting_version`
        # (#257). Writes nothing.
        "get_posting_description_hash",
        "get_company_watch",
        "list_due_company_watches",
        "list_due_ats_boards",
        "list_rejected_ats_boards",
        "count_ats_boards",
        "ai_usage_rows",
        "get_ai_pause",
        "platform_ai_usage_rows",
        "get_platform_ai_pause",
        "get_candidate_context",
        "list_pending_ai_work",
        "has_processed_gmail_message",
        "get_gmail_sync_state",
        "list_eligible_inbound_jobs",
        "list_application_events",
        "current_application_state",
        "pending_review_events",
        "get_navigation_session",
        "get_provider_credentials",
        "get_search_profile",
        "get_source_documents",
    }
)


def _synthesize(shape: str) -> Any:
    if shape == "id":
        return str(uuid.uuid4())
    if shape == "bool":
        return False
    if shape == "count":
        return 0
    raise AssertionError(f"unknown DryRunStore write shape: {shape!r}")  # pragma: no cover


def _make_dry_run_write(name: str, shape: str | tuple[str, ...] | None):
    """Build a `DryRunStore` method that never calls the real one.

    The wrapper discards argument values, except that a batch write preserves
    the input list's cardinality. It must never touch `self._store`,
    `self._store._client`, or any network call, which is what makes a dry run
    safe against the live database regardless of what the real method would
    have done with those arguments.
    """
    if shape is None:
        def _write(self, *args: Any, **kwargs: Any) -> None:
            return None
    elif shape == "echo_job_id":
        def _write(self, job_id: Any = None, *args: Any, **kwargs: Any) -> Any:
            # The real method returns the id it actually wrote against, which
            # differs from the one passed in only when the job was merged away
            # mid-run (#145). A dry run writes nothing, so nothing can have
            # moved under it: hand the caller its own id back. Synthesizing a
            # uuid here instead would put an id naming no row into the digest.
            return job_id
    elif shape == "posting_batch":
        def _write(self, *args: Any, **kwargs: Any) -> PostingBatch:
            # An empty batch is exactly what a store with no direct Postgres
            # connection returns, and every caller already handles it by
            # letting each job upsert resolve its own posting. A dry run's
            # job upserts write nothing either, so nothing downstream reads a
            # posting id that would have to be fabricated here.
            return PostingBatch()
    elif shape == "empty_list":
        def _write(self, *args: Any, **kwargs: Any) -> list[Any]:
            # `drain_extract_facets_queue` must never spend the platform key
            # or write a facet row in a dry run; an empty queue is exactly
            # what a store with no direct Postgres connection drains, and
            # every caller already treats "nothing drained" as "nothing to
            # report" rather than an error.
            return []

    elif shape == "job_upsert_results":
        def _write(
            self, jobs: list[Any] | None = None, *args: Any, **kwargs: Any
        ) -> list[tuple[Any, ...]]:
            result_shape = ("id", "bool", "bool")
            return [
                tuple(_synthesize(part) for part in result_shape)
                for _job in jobs or ()
            ]
    elif isinstance(shape, tuple):
        def _write(self, *args: Any, _shape=shape, **kwargs: Any) -> tuple[Any, ...]:
            return tuple(_synthesize(part) for part in _shape)
    else:
        def _write(self, *args: Any, _shape=shape, **kwargs: Any) -> Any:
            return _synthesize(_shape)
    _write.__name__ = name
    _write.__qualname__ = f"DryRunStore.{name}"
    return _write


class DryRunStore:
    """Wraps a real `PostgresJobStore`, discarding every write.

    Replaces `cli.py`'s old dry-run construction (`JobStore(db_path,
    read_only=True)` plus two `JobStore(":memory:")` ledgers). That
    `:memory:` copy was strictly weaker than this: it silently diverged from
    real state the moment a read depended on anything the in-memory copy
    hadn't independently been seeded with, and it still let writes happen
    -- just into a database nobody would ever look at, rather than
    preventing them. `DryRunStore` instead reads live data and provably
    never writes: every write method is replaced with a synthetic stand-in
    that fabricates a `uuid4()` string where the real method would return a
    new row's id, `False` for a boolean write outcome, `0` for a row count,
    the caller's own job id for a write that reports which row it landed on
    (`echo_job_id`), and `None` otherwise -- and does so WITHOUT calling the
    wrapped store,
    so a dry run cannot reach the client on any write path even if the real
    method's implementation changes.

    **Design choice and why:** an explicit write-method registry
    (`_POSTGRES_JOB_STORE_WRITE_METHODS`), asserted complete against
    `PostgresJobStore.__dict__` by a dedicated test, rather than a
    deny-list plus `__getattr__` fallback for everything else. The
    difference matters here specifically because the fallback direction is
    dangerous: `__getattr__` below delegates any name not found on this
    class straight to the wrapped `PostgresJobStore` instance. If a new
    write method were added to `PostgresJobStore` and nobody updated this
    class, a deny-list approach would fail *open* -- the unlisted method
    would delegate to the real store and write to the live database, which
    is exactly the failure mode a "dry run" exists to prevent. An
    allow-list of reads would fail closed (an `AttributeError` instead), but
    would also require this class to enumerate and re-implement every
    trivial read delegation for no safety benefit, and a merely-forgotten
    read is a much cheaper mistake than a merely-forgotten write. The
    completeness test closes the actual gap: it fails whenever
    `PostgresJobStore` gains *any* public method (read or write) that
    hasn't been consciously placed in one of the two registries, which is
    exactly the moment a human needs to decide which kind it is.
    """

    def __init__(self, store: "PostgresJobStore") -> None:
        self._store = store

    @property
    def client(self) -> Any:
        # `.client` hands back the live, fully write-capable SupabaseClient,
        # bypassing every write wrapper above. A dry run must never reach
        # it -- callers that need Supabase-backed behaviour (e.g. Brave
        # source discovery's persisted budget in `run_pipeline`) must not be
        # given a DryRunStore, or must be changed to not need `.client`.
        raise AssertionError("a dry run must not reach the client")

    #: The privileged connection is withheld from a dry run.
    #:
    #: `__getattr__` below fails open by design -- anything this class does not
    #: name reaches the real store. That is safe for read methods and for the
    #: write methods the registry stubs, but `_ingestion` is neither: it is the
    #: raw lease, and a caller holding it can write any platform table without
    #: passing through a store method at all. Naming it here is what stops
    #: `getattr(store, "platform_ingestion", None)` from handing a dry run a
    #: live connection to `job_hunter_source_cursors` and
    #: `job_hunter_source_crawls` (issue #184).
    platform_ingestion = None

    def __getattr__(self, name: str) -> Any:
        # Only reached for names DryRunStore doesn't define itself -- every
        # write method below is set directly on the class, so this path is
        # exclusively how reads reach the wrapped store.
        return getattr(self._store, name)

    def __enter__(self) -> "DryRunStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        pass


for _name, _shape in _POSTGRES_JOB_STORE_WRITE_METHODS.items():
    setattr(DryRunStore, _name, _make_dry_run_write(_name, _shape))
del _name, _shape
