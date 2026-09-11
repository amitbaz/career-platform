"""The privileged, user-free ``recheck_freshness`` stage (issue #186).

One message, one posting, one conditional request: does the advertisement
still exist, and does it still say what it said?

Imports nothing user-scoped at module level -- no store, no matching, no
scoring, no credentials -- so a worker holding only ingestion's privileged
connection can run it, exactly as `crawl_source.py` and `resolve_persist.py`
can (#183, constraint C1); `test_the_stage_module_imports_nothing_user_scoped`
keeps that true. The one deferral is the ATS board adapters, loaded on the
first board check (`_board`): their package imports the per-user store
module, which the worker then holds in memory but never constructs or calls.
Queue payloads carry a posting id and nothing else.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

import requests

from .availability import detect_closure
from .content_confidence import OFFICIAL_ATS
from .crawl_source import description_hash
from .http import Validators
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

#: The posting is gone. Never delivered again; its row stays.
CLOSED = "closed"
#: The posting is still up.
OPEN = "open"
#: The posting is up and its employer's own text has moved: updated, and
#: queued for objective extraction.
CHANGED = "changed"
#: The site answered, but not with anything that says whether the posting
#: exists -- bot protection, an auth wall. Recorded as a completed check, so
#: it is scheduled again rather than hammered, and the posting stays open.
UNVERIFIED = "unverified"
#: The site did not answer (timeout, 5xx); the queue retries the message.
FAILED = "failed"
#: The site asked us to slow down; the message is released, not blamed.
RATE_LIMITED = "rate_limited"

#: How long a rate-limited check waits when the site did not say.
_RATE_LIMITED_RETRY_SECONDS = 15 * 60

#: How long a claimed re-check stays invisible to other workers. It covers a
#: whole batch at the HTTP client's read budget, and is how long a worker run
#: may go without a heartbeat before `job_hunter_worker_health` reports it
#: unfinished (#258).
VISIBILITY_TIMEOUT_SECONDS = 15 * 60

#: The boards a posting can be re-checked on, by the adapter that crawls them.
_BOARD_PROVIDERS = frozenset({"greenhouse", "lever", "ashby"})


class _ConnectionLease(Protocol):
    def connection(self): ...


@dataclass(frozen=True)
class FreshnessOutcome:
    """What one re-check established about one posting."""

    posting_id: str
    outcome: str
    #: Why a posting was closed, in `job_hunter_postings.closed_reason`'s
    #: vocabulary. Empty for every other outcome.
    reason: str = ""


@dataclass(frozen=True)
class _Posting:
    id: str
    url: str
    canonical_url: str
    validators: Validators
    ats_provider: str
    ats_board: str
    ats_job_id: str
    content_confidence: str
    description_hash: str

    @property
    def on_a_board(self) -> bool:
        return (
            self.ats_provider in _BOARD_PROVIDERS
            and bool(self.ats_board)
            and bool(self.ats_job_id)
        )


@dataclass(frozen=True)
class _Board:
    """What one board answered, kept for the rest of the drain.

    `listings` is None when the board itself is gone. A 304 is not kept here:
    it answers the validators one posting sent, and says nothing to a posting
    on the same board that last looked at a different version of it. It is
    kept in `RecheckFreshnessStage._unchanged` instead, keyed by the
    validators it answered.
    """

    listings: dict[str, Any] | None
    validators: Validators


class RecheckFreshnessStage:
    """Re-check one posting and record what the check established.

    One instance serves one drain. It remembers every board it fetched, so
    re-checking forty postings on one board costs one request rather than
    forty -- the saving that makes the board the right channel at all.
    """

    def __init__(self, database: _ConnectionLease, http: Any) -> None:
        self._database = database
        self._http = http
        self._boards: dict[tuple[str, str], _Board] = {}
        #: Boards that answered 304 this drain, with the validators that got
        #: the 304. Postings on one board are usually all last checked from
        #: the same fetch, so they carry the same validators, and one 304
        #: answers for every one of them -- the common case of a board where
        #: nothing moved.
        self._unchanged: dict[tuple[str, str], Validators] = {}

    def __call__(self, message: QueueMessage) -> FreshnessOutcome:
        posting = self._read_posting(self._posting_id(message))
        if posting.on_a_board:
            return self._check_board(posting)
        return self._check_page(posting)

    def _check_board(self, posting: _Posting) -> FreshnessOutcome:
        """Is the posting still on its ATS board, and does it say the same?

        The board is read through the adapter that crawls it, so a listing's
        text comes back exactly as the stored text was written. That is what
        makes the hash comparison mean something -- and it is only made for an
        `official_ats` posting, the one kind whose stored text came from here.
        """
        key = (posting.ats_provider, posting.ats_board)
        board = self._boards.get(key)
        if board is None and self._unchanged.get(key) == posting.validators:
            # This drain already heard the board answer 304 to exactly these
            # validators: nothing on it moved since this posting last looked.
            return self._still_open(posting, posting.validators)
        if board is None:
            url, source = _board(*key)
            response = self._fetch(url, posting.validators)
            status = response.status_code
            if status == 304:
                self._unchanged[key] = posting.validators
                return self._still_open(
                    posting, _restated(response, posting.validators)
                )
            if status in (404, 410):
                board = _Board(listings=None, validators=Validators())
            elif status >= 400:
                return self._unverified(posting)
            else:
                try:
                    payload = response.json()
                except ValueError:
                    # A board that answers 200 with something that is not its
                    # JSON is not a board with no jobs on it.
                    return self._unverified(posting)
                board = _Board(
                    listings=_listings(source, posting.ats_board, payload),
                    validators=_restated(response, Validators()),
                )
            self._boards[key] = board

        if board.listings is None:
            return self._close(posting, "board_gone")
        listing = board.listings.get(posting.ats_job_id)
        if listing is None:
            return self._close(posting, "absent_from_board")
        if (
            posting.content_confidence == OFFICIAL_ATS
            and description_hash(listing.description) != posting.description_hash
        ):
            return self._changed(posting, listing.description, board.validators)
        return self._still_open(posting, board.validators)

    def _check_page(self, posting: _Posting) -> FreshnessOutcome:
        """Is the posting's own page still up? Never touches its description.

        The page is rarely the channel the stored text came from -- most
        postings were written from an aggregator's feed or an ATS API -- so
        its text would hash differently every time and buy a re-extraction on
        every check. The page answers existence, and only existence.
        """
        response = self._fetch(posting.url or posting.canonical_url, posting.validators)
        status = response.status_code
        if status == 304:
            return self._still_open(posting, _restated(response, posting.validators))
        if status in (404, 410):
            return self._close(posting, f"http_{status}")
        if status >= 400:
            return self._unverified(posting)
        if detect_closure(response.text):
            return self._close(posting, "closure_phrase")
        return self._still_open(posting, _restated(response, Validators()))

    def _fetch(self, url: str, validators: Validators) -> Any:
        """One conditional GET, with the failures that are not evidence raised.

        `retry=False`, because a failed check is retried by the queue -- with
        backoff and a dead-letter at the end -- rather than by sleeping inside
        the worker. What is returned is an answer from the site; what is
        raised is the site not answering, which says nothing about the job.
        """
        try:
            response = self._http.get(
                url, headers=validators.as_headers(), retry=False
            )
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
        if message.stage is not Stage.RECHECK_FRESHNESS:
            raise PermanentStageFailure("recheck_freshness received the wrong stage")
        if set(message.payload) != {"posting_id"}:
            raise PermanentStageFailure(
                "recheck_freshness payload must contain only posting_id"
            )
        posting_id = message.payload.get("posting_id")
        if not isinstance(posting_id, str):
            raise PermanentStageFailure("recheck_freshness posting_id must be a UUID")
        try:
            return str(uuid.UUID(posting_id))
        except ValueError as error:
            raise PermanentStageFailure(
                "recheck_freshness posting_id must be a UUID"
            ) from error

    def _read_posting(self, posting_id: str) -> _Posting:
        try:
            with self._database.connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "select id, url, canonical_url, "
                        "       freshness_etag, freshness_last_modified, "
                        "       ats_provider, ats_board, ats_job_id, "
                        "       content_confidence, description_hash "
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
            validators=Validators(etag=row[3] or "", last_modified=row[4] or ""),
            ats_provider=row[5] or "",
            ats_board=row[6] or "",
            ats_job_id=row[7] or "",
            content_confidence=row[8] or "",
            description_hash=row[9] or "",
        )

    def _changed(
        self, posting: _Posting, description: str, validators: Validators
    ) -> FreshnessOutcome:
        """Take the employer's new text, and queue it to be read again.

        One transaction for both, so a posting can never be left holding new
        text that nothing will ever extract. The hash is computed by Postgres
        exactly as `job_hunter_merge_posting_batch` computes it, and it is that
        hash -- not anything new -- that tells `extract_facets` its facets are
        stale and `job_hunter_needs_evaluation` that every user's score is.
        """
        try:
            with self._database.connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "update public.job_hunter_postings set "
                        "  description = %s, "
                        "  description_hash = "
                        "    encode(sha256(convert_to(%s, 'UTF8')), 'hex'), "
                        "  freshness_etag = %s, freshness_last_modified = %s, "
                        + _CHECKED
                        + " where id = %s",
                        (
                            description,
                            description,
                            validators.etag,
                            validators.last_modified,
                            posting.id,
                        ),
                    )
                PostgresStageQueue(self._database).enqueue(
                    Stage.EXTRACT_FACETS,
                    {"posting_id": posting.id},
                    connection=connection,
                )
        except Exception as error:
            raise TransientStageFailure("recording a changed posting failed") from error
        return FreshnessOutcome(posting.id, CHANGED)

    def _close(self, posting: _Posting, reason: str) -> FreshnessOutcome:
        self._write(
            "update public.job_hunter_postings set "
            "  closed_at = now(), closed_reason = %s, "
            + _CHECKED
            + " where id = %s",
            (reason, posting.id),
        )
        return FreshnessOutcome(posting.id, CLOSED, reason)

    def _unverified(self, posting: _Posting) -> FreshnessOutcome:
        """A completed check that learned nothing. Keeps the old validators."""
        self._write(
            "update public.job_hunter_postings set " + _CHECKED + " where id = %s",
            (posting.id,),
        )
        return FreshnessOutcome(posting.id, UNVERIFIED)

    def _still_open(self, posting: _Posting, validators: Validators) -> FreshnessOutcome:
        self._write(
            "update public.job_hunter_postings set "
            "  freshness_etag = %s, freshness_last_modified = %s, "
            + _CHECKED
            + " where id = %s",
            (validators.etag, validators.last_modified, posting.id),
        )
        return FreshnessOutcome(posting.id, OPEN)

    def _write(self, sql: str, params: tuple) -> None:
        try:
            with self._database.connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(sql, params)
        except Exception as error:
            raise TransientStageFailure("recording the re-check failed") from error


def _board(provider: str, board: str) -> tuple[str, Any]:
    """The URL a provider's adapter reads a whole board from, and the adapter.

    The one place a provider maps to its adapter. The URL is the adapter
    module's own template, so the re-check fetches exactly what the crawl
    fetches and the two can never drift apart.

    Imported here rather than at module scope: the `sources` package imports
    the per-user store, and this module must import nothing user-scoped. The
    same deferral `canonical.fetch_authoritative_description` makes, for a
    related reason.
    """
    from .sources import ashby, greenhouse, lever

    module, source, argument = {
        "ashby": (ashby, ashby.AshbySource, "board"),
        "greenhouse": (greenhouse, greenhouse.GreenhouseSource, "token"),
        "lever": (lever, lever.LeverSource, "site"),
    }[provider]
    return module._URL_TEMPLATE.format(**{argument: board}), source


class _Replay:
    """An HTTP client that answers every request with one payload.

    Lets an adapter parse a board this stage has already fetched -- through its
    own parser, unchanged -- without the adapter's own fetch, whose error
    handling swallows every failure into "this board yielded nothing". A gone
    board and a flaky one must not both read as "every posting on it closed".
    """

    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def get_json(self, _url: str, **_kwargs) -> Any:
        return self._payload


def _listings(source: Any, board: str, payload: Any) -> dict[str, Any]:
    """Every listing on a fetched board, by its ATS job id."""
    adapter = source(board, _Replay(payload))
    listings: dict[str, Any] = {}
    for job in adapter.discover():
        for key in (job.ats_job_id, job.source_job_id):
            if key:
                listings.setdefault(str(key), job)
    return listings


def _retry_after(response: Any) -> int:
    value = (getattr(response, "headers", None) or {}).get("Retry-After", "")
    return int(value) if str(value).isdigit() else _RATE_LIMITED_RETRY_SECONDS


def _restated(response: Any, fallback: Validators) -> Validators:
    """The validators to keep after `response`.

    A 304 may legally carry neither validator, and many servers send an
    empty-headed one; storing what it omitted would erase the validator it
    just confirmed is still good, and the posting would alternate conditional
    and full fetches forever. So each is kept from `fallback` unless the
    response restated it -- the same rule `HttpClient.get_json` applies to a
    source's cursor. A 200 passes an empty fallback: its validators replace
    the old ones outright.
    """
    headers = getattr(response, "headers", None) or {}
    return Validators(
        etag=headers.get("ETag", "") or fallback.etag,
        last_modified=headers.get("Last-Modified", "") or fallback.last_modified,
    )


#: Every completed check stamps the time and schedules the next one from the
#: posting's age, so the interval widens as the posting gets older.
_CHECKED = (
    "freshness_checked_at = now(), "
    "freshness_next_check_at = now() "
    "  + public.job_hunter_freshness_interval(now() - first_seen_at) "
)


# The consumer ---------------------------------------------------------------------


@dataclass
class FreshnessDrain:
    """What one drain of the recheck_freshness queue did, for its log line.

    `stopped_because` is there so an empty drain says why it was empty: a
    queue with nothing due, and a drain cut off by its own time budget, look
    the same from a count of zero (AGENTS.md rule 5).
    """

    claimed: int = 0
    outcomes: Counter = field(default_factory=Counter)
    stopped_because: str = ""
    queue_delays: QueueDelays = field(default_factory=QueueDelays)

    @property
    def completed(self) -> int:
        return sum(
            self.outcomes[name] for name in (OPEN, CLOSED, CHANGED, UNVERIFIED)
        )

    def summary(self) -> str:
        """One log line's worth, every outcome named even when it is zero."""
        counts = " ".join(
            f"{name}={self.outcomes[name]}"
            for name in (OPEN, CLOSED, CHANGED, UNVERIFIED, FAILED, RATE_LIMITED)
        )
        return (
            f"claimed={self.claimed} {counts} stopped_because={self.stopped_because}"
        )


