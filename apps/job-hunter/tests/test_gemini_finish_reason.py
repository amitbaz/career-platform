from datetime import datetime, timezone

import pytest

from job_hunter.ai import AIError, CallClass
from job_hunter.ai.gemini import build_gemini_provider, AIIncompleteResponse
from job_hunter.ai.usage import AIUsageTracker
from job_hunter.models import AIQuotaSettings


def _generate(provider, prompt, **kwargs):
    """Every adapter test is a user-subjective call; the class itself is
    exercised in tests/test_ai_call_class.py."""
    return provider.generate_text(prompt, call_class=CallClass.USER_SUBJECTIVE, **kwargs)



_USAGE_METADATA = {
    "promptTokenCount": 100,
    "candidatesTokenCount": 1800,
    "thoughtsTokenCount": 20,
    "cachedContentTokenCount": 0,
    "totalTokenCount": 1920,
}


class FakeResponse:
    status_code = 200
    text = ""

    def __init__(self, json_data):
        self._json_data = json_data

    def json(self):
        return self._json_data


def _max_tokens_response():
    return FakeResponse(
        {
            "candidates": [
                {
                    "content": {"parts": [{"text": '{"partial": "json'}]},
                    "finishReason": "MAX_TOKENS",
                }
            ],
            "usageMetadata": dict(_USAGE_METADATA),
        }
    )


