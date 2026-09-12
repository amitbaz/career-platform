"""The `recheck_freshness` stage (issue #186), at its seam.

One queue message in, the local stack's postings changed, messages for the
next stage out -- with a fake HTTP session standing in for the internet, so
each test decides exactly what the employer's page or board answers. The
schedule half (the interval, which postings the cron tick enqueues) is pgTAP's,
in `supabase/tests/pgtap/job_hunter_posting_freshness.sql`.
"""

from __future__ import annotations

import uuid

import pytest
import requests

from job_hunter.http import HttpClient
from job_hunter.models import Evaluation, Job
from job_hunter.recheck_freshness_stage import RecheckFreshnessStage
from job_hunter.stage_queue import (
    QueueMessage,
    QuotaExhausted,
    Stage,
    TransientStageFailure,
)

#: The delivery floor the tests' evaluations are scored against.
_FLOOR = 61


class _Response:
    def __init__(self, status_code, *, text="", payload=None, headers=None):
        self.status_code = status_code
        self.text = text
        self.url = ""
        self.headers = headers or {}
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)


class _Web:
    """The internet, as far as one test is concerned: one answer per URL."""

    headers: dict[str, str] = {}

    def __init__(self, routes):
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    def request(self, method, url, **kwargs):
        self.calls.append((url, kwargs))
        answer = self.routes[url]
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _http(routes) -> tuple[HttpClient, _Web]:
    client = HttpClient()
    web = _Web(routes)
    client._session = web
    return client, web


def _evaluation(job_id: str) -> Evaluation:
    return Evaluation(
        job_id=job_id,
        total_score=_FLOOR,
        scores={},
        decision="possible_match",
        hard_blockers=[],
        strengths=[],
        gaps=[],
        salary_note="",
        location_note="",
        rationale="",
        model="test-model",
    )


def _posting_id_of(database, job_id: str) -> str:
    with database.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "select posting_id from public.job_hunter_jobs where id = %s",
                (job_id,),
            )
            return str(cursor.fetchone()[0])


def _posting(database, posting_id: str) -> dict:
    columns = (
        "closed_at",
        "closed_reason",
        "freshness_checked_at",
        "freshness_next_check_at",
        "freshness_etag",
        "description",
        "description_hash",
    )
    with database.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"select {', '.join(columns)} from public.job_hunter_postings "
                "where id = %s",
                (posting_id,),
            )
            return dict(zip(columns, cursor.fetchone()))


def _job_waiting_for_delivery(store, database, **job_fields) -> tuple[str, str]:
    """A job one user is due to be sent, and the posting behind it."""
    job = Job(
        source="x",
        source_job_id=f"freshness-{uuid.uuid4()}",
        title="Senior Product Engineer",
        **job_fields,
    )
    job_id, _, _ = store.upsert_job(job)
    store.save_evaluation(job_id, _evaluation(job_id))
    assert job_id in store.pending_delivery_job_ids(_FLOOR)
    return job_id, _posting_id_of(database, job_id)


def _set(database, posting_id: str, assignments: str, params: tuple = ()) -> None:
    with database.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"update public.job_hunter_postings set {assignments} where id = %s",
                (*params, posting_id),
            )


def _extraction_messages(database, posting_id: str) -> int:
    with database.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "select count(*) from pgmq.q_job_hunter_extract_facets "
                "where message->>'posting_id' = %s",
                (posting_id,),
            )
            return int(cursor.fetchone()[0])


def _message(posting_id: str) -> QueueMessage:
    return QueueMessage(
        stage=Stage.RECHECK_FRESHNESS,
        message_id=1,
        payload={"posting_id": posting_id},
    )


