"""The `recover_posting` stage (issue #259), at its seam.

One queue message in, the local stack's posting changed, extract_facets
messages out for a genuine recovery -- with a fake HTTP session standing in
for the internet, so each test decides exactly what the employer's page or
ATS board answers. The schedule half (the interval, the trigger, which
postings the cron tick enqueues) is pgTAP's, in
`supabase/tests/pgtap/job_hunter_posting_recovery.sql`.
"""

from __future__ import annotations

import uuid

import pytest
import requests

from job_hunter.http import HttpClient
from job_hunter.recover_posting_stage import (
    RECOVERED,
    UNRESOLVED,
    RecoverPostingStage,
)
from job_hunter.stage_queue import (
    PermanentStageFailure,
    QueueMessage,
    QuotaExhausted,
    Stage,
    TransientStageFailure,
)


class _Response:
    def __init__(self, status_code, *, text="", payload=None, headers=None, url=""):
        self.status_code = status_code
        self.text = text
        self.url = url
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


def _insert_posting(database, **fields) -> str:
    posting_id = str(uuid.uuid4())
    columns = {
        "id": posting_id,
        "fingerprint": f"recovery-{uuid.uuid4()}",
        "url": "",
        "canonical_url": "",
        "company": "",
        "content_confidence": "partial_unknown",
        "ats_provider": None,
        "ats_board": None,
        "ats_job_id": None,
        "first_seen_at": "now()",
        "last_seen_at": "now()",
    }
    columns.update(fields)
    names = list(columns)
    placeholders = []
    values = []
    for name in names:
        if columns[name] == "now()":
            placeholders.append("now()")
        else:
            placeholders.append("%s")
            values.append(columns[name])
    with database.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"insert into public.job_hunter_postings ({', '.join(names)}) "
                f"values ({', '.join(placeholders)})",
                tuple(values),
            )
    return posting_id


def _posting(database, posting_id: str) -> dict:
    columns = (
        "description",
        "content_confidence",
        "canonical_url",
        "ats_provider",
        "ats_board",
        "ats_job_id",
        "recovery_attempts",
        "recovery_last_attempt_at",
        "recovery_last_outcome",
        "recovery_next_attempt_at",
    )
    with database.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"select {', '.join(columns)} from public.job_hunter_postings "
                "where id = %s",
                (posting_id,),
            )
            return dict(zip(columns, cursor.fetchone()))


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
        stage=Stage.RECOVER_POSTING, message_id=1, payload={"posting_id": posting_id}
    )


def test_a_posting_already_on_a_supported_ats_host_is_recovered_directly(
    ingestion_database,
):
    board_url = "https://jobs.lever.co/acme/abc-123"
    posting_id = _insert_posting(ingestion_database, url=board_url)
    http, web = _http(
        {
            "https://api.lever.co/v0/postings/acme?mode=json": _Response(
                200,
                payload=[
                    {
                        "hostedUrl": board_url,
                        "descriptionPlain": "A full official description.",
                    }
                ],
            )
        }
    )

    outcome = RecoverPostingStage(ingestion_database, http)(_message(posting_id))

    assert outcome.outcome == RECOVERED
    posting = _posting(ingestion_database, posting_id)
    assert posting["content_confidence"] == "official_ats"
    assert posting["description"] == "A full official description."
    assert posting["ats_provider"] == "lever"
    assert posting["ats_board"] == "acme"
    assert posting["ats_job_id"] == "abc-123"
    assert posting["recovery_attempts"] == 1
    assert posting["recovery_last_outcome"] == "recovered"
    assert posting["recovery_next_attempt_at"] is None
    assert _extraction_messages(ingestion_database, posting_id) == 1


def test_a_direct_ats_url_is_matched_by_itself_not_a_stale_canonical_url(
    ingestion_database,
):
    """A posting can carry a canonical_url left over from an earlier,
    unrelated resolution attempt that never matched anything. The direct
    tier's match came from posting.url, so that -- not the stale
    canonical_url -- must be what gets fetched against the board (#259
    review)."""
    board_url = "https://jobs.lever.co/acme/abc-123"
    posting_id = _insert_posting(
        ingestion_database,
        url=board_url,
        canonical_url="https://careers.example.test/stale-unrelated-page",
    )
    http, web = _http(
        {
            "https://api.lever.co/v0/postings/acme?mode=json": _Response(
                200,
                payload=[
                    {
                        "hostedUrl": board_url,
                        "descriptionPlain": "A full official description.",
                    }
                ],
            )
        }
    )

    outcome = RecoverPostingStage(ingestion_database, http)(_message(posting_id))

    assert outcome.outcome == RECOVERED
    posting = _posting(ingestion_database, posting_id)
    assert posting["description"] == "A full official description."
    assert posting["canonical_url"] == board_url


