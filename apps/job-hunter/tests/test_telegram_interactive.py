from job_hunter.models import NavigationCard, NavigationSession
from job_hunter.telegram import TelegramClient
from job_hunter.telegram_navigation import handle_callback_query


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data or {"result": {"message_id": 42}}
        self.text = text

    def json(self):
        return self._json_data


class FakeHttp:
    def __init__(self, response=None):
        self.response = response or FakeResponse()
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class FakeTelegram:
    def __init__(self, edit_result=True):
        self.edits = []
        self.answers = []
        self.edit_result = edit_result

    def edit_job_card(self, *, chat_id, message_id, text, keyboard):
        self.edits.append((str(chat_id), str(message_id), text, keyboard))
        return self.edit_result

    def answer_callback(self, callback_id, text=None, show_alert=False):
        self.answers.append((callback_id, text, show_alert))
        return True


def _session(*, message_id="99", expires_at="2099-01-01T00:00:00+00:00"):
    return NavigationSession(
        session_id="session1",
        cards=[
            NavigationCard(1, "Senior A", "Acme", "Berlin", 91, "https://example.test/a"),
            NavigationCard(2, "Senior B", "Beta", "Remote", 88, "https://example.test/b"),
        ],
        telegram_message_id=message_id,
        created_at="2026-09-01T00:00:00+00:00",
        expires_at=expires_at,
    )


def test_send_job_card_posts_inline_keyboard():
    http = FakeHttp()
    client = TelegramClient("token123", "chat456", http)
    keyboard = [[{"text": "Apply", "callback_data": "a|s|0"}]]

    result = client.send_job_card("card text", keyboard)

    assert result == "42"
    url, kwargs = http.calls[0]
    assert url.endswith("/sendMessage")
    assert kwargs["json"]["chat_id"] == "chat456"
    assert kwargs["json"]["text"] == "card text"
    assert kwargs["json"]["reply_markup"] == {"inline_keyboard": keyboard}


def test_edit_job_card_edits_same_message():
    http = FakeHttp()
    client = TelegramClient("token123", "chat456", http)
    keyboard = [[{"text": "2 / 3", "callback_data": "x|s|1"}]]

    assert client.edit_job_card(
        chat_id="123",
        message_id="42",
        text="next card",
        keyboard=keyboard,
    ) is True

    url, kwargs = http.calls[0]
    assert url.endswith("/editMessageText")
    assert kwargs["json"]["chat_id"] == "123"
    assert kwargs["json"]["message_id"] == "42"
    assert kwargs["json"]["reply_markup"] == {"inline_keyboard": keyboard}


def test_answer_callback_query():
    http = FakeHttp(FakeResponse(200, {"ok": True}))
    client = TelegramClient("token123", "chat456", http)

    assert client.answer_callback("cb-1", text="Apply functionality coming soon.") is True

    url, kwargs = http.calls[0]
    assert url.endswith("/answerCallbackQuery")
    assert kwargs["json"]["callback_query_id"] == "cb-1"
    assert kwargs["json"]["text"] == "Apply functionality coming soon."
    assert kwargs["json"]["show_alert"] is False


def test_next_callback_edits_same_message_and_acknowledges():
    telegram = FakeTelegram()
    handled = handle_callback_query(
        {
            "id": "cb-1",
            "data": "n|session1|1",
            "message": {"message_id": 99, "chat": {"id": 123}},
        },
        session_loader=lambda _sid: _session(),
        telegram=telegram,
    )

    assert handled is True
    assert telegram.edits[0][0:2] == ("123", "99")
    assert "Company: Beta" in telegram.edits[0][2]
    assert telegram.answers[-1] == ("cb-1", None, False)


