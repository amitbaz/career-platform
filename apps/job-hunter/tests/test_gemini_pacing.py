from job_hunter.gemini import GeminiClient
from job_hunter.gemini_usage import GeminiTemporaryCapacity


class _Tracker:
    def __init__(self):
        self.preflight_calls = 0
        self.success_calls = 0

    def preflight(self, purpose, prompt, now):
        self.preflight_calls += 1
        if self.preflight_calls == 1:
            raise GeminiTemporaryCapacity(
                "rolling capacity full",
                retry_after_seconds=12.5,
            )

    def record_success(self, *args, **kwargs):
        self.success_calls += 1


class _Response:
    status_code = 200
    text = ""

    def json(self):
        return {
            "candidates": [{"content": {"parts": [{"text": "ok"}]}}],
            "usageMetadata": {
                "promptTokenCount": 10,
                "candidatesTokenCount": 2,
                "totalTokenCount": 12,
            },
        }


class _Http:
    def __init__(self, slept):
        self.slept = slept
        self.post_calls = 0

    def post(self, *args, **kwargs):
        assert self.slept == [12.5]
        self.post_calls += 1
        return _Response()


def test_client_waits_and_rechecks_capacity_before_http_call():
    slept = []
    tracker = _Tracker()
    http = _Http(slept)
    client = GeminiClient(
        "key",
        "gemini-test",
        http,
        tracker,
        sleep_fn=slept.append,
    )

    assert client.generate_text("prompt", purpose="job_evaluation") == "ok"
    assert slept == [12.5]
    assert tracker.preflight_calls == 2
    assert http.post_calls == 1
    assert tracker.success_calls == 1