def test_a_posting_whose_page_is_gone_is_closed_and_no_longer_delivered(
    store, ingestion_database
):
    url = f"https://jobs.example.test/{uuid.uuid4()}"
    job_id, posting_id = _job_waiting_for_delivery(store, ingestion_database, url=url)
    http, _ = _http({url: _Response(404)})

    outcome = RecheckFreshnessStage(ingestion_database, http)(_message(posting_id))

    assert outcome.outcome == "closed"
    posting = _posting(ingestion_database, posting_id)
    assert posting["closed_at"] is not None
    assert posting["closed_reason"] == "http_404"
    assert job_id not in store.pending_delivery_job_ids(_FLOOR)
    # Closed, not deleted: the user's job still reads the advertisement.
    assert store.get_job(job_id).url == url


def test_a_page_that_says_the_posting_is_filled_closes_it(store, ingestion_database):
    url = f"https://jobs.example.test/{uuid.uuid4()}"
    job_id, posting_id = _job_waiting_for_delivery(store, ingestion_database, url=url)
    http, _ = _http(
        {url: _Response(200, text="<html><p>This position has been filled.</p></html>")}
    )

    outcome = RecheckFreshnessStage(ingestion_database, http)(_message(posting_id))

    assert (outcome.outcome, outcome.reason) == ("closed", "closure_phrase")
    assert _posting(ingestion_database, posting_id)["closed_reason"] == "closure_phrase"
    assert job_id not in store.pending_delivery_job_ids(_FLOOR)


def test_an_unchanged_posting_costs_one_conditional_request_and_nothing_else(
    store, ingestion_database
):
    url = f"https://jobs.example.test/{uuid.uuid4()}"
    job_id, posting_id = _job_waiting_for_delivery(store, ingestion_database, url=url)
    _set(ingestion_database, posting_id, "freshness_etag = %s", ('"v1"',))
    before = _posting(ingestion_database, posting_id)
    queued_before = _extraction_messages(ingestion_database, posting_id)
    http, web = _http({url: _Response(304)})

    outcome = RecheckFreshnessStage(ingestion_database, http)(_message(posting_id))

    assert outcome.outcome == "open"
    assert len(web.calls) == 1
    assert web.calls[0][1]["headers"]["If-None-Match"] == '"v1"'
    after = _posting(ingestion_database, posting_id)
    assert after["closed_at"] is None
    assert after["freshness_checked_at"] is not None
    assert after["freshness_next_check_at"] > after["freshness_checked_at"]
    assert (after["description"], after["description_hash"]) == (
        before["description"],
        before["description_hash"],
    )
    assert _extraction_messages(ingestion_database, posting_id) == queued_before
    assert job_id in store.pending_delivery_job_ids(_FLOOR)


def test_a_live_page_keeps_its_validator_for_the_next_check(store, ingestion_database):
    url = f"https://jobs.example.test/{uuid.uuid4()}"
    _, posting_id = _job_waiting_for_delivery(store, ingestion_database, url=url)
    http, _ = _http(
        {url: _Response(200, text="<html>Apply now</html>", headers={"ETag": '"v2"'})}
    )

    RecheckFreshnessStage(ingestion_database, http)(_message(posting_id))

    assert _posting(ingestion_database, posting_id)["freshness_etag"] == '"v2"'


def test_an_older_posting_waits_longer_for_its_next_check(store, ingestion_database):
    fresh_url = f"https://jobs.example.test/{uuid.uuid4()}"
    old_url = f"https://jobs.example.test/{uuid.uuid4()}"
    _, fresh = _job_waiting_for_delivery(store, ingestion_database, url=fresh_url)
    _, old = _job_waiting_for_delivery(store, ingestion_database, url=old_url)
    _set(ingestion_database, old, "first_seen_at = now() - interval '60 days'")
    live = _Response(200, text="<html>Apply now</html>")
    http, _ = _http({fresh_url: live, old_url: live})
    stage = RecheckFreshnessStage(ingestion_database, http)

    stage(_message(fresh))
    stage(_message(old))

    def wait(posting_id):
        posting = _posting(ingestion_database, posting_id)
        return posting["freshness_next_check_at"] - posting["freshness_checked_at"]

    assert wait(fresh).total_seconds() == 6 * 3600
    assert wait(old).total_seconds() == 7 * 24 * 3600