def _completed_response():
    return FakeResponse(
        {
            "candidates": [
                {
                    "content": {"parts": [{"text": '{"complete": true}'}]},
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": dict(_USAGE_METADATA),
        }
    )


class FakeHttp:
    def __init__(self, response):
        self.response = response

    def post(self, *_args, **_kwargs):
        return self.response


class FakeTracker:
    def __init__(self):
        self.success_calls = []
        self.error_calls = []

    def preflight(self, _purpose, _prompt, _now):
        return None

    def record_success(self, purpose, prompt, now, **kwargs):
        self.success_calls.append((purpose, prompt, now, kwargs))

    def record_error(self, purpose, prompt, now, **kwargs):
        self.error_calls.append((purpose, prompt, now, kwargs))


def _ledger_rows(store):
    return store.ai_usage_rows(
        "2026-09-03T00:00:00+00:00",
        "2026-09-04T00:00:00+00:00",
        provider="gemini",
        model="gemini-test",
    )


@pytest.fixture
def frozen_now(monkeypatch):
    now = datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("job_hunter.ai.gemini._now", lambda: now)
    return now


def test_max_tokens_is_recorded_as_an_error_with_provider_usage(frozen_now):
    tracker = FakeTracker()
    client = build_gemini_provider("key", "gemini-test", FakeHttp(_max_tokens_response()), tracker=tracker)

    with pytest.raises(AIIncompleteResponse) as excinfo:
        _generate(client, "profile prompt", purpose="candidate_context", max_output_tokens=1800)

    assert excinfo.value.provider_finish_reason == "MAX_TOKENS"
    assert tracker.success_calls == []
    assert len(tracker.error_calls) == 1
    purpose, prompt, recorded_at, usage = tracker.error_calls[0]
    assert (purpose, prompt, recorded_at) == ("candidate_context", "profile prompt", frozen_now)
    assert usage["error_code"] == "MAX_TOKENS"
    assert usage["prompt_tokens"] == 100
    assert usage["output_tokens"] == 1800
    assert usage["thinking_tokens"] == 20
    assert usage["total_tokens"] == 1920


def test_completed_response_is_recorded_as_a_single_success(frozen_now):
    tracker = FakeTracker()
    client = build_gemini_provider("key", "gemini-test", FakeHttp(_completed_response()), tracker=tracker)

    text = _generate(client, "profile prompt", purpose="candidate_context")

    assert text == '{"complete": true}'
    assert tracker.error_calls == []
    assert len(tracker.success_calls) == 1
    _, _, _, usage = tracker.success_calls[0]
    assert usage["output_tokens"] == 1800
    assert usage["total_tokens"] == 1920


def test_structurally_incomplete_response_is_recorded_as_an_error(frozen_now):
    """A candidate with no usable text never reached the caller as a success,
    so it must not be missing from the ledger either — the call still consumed
    provider quota."""
    tracker = FakeTracker()
    response = FakeResponse(
        {
            "candidates": [{"content": {"parts": []}}],
            "usageMetadata": dict(_USAGE_METADATA),
        }
    )
    client = build_gemini_provider("key", "gemini-test", FakeHttp(response), tracker=tracker)

    with pytest.raises(AIError):
        _generate(client, "profile prompt", purpose="candidate_context")

    assert tracker.success_calls == []
    assert len(tracker.error_calls) == 1
    _, _, _, usage = tracker.error_calls[0]
    assert usage["error_code"] == "missing_content"
    assert usage["total_tokens"] == 1920


def test_truncation_that_left_no_text_is_attributed_to_max_tokens(frozen_now):
    """Thinking can consume the whole output budget, leaving a MAX_TOKENS
    candidate with no text at all. The ledger must name the truncation rather
    than blame a malformed body, or the telemetry hides the same root cause
    this fix exists to surface."""
    tracker = FakeTracker()
    response = FakeResponse(
        {
            "candidates": [{"content": {"parts": []}, "finishReason": "MAX_TOKENS"}],
            "usageMetadata": dict(_USAGE_METADATA),
        }
    )
    client = build_gemini_provider("key", "gemini-test", FakeHttp(response), tracker=tracker)

    with pytest.raises(AIError):
        _generate(client, "profile prompt", purpose="candidate_context")

    assert tracker.success_calls == []
    assert len(tracker.error_calls) == 1
    _, _, _, usage = tracker.error_calls[0]
    assert usage["error_code"] == "MAX_TOKENS"
    assert usage["total_tokens"] == 1920


def test_truncated_candidate_without_content_is_attributed_to_max_tokens(frozen_now):
    """Same truncation, one step earlier: Google omitted `content` entirely."""
    tracker = FakeTracker()
    response = FakeResponse(
        {
            "candidates": [{"finishReason": "MAX_TOKENS"}],
            "usageMetadata": dict(_USAGE_METADATA),
        }
    )
    client = build_gemini_provider("key", "gemini-test", FakeHttp(response), tracker=tracker)

    with pytest.raises(AIError):
        _generate(client, "profile prompt", purpose="candidate_context")

    assert len(tracker.error_calls) == 1
    assert tracker.error_calls[0][3]["error_code"] == "MAX_TOKENS"


def test_max_tokens_ledger_row_has_error_status(store, frozen_now):
    tracker = AIUsageTracker(
        store,
        AIQuotaSettings(rpm=10, tpm=100000, rpd=100),
        "gemini-test",
        provider="gemini",
    )
    client = build_gemini_provider("key", "gemini-test", FakeHttp(_max_tokens_response()), tracker=tracker)

    with pytest.raises(AIIncompleteResponse):
        _generate(client, "profile prompt", purpose="candidate_context")

    rows = _ledger_rows(store)
    assert [row["status"] for row in rows] == ["error"]
    assert rows[0]["error_code"] == "MAX_TOKENS"
    assert rows[0]["prompt_tokens"] == 100
    assert rows[0]["output_tokens"] == 1800
    assert rows[0]["thinking_tokens"] == 20
    assert rows[0]["total_tokens"] == 1920


def test_completed_ledger_row_has_success_status(store, frozen_now):
    tracker = AIUsageTracker(
        store,
        AIQuotaSettings(rpm=10, tpm=100000, rpd=100),
        "gemini-test",
        provider="gemini",
    )
    client = build_gemini_provider("key", "gemini-test", FakeHttp(_completed_response()), tracker=tracker)

    _generate(client, "profile prompt", purpose="candidate_context")

    rows = _ledger_rows(store)
    assert [row["status"] for row in rows] == ["success"]
    assert rows[0]["total_tokens"] == 1920
