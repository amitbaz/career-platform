"""The privileged, user-free ``recover_posting`` stage (issue #259).

One message, one posting, one attempt to turn a thin or missing description
into a trustworthy one. Imports nothing user-scoped at module level -- no
store, no matching, no credentials -- so a worker holding only the ingestion
connection can run it, exactly as `crawl_source.py` and
`recheck_freshness_stage.py` can (#183, constraint C1). Queue payloads carry a
posting id and nothing else.

Only the free canonical-resolution tiers `canonical.py` already implements
run here: a URL already on a supported ATS host (`direct`), an HTTP redirect
to one (`redirect`), or the one distinct embedded ATS link on the posting's
own page (`embedded`). The paid/public targeted-search tier is deliberately
out of scope -- see the design doc's scope section -- because it needs a
search backend and, in production, a per-user Brave key, the opposite of a
user-free ingestion stage.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

import requests

from .canonical import (
    description_adapter_for,
    parse_supported_ats_url,
    resolve_embedded_ats_link,
)
from .content_confidence import OFFICIAL_ATS, is_sufficient
from .models import AtsReference
from .postgres_stage_queue import PostgresStageQueue
from .stage_queue import (
    PermanentStageFailure,
    QueueDelays,
    QueueMessage,
    QuotaExhausted,
    Stage,
    StageRunner,
    TransientStageFailure,
)

logger = logging.getLogger(__name__)

#: The attempt found enough to make the posting's content confidence
#: sufficient (a fresh fetch, or a race already won by another writer).
RECOVERED = "recovered"
#: The attempt completed and the posting is still insufficient.
UNRESOLVED = "unresolved"
#: The drain-level counters for a fetch that never completed as an attempt.
FAILED = "failed"
RATE_LIMITED = "rate_limited"

#: How long a rate-limited fetch waits when the site did not say.
_RATE_LIMITED_RETRY_SECONDS = 15 * 60

#: How long a claimed batch stays invisible to other workers.
VISIBILITY_TIMEOUT_SECONDS = 15 * 60


class _ConnectionLease(Protocol):
    def connection(self): ...


@dataclass(frozen=True)
class RecoveryOutcome:
    """What one attempt against one posting established."""

    posting_id: str
    outcome: str


@dataclass(frozen=True)
class _Posting:
    id: str
    url: str
    canonical_url: str
    company: str
    ats_provider: str
    ats_board: str
    ats_job_id: str
    content_confidence: str

    @property
    def known_ats(self) -> AtsReference | None:
        if self.ats_provider and self.ats_board and self.ats_job_id:
            return AtsReference(
                provider=self.ats_provider, board=self.ats_board, job_id=self.ats_job_id
            )
        return None


class _StrictHttp:
    """Adapts this stage's rate-limit-aware `_fetch` to the `get_json` shape
    the ATS adapters (`sources/ashby.py`, `lever.py`, `greenhouse.py`) call,
    so `RecoverPostingStage._fetch_description` sees `_fetch`'s
    `QuotaExhausted`/`TransientStageFailure` instead of `HttpClient.get_json`'s
    own retry-then-raise behaviour, which a broad `except Exception` around
    it would otherwise swallow (#259 review)."""

    def __init__(self, fetch: Callable[[str], Any]) -> None:
        self._fetch = fetch

    def get_json(self, url: str, **_kwargs: Any) -> Any:
        response = self._fetch(url)
        response.raise_for_status()
        return response.json()


class RecoverPostingStage:
    """Attempt one free canonical resolution against one posting."""

    def __init__(self, database: _ConnectionLease, http: Any) -> None:
        self._database = database
        self._http = http

    def __call__(self, message: QueueMessage) -> RecoveryOutcome:
        posting = self._read_posting(self._posting_id(message))

        if is_sufficient(posting.content_confidence):
            # Another writer (a richer crawl, another attempt) already
            # resolved this posting between it being enqueued and claimed.
            # No fetch needed; the trigger clears the schedule.
            return self._finish(posting.id, RECOVERED, content=None)

        ats = posting.known_ats or parse_supported_ats_url(posting.url)
        target_url = posting.canonical_url or posting.url
        if ats is None and posting.url:
            found = self._resolve_from_page(posting)
            if found is not None:
                ats, target_url = found

        if ats is None:
            return self._finish(posting.id, UNRESOLVED, content=None)

        description = self._fetch_description(ats, target_url)
        if not description:
            # An ATS reference is real evidence even without new text yet --
            # worth keeping so a later attempt (or recheck_freshness, once
            # the identity is known) does not have to rediscover it.
            return self._finish(
                posting.id, UNRESOLVED, content=None, ats=ats, target_url=target_url
            )

        return self._finish(
            posting.id, RECOVERED, content=description, ats=ats, target_url=target_url
        )

    def _resolve_from_page(
        self, posting: _Posting
    ) -> tuple[AtsReference, str] | None:
        """The `redirect` and `embedded` tiers: one fetch of the posting's own page."""
        response = self._fetch(posting.url)
        page_url = response.url or posting.url
        redirected = parse_supported_ats_url(page_url)
        if redirected is not None:
            return redirected, page_url

        embedded = resolve_embedded_ats_link(response.text, page_url)
        if embedded is not None:
            url, ats = embedded
            return ats, url
        return None

    def _fetch_description(self, ats: AtsReference, target_url: str) -> str | None:
        """Fetch the posting's full official text, with 429/5xx raised.

        Deliberately not `canonical.fetch_authoritative_description`: that
        wrapper swallows every failure into None, which is right for the
        legacy pipeline it serves and wrong here -- a rate limit or a
        board's own outage must reach the queue's retry/backoff, not be
        misrecorded as "no description found" (#259 review).
        """
        adapter = description_adapter_for(ats.provider)
        if adapter is None:
            return None
        try:
            return adapter.fetch_description(ats.board, target_url, _StrictHttp(self._fetch))
        except (QuotaExhausted, TransientStageFailure):
            raise
        except Exception:
            return None

    def _fetch(self, url: str) -> Any:
        """One GET, with the failures that are not evidence raised.

        `retry=False`: a failed fetch is retried by the queue, with its own
        backoff and eventual dead-letter, rather than by sleeping inside the
        worker. Every status this returns for -- including 404 -- is an
        answer the caller can reason about; only "the site did not answer at
        all" (429, 5xx, a network error) is raised.
        """
        try:
            response = self._http.get(url, retry=False)
        except requests.RequestException as error:
            raise TransientStageFailure(f"could not reach {url}") from error
        status = response.status_code
        if status == 429:
            raise QuotaExhausted(_retry_after(response))
        if status >= 500:
            raise TransientStageFailure(f"{url} answered {status}")
        return response

    @staticmethod
    def _posting_id(message: QueueMessage) -> str:
        if message.stage is not Stage.RECOVER_POSTING:
            raise PermanentStageFailure("recover_posting received the wrong stage")
        if set(message.payload) != {"posting_id"}:
            raise PermanentStageFailure(
                "recover_posting payload must contain only posting_id"
            )
        posting_id = message.payload.get("posting_id")
        if not isinstance(posting_id, str):
            raise PermanentStageFailure("recover_posting posting_id must be a UUID")
        try:
            return str(uuid.UUID(posting_id))
        except ValueError as error:
            raise PermanentStageFailure(
                "recover_posting posting_id must be a UUID"
            ) from error

    def _read_posting(self, posting_id: str) -> _Posting:
        try:
            with self._database.connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "select id, url, canonical_url, company, "
                        "       ats_provider, ats_board, ats_job_id, content_confidence "
                        "  from public.job_hunter_postings where id = %s",
                        (posting_id,),
                    )
                    row = cursor.fetchone()
        except Exception as error:
            raise TransientStageFailure("reading the posting failed") from error
        if row is None:
            raise PermanentStageFailure(f"posting_id={posting_id} no longer exists")
        return _Posting(
            id=str(row[0]),
            url=row[1] or "",
            canonical_url=row[2] or "",
            company=row[3] or "",
            ats_provider=row[4] or "",
            ats_board=row[5] or "",
            ats_job_id=row[6] or "",
            content_confidence=row[7] or "",
        )

    def _finish(
        self,
        posting_id: str,
        outcome: str,
        *,
        content: str | None,
        ats: AtsReference | None = None,
        target_url: str = "",
    ) -> RecoveryOutcome:
        """Record one completed attempt.

        Always stamps the bookkeeping columns and computes a fresh decaying
        next-attempt time from job_hunter_recovery_interval -- and lets
        job_hunter_posting_recovery_schedule (the trigger) override that to
        null in the same statement whenever content_confidence, set in the
        same SET list, is now sufficient. One transaction, so a posting can
        never be left holding new text nothing will ever extract.
        """
        set_parts = [
            "recovery_attempts = recovery_attempts + 1",
            "recovery_last_attempt_at = now()",
            "recovery_last_outcome = %s",
            "recovery_next_attempt_at = "
            "  now() + public.job_hunter_recovery_interval(now() - first_seen_at)",
        ]
        params: list[Any] = [outcome]
        if ats is not None:
            set_parts += [
                "canonical_url = %s",
                "ats_provider = coalesce(nullif(ats_provider, ''), %s)",
                "ats_board = coalesce(nullif(ats_board, ''), %s)",
                "ats_job_id = coalesce(nullif(ats_job_id, ''), %s)",
            ]
            params += [target_url, ats.provider, ats.board, ats.job_id]
        if content:
            set_parts += [
                "description = %s",
                "description_hash = encode(sha256(convert_to(%s, 'UTF8')), 'hex')",
                "content_confidence = %s",
            ]
            params += [content, content, OFFICIAL_ATS]
        params.append(posting_id)

        sql = "update public.job_hunter_postings set " + ", ".join(set_parts) + " where id = %s"
        try:
            with self._database.connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(sql, tuple(params))
                if content:
                    PostgresStageQueue(self._database).enqueue(
                        Stage.EXTRACT_FACETS,
                        {"posting_id": posting_id},
                        connection=connection,
                    )
        except Exception as error:
            raise TransientStageFailure("recording a recovery attempt failed") from error
        return RecoveryOutcome(posting_id, outcome)


