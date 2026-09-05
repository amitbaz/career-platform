from datetime import datetime, timedelta, timezone

import pytest

from job_hunter.gemini import GeminiClient, GeminiError
from job_hunter.gemini_usage import GeminiQuotaPaused, GeminiUsageTracker
from job_hunter.models import GeminiQuotaSettings
from job_hunter.store import JobStore


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text

    def json(self):
        return self._json_data


class FakeHttp:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class FakeTracker:
    """Records calls without touching a real store, for tests that only need to
    assert what GeminiClient told the tracker (as opposed to the 429 tests below,
    which use a real tracker to exercise the actual pause it computes)."""

    def __init__(self):
        self.preflight_calls = []
        self.success_calls = []
        self.error_calls = []
        self.calls_429 = []

    def preflight(self, purpose, prompt, now):
        self.preflight_calls.append((purpose, prompt, now))

    def record_success(self, purpose, prompt, now, **kwargs):
        self.success_calls.append((purpose, prompt, now, kwargs))

    def record_error(self, purpose, prompt, now, **kwargs):
        self.error_calls.append((purpose, prompt, now, kwargs))

    def record_429(self, purpose, prompt, now, **kwargs):
        self.calls_429.append((purpose, prompt, now, kwargs))


def _candidate_response(text="hello world"):
    return {
        "candidates": [
            {"content": {"parts": [{"text": text}]}}
        ]
    }


def _usage_response(text="{}"):
    return {
        "candidates": [{"content": {"parts": [{"text": text}]}}],
        "usageMetadata": {
            "promptTokenCount": 120,
            "candidatesTokenCount": 40,
            "thoughtsTokenCount": 30,
            "cachedContentTokenCount": 0,
            "totalTokenCount": 190,
        },
    }


def _real_tracker():
    store = JobStore(":memory:")
    quota = GeminiQuotaSettings(rpm=10, tpm=1000, rpd=100)
    tracker = GeminiUsageTracker(store, quota, "gemini-2.5-flash-lite", run_id="run-1")
    return tracker, store


