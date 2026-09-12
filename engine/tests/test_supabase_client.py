import json

import pytest

from engine.config import SupabaseSettings
from engine.supabase_client import (
    SupabaseAuthError,
    SupabaseClient,
    SupabasePermissionError,
    SupabaseRequestError,
)

SETTINGS = SupabaseSettings(
    user_id="aaaaaaaa-0000-0000-0000-000000000001",
    url="https://example.supabase.co",
    publishable_key="sb_publishable_example",
    signing_key_jwk={"kid": "k", "kty": "EC"},
)


class FakeResponse:
    """Models ``requests.Response`` honestly enough for these tests.

    ``text`` mirrors the body a real response would carry: when the caller
    doesn't pass one explicitly, it's derived from ``payload`` via
    ``json.dumps``, same as PostgREST would actually send. An explicit
    ``text`` (used by the error-path tests, and by callers modeling a
    genuinely empty body) always wins.
    """

    def __init__(self, status_code: int, payload=None, text: str | None = None):
        self.status_code = status_code
        self._payload = [] if payload is None else payload
        self.text = json.dumps(self._payload) if text is None else text

    def json(self):
        return self._payload


class FakeMinter:
    def __init__(self, user_id: str = SETTINGS.user_id) -> None:
        self.user_id = user_id

    def token(self) -> str:
        return "test-token"


class FakeHttp:
    """Records every call and returns queued responses in order."""

    def __init__(self, *responses):
        self.responses = list(responses) or [FakeResponse(200)]
        self.calls = []

    def _record(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.responses.pop(0) if self.responses else FakeResponse(200)

    def get(self, url, **kwargs):
        return self._record("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._record("POST", url, **kwargs)

    def patch(self, url, **kwargs):
        return self._record("PATCH", url, **kwargs)

    def delete(self, url, **kwargs):
        return self._record("DELETE", url, **kwargs)


def _client(*responses):
    http = FakeHttp(*responses)
    return SupabaseClient(http, SETTINGS, FakeMinter()), http


def test_settings_and_minter_user_id_mismatch_is_refused():
    http = FakeHttp()
    mismatched_minter = FakeMinter(user_id="bbbbbbbb-0000-0000-0000-000000000002")

    with pytest.raises(ValueError, match="disagree"):
        SupabaseClient(http, SETTINGS, mismatched_minter)


def test_select_builds_the_url_and_passes_filters():
    client, http = _client(FakeResponse(200, [{"id": "1"}]))

    rows = client.select("job_hunter_jobs", params={"select": "id", "status": "eq.new"})

    assert rows == [{"id": "1"}]
    call = http.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == "https://example.supabase.co/rest/v1/job_hunter_jobs"
    # order=id.asc is injected on the paging path (no limit/offset supplied):
    # select() stitches together independent page requests, and without a
    # total order two pages could skip or duplicate a row if a write lands
    # between them. This is not incidental noise -- do not delete it.
    assert call["params"] == {"select": "id", "status": "eq.new", "order": "id.asc"}


def test_select_with_a_caller_supplied_limit_does_not_page_or_inject_order():
    client, http = _client(FakeResponse(200, [{"id": "1"}]))

    rows = client.select("job_hunter_jobs", params={"status": "eq.new", "limit": "5"})

    assert rows == [{"id": "1"}]
    assert len(http.calls) == 1, "a caller-supplied limit must short-circuit to one request"
    call = http.calls[0]
    assert call["method"] == "GET"
    # A caller that passes its own limit means it, and gets exactly what it
    # asked for -- no injected order, no Range paging.
    assert call["params"] == {"status": "eq.new", "limit": "5"}
    assert "Range" not in call["headers"]


def test_every_request_carries_both_auth_headers():
    client, http = _client()

    client.select("job_hunter_jobs")

    headers = http.calls[0]["headers"]
    assert headers["Authorization"] == "Bearer test-token"
    assert headers["apikey"] == "sb_publishable_example"


def test_insert_posts_rows_and_asks_for_them_back():
    client, http = _client(FakeResponse(201, [{"id": "1"}]))

    rows = client.insert("job_hunter_jobs", [{"fingerprint": "f"}])

    assert rows == [{"id": "1"}]
    call = http.calls[0]
    assert call["method"] == "POST"
    assert call["json"] == [{"fingerprint": "f"}]
    assert call["headers"]["Prefer"] == "return=representation"


def test_update_patches_with_filters():
    client, http = _client(FakeResponse(200, [{"id": "1"}]))

    rows = client.update("job_hunter_jobs", {"status": "seen"}, params={"id": "eq.1"})

    assert rows == [{"id": "1"}]
    call = http.calls[0]
    assert call["method"] == "PATCH"
    assert call["json"] == {"status": "seen"}
    assert call["params"] == {"id": "eq.1"}


def test_delete_sends_filters():
    client, http = _client(FakeResponse(200, []))

    assert client.delete("job_hunter_jobs", params={"id": "eq.1"}) == []
    assert http.calls[0]["method"] == "DELETE"


@pytest.mark.parametrize("method", ["update", "delete"])
def test_unfiltered_writes_are_refused(method):
    client, _ = _client()

    with pytest.raises(ValueError, match="filter"):
        if method == "update":
            client.update("job_hunter_jobs", {"status": "seen"}, params={})
        else:
            client.delete("job_hunter_jobs", params={})


def test_200_with_an_empty_body_returns_an_empty_list():
    client, _ = _client(FakeResponse(200, text=""))

    assert client.select("job_hunter_jobs") == []


def test_401_raises_an_auth_error():
    client, _ = _client(FakeResponse(401, text="JWT expired"))

    with pytest.raises(SupabaseAuthError):
        client.select("job_hunter_jobs")


def test_403_raises_a_permission_error():
    client, _ = _client(FakeResponse(403, text="row-level security"))

    with pytest.raises(SupabasePermissionError):
        client.insert("job_hunter_jobs", [{"fingerprint": "f"}])


def test_other_failures_raise_a_request_error_naming_the_status():
    client, _ = _client(FakeResponse(409, text="duplicate key"))

    with pytest.raises(SupabaseRequestError, match="409"):
        client.insert("job_hunter_jobs", [{"fingerprint": "f"}])


def test_errors_do_not_leak_the_token():
    client, _ = _client(FakeResponse(500, text="boom"))

    with pytest.raises(SupabaseRequestError) as excinfo:
        client.select("job_hunter_jobs")

    assert "test-token" not in str(excinfo.value)


def test_request_errors_carry_the_status_and_the_postgres_error_code():
    # PostgREST reports the SQLSTATE in the body. A caller that has to tell a
    # foreign key violation from any other 409 needs it as a field, not as a
    # substring of the message (#145).
    body = {
        "code": "23503",
        "message": 'insert or update on table "job_hunter_evaluations" violates foreign key',
        "details": 'Key is not present in table "job_hunter_jobs".',
    }
    client, _ = _client(FakeResponse(409, payload=body))

    with pytest.raises(SupabaseRequestError) as excinfo:
        client.insert("job_hunter_evaluations", [{"job_id": "gone"}])

    assert excinfo.value.status_code == 409
    assert excinfo.value.code == "23503"


def test_a_request_error_with_an_unparseable_body_has_no_code():
    class UnparseableResponse(FakeResponse):
        def json(self):
            raise ValueError("not json")

    client, _ = _client(UnparseableResponse(502, text="<html>gateway</html>"))

    with pytest.raises(SupabaseRequestError) as excinfo:
        client.select("job_hunter_jobs")

    assert excinfo.value.status_code == 502
    assert excinfo.value.code is None
