import requests
import pytest

from engine.ai import AIError, CallClass
from engine.ai.gemini import build_gemini_provider


def _generate(provider, prompt, **kwargs):
    """Every adapter test is a user-subjective call; the class itself is
    exercised in tests/test_ai_call_class.py."""
    return provider.generate_text(prompt, call_class=CallClass.USER_SUBJECTIVE, **kwargs)




class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text

    def json(self):
        return self._json_data


class CapturingHttp:
    def __init__(self, response=None, exception=None):
        self.response = response
        self.exception = exception
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.exception is not None:
            raise self.exception
        return self.response


class FakeTracker:
    def __init__(self):
        self.preflight_calls = []
        self.error_calls = []

    def preflight(self, purpose, prompt, now):
        self.preflight_calls.append((purpose, prompt, now))

    def record_error(self, purpose, prompt, now, **kwargs):
        self.error_calls.append((purpose, prompt, now, kwargs))


def test_gemini_disables_automatic_retries():
    http = CapturingHttp(FakeResponse(503, None, "high demand"))
    tracker = FakeTracker()
    client = build_gemini_provider("key", "gemini-3.6-flash", http, tracker=tracker)

    with pytest.raises(AIError):
        _generate(client, "evaluate", purpose="job_evaluation")

    assert len(http.calls) == 1
    _, kwargs = http.calls[0]
    assert kwargs["retry_status_codes"] == {500, 502, 503, 504}
    assert kwargs["retry"] is False
    assert len(tracker.error_calls) == 1
    assert tracker.error_calls[0][3] == {"http_status": 503}


def test_gemini_records_network_exception_once_and_reraises():
    error = requests.ReadTimeout("slow Gemini response")
    http = CapturingHttp(exception=error)
    tracker = FakeTracker()
    client = build_gemini_provider("key", "gemini-3.6-flash", http, tracker=tracker)

    with pytest.raises(requests.ReadTimeout):
        _generate(client, "evaluate", purpose="job_evaluation")

    assert len(http.calls) == 1
    assert len(tracker.error_calls) == 1
    purpose, prompt, _now, kwargs = tracker.error_calls[0]
    assert purpose == "job_evaluation"
    assert prompt == "evaluate"
    assert kwargs == {"error_code": "ReadTimeout"}
