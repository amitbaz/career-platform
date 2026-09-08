from pathlib import Path

from job_hunter.config import load_gmail_settings as load_database_gmail_settings
from job_hunter.gmail_models import GmailSettings, GmailSyncSummary
from job_hunter.models import (
    AIQuotaSettings,
    ProviderCredentials,
    RunSummary,
    SearchPolicy,
    Settings,
)
from job_hunter.postgres_store import DryRunStore
from job_hunter import cli


class _FakeSupabaseClient:
    """Stands in for a real `SupabaseClient` so cli tests never touch the network.

    `cli._build_client` is the single seam every construction site in
    `cli.py` goes through to reach Postgres (issue #70 task 14b), so
    patching it here is enough to keep every `cli.main([...])` test
    fully offline, the same way the old suite patched `cli.JobStore`.
    """

    def __init__(self):
        self.calls = []


def _patch_build_client(monkeypatch):
    monkeypatch.setattr(cli, "_build_client", lambda http: _FakeSupabaseClient())


def _settings(tmp_path, **overrides):
    policy = SearchPolicy(
        target_titles=[],
        positive_keywords=[],
        blocked_title_keywords=[],
        salary_floor_eur=90000,
        thresholds={"package": 75, "possible": 65},
    )
    defaults = dict(
        ai_api_key="key",
        candidate_profile="profile",
        cover_letter_template="template",
        timezone="Europe/Berlin",
        scheduled_hour=9,
        policy=policy,
        ai_quota=AIQuotaSettings(rpm=10, tpm=250000, rpd=500),
        dry_run=True,
        output_dir=str(tmp_path / "var"),
    )
    defaults.update(overrides)
    return Settings(**defaults)


def test_run_scheduled_skips_outside_target_hour(monkeypatch, tmp_path, caplog):
    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda path: settings)
    monkeypatch.setattr(cli, "should_run_scheduled", lambda now, tz, hour: False)
    _patch_build_client(monkeypatch)

    called = []
    monkeypatch.setattr(cli, "run_pipeline", lambda s, **kwargs: called.append(s) or RunSummary())

    with caplog.at_level("INFO"):
        exit_code = cli.main(["run", "--scheduled"])

    assert exit_code == 0
    assert called == []
    assert any("skipped" in record.message.lower() for record in caplog.records)


def test_run_scheduled_proceeds_at_target_hour(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda path: settings)
    monkeypatch.setattr(cli, "should_run_scheduled", lambda now, tz, hour: True)
    _patch_build_client(monkeypatch)

    called = []
    monkeypatch.setattr(cli, "run_pipeline", lambda s, **kwargs: called.append(s) or RunSummary(ready_to_apply=1))

    exit_code = cli.main(["run", "--scheduled"])

    assert exit_code == 0
    assert called == [settings]


def test_run_manual_always_proceeds_without_time_guard(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda path: settings)
    _patch_build_client(monkeypatch)

    def _boom(*args, **kwargs):
        raise AssertionError("should_run_scheduled must not be called for manual runs")

    monkeypatch.setattr(cli, "should_run_scheduled", _boom)

    called = []
    monkeypatch.setattr(cli, "run_pipeline", lambda s, **kwargs: called.append(s) or RunSummary())

    exit_code = cli.main(["run"])

    assert exit_code == 0
    assert called == [settings]


