import pytest
import requests

import job_hunter.search_backend as search_backend
from job_hunter.search_backend import (
    BraveSearchBackend,
    DuckDuckGoSearchBackend,
    FallbackSearchBackend,
    build_search_backend,
)


class _Response:
    def __init__(self, payload, *, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"{self.status_code} Client Error",
                response=self,
            )

    def json(self):
        return self._payload


class _Http:
    def __init__(self, payload, *, status_code=200, text=""):
        self.payload = payload
        self.status_code = status_code
        self.text = text
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Response(self.payload, status_code=self.status_code, text=self.text)


def test_brave_search_normalizes_web_results_and_authenticates():
    http = _Http(
        {
            "web": {
                "results": [
                    {
                        "title": " Senior Frontend Engineer ",
                        "url": "https://example.com/jobs/42?utm_source=search",
                    },
                    {"title": "", "url": "https://example.com/ignored"},
                ]
            }
        }
    )

    response = BraveSearchBackend(http, "secret-key").search("frontend london")

    assert response.backend == "brave"
    assert len(response.hits) == 1
    assert response.hits[0].title == "Senior Frontend Engineer"
    assert response.hits[0].url == "https://example.com/jobs/42"
    _url, kwargs = http.calls[0]
    assert kwargs["headers"]["X-Subscription-Token"] == "secret-key"
    assert kwargs["headers"]["Accept"] == "application/json"
    assert kwargs["headers"]["Accept-Encoding"] == "gzip"
    assert kwargs["headers"]["Cache-Control"] == "no-cache"
    assert kwargs["params"]["q"] == "frontend london"


def test_brave_search_makes_zero_http_calls_when_budget_reservation_is_denied():
    http = _Http({"web": {"results": []}})
    backend = BraveSearchBackend(http, "secret-key", on_attempt=lambda: False)

    with pytest.raises(search_backend.SearchBudgetExhausted):
        backend.search("frontend london")

    assert http.calls == []


def test_brave_validation_error_logs_only_sanitized_metadata(caplog):
    http = _Http(
        {
            "type": "ErrorResponse",
            "error": {
                "code": "VALIDATION",
                "detail": "Unable to validate request parameter(s)",
                "meta": {
                    "errors": [
                        {
                            "loc": ["header", "cache-control"],
                            "msg": "Input should be 'no-cache'",
                            "input": "RAW_PRIVATE_MARKER",
                        }
                    ]
                },
            },
        },
        status_code=422,
        text="RAW_PRIVATE_MARKER secret-key",
    )

    with pytest.raises(requests.HTTPError):
        BraveSearchBackend(http, "secret-key").search("frontend london")

    log = caplog.text
    assert "status=422" in log
    assert "code=VALIDATION" in log
    assert "Unable to validate request parameter(s)" in log
    assert "header.cache-control" in log
    assert "Input should be 'no-cache'" in log
    assert "RAW_PRIVATE_MARKER" not in log
    assert "secret-key" not in log


def test_build_search_backend_keeps_duckduckgo_when_no_brave_key():
    backend = build_search_backend(_Http({}), None)

    assert isinstance(backend, FallbackSearchBackend)
    assert backend._primary is None
    assert isinstance(backend._secondary, DuckDuckGoSearchBackend)
