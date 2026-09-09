from __future__ import annotations

import pytest
import requests

from job_hunter.http import NOT_MODIFIED, HttpClient, Validators


class _FakeResponse:
    def __init__(self, status_code: int, headers: dict[str, str], payload):
        self.status_code = status_code
        self.headers = headers
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)


class _RecordingSession:
    def __init__(self, response):
        self.response = response
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.response

    headers: dict[str, str] = {}


def _client_with(response) -> tuple[HttpClient, _RecordingSession]:
    client = HttpClient()
    session = _RecordingSession(response)
    client._session = session
    return client, session


def test_a_304_returns_the_sentinel_rather_than_raising():
    """An unchanged board must be distinguishable from an empty one."""
    client, _ = _client_with(_FakeResponse(304, {}, None))
    result = client.get_json("https://example.test/jobs", validators=Validators(etag='"abc"'))
    assert result is NOT_MODIFIED


def test_validators_are_sent_as_conditional_headers():
    client, session = _client_with(_FakeResponse(304, {}, None))
    client.get_json(
        "https://example.test/jobs",
        validators=Validators(etag='"abc"', last_modified="Wed, 21 Oct 2026 07:28:00 GMT"),
    )
    _method, _url, kwargs = session.calls[0]
    assert kwargs["headers"]["If-None-Match"] == '"abc"'
    assert kwargs["headers"]["If-Modified-Since"] == "Wed, 21 Oct 2026 07:28:00 GMT"


def test_no_validators_means_no_conditional_headers():
    """A first crawl must not send an empty If-None-Match."""
    client, session = _client_with(_FakeResponse(200, {}, {"jobs": []}))
    client.get_json("https://example.test/jobs")
    _method, _url, kwargs = session.calls[0]
    assert "If-None-Match" not in kwargs.get("headers", {})
    assert "If-Modified-Since" not in kwargs.get("headers", {})


def test_a_200_returns_the_payload_and_exposes_new_validators():
    response = _FakeResponse(
        200,
        {"ETag": '"def"', "Last-Modified": "Thu, 22 Oct 2026 07:28:00 GMT"},
        {"jobs": [{"id": 1}]},
    )
    client, _ = _client_with(response)
    payload = client.get_json("https://example.test/jobs", validators=Validators(etag='"abc"'))
    assert payload == {"jobs": [{"id": 1}]}
    assert client.last_validators() == Validators(
        etag='"def"', last_modified="Thu, 22 Oct 2026 07:28:00 GMT"
    )


def test_a_500_still_raises():
    """Conditional support must not swallow a real failure."""
    client, _ = _client_with(_FakeResponse(500, {}, None))
    with pytest.raises(requests.HTTPError):
        client.get_json("https://example.test/jobs", retry=False)