def _retry_after(response: Any) -> int:
    value = (getattr(response, "headers", None) or {}).get("Retry-After", "")
    return int(value) if str(value).isdigit() else _RATE_LIMITED_RETRY_SECONDS


# The consumer ---------------------------------------------------------------------


@dataclass
class RecoveryDrain:
    """What one drain of the recover_posting queue did, for its log line."""

    claimed: int = 0
    outcomes: Counter = field(default_factory=Counter)
    stopped_because: str = ""
    queue_delays: QueueDelays = field(default_factory=QueueDelays)

    @property
    def completed(self) -> int:
        return sum(self.outcomes[name] for name in (RECOVERED, UNRESOLVED))

    def summary(self) -> str:
        counts = " ".join(
            f"{name}={self.outcomes[name]}"
            for name in (RECOVERED, UNRESOLVED, FAILED, RATE_LIMITED)
        )
        return (
            f"claimed={self.claimed} {counts} stopped_because={self.stopped_because}"
        )


def drain_recover_posting(
    database: _ConnectionLease,
    http: Any,
    *,
    limit: int,
    batch_size: int = 25,
    time_budget_seconds: float = 20 * 60,
    clock: Callable[[], float] = time.monotonic,
    on_batch: Callable[[RecoveryDrain], None] | None = None,
) -> RecoveryDrain:
    """Drain up to `limit` due recovery attempts, in batches, within a time budget.

    Its own process on its own schedule (`python -m job_hunter
    recover-posting`), sharing nothing with the other stages. Failures take
    the queue's common path (`stage_queue.StageRunner`): a site that did not
    answer is retried with backoff and dead-lettered after a few attempts,
    which only delays *this* message -- the posting itself is re-enqueued by
    the cron tick at its own next due time regardless. A rate limit releases
    the message without counting one. The budget is checked between batches,
    never mid-request.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    runner = StageRunner(
        PostgresStageQueue(database),
        visibility_timeout_seconds=VISIBILITY_TIMEOUT_SECONDS,
    )
    stage = RecoverPostingStage(database, http)
    drain = RecoveryDrain()
    started = clock()

    while True:
        if drain.claimed >= limit:
            drain.stopped_because = "limit"
            break
        if clock() - started >= time_budget_seconds:
            drain.stopped_because = "time_budget"
            break
        seen = 0

        def handler(message: QueueMessage) -> RecoveryOutcome:
            nonlocal seen
            seen += 1
            drain.queue_delays.observe(message)
            try:
                outcome = stage(message)
            except QuotaExhausted:
                drain.outcomes[RATE_LIMITED] += 1
                raise
            except Exception:
                drain.outcomes[FAILED] += 1
                raise
            drain.outcomes[outcome.outcome] += 1
            return outcome

        runner.run_once(
            Stage.RECOVER_POSTING,
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