def test_apply_callback_only_answers_placeholder():
    telegram = FakeTelegram()
    handled = handle_callback_query(
        {
            "id": "cb-apply",
            "data": "a|session1|0",
            "message": {"message_id": 99, "chat": {"id": 123}},
        },
        session_loader=lambda _sid: (_ for _ in ()).throw(AssertionError("session should not load")),
        telegram=telegram,
    )

    assert handled is True
    assert telegram.edits == []
    assert telegram.answers[-1] == (
        "cb-apply",
        "Apply functionality coming soon.",
        False,
    )


def test_noop_callback_only_acknowledges():
    telegram = FakeTelegram()
    assert handle_callback_query(
        {
            "id": "cb-noop",
            "data": "x|session1|0",
            "message": {"message_id": 99, "chat": {"id": 123}},
        },
        session_loader=lambda _sid: _session(),
        telegram=telegram,
    ) is True
    assert telegram.edits == []
    assert telegram.answers[-1] == ("cb-noop", None, False)


def test_malformed_callback_is_rejected_safely():
    telegram = FakeTelegram()
    assert handle_callback_query(
        {"id": "cb-bad", "data": "bad", "message": {"message_id": 99, "chat": {"id": 123}}},
        session_loader=lambda _sid: _session(),
        telegram=telegram,
    ) is False
    assert telegram.edits == []
    assert telegram.answers[-1][1] == "This action is no longer available."


def test_missing_or_expired_session_does_not_edit():
    for session in (None, _session(expires_at="2000-01-01T00:00:00+00:00")):
        telegram = FakeTelegram()
        assert handle_callback_query(
            {
                "id": "cb-stale",
                "data": "n|session1|1",
                "message": {"message_id": 99, "chat": {"id": 123}},
            },
            session_loader=lambda _sid, value=session: value,
            telegram=telegram,
        ) is False
        assert telegram.edits == []
        assert telegram.answers[-1][1] == "This job list has expired."


def test_invalid_index_or_message_mismatch_is_rejected():
    for data, session in (
        ("n|session1|9", _session()),
        ("n|session1|1", _session(message_id="100")),
    ):
        telegram = FakeTelegram()
        assert handle_callback_query(
            {
                "id": "cb-invalid",
                "data": data,
                "message": {"message_id": 99, "chat": {"id": 123}},
            },
            session_loader=lambda _sid, value=session: value,
            telegram=telegram,
        ) is False
        assert telegram.edits == []
        assert telegram.answers[-1][1] == "This action is no longer available."


def test_edit_failure_returns_short_callback_error():
    telegram = FakeTelegram(edit_result=False)
    assert handle_callback_query(
        {
            "id": "cb-fail",
            "data": "n|session1|1",
            "message": {"message_id": 99, "chat": {"id": 123}},
        },
        session_loader=lambda _sid: _session(),
        telegram=telegram,
    ) is False
    assert telegram.answers[-1][1] == "Could not update this job right now."


def test_gen_cl_callback_invokes_on_generate_and_answers():
    telegram = FakeTelegram()
    generated = []
    handled = handle_callback_query(
        {
            "id": "cb-gen",
            "data": "c|session1|1",
            "message": {"message_id": 99, "chat": {"id": 123}},
        },
        session_loader=lambda _sid: _session(),
        telegram=telegram,
        on_generate=lambda job_id: generated.append(job_id),
    )

    assert handled is True
    assert generated == [2]  # cards[1].job_id
    assert telegram.edits == []
    assert telegram.answers[-1][0] == "cb-gen"
    assert "cover letter" in telegram.answers[-1][1].lower()


def test_gen_cl_callback_on_expired_session_does_not_call_on_generate():
    telegram = FakeTelegram()
    generated = []
    handled = handle_callback_query(
        {
            "id": "cb-gen",
            "data": "c|session1|0",
            "message": {"message_id": 99, "chat": {"id": 123}},
        },
        session_loader=lambda _sid: _session(expires_at="2000-01-01T00:00:00+00:00"),
        telegram=telegram,
        on_generate=lambda job_id: generated.append(job_id),
    )

    assert handled is False
    assert generated == []
    assert telegram.answers[-1][1] == "This job list has expired."