def test_run_loads_settings_from_the_constructed_store(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    captured = {}

    class Store:
        def __init__(self, client):
            captured["store"] = self

    def load_from_store(store):
        captured["settings_store"] = store
        return settings

    monkeypatch.setattr(cli, "PostgresJobStore", Store)
    monkeypatch.setattr(cli, "load_settings", load_from_store)
    monkeypatch.setattr(cli, "run_pipeline", lambda *args, **kwargs: RunSummary())
    _patch_build_client(monkeypatch)

    assert cli.main(["run"]) == 0
    assert captured["settings_store"] is captured["store"]


def test_run_creates_output_parent_directory(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda path: settings)
    monkeypatch.setattr(cli, "run_pipeline", lambda s, **kwargs: RunSummary())
    _patch_build_client(monkeypatch)

    assert not (Path(settings.output_dir) / "cover_letters").exists()

    cli.main(["run"])

    assert (Path(settings.output_dir) / "cover_letters").exists()


def test_run_unhandled_exception_returns_nonzero(monkeypatch, tmp_path):
    def _raise(path):
        raise RuntimeError("config broke")

    monkeypatch.setattr(cli, "load_settings", _raise)

    exit_code = cli.main(["run"])

    assert exit_code == 1


def test_run_fails_when_every_evaluation_this_run_failed(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda path: settings)
    _patch_build_client(monkeypatch)
    monkeypatch.setattr(
        cli,
        "run_pipeline",
        lambda s, **kwargs: RunSummary(errors=3, evaluation_attempted=3, evaluated=0),
    )

    exit_code = cli.main(["run"])

    assert exit_code == 1


def test_run_succeeds_when_some_evaluations_this_run_succeeded(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda path: settings)
    _patch_build_client(monkeypatch)
    monkeypatch.setattr(
        cli,
        "run_pipeline",
        lambda s, **kwargs: RunSummary(errors=1, evaluation_attempted=2, evaluated=1),
    )

    exit_code = cli.main(["run"])

    assert exit_code == 0


def test_run_succeeds_when_no_evaluation_was_needed(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda path: settings)
    _patch_build_client(monkeypatch)
    monkeypatch.setattr(
        cli, "run_pipeline", lambda s, **kwargs: RunSummary(evaluation_attempted=0, evaluated=0)
    )

    exit_code = cli.main(["run"])

    assert exit_code == 0


def test_parser_accepts_sync_gmail_dry_run():
    args = cli.build_parser().parse_args(["sync-gmail", "--dry-run"])

    assert args.command == "sync-gmail"
    assert args.dry_run is True
    assert args.force_backfill is False


def test_parser_accepts_sync_gmail_force_backfill():
    args = cli.build_parser().parse_args(["sync-gmail", "--force-backfill"])

    assert args.command == "sync-gmail"
    assert args.dry_run is False
    assert args.force_backfill is True


def test_force_backfill_help_mentions_120_day_window():
    parser = cli.build_parser()
    sync_gmail_parser = next(
        action.choices["sync-gmail"]
        for action in parser._subparsers._group_actions
        if action.dest == "command"
    )
    force_backfill_action = next(
        action
        for action in sync_gmail_parser._actions
        if action.dest == "force_backfill"
    )

    assert "120-day" in force_backfill_action.help
    assert "12-month" not in force_backfill_action.help


def _gmail_settings():
    return GmailSettings(
        client_id="client",
        client_secret="secret",
        refresh_token="refresh",
        ai_api_key="gemini",
        ai_quota=AIQuotaSettings(rpm=10, tpm=250000, rpd=500),
    )


def _patch_gmail_sync_dependencies(monkeypatch, run):
    class SyncService:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def sync(self, now, *, dry_run, force_backfill):
            return run(now=now, dry_run=dry_run, force_backfill=force_backfill)

    monkeypatch.setattr(
        cli, "load_gmail_settings", lambda store: _gmail_settings(), raising=False
    )
    monkeypatch.setattr(cli, "HttpClient", object, raising=False)
    monkeypatch.setattr(cli, "GoogleOAuthTokenProvider", lambda settings: object(), raising=False)
    monkeypatch.setattr(cli, "GmailClient", lambda http, token_provider: object(), raising=False)
    monkeypatch.setattr(cli, "GeminiProvider", lambda api_key, model, http, tracker=None: object(), raising=False)
    _patch_build_client(monkeypatch)
    monkeypatch.setattr(cli, "GmailSyncService", SyncService, raising=False)


def test_sync_gmail_does_not_load_candidate_profile_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda path: (_ for _ in ()).throw(AssertionError("must not load candidate profile settings")),
    )
    _patch_gmail_sync_dependencies(monkeypatch, lambda **kwargs: GmailSyncSummary())

    assert cli.main(["sync-gmail"]) == 0


def test_sync_gmail_builds_the_store_before_loading_gmail_settings(monkeypatch):
    events = []
    captured = {}
    _patch_gmail_sync_dependencies(monkeypatch, lambda **kwargs: GmailSyncSummary())

    class Http:
        def __init__(self):
            events.append("http")

    class Store:
        def __init__(self, client):
            events.append("store")
            captured["store"] = self

    def build_client(http):
        events.append("client")
        return _FakeSupabaseClient()

    def load_from_store(store):
        events.append("load")
        captured["settings_store"] = store
        return _gmail_settings()

    monkeypatch.setattr(cli, "HttpClient", Http)
    monkeypatch.setattr(cli, "_build_client", build_client)
    monkeypatch.setattr(cli, "PostgresJobStore", Store)
    monkeypatch.setattr(cli, "load_gmail_settings", load_from_store)

    assert cli.main(["sync-gmail"]) == 0
    assert events[:4] == ["http", "client", "store", "load"]
    assert captured["settings_store"] is captured["store"]