# The ATS channel ---------------------------------------------------------------
#
# A posting with a Greenhouse, Lever or Ashby identity is re-checked on its
# board, through the same API and the same adapter that wrote its text -- the
# one channel where a description hash can be compared with the stored one.


def _board_url(token: str) -> str:
    return f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"


def _board(*listings: tuple[str, str]) -> _Response:
    """A Greenhouse board answering with `(job_id, description)` listings."""
    return _Response(
        200,
        payload={
            "jobs": [
                {
                    "id": int(job_id),
                    "title": "Senior Product Engineer",
                    "absolute_url": f"https://job-boards.greenhouse.io/x/jobs/{job_id}",
                    "content": description,
                    "location": {"name": "Remote"},
                }
                for job_id, description in listings
            ]
        },
    )


def _ats_job_waiting_for_delivery(
    store, database, token: str, job_id: str, description: str
) -> tuple[str, str]:
    return _job_waiting_for_delivery(
        store,
        database,
        url=f"https://job-boards.greenhouse.io/{token}/jobs/{job_id}",
        description=description,
        content_confidence="official_ats",
        ats_provider="greenhouse",
        ats_board=token,
        ats_job_id=job_id,
    )


def test_a_posting_its_board_no_longer_lists_is_closed(store, ingestion_database):
    token = f"board-{uuid.uuid4().hex[:12]}"
    job_id, posting_id = _ats_job_waiting_for_delivery(
        store, ingestion_database, token, "1001", "Build things."
    )
    http, _ = _http({_board_url(token): _board(("2002", "Something else."))})

    outcome = RecheckFreshnessStage(ingestion_database, http)(_message(posting_id))

    assert (outcome.outcome, outcome.reason) == ("closed", "absent_from_board")
    assert job_id not in store.pending_delivery_job_ids(_FLOOR)


def test_a_posting_whose_board_is_gone_is_closed(store, ingestion_database):
    token = f"board-{uuid.uuid4().hex[:12]}"
    _, posting_id = _ats_job_waiting_for_delivery(
        store, ingestion_database, token, "1001", "Build things."
    )
    http, _ = _http({_board_url(token): _Response(404)})

    outcome = RecheckFreshnessStage(ingestion_database, http)(_message(posting_id))

    assert (outcome.outcome, outcome.reason) == ("closed", "board_gone")


def test_a_changed_description_is_updated_and_queued_for_re_extraction(
    store, ingestion_database
):
    token = f"board-{uuid.uuid4().hex[:12]}"
    job_id, posting_id = _ats_job_waiting_for_delivery(
        store, ingestion_database, token, "1001", "Build things."
    )
    before = _posting(ingestion_database, posting_id)
    queued_before = _extraction_messages(ingestion_database, posting_id)
    http, _ = _http({_board_url(token): _board(("1001", "Build better things."))})

    outcome = RecheckFreshnessStage(ingestion_database, http)(_message(posting_id))

    assert outcome.outcome == "changed"
    after = _posting(ingestion_database, posting_id)
    assert after["description"] == "Build better things."
    assert after["description_hash"] != before["description_hash"]
    assert after["closed_at"] is None
    assert _extraction_messages(ingestion_database, posting_id) == queued_before + 1
    # The same hash is what scoring's currency is decided from, so the user's
    # evaluation is now stale too -- the existing notion of a changed posting.
    assert store.needs_evaluation(job_id) is True


def test_an_unchanged_board_listing_changes_nothing(store, ingestion_database):
    token = f"board-{uuid.uuid4().hex[:12]}"
    _, posting_id = _ats_job_waiting_for_delivery(
        store, ingestion_database, token, "1001", "Build things."
    )
    before = _posting(ingestion_database, posting_id)
    queued_before = _extraction_messages(ingestion_database, posting_id)
    http, _ = _http({_board_url(token): _board(("1001", "Build things."))})

    outcome = RecheckFreshnessStage(ingestion_database, http)(_message(posting_id))

    assert outcome.outcome == "open"
    assert _posting(ingestion_database, posting_id)["description_hash"] == (
        before["description_hash"]
    )
    assert _extraction_messages(ingestion_database, posting_id) == queued_before


