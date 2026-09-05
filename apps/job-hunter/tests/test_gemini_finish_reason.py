from datetime import datetime, timezone

import pytest

from job_hunter.gemini import GeminiClient, GeminiIncompleteResponse


class FakeResponse:
    status_code = 200
    text = ""

    def json(self):
        return {
            "candidates": [
                {
                    "content": {"parts": [{"text": '{"partial": "json'}]},
                    "finishReason": "MAX_TOKENS",
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 100,
                "candidatesTokenCount": 1800,
                "thoughtsTokenCount": 20,
                "cachedContentTokenCount": 0,
                "totalTokenCount": 1920,
            },
        }


class FakeHttp:
    def post(self, *_args, **_kwargs):
        return FakeResponse()


class FakeTracker:
    def __init__(self):
        self.success_calls = []

    def preflight(self, _purpose, _prompt, _now):
        return None

    def record_success(self, purpose, prompt, now, **kwargs):
        self.success_calls.append((purpose, prompt, now, kwargs))


def test_max_tokens_is_classified_as_incomplete_after_usage_is_recorded(monkeypatch):
    tracker = FakeTracker()
    client = GeminiClient("key", "gemini-test", FakeHttp(), tracker)
    now = datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("job_hunter.gemini._now", lambda: now)

    with pytest.raises(GeminiIncompleteResponse) as excinfo:
        client.generate_text("profile prompt", purpose="candidate_context", max_output_tokens=1800)

    assert excinfo.value.finish_reason == "MAX_TOKENS"
    assert len(tracker.success_calls) == 1
    purpose, prompt, recorded_at, usage = tracker.success_calls[0]
    assert (purpose, prompt, recorded_at) == ("candidate_context", "profile prompt", now)
    assert usage["output_tokens"] == 1800
    assert usage["total_tokens"] == 1920
