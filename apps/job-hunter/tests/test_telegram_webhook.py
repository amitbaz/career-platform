from job_hunter.config import WebhookSettings
from job_hunter.models import NavigationCard, NavigationSession
from job_hunter.telegram_webhook import create_app


class FakeNavigationRepository:
    def __init__(self, session=None, error=None):
        self.session = session
        self.error = error
        self.calls = []

    def get_session(self, session_id):
        self.calls.append(session_id)
        if self.error is not None:
            raise self.error
        return self.session


class FakeTelegram:
    def __init__(self):
        self.edits = []
        self.answers = []

    def edit_job_card(self, *, chat_id, message_id, text, keyboard):
        self.edits.append((str(chat_id), str(message_id), text, keyboard))
        return True

    def answer_callback(self, callback_id, text=None, show_alert=False):
        self.answers.append((callback_id, text, show_alert))
        return True


def _settings():
    return WebhookSettings(
        telegram_bot_token="bot-token",
        telegram_webhook_secret="webhook-secret",
        github_repository="amitbaz/job-hunter-bot",
        github_state_token="github-token",
        github_dispatch_token="dispatch-token",
        github_state_artifact_name="job-hunter-state",
        github_state_cache_dir="/tmp/job-hunter-state",
    )


def _session():
    return NavigationSession(
        session_id="session1",
        cards=[
            NavigationCard(1, "Senior A", "Acme", "Berlin", 91, "https://example.test/a"),
            NavigationCard(2, "Senior B", "Beta", "Remote", 88, "https://example.test/b"),
        ],
        telegram_message_id="99",
        created_at="2026-09-01T00:00:00+00:00",
        expires_at="2099-01-01T00:00:00+00:00",
    )


def _callback_update(data="n|session1|1"):
    return {
        "update_id": 10,
        "callback_query": {
            "id": "cb-1",
            "data": data,
            "message": {"message_id": 99, "chat": {"id": 123}},
        },
    }


def test_health_endpoint_does_not_require_secret():
    app = create_app(
        settings=_settings(),
        navigation_repository=FakeNavigationRepository(),
        telegram=FakeTelegram(),
    )
    response = app.test_client().get("/health")
    assert response.status_code == 200
    assert response.get_json() == {"ok": True}


def test_webhook_rejects_wrong_secret_before_repository_access():
    repository = FakeNavigationRepository(_session())
    telegram = FakeTelegram()
    app = create_app(settings=_settings(), navigation_repository=repository, telegram=telegram)

    response = app.test_client().post(
        "/telegram/webhook",
        json=_callback_update(),
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
    )

    assert response.status_code == 403
    assert repository.calls == []
    assert telegram.edits == []


def test_webhook_navigation_reads_repository_and_edits_card():
    repository = FakeNavigationRepository(_session())
    telegram = FakeTelegram()
    app = create_app(settings=_settings(), navigation_repository=repository, telegram=telegram)

    response = app.test_client().post(
        "/telegram/webhook",
        json=_callback_update(),
        headers={"X-Telegram-Bot-Api-Secret-Token": "webhook-secret"},
    )

    assert response.status_code == 200
    assert repository.calls == ["session1"]
    assert telegram.edits[0][0:2] == ("123", "99")
    assert "Company: Beta" in telegram.edits[0][2]
    assert telegram.answers[-1] == ("cb-1", None, False)


def test_apply_callback_does_not_need_repository():
    repository = FakeNavigationRepository(error=AssertionError("repository should not load"))
    telegram = FakeTelegram()
    app = create_app(settings=_settings(), navigation_repository=repository, telegram=telegram)

    response = app.test_client().post(
        "/telegram/webhook",
        json=_callback_update("a|session1|0"),
        headers={"X-Telegram-Bot-Api-Secret-Token": "webhook-secret"},
    )

    assert response.status_code == 200
    assert repository.calls == []
    assert telegram.answers[-1][1] == "Apply functionality coming soon."