def test_a_description_from_another_channel_is_never_overwritten(
    store, ingestion_database
):
    """Only `official_ats` text came from the board, so only it is compared."""
    token = f"board-{uuid.uuid4().hex[:12]}"
    _, posting_id = _job_waiting_for_delivery(
        store,
        ingestion_database,
        url=f"https://job-boards.greenhouse.io/{token}/jobs/1001",
        description="An aggregator's rendering of the advert.",
        content_confidence="aggregator_text",
        ats_provider="greenhouse",
        ats_board=token,
        ats_job_id="1001",
    )
    before = _posting(ingestion_database, posting_id)
    http, _ = _http({_board_url(token): _board(("1001", "The employer's own text."))})

    outcome = RecheckFreshnessStage(ingestion_database, http)(_message(posting_id))

    assert outcome.outcome == "open"
    assert _posting(ingestion_database, posting_id)["description"] == (
        before["description"]
    )


def test_one_board_fetch_serves_every_posting_on_it(store, ingestion_database):
    token = f"board-{uuid.uuid4().hex[:12]}"
    _, first = _ats_job_waiting_for_delivery(
        store, ingestion_database, token, "1001", "Build things."
    )
    _, second = _ats_job_waiting_for_delivery(
        store, ingestion_database, token, "1002", "Ship things."
    )
    http, web = _http(
        {_board_url(token): _board(("1001", "Build things."), ("1002", "Ship things."))}
    )
    stage = RecheckFreshnessStage(ingestion_database, http)

    stage(_message(first))
    stage(_message(second))

    assert len(web.calls) == 1


# What is not evidence ------------------------------------------------------------
#
# Failing to reach a page never closes a posting. The same rule
# `availability.py` has always applied: a timeout, a 5xx, bot protection or a
# rate limit says something about the network or the site, and nothing about
# whether the job is still open.


@pytest.mark.parametrize(
    "answer",
    [requests.ConnectTimeout("too slow"), requests.ConnectionError("refused"), _Response(503)],
    ids=["timeout", "connection-error", "server-error"],
)
def test_an_unreachable_page_is_retried_rather_than_believed(
    store, ingestion_database, answer
):
    url = f"https://jobs.example.test/{uuid.uuid4()}"
    job_id, posting_id = _job_waiting_for_delivery(store, ingestion_database, url=url)
    http, _ = _http({url: answer})

    with pytest.raises(TransientStageFailure):
        RecheckFreshnessStage(ingestion_database, http)(_message(posting_id))

    assert _posting(ingestion_database, posting_id)["closed_at"] is None
    assert job_id in store.pending_delivery_job_ids(_FLOOR)


def test_a_flaky_board_is_retried_rather_than_closing_everything_on_it(
    store, ingestion_database
):
    token = f"board-{uuid.uuid4().hex[:12]}"
    job_id, posting_id = _ats_job_waiting_for_delivery(
        store, ingestion_database, token, "1001", "Build things."
    )
    http, _ = _http({_board_url(token): _Response(502)})

    with pytest.raises(TransientStageFailure):
        RecheckFreshnessStage(ingestion_database, http)(_message(posting_id))

    assert _posting(ingestion_database, posting_id)["closed_at"] is None
    assert job_id in store.pending_delivery_job_ids(_FLOOR)


def test_being_rate_limited_returns_the_check_to_the_queue(store, ingestion_database):
    url = f"https://jobs.example.test/{uuid.uuid4()}"
    _, posting_id = _job_waiting_for_delivery(store, ingestion_database, url=url)
    http, _ = _http({url: _Response(429, headers={"Retry-After": "120"})})

    with pytest.raises(QuotaExhausted) as raised:
        RecheckFreshnessStage(ingestion_database, http)(_message(posting_id))

    assert raised.value.retry_after_seconds == 120
    posting = _posting(ingestion_database, posting_id)
    assert posting["closed_at"] is None
    assert posting["freshness_checked_at"] is None