def _quota_error_response(message: str, quota_id: str | None = None):
    details = []
    if quota_id is not None:
        details.append(
            {
                "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [{"quotaId": quota_id}],
            }
        )
    return FakeResponse(
        429,
        {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": message, "details": details}},
        message,
    )


def test_generate_text_posts_to_expected_url_with_key_header():
    http = FakeHttp(FakeResponse(200, _candidate_response("hi")))
    client = GeminiClient("secret-key", "gemini-2.5-flash-lite", http)

    result = client.generate_text("say hi")

    assert result == "hi"
    url, kwargs = http.calls[0]
    assert url == "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent"
    assert kwargs["headers"]["x-goog-api-key"] == "secret-key"
    assert kwargs["json"]["contents"][0]["parts"][0]["text"] == "say hi"
    assert "generationConfig" not in kwargs["json"]


def test_generate_text_json_mode_sets_response_mime_type():
    http = FakeHttp(FakeResponse(200, _candidate_response("{}")))
    client = GeminiClient("secret-key", "gemini-2.5-flash-lite", http)

    client.generate_text("give me json", json_mode=True)

    _, kwargs = http.calls[0]
    assert kwargs["json"]["generationConfig"]["responseMimeType"] == "application/json"


def test_generate_text_json_schema_sets_structured_output_config():
    http = FakeHttp(FakeResponse(200, _candidate_response("{}")))
    client = GeminiClient("secret-key", "gemini-2.5-flash-lite", http)
    schema = {
        "type": "OBJECT",
        "properties": {"kind": {"type": "STRING"}},
        "required": ["kind"],
    }

    client.generate_text("classify this", json_schema=schema)

    _, kwargs = http.calls[0]
    generation_config = kwargs["json"]["generationConfig"]
    assert generation_config["responseMimeType"] == "application/json"
    assert generation_config["responseSchema"] == schema


def test_generate_text_raises_on_non_2xx():
    http = FakeHttp(FakeResponse(429, None, "rate limited"))
    client = GeminiClient("secret-key", "gemini-2.5-flash-lite", http)

    with pytest.raises(GeminiError):
        client.generate_text("say hi")


def test_generate_text_raises_on_missing_content():
    http = FakeHttp(FakeResponse(200, {"candidates": []}))
    client = GeminiClient("secret-key", "gemini-2.5-flash-lite", http)

    with pytest.raises(GeminiError):
        client.generate_text("say hi")


def test_generate_text_concatenates_multiple_parts():
    data = {"candidates": [{"content": {"parts": [{"text": "hello "}, {"text": "world"}]}}]}
    http = FakeHttp(FakeResponse(200, data))
    client = GeminiClient("secret-key", "gemini-2.5-flash-lite", http)

    assert client.generate_text("say hi") == "hello world"


def test_generate_text_posts_with_retry_status_codes_excluding_429():
    http = FakeHttp(FakeResponse(200, _candidate_response("hi")))
    client = GeminiClient("secret-key", "gemini-2.5-flash-lite", http)

    client.generate_text("say hi")

    _, kwargs = http.calls[0]
    assert kwargs["retry_status_codes"] == {500, 502, 503, 504}


def test_generate_text_builds_generation_config_for_thinking_and_output_controls():
    http = FakeHttp(FakeResponse(200, _candidate_response("hi")))
    client = GeminiClient("secret-key", "gemini-2.5-flash-lite", http)

    client.generate_text("say hi", thinking_level="minimal", max_output_tokens=800)

    _, kwargs = http.calls[0]
    assert kwargs["json"]["generationConfig"] == {
        "thinkingConfig": {"thinkingLevel": "minimal"},
        "maxOutputTokens": 800,
    }


def test_generate_text_generation_config_combines_thinking_output_and_json():
    http = FakeHttp(FakeResponse(200, _candidate_response("{}")))
    client = GeminiClient("secret-key", "gemini-2.5-flash-lite", http)

    client.generate_text(
        "classify",
        thinking_level="low",
        max_output_tokens=1200,
        json_mode=True,
    )

    _, kwargs = http.calls[0]
    assert kwargs["json"]["generationConfig"] == {
        "thinkingConfig": {"thinkingLevel": "low"},
        "maxOutputTokens": 1200,
        "responseMimeType": "application/json",
    }


def test_generate_text_calls_tracker_preflight_with_purpose_and_prompt(monkeypatch):
    http = FakeHttp(FakeResponse(200, _candidate_response("hi")))
    tracker = FakeTracker()
    client = GeminiClient("secret-key", "gemini-2.5-flash-lite", http, tracker)
    fixed_now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("job_hunter.gemini._now", lambda: fixed_now)

    client.generate_text("say hi", purpose="gmail_semantic")

    assert tracker.preflight_calls == [("gmail_semantic", "say hi", fixed_now)]


def test_generate_text_records_success_with_exact_usage_metadata(monkeypatch):
    http = FakeHttp(FakeResponse(200, _usage_response()))
    tracker = FakeTracker()
    client = GeminiClient("secret-key", "gemini-2.5-flash-lite", http, tracker)
    fixed_now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("job_hunter.gemini._now", lambda: fixed_now)

    client.generate_text("say hi", purpose="gmail_semantic")

    purpose, prompt, now, kwargs = tracker.success_calls[0]
    assert (purpose, prompt, now) == ("gmail_semantic", "say hi", fixed_now)
    assert kwargs == {
        "prompt_tokens": 120,
        "output_tokens": 40,
        "thinking_tokens": 30,
        "cached_tokens": 0,
        "total_tokens": 190,
    }


def test_generate_text_missing_usage_metadata_records_estimate_and_warns(monkeypatch, caplog):
    http = FakeHttp(FakeResponse(200, _candidate_response("hi")))
    tracker = FakeTracker()
    client = GeminiClient("secret-key", "gemini-2.5-flash-lite", http, tracker)
    fixed_now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("job_hunter.gemini._now", lambda: fixed_now)

    with caplog.at_level("WARNING"):
        result = client.generate_text("say hi", purpose="gmail_semantic")

    assert result == "hi"
    purpose, prompt, now, kwargs = tracker.success_calls[0]
    assert (purpose, prompt, now) == ("gmail_semantic", "say hi", fixed_now)
    assert kwargs == {}
    assert any("usageMetadata" in record.message for record in caplog.records)


def test_generate_text_records_error_for_non_429_failure():
    http = FakeHttp(FakeResponse(500, None, "server error"))
    tracker = FakeTracker()
    client = GeminiClient("secret-key", "gemini-2.5-flash-lite", http, tracker)

    with pytest.raises(GeminiError):
        client.generate_text("say hi", purpose="job_evaluation")

    purpose, prompt, _now, kwargs = tracker.error_calls[0]
    assert purpose == "job_evaluation"
    assert prompt == "say hi"
    assert kwargs == {"http_status": 500}


def test_generate_text_429_without_tracker_raises_generic_gemini_error():
    # A tracker-less client is a test affordance, not a supported production
    # path (Task 8 forbids building one); it keeps the old plain-error behavior.
    http = FakeHttp(FakeResponse(429, None, "rate limited"))
    client = GeminiClient("secret-key", "gemini-2.5-flash-lite", http)

    with pytest.raises(GeminiError):
        client.generate_text("say hi")


def test_generate_text_daily_quota_429_pauses_until_pacific_reset(monkeypatch):
    tracker, store = _real_tracker()
    http = FakeHttp(
        _quota_error_response(
            "Quota exceeded: quota_exceeded for requests per day.",
            quota_id="GenerateRequestsPerDayPerProjectPerModel-FreeTier",
        )
    )
    client = GeminiClient("secret-key", "gemini-2.5-flash-lite", http, tracker)
    now = datetime(2026, 9, 1, 20, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("job_hunter.gemini._now", lambda: now)

    with pytest.raises(GeminiQuotaPaused) as excinfo:
        client.generate_text("say hi", purpose="job_evaluation")

    assert excinfo.value.reason == "daily_quota"
    assert len(http.calls) == 1
    assert http.calls[0][1]["retry_status_codes"] == {500, 502, 503, 504}
    pause = store.get_gemini_pause("gemini-2.5-flash-lite")
    assert pause["reason"] == "daily_quota"


def test_generate_text_rate_limit_429_pauses_ninety_seconds(monkeypatch):
    tracker, store = _real_tracker()
    http = FakeHttp(
        _quota_error_response(
            "Resource exhausted: rate_limit_exceeded, too_many_requests.",
            quota_id="GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
        )
    )
    client = GeminiClient("secret-key", "gemini-2.5-flash-lite", http, tracker)
    now = datetime(2026, 9, 1, 20, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("job_hunter.gemini._now", lambda: now)

    with pytest.raises(GeminiQuotaPaused) as excinfo:
        client.generate_text("say hi", purpose="job_evaluation")

    assert excinfo.value.reason == "rate_limit"
    assert len(http.calls) == 1
    pause = store.get_gemini_pause("gemini-2.5-flash-lite")
    assert pause["reason"] == "rate_limit"
    paused_until = datetime.fromisoformat(pause["paused_until"])
    assert paused_until == now + timedelta(seconds=90)


def test_generate_text_unknown_429_pauses_conservatively(monkeypatch):
    tracker, store = _real_tracker()
    http = FakeHttp(_quota_error_response("Something went wrong."))
    client = GeminiClient("secret-key", "gemini-2.5-flash-lite", http, tracker)
    now = datetime(2026, 9, 1, 20, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("job_hunter.gemini._now", lambda: now)

    with pytest.raises(GeminiQuotaPaused) as excinfo:
        client.generate_text("say hi", purpose="job_evaluation")

    assert excinfo.value.reason == "unknown"
    assert len(http.calls) == 1
    pause = store.get_gemini_pause("gemini-2.5-flash-lite")
    assert pause["reason"] == "unknown"


def test_generate_text_pause_blocks_subsequent_call_without_new_http_request(monkeypatch):
    tracker, _store = _real_tracker()
    http = FakeHttp(
        _quota_error_response(
            "quota_exceeded",
            quota_id="GenerateRequestsPerDayPerProjectPerModel-FreeTier",
        )
    )
    client = GeminiClient("secret-key", "gemini-2.5-flash-lite", http, tracker)
    now = datetime(2026, 9, 1, 20, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("job_hunter.gemini._now", lambda: now)

    with pytest.raises(GeminiQuotaPaused):
        client.generate_text("say hi", purpose="job_evaluation")

    assert len(http.calls) == 1

    with pytest.raises(GeminiQuotaPaused):
        client.generate_text("say hi again", purpose="job_evaluation")

    # The second call was refused locally by the tracker's preflight; no
    # second HTTP request was ever sent.
    assert len(http.calls) == 1


def test_generate_text_429_does_not_reinvoke_preflight_after_record_429(monkeypatch):
    """Regression: the exception must come directly from record_429's return
    value, never from re-running preflight() afterward. A second preflight()
    call re-derives the pause from persisted store state and re-runs the
    daily budget check against the row record_429 just wrote — which used to
    let a bare GeminiError or the wrong exception type (GeminiBudgetExceeded)
    slip through depending on rate_pause_seconds or how full the ceiling was.
    """
    tracker = FakeTracker()
    tracker.record_429 = lambda *a, **k: ("2026-09-01T20:01:30+00:00", "rate_limit")
    http = FakeHttp(FakeResponse(429, None, "rate limited"))
    client = GeminiClient("secret-key", "gemini-2.5-flash-lite", http, tracker)

    with pytest.raises(GeminiQuotaPaused) as excinfo:
        client.generate_text("say hi", purpose="job_evaluation")

    assert excinfo.value.paused_until == "2026-09-01T20:01:30+00:00"
    assert excinfo.value.reason == "rate_limit"
    # Only the single pre-HTTP preflight() call happened; none was re-invoked
    # after the 429 to re-derive the pause.
    assert len(tracker.preflight_calls) == 1


def test_generate_text_429_raises_quota_paused_despite_tight_daily_ceiling(monkeypatch):
    """Regression: the exception type must not depend on how full the daily
    budget is. With a tight rpd ceiling, the old re-preflight-after-record_429
    mechanism counted the just-written quota_429 row, tripped the budget
    check, and raised GeminiBudgetExceeded plus a second, spurious
    blocked_budget row for a call that indisputably reached Google. It must
    now raise GeminiQuotaPaused and write exactly one quota_429 row.
    """
    store = JobStore(":memory:")
    quota = GeminiQuotaSettings(rpm=10, tpm=1000, rpd=2, rate_pause_seconds=1)
    tracker = GeminiUsageTracker(store, quota, "gemini-2.5-flash-lite", run_id="run-1")
    http = FakeHttp(_quota_error_response("Something went wrong."))
    client = GeminiClient("secret-key", "gemini-2.5-flash-lite", http, tracker)
    now = datetime(2026, 9, 1, 20, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("job_hunter.gemini._now", lambda: now)

    with pytest.raises(GeminiQuotaPaused) as excinfo:
        client.generate_text("say hi", purpose="job_evaluation")

    assert excinfo.value.reason == "unknown"
    rows = store.gemini_usage_rows(
        "2026-09-01T00:00:00+00:00",
        "2026-09-02T00:00:00+00:00",
        model="gemini-2.5-flash-lite",
    )
    assert [row["status"] for row in rows] == ["quota_429"]
