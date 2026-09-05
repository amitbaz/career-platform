import pytest

from job_hunter.github_dispatch import trigger_repository_dispatch


class FakeResponse:
    def __init__(self, status_code=204):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")


class FakeHttp:
    def __init__(self, response=None):
        self.response = response or FakeResponse()
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def test_trigger_repository_dispatch_posts_expected_payload():
    http = FakeHttp()

    trigger_repository_dispatch(
        "amitbaz/job-hunter-bot",
        "gh-token",
        "generate_cover_letter",
        {"job_id": 42},
        http=http,
    )

    url, kwargs = http.calls[0]
    assert url == "https://api.github.com/repos/amitbaz/job-hunter-bot/dispatches"
    assert kwargs["headers"]["Authorization"] == "Bearer gh-token"
    assert kwargs["headers"]["Accept"] == "application/vnd.github+json"
    assert kwargs["json"] == {
        "event_type": "generate_cover_letter",
        "client_payload": {"job_id": 42},
    }


def test_trigger_repository_dispatch_raises_on_error_status():
    http = FakeHttp(FakeResponse(422))

    with pytest.raises(RuntimeError):
        trigger_repository_dispatch(
            "amitbaz/job-hunter-bot", "gh-token", "generate_cover_letter", {"job_id": 1}, http=http
        )