def test_noop_callback_does_not_need_repository():
    repository = FakeNavigationRepository(error=AssertionError("repository should not load"))
    telegram = FakeTelegram()
    app = create_app(settings=_settings(), navigation_repository=repository, telegram=telegram)

    response = app.test_client().post(
        "/telegram/webhook",
        json=_callback_update("x|session1|0"),
        headers={"X-Telegram-Bot-Api-Secret-Token": "webhook-secret"},
    )

    assert response.status_code == 200
    assert repository.calls == []


def test_webhook_missing_session_reports_syncing():
    repository = FakeNavigationRepository(session=None)
    telegram = FakeTelegram()
    app = create_app(settings=_settings(), navigation_repository=repository, telegram=telegram)

    response = app.test_client().post(
        "/telegram/webhook",
        json=_callback_update(),
        headers={"X-Telegram-Bot-Api-Secret-Token": "webhook-secret"},
    )

    assert response.status_code == 200
    assert repository.calls == ["session1"]
    assert telegram.answers[-1][1] == "Job list is still syncing. Try again shortly."


def test_webhook_repository_failure_is_acknowledged():
    repository = FakeNavigationRepository(error=RuntimeError("github unavailable"))
    telegram = FakeTelegram()
    app = create_app(settings=_settings(), navigation_repository=repository, telegram=telegram)

    response = app.test_client().post(
        "/telegram/webhook",
        json=_callback_update(),
        headers={"X-Telegram-Bot-Api-Secret-Token": "webhook-secret"},
    )

    assert response.status_code == 200
    assert telegram.answers[-1][1] == "Could not load this job list right now."


def test_webhook_ignores_non_callback_updates():
    repository = FakeNavigationRepository(error=AssertionError("repository should not load"))
    telegram = FakeTelegram()
    app = create_app(settings=_settings(), navigation_repository=repository, telegram=telegram)

    response = app.test_client().post(
        "/telegram/webhook",
        json={"update_id": 11, "message": {"text": "hello"}},
        headers={"X-Telegram-Bot-Api-Secret-Token": "webhook-secret"},
    )

    assert response.status_code == 200
    assert repository.calls == []


class FakeDispatcher:
    def __init__(self):
        self.calls = []

    def __call__(self, repo, token, event_type, client_payload, *, http=None):
        self.calls.append((repo, token, event_type, client_payload))


def test_gen_cl_callback_triggers_repository_dispatch(monkeypatch):
    dispatcher = FakeDispatcher()
    monkeypatch.setattr("job_hunter.telegram_webhook.trigger_repository_dispatch", dispatcher)

    repository = FakeNavigationRepository(_session())
    telegram = FakeTelegram()
    app = create_app(settings=_settings(), navigation_repository=repository, telegram=telegram)

    response = app.test_client().post(
        "/telegram/webhook",
        json=_callback_update("c|session1|1"),
        headers={"X-Telegram-Bot-Api-Secret-Token": "webhook-secret"},
    )

    assert response.status_code == 200
    assert dispatcher.calls == [
        ("amitbaz/job-hunter-bot", "dispatch-token", "generate_cover_letter", {"job_id": 2})
    ]
    assert "cover letter" in telegram.answers[-1][1].lower()


def test_gen_cl_dispatch_failure_still_acknowledges_callback(monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("github unavailable")

    monkeypatch.setattr("job_hunter.telegram_webhook.trigger_repository_dispatch", _boom)

    repository = FakeNavigationRepository(_session())
    telegram = FakeTelegram()
    app = create_app(settings=_settings(), navigation_repository=repository, telegram=telegram)

    response = app.test_client().post(
        "/telegram/webhook",
        json=_callback_update("c|session1|1"),
        headers={"X-Telegram-Bot-Api-Secret-Token": "webhook-secret"},
    )

    assert response.status_code == 200