def test_sync_gmail_uses_provider_credentials_without_loading_candidate_documents(
    monkeypatch,
):
    captured = {}
    _patch_gmail_sync_dependencies(monkeypatch, lambda **kwargs: GmailSyncSummary())
    monkeypatch.setenv("GMAIL_CLIENT_ID", "client")
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GMAIL_REFRESH_TOKEN", "refresh")
    monkeypatch.setenv("GEMINI_FREE_RPM", "10")
    monkeypatch.setenv("GEMINI_FREE_TPM", "250000")
    monkeypatch.setenv("GEMINI_FREE_RPD", "500")

    class Store:
        def __init__(self, client):
            pass

        def get_provider_credentials(self):
            return ProviderCredentials(gemini_api_key="stored-gemini")

        def get_source_documents(self):
            raise AssertionError("Gmail sync must not load candidate documents")

    def build(api_key, model, http, *, tracker=None):
        captured["api_key"] = api_key
        return object()

    monkeypatch.setattr(cli, "PostgresJobStore", Store)
    monkeypatch.setattr(cli, "load_gmail_settings", load_database_gmail_settings)
    monkeypatch.setattr(cli, "build_gemini_provider", build)

    assert cli.main(["sync-gmail"]) == 0
    assert captured["api_key"] == "stored-gemini"


def test_sync_gmail_returns_nonzero_on_fatal_auth_error(monkeypatch, tmp_path):
    _patch_gmail_sync_dependencies(
        monkeypatch,
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("Gmail authorization failed")),
    )

    assert cli.main(["sync-gmail"]) == 1


def test_sync_gmail_returns_zero_when_service_completes_with_message_errors(
    monkeypatch, tmp_path, caplog
):
    _patch_gmail_sync_dependencies(monkeypatch, lambda **kwargs: GmailSyncSummary(errors=2))

    assert cli.main(["sync-gmail"]) == 0
    assert any("will retry" in record.message for record in caplog.records)


def test_sync_gmail_dry_run_wraps_the_store_in_a_dry_run_store(monkeypatch, tmp_path):
    """`--dry-run` must hand the sync service (and its Gemini tracker) a
    `DryRunStore`, never the real `PostgresJobStore` -- the mechanism that
    replaces the old SQLite read-only-open/`:memory:` construction (see
    `postgres_store.DryRunStore` and `tests/test_postgres_store_dry_run.py`
    for the "never touches the client on a write" proof at the unit level).
    This test only proves cli.py wires the wrapper in; it doesn't re-prove
    DryRunStore's own guarantees.
    """
    settings = _gmail_settings()
    captured = {}

    class InspectingService:
        def __init__(self, *, store, ai, **kwargs):
            captured["store"] = store

        def sync(self, now, *, dry_run, force_backfill):
            assert dry_run is True
            return GmailSyncSummary()

    class InspectingTracker:
        def __init__(self, store, quota, model, *, provider):
            captured["tracker_store"] = store

    monkeypatch.setattr(cli, "load_gmail_settings", lambda store: settings)
    monkeypatch.setattr(cli, "HttpClient", object)
    monkeypatch.setattr(cli, "GoogleOAuthTokenProvider", lambda value: object())
    monkeypatch.setattr(cli, "GmailClient", lambda http, token_provider: object())
    monkeypatch.setattr(
        cli, "build_gemini_provider", lambda api_key, model, http, *, tracker=None: object()
    )
    monkeypatch.setattr(cli, "AIUsageTracker", InspectingTracker)
    _patch_build_client(monkeypatch)
    monkeypatch.setattr(cli, "GmailSyncService", InspectingService)

    assert cli.main(["sync-gmail", "--dry-run"]) == 0
    assert isinstance(captured["store"], DryRunStore)
    assert isinstance(captured["tracker_store"], DryRunStore)


class _CapturingTracker:
    instances = []

    def __init__(self, store, quota, model, *, provider):
        self.store = store
        self.quota = quota
        self.model = model
        self.provider = provider
        type(self).instances.append(self)


class _CapturingProvider:
    instances = []

    def __init__(self, api_key, model, http, *, tracker=None):
        self.api_key = api_key
        self.model = model
        self.tracker = tracker
        type(self).instances.append(self)


