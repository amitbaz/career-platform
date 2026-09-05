import sqlite3
from pathlib import Path

from job_hunter.gmail_models import GmailSettings, GmailSyncSummary
from job_hunter.models import GeminiQuotaSettings, RunSummary, SearchPolicy, Settings
from job_hunter.store import JobStore
from job_hunter import cli


def _settings(tmp_path, **overrides):
    policy = SearchPolicy(
        target_titles=[],
        positive_keywords=[],
        blocked_title_keywords=[],
        salary_floor_eur=90000,
        thresholds={"package": 75, "possible": 65},
    )
    defaults = dict(
        gemini_api_key="key",
        candidate_profile="profile",
        cover_letter_template="template",
        timezone="Europe/Berlin",
        scheduled_hour=9,
        policy=policy,
        gemini_quota=GeminiQuotaSettings(rpm=10, tpm=250000, rpd=500),
        dry_run=True,
        db_path=str(tmp_path / "var" / "state.sqlite3"),
    )
    defaults.update(overrides)
    return Settings(**defaults)


def test_run_scheduled_skips_outside_target_hour(monkeypatch, tmp_path, caplog):
    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda path: settings)
    monkeypatch.setattr(cli, "should_run_scheduled", lambda now, tz, hour: False)

    called = []
    monkeypatch.setattr(cli, "run_pipeline", lambda s, **kwargs: called.append(s) or RunSummary())

    with caplog.at_level("INFO"):
        exit_code = cli.main(["run", "--scheduled", "--config", "config/search.yml"])

    assert exit_code == 0
    assert called == []
    assert any("skipped" in record.message.lower() for record in caplog.records)


def test_run_scheduled_proceeds_at_target_hour(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda path: settings)
    monkeypatch.setattr(cli, "should_run_scheduled", lambda now, tz, hour: True)

    called = []
    monkeypatch.setattr(cli, "run_pipeline", lambda s, **kwargs: called.append(s) or RunSummary(ready_to_apply=1))

    exit_code = cli.main(["run", "--scheduled"])

    assert exit_code == 0
    assert called == [settings]


def test_run_manual_always_proceeds_without_time_guard(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda path: settings)

    def _boom(*args, **kwargs):
        raise AssertionError("should_run_scheduled must not be called for manual runs")

    monkeypatch.setattr(cli, "should_run_scheduled", _boom)

    called = []
    monkeypatch.setattr(cli, "run_pipeline", lambda s, **kwargs: called.append(s) or RunSummary())

    exit_code = cli.main(["run"])

    assert exit_code == 0
    assert called == [settings]


def test_run_creates_db_and_output_parent_directories(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda path: settings)
    monkeypatch.setattr(cli, "run_pipeline", lambda s, **kwargs: RunSummary())

    assert not Path(settings.db_path).parent.exists()

    cli.main(["run"])

    assert Path(settings.db_path).parent.exists()
    assert (Path(settings.db_path).parent / "cover_letters").exists()


def test_run_unhandled_exception_returns_nonzero(monkeypatch, tmp_path):
    def _raise(path):
        raise RuntimeError("config broke")

    monkeypatch.setattr(cli, "load_settings", _raise)

    exit_code = cli.main(["run"])

    assert exit_code == 1


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


def _gmail_settings(tmp_path):
    return GmailSettings(
        client_id="client",
        client_secret="secret",
        refresh_token="refresh",
        gemini_api_key="gemini",
        gemini_quota=GeminiQuotaSettings(rpm=10, tpm=250000, rpd=500),
        db_path=str(tmp_path / "gmail.sqlite3"),
    )