def test_a_redirect_to_a_supported_ats_host_is_recovered(ingestion_database):
    original_url = f"https://boards.example.test/{uuid.uuid4()}"
    board_url = "https://jobs.ashbyhq.com/acme/def-456"
    posting_id = _insert_posting(ingestion_database, url=original_url)
    http, web = _http(
        {
            original_url: _Response(200, text="", url=board_url),
            "https://api.ashbyhq.com/posting-api/job-board/acme?includeCompensation=true": _Response(
                200,
                payload={
                    "jobs": [
                        {
                            "jobUrl": board_url,
                            "descriptionPlain": "The redirected official text.",
                        }
                    ]
                },
            ),
        }
    )

    outcome = RecoverPostingStage(ingestion_database, http)(_message(posting_id))

    assert outcome.outcome == RECOVERED
    posting = _posting(ingestion_database, posting_id)
    assert posting["description"] == "The redirected official text."
    assert posting["canonical_url"] == board_url


def test_one_embedded_ats_link_on_the_page_is_recovered(ingestion_database):
    original_url = f"https://careers.example.test/{uuid.uuid4()}"
    board_url = "https://boards.greenhouse.io/acme/jobs/789"
    posting_id = _insert_posting(ingestion_database, url=original_url)
    http, web = _http(
        {
            original_url: _Response(
                200,
                text=f'<html><a href="{board_url}">Apply</a></html>',
                url=original_url,
            ),
            "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true": _Response(
                200,
                payload={
                    "jobs": [
                        {
                            "absolute_url": board_url,
                            "content": "<p>The embedded official text.</p>",
                        }
                    ]
                },
            ),
        }
    )

    outcome = RecoverPostingStage(ingestion_database, http)(_message(posting_id))

    assert outcome.outcome == RECOVERED
    posting = _posting(ingestion_database, posting_id)
    assert posting["description"] == "The embedded official text."
    assert posting["ats_provider"] == "greenhouse"


def test_a_page_with_two_distinct_embedded_ats_links_is_not_trusted(ingestion_database):
    original_url = f"https://careers.example.test/{uuid.uuid4()}"
    posting_id = _insert_posting(ingestion_database, url=original_url)
    http, web = _http(
        {
            original_url: _Response(
                200,
                text=(
                    '<html><a href="https://boards.greenhouse.io/acme/jobs/1">A</a>'
                    '<a href="https://boards.greenhouse.io/acme/jobs/2">B</a></html>'
                ),
                url=original_url,
            )
        }
    )

    outcome = RecoverPostingStage(ingestion_database, http)(_message(posting_id))

    assert outcome.outcome == UNRESOLVED
    posting = _posting(ingestion_database, posting_id)
    assert posting["ats_provider"] is None
    assert posting["recovery_attempts"] == 1
    assert posting["recovery_next_attempt_at"] is not None


def test_no_ats_reference_anywhere_is_unresolved_and_rescheduled(ingestion_database):
    original_url = f"https://careers.example.test/{uuid.uuid4()}"
    posting_id = _insert_posting(ingestion_database, url=original_url)
    http, web = _http({original_url: _Response(200, text="<html>nothing here</html>")})

    outcome = RecoverPostingStage(ingestion_database, http)(_message(posting_id))

    assert outcome.outcome == UNRESOLVED
    posting = _posting(ingestion_database, posting_id)
    assert posting["recovery_attempts"] == 1
    assert posting["recovery_last_outcome"] == "unresolved"
    assert posting["recovery_next_attempt_at"] is not None
    assert _extraction_messages(ingestion_database, posting_id) == 0


def test_an_ats_reference_with_no_matching_board_listing_keeps_identity_but_stays_unresolved(
    ingestion_database,
):
    board_url = "https://jobs.lever.co/acme/gone-999"
    posting_id = _insert_posting(ingestion_database, url=board_url)
    http, web = _http(
        {"https://api.lever.co/v0/postings/acme?mode=json": _Response(200, payload=[])}
    )

    outcome = RecoverPostingStage(ingestion_database, http)(_message(posting_id))

    assert outcome.outcome == UNRESOLVED
    posting = _posting(ingestion_database, posting_id)
    assert posting["ats_provider"] == "lever"
    assert posting["content_confidence"] == "partial_unknown"
    assert posting["recovery_next_attempt_at"] is not None