def test_a_blocked_page_is_recorded_as_unverified_and_stays_open(
    store, ingestion_database
):
    url = f"https://jobs.example.test/{uuid.uuid4()}"
    job_id, posting_id = _job_waiting_for_delivery(store, ingestion_database, url=url)
    http, _ = _http({url: _Response(403, text="<html>Access denied</html>")})

    outcome = RecheckFreshnessStage(ingestion_database, http)(_message(posting_id))

    assert outcome.outcome == "unverified"
    posting = _posting(ingestion_database, posting_id)
    assert posting["closed_at"] is None
    # A completed check, so it is scheduled again rather than retried at once.
    assert posting["freshness_checked_at"] is not None
    assert job_id in store.pending_delivery_job_ids(_FLOOR)


# What a closed posting stops ----------------------------------------------------


def test_a_closed_posting_is_not_read_for_facets(store, ingestion_database):
    """Extraction spends the platform key; an advertisement that is gone is
    not worth a read, whether the message was queued before or after it
    closed."""
    from job_hunter.extract_facets_stage import (
        ExtractFacetsStage,
        FacetsAlreadyCurrent,
    )

    url = f"https://jobs.example.test/{uuid.uuid4()}"
    job = Job(
        source="x",
        source_job_id=f"freshness-{uuid.uuid4()}",
        title="Senior Product Engineer",
        url=url,
        description="Build things.",
    )
    job_id, _, _ = store.upsert_job(job)
    posting_id = _posting_id_of(ingestion_database, job_id)
    _set(ingestion_database, posting_id, "closed_at = now(), closed_reason = 'http_404'")
    queued = _extraction_messages(ingestion_database, posting_id)

    store.upsert_job(job)
    result = ExtractFacetsStage(ingestion_database, ai=None)(
        QueueMessage(
            stage=Stage.EXTRACT_FACETS, message_id=1, payload={"posting_id": posting_id}
        )
    )

    assert _extraction_messages(ingestion_database, posting_id) == queued
    assert isinstance(result, FacetsAlreadyCurrent)


def test_an_unchanged_board_answers_once_for_every_posting_on_it(
    store, ingestion_database
):
    """The common case: nothing on the board moved since the postings on it
    were last checked. One 304 answers for all of them."""
    token = f"board-{uuid.uuid4().hex[:12]}"
    _, first = _ats_job_waiting_for_delivery(
        store, ingestion_database, token, "1001", "Build things."
    )
    _, second = _ats_job_waiting_for_delivery(
        store, ingestion_database, token, "1002", "Ship things."
    )
    for posting_id in (first, second):
        _set(ingestion_database, posting_id, "freshness_etag = %s", ('"board-v1"',))
    http, web = _http({_board_url(token): _Response(304)})
    stage = RecheckFreshnessStage(ingestion_database, http)

    outcomes = [stage(_message(first)), stage(_message(second))]

    assert [outcome.outcome for outcome in outcomes] == ["open", "open"]
    assert len(web.calls) == 1


def test_the_stage_module_imports_nothing_user_scoped():
    """The stage runs on a worker holding only ingestion's connection (#183,
    C1). Importing it must not pull in the per-user store, matching or
    credentials -- a claim its docstring makes and this is what keeps true."""
    import subprocess
    import sys

    probe = (
        "import sys, job_hunter.recheck_freshness_stage; "
        "loaded = [m for m in ('job_hunter.postgres_store', 'job_hunter.supabase_client', "
        "'job_hunter.config', 'job_hunter.matching', 'job_hunter.evaluation') "
        "if m in sys.modules]; "
        "print(','.join(loaded))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == ""