def _patch_gmail_sync_dependencies(monkeypatch, tmp_path, run):
    class SyncService:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def sync(self, now, *, dry_run, force_backfill):
            return run(now=now, dry_run=dry_run, force_backfill=force_backfill)

    monkeypatch.setattr(cli, "load_gmail_settings", lambda: _gmail_settings(tmp_path), raising=False)
    monkeypatch.setattr(cli, "HttpClient", object, raising=False)
    monkeypatch.setattr(cli, "GoogleOAuthTokenProvider", lambda settings: object(), raising=False)
    monkeypatch.setattr(cli, "GmailClient", lambda http, token_provider: object(), raising=False)
    monkeypatch.setattr(cli, "GeminiClient", lambda api_key, model, http, tracker=None: object(), raising=False)
    monkeypatch.setattr(cli, "JobStore", lambda path: object(), raising=False)
    monkeypatch.setattr(cli, "GmailSyncService", SyncService, raising=False)


def test_sync_gmail_does_not_load_candidate_profile_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda path: (_ for _ in ()).throw(AssertionError("must not load candidate profile settings")),
    )
    _patch_gmail_sync_dependencies(
        monkeypatch, tmp_path, lambda **kwargs: GmailSyncSummary()
    )

    assert cli.main(["sync-gmail"]) == 0


def test_sync_gmail_returns_nonzero_on_fatal_auth_error(monkeypatch, tmp_path):
    _patch_gmail_sync_dependencies(
        monkeypatch,
        tmp_path,
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("Gmail authorization failed")),
    )

    assert cli.main(["sync-gmail"]) == 1


def test_sync_gmail_returns_zero_when_service_completes_with_message_errors(
    monkeypatch, tmp_path, caplog
):
    _patch_gmail_sync_dependencies(
        monkeypatch, tmp_path, lambda **kwargs: GmailSyncSummary(errors=2)
    )

    assert cli.main(["sync-gmail"]) == 0
    assert any("will retry" in record.message for record in caplog.records)


def test_sync_gmail_dry_run_does_not_create_missing_database_path(
    monkeypatch, tmp_path
):
    settings = _gmail_settings(tmp_path)
    settings = GmailSettings(
        client_id=settings.client_id,
        client_secret=settings.client_secret,
        refresh_token=settings.refresh_token,
        gemini_api_key=settings.gemini_api_key,
        gemini_quota=settings.gemini_quota,
        db_path=str(tmp_path / "missing" / "state.sqlite3"),
    )

    class InspectingService:
        def __init__(self, *, store, **kwargs):
            self.store = store

        def sync(self, now, *, dry_run, force_backfill):
            assert dry_run is True
            self.store.save_gmail_sync_state(
                account_id="dry-run@example.com",
                history_id="100",
                last_successful_sync_at=now.isoformat(),
                backfill_completed_at=now.isoformat(),
            )
            return GmailSyncSummary()

    monkeypatch.setattr(cli, "load_gmail_settings", lambda: settings)
    monkeypatch.setattr(cli, "HttpClient", object)
    monkeypatch.setattr(cli, "GoogleOAuthTokenProvider", lambda value: object())
    monkeypatch.setattr(cli, "GmailClient", lambda http, token_provider: object())
    monkeypatch.setattr(cli, "GeminiClient", lambda api_key, model, http, tracker=None: object())
    monkeypatch.setattr(cli, "GmailSyncService", InspectingService)

    assert cli.main(["sync-gmail", "--dry-run"]) == 0
    assert not Path(settings.db_path).parent.exists()
    assert not Path(settings.db_path).exists()


