"""Bounded retry for transient Gemini failures (issue #35).

Covers HTTP 5xx and network-timeout retries in GeminiClient.generate_text:
retry-then-succeed, repeated-failure-then-clean-raise, pacing/accounting on
every attempt, and that non-transient failures never consume retry budget.
"""

from datetime import datetime, timezone

import requests
import pytest

from job_hunter.gemini import GeminiClient, GeminiError


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text

    def json(self):
        return self._json_data


class SequencedHttp:
    """Returns/raises the next scripted outcome on each call.post()."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeTracker:
    def __init__(self):
        self.preflight_calls = []
        self.success_calls = []
        self.error_calls = []

    def preflight(self, purpose, prompt, now):
        self.preflight_calls.append((purpose, prompt, now))

    def record_success(self, purpose, prompt, now, **kwargs):
        self.success_calls.append((purpose, prompt, now, kwargs))

    def record_error(self, purpose, prompt, now, **kwargs):
        self.error_calls.append((purpose, prompt, now, kwargs))


def _ok_response(text="hi"):
    return FakeResponse(200, {"candidates": [{"content": {"parts": [{"text": text}]}}]})


def _sleep_recorder():
    calls = []

    def _sleep(seconds):
        calls.append(seconds)

    return _sleep, calls


def test_http_500_then_success_retries_and_returns_text(monkeypatch):
    http = SequencedHttp([FakeResponse(500, None, "server error"), _ok_response("recovered")])
    tracker = FakeTracker()
    sleep_fn, sleeps = _sleep_recorder()
    client = GeminiClient("key", "gemini-test", http, tracker, sleep_fn=sleep_fn)
    monkeypatch.setattr(
        "job_hunter.gemini._now", lambda: datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc)
    )

    result = client.generate_text("evaluate", purpose="job_evaluation", max_attempts=2)

    assert result == "recovered"
    assert len(http.calls) == 2
    assert len(sleeps) == 1
    assert len(tracker.preflight_calls) == 2, "pacing must be re-checked on the retry"
    assert len(tracker.error_calls) == 1
    assert tracker.error_calls[0][3] == {"http_status": 500}
    assert len(tracker.success_calls) == 1


def test_read_timeout_then_success_retries_and_returns_text(monkeypatch):
    http = SequencedHttp([requests.ReadTimeout("slow"), _ok_response("recovered")])
    tracker = FakeTracker()
    sleep_fn, sleeps = _sleep_recorder()
    client = GeminiClient("key", "gemini-test", http, tracker, sleep_fn=sleep_fn)
    monkeypatch.setattr(
        "job_hunter.gemini._now", lambda: datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc)
    )

    result = client.generate_text("evaluate", purpose="job_evaluation", max_attempts=2)

    assert result == "recovered"
    assert len(http.calls) == 2
    assert len(sleeps) == 1
    assert len(tracker.preflight_calls) == 2
    assert tracker.error_calls[0][3] == {"error_code": "ReadTimeout"}
    assert len(tracker.success_calls) == 1


def test_repeated_transient_failure_fails_cleanly_without_exceeding_bound(monkeypatch):
    http = SequencedHttp([FakeResponse(503, None, "busy"), FakeResponse(503, None, "still busy")])
    tracker = FakeTracker()
    sleep_fn, _sleeps = _sleep_recorder()
    client = GeminiClient("key", "gemini-test", http, tracker, sleep_fn=sleep_fn)
    monkeypatch.setattr(
        "job_hunter.gemini._now", lambda: datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc)
    )

    with pytest.raises(GeminiError):
        client.generate_text("evaluate", purpose="job_evaluation", max_attempts=2)

    # Bounded: exactly two attempts were made, never more.
    assert len(http.calls) == 2
    assert len(tracker.error_calls) == 2
    assert len(tracker.success_calls) == 0


def test_default_max_attempts_does_not_retry_transient_failure():
    http = SequencedHttp([FakeResponse(500, None, "server error")])
    tracker = FakeTracker()
    client = GeminiClient("key", "gemini-test", http, tracker)

    with pytest.raises(GeminiError):
        client.generate_text("evaluate", purpose="job_evaluation")

    assert len(http.calls) == 1


def test_non_retryable_http_error_is_not_retried_even_with_budget():
    http = SequencedHttp([FakeResponse(400, None, "bad request")])
    tracker = FakeTracker()
    client = GeminiClient("key", "gemini-test", http, tracker)

    with pytest.raises(GeminiError):
        client.generate_text("evaluate", purpose="job_evaluation", max_attempts=3)

    assert len(http.calls) == 1


def test_connection_error_is_not_retried_blindly():
    """Only network timeouts are treated as safe-to-retry; other
    requests.RequestException subtypes still fail on the first attempt."""
    http = SequencedHttp([requests.ConnectionError("refused")])
    tracker = FakeTracker()
    client = GeminiClient("key", "gemini-test", http, tracker)

    with pytest.raises(requests.ConnectionError):
        client.generate_text("evaluate", purpose="job_evaluation", max_attempts=3)

    assert len(http.calls) == 1
