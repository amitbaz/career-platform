"""A source's stored cursor reaching the adapter, and coming back updated.

`_iter_source_jobs` is the only place that can hold a conditional-request
scope: the adapters issue the requests and know nothing about cursors, and
`collect_candidates` has moved on to the next source by the time one
finishes. These tests drive that seam directly.
"""

from __future__ import annotations

import time

from job_hunter.discovery import (
    SOURCE_COMPLETED,
    SOURCE_FAILED,
    SOURCE_NOT_MODIFIED,
    DiscoveryStats,
    _iter_source_jobs,
)
from job_hunter.http import HttpClient, NotModifiedSignal, Validators
from job_hunter.models import Job


class _RecordingCursors:
    def __init__(self, stored: dict[str, tuple[str, Validators]] | None = None):
        self.stored = stored or {}
        self.writes: list[tuple[str, str, Validators]] = []

    def read(self, source_key):
        return self.stored.get(source_key, ("", Validators()))

    def write(self, source_key, url, validators):
        self.writes.append((source_key, url, validators))


class _FakeResponse:
    def __init__(self, status_code, headers, payload):
        self.status_code = status_code
        self.headers = headers
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


class _Session:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response

    headers: dict[str, str] = {}


def _client(response):
    client = HttpClient()
    client._session = _Session(response)
    return client


def _job(title="Engineer"):
    return Job(
        title=title,
        company="Acme",
        location="Remote",
        url="https://acme.example/1",
        source="demo",
        description="d",
    )


class _Source:
    """A source shaped like every real adapter: it calls get_json and has no
    idea conditional requests exist."""

    source_label = "demo"

    def __init__(self, http, jobs=None):
        self._http = http
        self._jobs = jobs if jobs is not None else [_job()]

    def discover(self):
        self._http.get_json("https://acme.example/feed")
        yield from self._jobs


def _drain(source, http, cursors, stats=None):
    stats = stats or DiscoveryStats()
    jobs = list(
        _iter_source_jobs(
            source, http, stats, "demo", time.monotonic, None, cursors
        )
    )
    return jobs, stats


def test_a_stored_cursor_is_sent_as_a_conditional_header():
    http = _client(_FakeResponse(200, {"ETag": '"new"'}, {"ok": 1}))
    cursors = _RecordingCursors(
        {"demo": ("https://acme.example/feed", Validators(etag='"old"'))}
    )

    _drain(_Source(http), http, cursors)

    assert http._session.calls[0][1]["headers"]["If-None-Match"] == '"old"'


def test_a_304_ends_the_source_as_not_modified_rather_than_failed():
    """The adapter raises NotModifiedSignal from inside discover(). If this
    were mistaken for a failure the source would be demoted for being
    efficient."""
    http = _client(_FakeResponse(304, {"ETag": '"same"'}, None))
    cursors = _RecordingCursors(
        {"demo": ("https://acme.example/feed", Validators(etag='"same"'))}
    )

    jobs, stats = _drain(_Source(http), http, cursors)

    assert jobs == []
    assert stats.source_outcomes["demo"] == SOURCE_NOT_MODIFIED


def test_not_modified_is_distinct_from_an_empty_board():
    """'Unchanged' and 'the board is genuinely empty' must not collapse into
    one outcome -- that collapse is what makes an outage look like a quiet
    day."""
    http = _client(_FakeResponse(200, {}, {"ok": 1}))
    empty, stats = _drain(_Source(http, jobs=[]), http, _RecordingCursors())

    assert empty == []
    assert stats.source_outcomes["demo"] == SOURCE_COMPLETED
    assert stats.source_outcomes["demo"] != SOURCE_NOT_MODIFIED


def test_the_cursor_advances_after_a_successful_crawl():
    http = _client(_FakeResponse(200, {"ETag": '"v2"'}, {"ok": 1}))
    cursors = _RecordingCursors(
        {"demo": ("https://acme.example/feed", Validators(etag='"v1"'))}
    )

    _drain(_Source(http), http, cursors)

    assert cursors.writes == [
        ("demo", "https://acme.example/feed", Validators(etag='"v2"'))
    ]


def test_a_first_crawl_bootstraps_a_cursor_it_did_not_have():
    http = _client(_FakeResponse(200, {"ETag": '"first"'}, {"ok": 1}))
    cursors = _RecordingCursors()

    _drain(_Source(http), http, cursors)

    assert cursors.writes == [
        ("demo", "https://acme.example/feed", Validators(etag='"first"'))
    ]
    assert "headers" not in http._session.calls[0][1], "nothing to be conditional about"


def test_a_failing_source_still_advances_its_cursor():
    """The validators describe what the board answered, not whether we
    finished reading it."""

    class _Failing(_Source):
        def discover(self):
            self._http.get_json("https://acme.example/feed")
            yield _job()
            raise RuntimeError("upstream died")

    http = _client(_FakeResponse(200, {"ETag": '"v2"'}, {"ok": 1}))
    cursors = _RecordingCursors()

    jobs, stats = _drain(_Failing(http), http, cursors)

    assert [j.title for j in jobs] == ["Engineer"]
    assert stats.source_outcomes["demo"] == SOURCE_FAILED
    assert cursors.writes == [
        ("demo", "https://acme.example/feed", Validators(etag='"v2"'))
    ]


def test_no_cursor_store_leaves_every_request_unconditional():
    """Deployments without a privileged connection keep the old behaviour."""
    http = _client(_FakeResponse(200, {"ETag": '"v2"'}, {"ok": 1}))

    jobs, stats = _drain(_Source(http), http, None)

    assert [j.title for j in jobs] == ["Engineer"]
    assert "headers" not in http._session.calls[0][1]


def test_the_signal_never_escapes_to_the_caller():
    """NotModifiedSignal is a BaseException so adapters cannot swallow it.
    That makes it this function's job to stop it, or it would tear down the
    whole run instead of one source."""
    http = _client(_FakeResponse(304, {}, None))
    cursors = _RecordingCursors(
        {"demo": ("https://acme.example/feed", Validators(etag='"same"'))}
    )

    try:
        _drain(_Source(http), http, cursors)
    except NotModifiedSignal:  # pragma: no cover - the assertion is that we do not get here
        raise AssertionError("the signal escaped _iter_source_jobs")
