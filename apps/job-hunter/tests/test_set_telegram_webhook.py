import pytest

from scripts.set_telegram_webhook import set_webhook


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self.payload = payload or {"ok": True, "result": True}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class FakeHttp:
    def __init__(self, response=None):
        self.response = response or FakeResponse()
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def test_set_webhook_registers_callback_only_updates_with_secret():
    http = FakeHttp()

    result = set_webhook(
        "bot-token",
        "https://jobs.example.test/telegram/webhook",
        "webhook-secret",
        http=http,
    )

    assert result["ok"] is True
    url, kwargs = http.calls[0]
    assert url == "https://api.telegram.org/botbot-token/setWebhook"
    assert kwargs["json"] == {
        "url": "https://jobs.example.test/telegram/webhook",
        "secret_token": "webhook-secret",
        "allowed_updates": ["callback_query"],
    }


def test_set_webhook_rejects_non_https_url():
    with pytest.raises(ValueError):
        set_webhook("bot-token", "http://localhost/webhook", "secret", http=FakeHttp())


def test_set_webhook_rejects_telegram_ok_false():
    http = FakeHttp(FakeResponse(payload={"ok": False, "description": "bad webhook"}))
    with pytest.raises(RuntimeError, match="bad webhook"):
        set_webhook(
            "bot-token",
            "https://jobs.example.test/telegram/webhook",
            "secret",
            http=http,
        )