def drain_recheck_freshness(
    database: _ConnectionLease,
    http: Any,
    *,
    limit: int,
    batch_size: int = 25,
    time_budget_seconds: float = 20 * 60,
    clock: Callable[[], float] = time.monotonic,
    on_batch: Callable[[FreshnessDrain], None] | None = None,
) -> FreshnessDrain:
    """Drain up to `limit` due re-checks, in batches, within a time budget.

    Its own process on its own schedule (`python -m job_hunter
    recheck-freshness`), sharing nothing with the daily run, so a slow pass
    cannot delay a crawl or an extraction. Failures take the queue's common
    path (`stage_queue.StageRunner`): a site that did not answer is retried
    with backoff and dead-lettered after a few attempts; a rate limit releases
    the message without counting one. The budget is checked between batches,
    never mid-request.

    The visibility timeout covers a whole batch at the HTTP client's read
    budget, so a slow batch is not redelivered to a second worker while the
    first is still on it.

    `on_batch` is called with the drain after every batch, including the one
    that finds the queue empty (#258).
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    runner = StageRunner(
        PostgresStageQueue(database),
        visibility_timeout_seconds=VISIBILITY_TIMEOUT_SECONDS,
    )
    stage = RecheckFreshnessStage(database, http)
    drain = FreshnessDrain()
    started = clock()

    while True:
        if drain.claimed >= limit:
            drain.stopped_because = "limit"
            break
        if clock() - started >= time_budget_seconds:
            drain.stopped_because = "time_budget"
            break
        seen = 0

        def handler(message: QueueMessage) -> FreshnessOutcome:
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
            Stage.RECHECK_FRESHNESS,
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
