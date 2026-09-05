import logging

import requests

from job_hunter.circuit_breaker import CircuitBreaker
from job_hunter.http import HttpClient
from job_hunter.search_backend import SearchBudgetExhausted
from job_hunter.sources.targeted_search import TargetedSearchSource
from job_hunter.sources.wellfound import WellfoundListing, WellfoundSource


class _Response:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"status {self.status_code}",
                response=self,
            )


_LISTING_TWO_JOBS = """
<a href="/jobs/1-job-one">Job One</a>
<a href="/jobs/2-job-two">Job Two</a>
"""

_LISTING_ONE_JOB = '<a href="/jobs/2-job-two">Job Two</a>'

_DETAIL_HTML = """
<html>
  <head><title>Frontend Engineer at Omnea • London | Wellfound</title></head>
  <body>
    <h1>Frontend Engineer</h1>
    <p>Own the design system and front-end architecture.</p>
  </body>
</html>
"""


def _client_with_responses(monkeypatch, responder):
    client = HttpClient()
    calls: list[str] = []
    sleeps: list[float] = []

    def fake_request(method, url, **kwargs):
        calls.append(url)
        return responder(url)

    monkeypatch.setattr(client._session, "request", fake_request)
    monkeypatch.setattr("job_hunter.http.time.sleep", sleeps.append)
    return client, calls, sleeps


def test_wellfound_detail_429_is_single_attempt_compact_and_non_fatal(
    monkeypatch, caplog
):
    listing_url = "https://wellfound.com/role/l/frontend-engineer/london"

    def responder(url: str):
        if url == listing_url:
            return _Response(200, _LISTING_TWO_JOBS)
        if "/jobs/1-" in url:
            return _Response(429)
        if "/jobs/2-" in url:
            return _Response(200, _DETAIL_HTML)
        raise AssertionError(f"unexpected URL: {url}")

    http, calls, sleeps = _client_with_responses(monkeypatch, responder)
    source = WellfoundSource(
        http,
        [WellfoundListing(url=listing_url, market_id="london")],
    )

    with caplog.at_level(logging.WARNING):
        jobs = source.discover()

    assert [job.source_job_id for job in jobs] == ["2"]
    assert sum("/jobs/1-" in url for url in calls) == 1
    assert sum("/jobs/2-" in url for url in calls) == 1
    assert sleeps == []

    rate_limit_records = [
        record for record in caplog.records if "wellfound rate limited" in record.getMessage()
    ]
    assert len(rate_limit_records) == 1
    assert rate_limit_records[0].exc_info is None
    assert "detail" in rate_limit_records[0].getMessage()
    assert "429" in rate_limit_records[0].getMessage()


def test_wellfound_listing_429_does_not_block_later_listing(monkeypatch, caplog):
    london_url = "https://wellfound.com/role/l/frontend-engineer/london"
    europe_url = "https://wellfound.com/role/l/frontend-engineer/europe"

    def responder(url: str):
        if url == london_url:
            return _Response(429)
        if url == europe_url:
            return _Response(200, _LISTING_ONE_JOB)
        if "/jobs/2-" in url:
            return _Response(200, _DETAIL_HTML)
        raise AssertionError(f"unexpected URL: {url}")

    http, calls, sleeps = _client_with_responses(monkeypatch, responder)
    source = WellfoundSource(
        http,
        [
            WellfoundListing(url=london_url, market_id="london"),
            WellfoundListing(url=europe_url, market_id="germany_eu"),
        ],
    )

    with caplog.at_level(logging.WARNING):
        jobs = source.discover()

    assert [job.source_job_id for job in jobs] == ["2"]
    assert jobs[0].market_hint == "germany_eu"
    assert calls.count(london_url) == 1
    assert calls.count(europe_url) == 1
    assert sleeps == []

    rate_limit_records = [
        record for record in caplog.records if "wellfound rate limited" in record.getMessage()
    ]
    assert len(rate_limit_records) == 1
    assert rate_limit_records[0].exc_info is None
    assert "listing" in rate_limit_records[0].getMessage()
    assert "429" in rate_limit_records[0].getMessage()


def test_shared_open_search_circuit_logs_once_and_makes_no_provider_calls(caplog):
    class Backend:
        name = "test"

        def __init__(self) -> None:
            self.calls = 0

        def search(self, query: str):
            self.calls += 1
            raise AssertionError("open circuit must prevent provider calls")

    backend = Backend()
    breaker = CircuitBreaker(failure_threshold=1)
    breaker.record_failure()
    assert breaker.is_open

    with caplog.at_level(logging.WARNING):
        for index in range(8):
            jobs = TargetedSearchSource(
                backend,
                [f"canonical query {index}"],
                breaker=breaker,
            ).discover()
            assert jobs == []

    assert backend.calls == 0
    circuit_records = [
        record
        for record in caplog.records
        if "targeted search circuit open" in record.getMessage()
    ]
    assert len(circuit_records) == 1


def test_brave_budget_exhaustion_does_not_trip_shared_search_circuit(caplog):
    class BudgetExhaustedBackend:
        name = "brave"

        def __init__(self) -> None:
            self.calls = 0

        def search(self, query: str):
            self.calls += 1
            raise SearchBudgetExhausted("Brave Search budget exhausted")

    backend = BudgetExhaustedBackend()
    breaker = CircuitBreaker(failure_threshold=1)

    with caplog.at_level(logging.INFO):
        jobs = TargetedSearchSource(
            backend,
            ["q1", "q2"],
            breaker=breaker,
        ).discover()

    assert jobs == []
    assert backend.calls == 1
    assert not breaker.is_open
    assert "targeted search budget exhausted" in caplog.text
    assert not any(record.exc_info for record in caplog.records)
