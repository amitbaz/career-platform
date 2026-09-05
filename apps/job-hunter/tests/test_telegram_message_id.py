from job_hunter.telegram import TelegramClient


class FakeResponse:
    status_code = 200
    text = ""

    def json(self):
        return {"ok": True}


class FakeHttp:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse()


def test_edit_job_card_keeps_numeric_message_id_as_integer():
    http = FakeHttp()
    client = TelegramClient("token", None, http)

    assert client.edit_job_card(
        chat_id=123,
        message_id=42,
        text="card",
        keyboard=[],
    ) is True

    _, kwargs = http.calls[0]
    assert kwargs["json"]["message_id"] == 42