def test_run_constructs_one_tracked_provider_sharing_the_user_ledger(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    pipeline_kwargs = {}

    def _run_pipeline(s, **kwargs):
        pipeline_kwargs.update(kwargs)
        return RunSummary()

    monkeypatch.setattr(cli, "load_settings", lambda path: settings)
    monkeypatch.setattr(cli, "run_pipeline", _run_pipeline)
    monkeypatch.setattr(cli, "AIUsageTracker", _CapturingTracker)
    monkeypatch.setattr(cli, "build_gemini_provider", _CapturingProvider)
    _patch_build_client(monkeypatch)
    _CapturingTracker.instances.clear()
    _CapturingProvider.instances.clear()

    assert cli.main(["run"]) == 0

    assert len(_CapturingTracker.instances) == 1
    assert len(_CapturingProvider.instances) == 1
    tracker = _CapturingTracker.instances[0]
    provider = _CapturingProvider.instances[0]
    assert tracker.provider == "gemini"
    assert tracker.model == settings.ai_model
    assert tracker.quota == settings.ai_quota
    assert provider.tracker is tracker
    # The run reports what it spent from the same ledger the provider writes
    # to; reaching into the provider for it is what silently broke once.
    assert pipeline_kwargs["usage"] is tracker


def test_sync_gmail_constructs_one_tracked_provider_sharing_the_user_ledger(monkeypatch, tmp_path):
    settings = _gmail_settings()

    class SyncService:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def sync(self, now, *, dry_run, force_backfill):
            return GmailSyncSummary()

    monkeypatch.setattr(cli, "load_gmail_settings", lambda store: settings)
    monkeypatch.setattr(cli, "HttpClient", object)
    monkeypatch.setattr(cli, "GoogleOAuthTokenProvider", lambda value: object())
    monkeypatch.setattr(cli, "GmailClient", lambda http, token_provider: object())
    monkeypatch.setattr(cli, "AIUsageTracker", _CapturingTracker)
    monkeypatch.setattr(cli, "build_gemini_provider", _CapturingProvider)
    _patch_build_client(monkeypatch)
    monkeypatch.setattr(cli, "GmailSyncService", SyncService)
    _CapturingTracker.instances.clear()
    _CapturingProvider.instances.clear()

    assert cli.main(["sync-gmail"]) == 0

    assert len(_CapturingTracker.instances) == 1
    assert len(_CapturingProvider.instances) == 1
    tracker = _CapturingTracker.instances[0]
    provider = _CapturingProvider.instances[0]
    assert tracker.provider == "gemini"
    assert tracker.model == settings.ai_model
    assert tracker.quota == settings.ai_quota
    assert provider.tracker is tracker


def test_parser_accepts_generate_cover_letter_job_id():
    args = cli.build_parser().parse_args(["generate-cover-letter", "--job-id", "7"])
    assert args.command == "generate-cover-letter"
    assert args.job_id == "7"


def test_generate_cover_letter_delegates_with_job_id(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda path: settings)
    _patch_build_client(monkeypatch)

    calls = []
    monkeypatch.setattr(
        cli,
        "generate_cover_letter_on_demand",
        lambda s, job_id, **kwargs: calls.append((s, job_id)) or True,
    )

    exit_code = cli.main(["generate-cover-letter", "--job-id", "42"])

    assert exit_code == 0
    assert calls == [(settings, "42")]


def test_generate_cover_letter_loads_settings_from_the_constructed_store(
    monkeypatch, tmp_path
):
    settings = _settings(tmp_path)
    captured = {}

    class Store:
        def __init__(self, client):
            captured["store"] = self

    def load_from_store(store):
        captured["settings_store"] = store
        return settings

    monkeypatch.setattr(cli, "PostgresJobStore", Store)
    monkeypatch.setattr(cli, "load_settings", load_from_store)
    monkeypatch.setattr(
        cli, "generate_cover_letter_on_demand", lambda *args, **kwargs: True
    )
    _patch_build_client(monkeypatch)

    assert cli.main(["generate-cover-letter", "--job-id", "42"]) == 0
    assert captured["settings_store"] is captured["store"]


def test_generate_cover_letter_returns_nonzero_when_not_delivered(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda path: settings)
    _patch_build_client(monkeypatch)
    monkeypatch.setattr(cli, "generate_cover_letter_on_demand", lambda s, job_id, **kwargs: False)

    exit_code = cli.main(["generate-cover-letter", "--job-id", "42"])

    assert exit_code == 1