def test_sync_gmail_dry_run_opens_existing_database_read_only(
    monkeypatch, tmp_path
):
    settings = _gmail_settings(tmp_path)
    store = JobStore(settings.db_path)
    store.save_gmail_sync_state(
        account_id="candidate@example.com",
        history_id="before",
        last_successful_sync_at="2026-08-30T12:00:00+00:00",
        backfill_completed_at="2026-08-30T12:00:00+00:00",
    )
    store.close()

    with sqlite3.connect(settings.db_path) as connection:
        schema_before = connection.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        rows_before = connection.execute(
            "SELECT * FROM gmail_sync_state ORDER BY account_id"
        ).fetchall()

    class InspectingService:
        write_was_blocked = False

        def __init__(self, *, store, **kwargs):
            self.store = store

        def sync(self, now, *, dry_run, force_backfill):
            assert dry_run is True
            try:
                self.store.save_gmail_sync_state(
                    account_id="candidate@example.com",
                    history_id="after",
                    last_successful_sync_at=now.isoformat(),
                    backfill_completed_at=now.isoformat(),
                )
            except sqlite3.OperationalError:
                type(self).write_was_blocked = True
            return GmailSyncSummary()

    monkeypatch.setattr(cli, "load_gmail_settings", lambda: settings)
    monkeypatch.setattr(cli, "HttpClient", object)
    monkeypatch.setattr(cli, "GoogleOAuthTokenProvider", lambda value: object())
    monkeypatch.setattr(cli, "GmailClient", lambda http, token_provider: object())
    monkeypatch.setattr(cli, "GeminiClient", lambda api_key, model, http, tracker=None: object())
    monkeypatch.setattr(cli, "GmailSyncService", InspectingService)

    assert cli.main(["sync-gmail", "--dry-run"]) == 0
    assert InspectingService.write_was_blocked is True
    with sqlite3.connect(settings.db_path) as connection:
        assert connection.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall() == schema_before
        assert connection.execute(
            "SELECT * FROM gmail_sync_state ORDER BY account_id"
        ).fetchall() == rows_before


def test_sync_gmail_dry_run_reads_legacy_database_without_gmail_schema(
    monkeypatch, tmp_path
):
    settings = _gmail_settings(tmp_path)
    with sqlite3.connect(settings.db_path) as connection:
        connection.execute(
            """
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL
            )
            """
        )
        schema_before = connection.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()

    class InspectingService:
        def __init__(self, *, store, **kwargs):
            self.store = store

        def sync(self, now, *, dry_run, force_backfill):
            assert dry_run is True
            assert self.store.get_gmail_sync_state("candidate@example.com") is None
            return GmailSyncSummary()

    monkeypatch.setattr(cli, "load_gmail_settings", lambda: settings)
    monkeypatch.setattr(cli, "HttpClient", object)
    monkeypatch.setattr(cli, "GoogleOAuthTokenProvider", lambda value: object())
    monkeypatch.setattr(cli, "GmailClient", lambda http, token_provider: object())
    monkeypatch.setattr(cli, "GeminiClient", lambda api_key, model, http, tracker=None: object())
    monkeypatch.setattr(cli, "GmailSyncService", InspectingService)

    assert cli.main(["sync-gmail", "--dry-run"]) == 0
    with sqlite3.connect(settings.db_path) as connection:
        assert connection.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall() == schema_before


class _CapturingTracker:
    instances = []

    def __init__(self, store, quota, model, *, run_id=None):
        self.store = store
        self.quota = quota
        self.model = model
        self.run_id = run_id
        type(self).instances.append(self)


class _CapturingGemini:
    instances = []

    def __init__(self, api_key, model, http, tracker=None):
        self.api_key = api_key
        self.model = model
        self.tracker = tracker
        type(self).instances.append(self)


def test_run_constructs_one_tracked_gemini_client_sharing_run_id(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda path: settings)
    monkeypatch.setattr(cli, "run_pipeline", lambda s, **kwargs: RunSummary())
    monkeypatch.setattr(cli, "GeminiUsageTracker", _CapturingTracker)
    monkeypatch.setattr(cli, "GeminiClient", _CapturingGemini)
    monkeypatch.setenv("GEMINI_RUN_ID", "run-123")
    _CapturingTracker.instances.clear()
    _CapturingGemini.instances.clear()

    assert cli.main(["run"]) == 0

    assert len(_CapturingTracker.instances) == 1
    assert len(_CapturingGemini.instances) == 1
    tracker = _CapturingTracker.instances[0]
    gemini = _CapturingGemini.instances[0]
    assert tracker.run_id == "run-123"
    assert tracker.model == settings.gemini_model
    assert tracker.quota == settings.gemini_quota
    assert gemini.tracker is tracker


