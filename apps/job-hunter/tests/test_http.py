import requests

from job_hunter.http import HttpClient


class FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code
        self.text = ""

    def json(self):
        return {}


def test_request_retries_on_5xx_then_succeeds(monkeypatch):
    monkeypatch.setattr("job_hunter.http.time.sleep", lambda _: None)

    responses = [FakeResponse(503), FakeResponse(503), FakeResponse(200)]
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return responses[len(calls) - 1]

    client = HttpClient()
    monkeypatch.setattr(client._session, "request", fake_request)

    response = client.get("https://example.com/thing")

    assert response.status_code == 200
    assert len(calls) == 3


def test_request_retries_on_connection_error_then_succeeds(monkeypatch):
    monkeypatch.setattr("job_hunter.http.time.sleep", lambda _: None)

    attempts = {"n": 0}

    def fake_request(method, url, **kwargs):
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise requests.ConnectionError("boom")
        return FakeResponse(200)

    client = HttpClient()
    monkeypatch.setattr(client._session, "request", fake_request)

    response = client.get("https://example.com/thing")

    assert response.status_code == 200
    assert attempts["n"] == 2


def test_request_raises_after_exhausting_retries_on_connection_error(monkeypatch):
    monkeypatch.setattr("job_hunter.http.time.sleep", lambda _: None)

    def fake_request(method, url, **kwargs):
        raise requests.ConnectionError("boom")

    client = HttpClient()
    monkeypatch.setattr(client._session, "request", fake_request)

    try:
        client.get("https://example.com/thing")
        assert False, "expected ConnectionError to propagate"
    except requests.ConnectionError:
        pass


def test_request_retries_can_be_disabled(monkeypatch):
    monkeypatch.setattr("job_hunter.http.time.sleep", lambda _: None)
    attempts = {"n": 0}

    def fake_request(method, url, **kwargs):
        attempts["n"] += 1
        raise requests.ConnectionError("boom")

    client = HttpClient()
    monkeypatch.setattr(client._session, "request", fake_request)

    try:
        client.get("https://example.com/thing", retry=False)
        assert False, "expected ConnectionError to propagate"
    except requests.ConnectionError:
        pass

    assert attempts["n"] == 1


def test_retry_status_codes_override_excludes_429_from_retry(monkeypatch):
    monkeypatch.setattr("job_hunter.http.time.sleep", lambda _: None)

    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return FakeResponse(429)

    client = HttpClient()
    monkeypatch.setattr(client._session, "request", fake_request)

    response = client.post(
        "https://example.com/thing", retry_status_codes={500, 502, 503, 504}
    )

    assert response.status_code == 429
    assert len(calls) == 1
    # The override must not reach requests.Session.request as a kwarg.
    assert "retry_status_codes" not in calls[0][2]


def test_retry_status_codes_default_still_retries_429(monkeypatch):
    monkeypatch.setattr("job_hunter.http.time.sleep", lambda _: None)

    responses = [FakeResponse(429), FakeResponse(200)]
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return responses[len(calls) - 1]

    client = HttpClient()
    monkeypatch.setattr(client._session, "request", fake_request)

    response = client.post("https://example.com/thing")

    assert response.status_code == 200
    assert len(calls) == 2


def test_post_body_is_byte_identical_across_retry_attempts(monkeypatch):
    monkeypatch.setattr("job_hunter.http.time.sleep", lambda _: None)

    responses = [FakeResponse(503), FakeResponse(200)]
    seen_kwargs = []

    def fake_request(method, url, **kwargs):
        seen_kwargs.append(kwargs)
        return responses[len(seen_kwargs) - 1]

    client = HttpClient()
    monkeypatch.setattr(client._session, "request", fake_request)

    payload_bytes = b"%PDF-1.4 fake pdf content"
    client.post(
        "https://example.com/upload",
        data={"chat_id": "1", "caption": "hello"},
        files={"document": ("letter.pdf", payload_bytes)},
    )

    assert len(seen_kwargs) == 2
    first, second = seen_kwargs
    assert first["data"] == second["data"]
    assert first["files"] == second["files"]
    first_bytes = first["files"]["document"][1]
    second_bytes = second["files"]["document"][1]
    assert first_bytes == second_bytes == payload_bytes


def test_patch_sends_patch_and_returns_response(monkeypatch):
    client = HttpClient()
    seen = {}

    def fake_request(method, url, **kwargs):
        seen["method"] = method
        seen["url"] = url
        return FakeResponse(200)

    monkeypatch.setattr(client._session, "request", fake_request)

    response = client.patch("https://example.test/rows", json={"a": 1})

    assert seen["method"] == "PATCH"
    assert seen["url"] == "https://example.test/rows"
    assert response.status_code == 200


def test_delete_sends_delete_and_returns_response(monkeypatch):
    client = HttpClient()
    seen = {}

    def fake_request(method, url, **kwargs):
        seen["method"] = method
        return FakeResponse(200)

    monkeypatch.setattr(client._session, "request", fake_request)

    assert client.delete("https://example.test/rows").status_code == 200
    assert seen["method"] == "DELETE"


def test_patch_applies_the_default_timeout(monkeypatch):
    client = HttpClient()
    seen = {}

    def fake_request(method, url, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        return FakeResponse(200)

    monkeypatch.setattr(client._session, "request", fake_request)
    client.patch("https://example.test/rows")

    assert seen["timeout"] == (5, 25)


def test_timeout_for_read_keeps_the_connect_budget():
    """A longer read budget must not silently widen the connect budget too.

    A slow TCP handshake still means something is wrong, even when the
    caller is willing to wait a long time for the response body.
    """
    client = HttpClient()
    connect, read = client.timeout_for_read(120)

    assert read == 120
    assert (connect, read) != client._timeout
    assert connect == client._timeout[0]
