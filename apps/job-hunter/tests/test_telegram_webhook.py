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
        github_dispatch_token="dispatch-token",
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


def _patch_supabase_construction(monkeypatch, webhook_module):
    from job_hunter.config import SupabaseSettings

    monkeypatch.setattr(
        webhook_module,
        "load_supabase_settings",
        lambda: SupabaseSettings(
            user_id="00000000-0000-0000-0000-000000000000",
            url="https://example.test",
            publishable_key="pk",
            signing_key_jwk={"kty": "EC"},
        ),
    )
    monkeypatch.setattr(webhook_module, "AccessTokenMinter", lambda user_id, jwk: object())
    monkeypatch.setattr(webhook_module, "SupabaseClient", lambda http, settings, minter: object())


def test_default_navigation_store_factory_returns_a_dry_run_store(monkeypatch):
    """The internet-facing webhook must never hold a write-capable store.

    RLS scopes the per-user token to its owner, so this isn't cross-user
    exposure -- the risk `DryRunStore` guards against here is
    self-corruption from a future webhook edit. This pins the invariant
    the deleted SQLite `test_repository_opens_snapshot_read_only` used to
    defend, now for the live Postgres-backed default construction path.
    The factory is tested directly (rather than through `create_app`)
    because `create_app` must not call it eagerly -- see fix 7.
    """
    from job_hunter import telegram_webhook as webhook_module
    from job_hunter.postgres_store import DryRunStore

    _patch_supabase_construction(monkeypatch, webhook_module)

    store = webhook_module._default_navigation_store_factory(http=object())

    assert isinstance(store, DryRunStore)


def test_health_endpoint_does_not_require_secret():
    app = create_app(
        settings=_settings(),
        navigation_repository=FakeNavigationRepository(),
        telegram=FakeTelegram(),
    )
    response = app.test_client().get("/health")
    assert response.status_code == 200
    assert response.get_json() == {"ok": True}


def test_health_reports_503_and_missing_names_when_misconfigured(monkeypatch):
    """`/health` must never bodyless-500 from a missing env var.

    Clearing all required env vars and using the default (unconfigured)
    construction path -- no injected `settings` or `navigation_repository`
    -- proves import-time safety and the 503-with-names contract in one
    test: `create_app()` itself must not raise just from missing env vars.
    """
    for name in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_WEBHOOK_SECRET",
        "GITHUB_REPOSITORY",
        "GITHUB_DISPATCH_TOKEN",
        "JOB_HUNTER_USER_ID",
        "SUPABASE_URL",
        "SUPABASE_PUBLISHABLE_KEY",
        "SUPABASE_SIGNING_KEY_B64",
    ):
        monkeypatch.delenv(name, raising=False)

    app = create_app()  # must not raise
    response = app.test_client().get("/health")

    assert response.status_code == 503
    body = response.get_json()
    assert body["ok"] is False
    assert set(body["missing"]) == {
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_WEBHOOK_SECRET",
        "GITHUB_REPOSITORY",
        "GITHUB_DISPATCH_TOKEN",
        "JOB_HUNTER_USER_ID",
        "SUPABASE_URL",
        "SUPABASE_PUBLISHABLE_KEY",
        "SUPABASE_SIGNING_KEY_B64",
    }
    # Never leak values, only names -- there are none set here to leak, but
    # the contract is names-only regardless of what a real deployment holds.
    assert "value" not in str(body)


def test_health_reports_ok_when_fully_configured(monkeypatch):
    for name, value in {
        "TELEGRAM_BOT_TOKEN": "bot-token",
        "TELEGRAM_WEBHOOK_SECRET": "webhook-secret",
        "GITHUB_REPOSITORY": "amitbaz/job-hunter-bot",
        "GITHUB_DISPATCH_TOKEN": "dispatch-token",
        "JOB_HUNTER_USER_ID": "00000000-0000-0000-0000-000000000000",
        "SUPABASE_URL": "https://example.test",
        "SUPABASE_PUBLISHABLE_KEY": "pk",
        "SUPABASE_SIGNING_KEY_B64": "sig",
    }.items():
        monkeypatch.setenv(name, value)

    app = create_app()
    response = app.test_client().get("/health")

    assert response.status_code == 200
    assert response.get_json() == {"ok": True}


def test_webhook_still_fails_when_misconfigured(monkeypatch):
    """Fail-fast is correct for `/telegram/webhook`: a callback it cannot
    serve is worse than one it refuses. Unlike `/health`, hitting the
    route itself (not just constructing the app) must surface the missing
    configuration -- resolving settings happens inside the route handler.
    """
    for name in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_WEBHOOK_SECRET",
        "GITHUB_REPOSITORY",
        "GITHUB_DISPATCH_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)

    app = create_app()  # construction itself must not raise
    app.testing = False  # let Flask's default error handler produce a 500
    response = app.test_client().post(
        "/telegram/webhook",
        json=_callback_update(),
        headers={"X-Telegram-Bot-Api-Secret-Token": "whatever"},
    )

    assert response.status_code == 500


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
