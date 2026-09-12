from __future__ import annotations

import pytest
import requests

from engine.http import (
    NOT_MODIFIED,
    HttpClient,
    NotModifiedSignal,
    Validators,
)


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


# The conditional() scope -------------------------------------------------
#
# The scope is how a stored cursor reaches an adapter that knows nothing
# about cursors. These assert the two halves that make that safe: the right
# request carries the validator, and a 304 unwinds rather than returning a
# value the adapter would misread.


class _UrlRoutedSession:
    """A session answering each URL differently, recording what it was sent."""

    def __init__(self, by_url: dict[str, _FakeResponse]):
        self.by_url = by_url
        self.calls: list[tuple[str, dict]] = []

    def request(self, method, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.by_url[url]

    headers: dict[str, str] = {}


def _routed(by_url) -> tuple[HttpClient, _UrlRoutedSession]:
    client = HttpClient()
    session = _UrlRoutedSession(by_url)
    client._session = session
    return client, session


def test_a_scope_makes_its_own_url_conditional():
    client, session = _routed({"https://b/f": _FakeResponse(200, {}, {"ok": 1})})
    stored = Validators(etag='"abc"')

    with client.conditional(stored, url="https://b/f"):
        client.get_json("https://b/f")

    assert session.calls[0][1]["headers"]["If-None-Match"] == '"abc"'


def test_a_scope_does_not_send_one_boards_validator_to_another_board():
    """learned_ats walks many boards in one crawl. A 304 provoked by the
    wrong board's ETag would be a lie, so only the stored URL is conditional."""
    client, session = _routed(
        {
            "https://b/one": _FakeResponse(200, {}, {"ok": 1}),
            "https://b/two": _FakeResponse(200, {}, {"ok": 2}),
        }
    )

    with client.conditional(Validators(etag='"one"'), url="https://b/one"):
        client.get_json("https://b/one")
        client.get_json("https://b/two")

    assert session.calls[0][1]["headers"]["If-None-Match"] == '"one"'
    assert "headers" not in session.calls[1][1] or (
        "If-None-Match" not in session.calls[1][1].get("headers", {})
    )


def test_a_304_inside_a_scope_raises_rather_than_returning_the_sentinel():
    """The adapter called get_json expecting a dict. Handing it NOT_MODIFIED
    would crash it somewhere less obvious, or be read as an empty board."""
    client, _ = _routed({"https://b/f": _FakeResponse(304, {}, None)})

    with client.conditional(Validators(etag='"abc"'), url="https://b/f") as scope:
        with pytest.raises(NotModifiedSignal):
            client.get_json("https://b/f")

    assert scope.not_modified is True


def test_the_signal_survives_an_adapter_that_swallows_exceptions():
    """Every adapter catches Exception somewhere. If the signal were an
    Exception, 'unchanged' would silently become 'this source returned
    nothing' -- the exact collapse conditional requests exist to prevent."""
    client, _ = _routed({"https://b/f": _FakeResponse(304, {}, None)})

    def adapter_that_swallows():
        try:
            return client.get_json("https://b/f")
        except Exception:  # noqa: BLE001 - the point of the test
            return {"jobs": []}

    with client.conditional(Validators(etag='"abc"'), url="https://b/f"):
        with pytest.raises(NotModifiedSignal):
            adapter_that_swallows()


def test_a_first_crawl_adopts_the_first_url_as_its_cursor():
    """A source with no stored cursor has no URL to be conditional about.
    The first GET claims the scope so the next crawl has something to send."""
    client, session = _routed(
        {
            "https://b/page1": _FakeResponse(200, {"ETag": '"p1"'}, {"ok": 1}),
            "https://b/page2": _FakeResponse(200, {"ETag": '"p2"'}, {"ok": 2}),
        }
    )

    with client.conditional(Validators(), url="") as scope:
        client.get_json("https://b/page1")
        client.get_json("https://b/page2")

    assert scope.observed_url == "https://b/page1"
    assert scope.observed.etag == '"p1"', "page 2 must not overwrite the board's identity"
    assert "headers" not in session.calls[0][1]


def test_a_scope_records_the_validators_to_store_for_next_time():
    client, _ = _routed(
        {"https://b/f": _FakeResponse(200, {"ETag": '"v2"', "Last-Modified": "Wed"}, {"ok": 1})}
    )

    with client.conditional(Validators(etag='"v1"'), url="https://b/f") as scope:
        client.get_json("https://b/f")

    assert scope.observed == Validators(etag='"v2"', last_modified="Wed")


def test_an_explicit_validators_argument_still_returns_the_sentinel():
    """crawl_source's probe passes validators directly and reads the return
    value. A scope must not change that call's contract."""
    client, _ = _routed({"https://b/f": _FakeResponse(304, {}, None)})

    with client.conditional(Validators(etag='"scope"'), url="https://b/f"):
        assert client.get_json("https://b/f", validators=Validators(etag='"x"')) is NOT_MODIFIED


def test_scopes_restore_the_previous_one():
    client, _ = _routed({"https://b/f": _FakeResponse(200, {}, {"ok": 1})})

    with client.conditional(Validators(etag='"outer"'), url="https://b/f"):
        with client.conditional(Validators(etag='"inner"'), url="https://b/f"):
            pass
        assert client._scope.validators.etag == '"outer"'
    assert client._scope is None


def test_no_scope_means_no_conditional_headers_at_all():
    client, session = _routed({"https://b/f": _FakeResponse(200, {}, {"ok": 1})})
    client.get_json("https://b/f")
    assert "headers" not in session.calls[0][1]


def test_a_304_keeps_the_validators_it_just_confirmed():
    """RFC 7232 lets a 304 carry neither validator. Storing what it omitted
    would erase the one it just proved still good, and the source would
    alternate conditional and full fetches forever."""
    client, _ = _routed({"https://b/f": _FakeResponse(304, {}, None)})
    sent = Validators(etag='"v1"', last_modified="Tue")

    with client.conditional(sent, url="https://b/f") as scope:
        with pytest.raises(NotModifiedSignal):
            client.get_json("https://b/f")

    assert scope.observed == sent


def test_a_304_that_restates_a_validator_takes_the_new_one():
    client, _ = _routed({"https://b/f": _FakeResponse(304, {"ETag": '"v2"'}, None)})

    with client.conditional(Validators(etag='"v1"'), url="https://b/f") as scope:
        with pytest.raises(NotModifiedSignal):
            client.get_json("https://b/f")

    assert scope.observed.etag == '"v2"'


def test_only_the_first_matching_request_in_a_scope_is_conditional():
    """Paginated sources walk many pages from one URL, varying only params.
    Matching every page would send page 0's validator to page 1 and then
    store page 1's ETag under the identity of the whole board."""
    client, session = _routed(
        {"https://b/feed": _FakeResponse(200, {"ETag": '"page"'}, {"ok": 1})}
    )

    with client.conditional(Validators(etag='"board"'), url="https://b/feed") as scope:
        client.get_json("https://b/feed")
        client.get_json("https://b/feed", params={"cursor": "2"})

    assert session.calls[0][1]["headers"]["If-None-Match"] == '"board"'
    assert "headers" not in session.calls[1][1], "page 2 is not the board"
    assert scope.observed_url == "https://b/feed"