def test_sync_gmail_constructs_one_tracked_gemini_client_sharing_run_id(monkeypatch, tmp_path):
    settings = _gmail_settings(tmp_path)

    class SyncService:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def sync(self, now, *, dry_run, force_backfill):
            return GmailSyncSummary()

    monkeypatch.setattr(cli, "load_gmail_settings", lambda: settings)
    monkeypatch.setattr(cli, "HttpClient", object)
    monkeypatch.setattr(cli, "GoogleOAuthTokenProvider", lambda value: object())
    monkeypatch.setattr(cli, "GmailClient", lambda http, token_provider: object())
    monkeypatch.setattr(cli, "GeminiUsageTracker", _CapturingTracker)
    monkeypatch.setattr(cli, "GeminiClient", _CapturingGemini)
    monkeypatch.setattr(cli, "GmailSyncService", SyncService)
    monkeypatch.setenv("GEMINI_RUN_ID", "run-123")
    _CapturingTracker.instances.clear()
    _CapturingGemini.instances.clear()

    assert cli.main(["sync-gmail"]) == 0

    assert len(_CapturingTracker.instances) == 1
    assert len(_CapturingGemini.instances) == 1
    tracker = _CapturingTracker.instances[0]
    gemini = _CapturingGemini.instances[0]
    assert tracker.run_id == "run-123"
    assert tracker.model == settings.gemini_model
    assert tracker.quota == settings.gemini_quota
    assert gemini.tracker is tracker


def test_sync_gmail_dry_run_tracks_gemini_usage_without_touching_readonly_store(
    monkeypatch, tmp_path
):
    """--dry-run still makes real Gemini calls, so the tracker must stay active --

    but it must record against its own ephemeral ledger, never the read-only
    handle onto the real database.
    """
    settings = _gmail_settings(tmp_path)
    store = JobStore(settings.db_path)
    store.close()

    class RecordingService:
        def __init__(self, *, gemini, **kwargs):
            self.gemini = gemini

        def sync(self, now, *, dry_run, force_backfill):
            assert dry_run is True
            # Exercise a real preflight + record cycle through the real,
            # non-mocked GeminiClient/GeminiUsageTracker constructed by cli.py.
            self.gemini._tracker.preflight("job_evaluation", "prompt", now)
            self.gemini._tracker.record_success("job_evaluation", "prompt", now)
            return GmailSyncSummary()

    monkeypatch.setattr(cli, "load_gmail_settings", lambda: settings)
    monkeypatch.setattr(cli, "HttpClient", object)
    monkeypatch.setattr(cli, "GoogleOAuthTokenProvider", lambda value: object())
    monkeypatch.setattr(cli, "GmailClient", lambda http, token_provider: object())
    monkeypatch.setattr(cli, "GmailSyncService", RecordingService)

    assert cli.main(["sync-gmail", "--dry-run"]) == 0

    with sqlite3.connect(settings.db_path) as connection:
        rows = connection.execute("SELECT * FROM gemini_usage").fetchall()
    assert rows == []


def test_parser_accepts_generate_cover_letter_job_id():
    args = cli.build_parser().parse_args(["generate-cover-letter", "--job-id", "7"])
    assert args.command == "generate-cover-letter"
    assert args.job_id == 7
    assert args.config == "config/search.yml"


def test_generate_cover_letter_delegates_with_job_id(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda path: settings)

    calls = []
    monkeypatch.setattr(
        cli,
        "generate_cover_letter_on_demand",
        lambda s, job_id, **kwargs: calls.append((s, job_id)) or True,
    )

    exit_code = cli.main(["generate-cover-letter", "--job-id", "42"])

    assert exit_code == 0
    assert calls == [(settings, 42)]


def test_generate_cover_letter_returns_nonzero_when_not_delivered(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda path: settings)
    monkeypatch.setattr(cli, "generate_cover_letter_on_demand", lambda s, job_id, **kwargs: False)

    exit_code = cli.main(["generate-cover-letter", "--job-id", "42"])

    assert exit_code == 1