def test_a_posting_already_sufficient_by_the_time_it_is_claimed_needs_no_fetch(
    ingestion_database,
):
    posting_id = _insert_posting(
        ingestion_database,
        url="https://example.test/already-fine",
        content_confidence="official_ats",
    )
    http, web = _http({})

    outcome = RecoverPostingStage(ingestion_database, http)(_message(posting_id))

    assert outcome.outcome == RECOVERED
    assert web.calls == []
    posting = _posting(ingestion_database, posting_id)
    assert posting["recovery_attempts"] == 1
    assert posting["recovery_next_attempt_at"] is None


def test_a_rate_limited_board_response_on_the_direct_ats_path_is_released_too(
    ingestion_database,
):
    """The already-known-ATS path (no page fetch needed) must raise the same
    way the page-fetch path does -- a board rate limit is not "no description
    found" (#259 review)."""
    board_url = "https://jobs.lever.co/acme/abc-123"
    posting_id = _insert_posting(ingestion_database, url=board_url)
    http, web = _http(
        {
            "https://api.lever.co/v0/postings/acme?mode=json": _Response(
                429, headers={"Retry-After": "90"}
            )
        }
    )

    with pytest.raises(QuotaExhausted) as excinfo:
        RecoverPostingStage(ingestion_database, http)(_message(posting_id))

    assert excinfo.value.retry_after_seconds == 90
    posting = _posting(ingestion_database, posting_id)
    assert posting["recovery_attempts"] == 0


def test_a_server_error_on_the_direct_ats_path_is_a_transient_stage_failure(
    ingestion_database,
):
    board_url = "https://jobs.lever.co/acme/abc-123"
    posting_id = _insert_posting(ingestion_database, url=board_url)
    http, web = _http(
        {"https://api.lever.co/v0/postings/acme?mode=json": _Response(503)}
    )

    with pytest.raises(TransientStageFailure):
        RecoverPostingStage(ingestion_database, http)(_message(posting_id))

    posting = _posting(ingestion_database, posting_id)
    assert posting["recovery_attempts"] == 0


def test_a_rate_limited_fetch_is_released_without_counting_an_attempt(ingestion_database):
    original_url = f"https://careers.example.test/{uuid.uuid4()}"
    posting_id = _insert_posting(ingestion_database, url=original_url)
    http, web = _http(
        {original_url: _Response(429, headers={"Retry-After": "120"})}
    )

    with pytest.raises(QuotaExhausted) as excinfo:
        RecoverPostingStage(ingestion_database, http)(_message(posting_id))

    assert excinfo.value.retry_after_seconds == 120
    posting = _posting(ingestion_database, posting_id)
    assert posting["recovery_attempts"] == 0


def test_a_server_error_is_a_transient_stage_failure(ingestion_database):
    original_url = f"https://careers.example.test/{uuid.uuid4()}"
    posting_id = _insert_posting(ingestion_database, url=original_url)
    http, web = _http({original_url: _Response(503)})

    with pytest.raises(TransientStageFailure):
        RecoverPostingStage(ingestion_database, http)(_message(posting_id))


def test_the_wrong_stage_is_a_permanent_failure(ingestion_database):
    message = QueueMessage(
        stage=Stage.RECHECK_FRESHNESS, message_id=1, payload={"posting_id": "x"}
    )
    with pytest.raises(PermanentStageFailure):
        RecoverPostingStage(ingestion_database, None)(message)


def test_a_non_uuid_posting_id_is_a_permanent_failure(ingestion_database):
    message = QueueMessage(
        stage=Stage.RECOVER_POSTING, message_id=1, payload={"posting_id": "not-a-uuid"}
    )
    with pytest.raises(PermanentStageFailure):
        RecoverPostingStage(ingestion_database, None)(message)


def test_the_stage_module_imports_nothing_user_scoped():
    """The stage runs on a worker holding only ingestion's connection (#183,
    C1). Importing it must not pull in the per-user store, matching or
    credentials -- a claim its docstring makes and this is what keeps true."""
    import subprocess
    import sys

    probe = (
        "import sys, job_hunter.recover_posting_stage; "
        "loaded = [m for m in ('job_hunter.postgres_store', 'job_hunter.supabase_client', "
        "'job_hunter.config', 'job_hunter.matching', 'job_hunter.evaluation', "
        "'job_hunter.sources') "
        "if m in sys.modules]; "
        "print(','.join(loaded))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == ""
