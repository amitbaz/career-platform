"""The ingestion worker commands record every invocation and surface health (#258).

Exercised through `recheck-freshness`, the worker with the fewest
dependencies. `crawl-source` and `extract-facets` go through the same
`cli._recorded_drain`.
"""

from __future__ import annotations

import pytest

from job_hunter import cli
from job_hunter import extract_facets_stage
from job_hunter import recheck_freshness_stage
from job_hunter.ai import gemini as ai_gemini
from job_hunter.ai import usage as ai_usage
from job_hunter.extract_facets_stage import ExtractFacetsDrain
from job_hunter.models import AIQuotaSettings, AIUsageSummary
from job_hunter.recheck_freshness_stage import FreshnessDrain


class _Database:
    closed = False

    def close(self):
        self.closed = True


class _Run:
    instances: list["_Run"] = []

    def __init__(self, database, worker, *, stale_after_seconds):
        self.worker = worker
        self.stale_after_seconds = stale_after_seconds
        self.events: list = []
        self.id = "run-1"
        _Run.instances.append(self)

    def start(self):
        self.events.append("start")

    def heartbeat(self, drain):
        self.events.append("heartbeat")

    def finish(self, drain):
        self.events.append(("finish", drain.stopped_because))

    def fail(self, error):
        self.events.append(("fail", str(error)))


@pytest.fixture
def worker(monkeypatch):
    _Run.instances.clear()
    database = _Database()
    health = {"healthy": True}
    monkeypatch.setattr(cli, "load_ingestion_dsn", lambda: "postgresql://unused")
    monkeypatch.setattr(cli, "IngestionDatabase", lambda dsn: database)
    monkeypatch.setattr(cli, "WorkerRun", _Run)
    monkeypatch.setattr(cli, "report_worker_health", lambda db: health["healthy"])
    return database, health


def _drain_returning(drain):
    def fake(database, http, *, limit, on_batch):
        on_batch(drain)
        return drain

    return fake


def test_an_empty_drain_is_recorded_as_a_finished_run(worker, monkeypatch):
    database, _ = worker
    monkeypatch.setattr(
        recheck_freshness_stage,
        "drain_recheck_freshness",
        _drain_returning(FreshnessDrain(stopped_because="queue_empty")),
    )

    assert cli.main(["recheck-freshness"]) == 0

    [run] = _Run.instances
    assert run.worker == "recheck_freshness"
    assert run.stale_after_seconds == recheck_freshness_stage.VISIBILITY_TIMEOUT_SECONDS
    assert run.events == ["start", "heartbeat", ("finish", "queue_empty")]
    assert database.closed


def test_a_worker_exits_non_zero_when_another_worker_is_unhealthy(
    worker, monkeypatch, caplog
):
    """Its own drain was fine, but a failed cron run is visible and a log line is not."""
    _, health = worker
    health["healthy"] = False
    monkeypatch.setattr(
        recheck_freshness_stage,
        "drain_recheck_freshness",
        _drain_returning(FreshnessDrain(stopped_because="queue_empty")),
    )

    assert cli.main(["recheck-freshness"]) == 1
    assert "not every ingestion worker is healthy" in caplog.text


def test_a_drain_that_raises_finishes_its_run_as_an_error(worker, monkeypatch):
    database, _ = worker

    def boom(database, http, *, limit, on_batch):
        raise RuntimeError("stack unreachable")

    monkeypatch.setattr(recheck_freshness_stage, "drain_recheck_freshness", boom)

    assert cli.main(["recheck-freshness"]) == 1
    [run] = _Run.instances
    assert run.events == ["start", ("fail", "stack unreachable")]
    assert database.closed


def test_extract_facets_without_a_platform_key_is_a_recorded_failed_run(
    worker, monkeypatch
):
    """The database is reachable, so the invocation is recordable: a missing
    key must leave a failed run with its reason, not an absent one."""
    database, _ = worker
    monkeypatch.setattr(cli, "load_platform_ai_settings", lambda: None)

    assert cli.main(["extract-facets"]) == 1

    [run] = _Run.instances
    assert run.worker == "extract_facets"
    assert run.events[0] == "start"
    kind, reason = run.events[1]
    assert kind == "fail" and "PLATFORM_GEMINI_API_KEY" in reason
    assert database.closed


def test_extract_facets_logs_the_platform_ai_usage_it_spent(worker, monkeypatch, caplog):
    """The engine's per-day cost report now lives on the stage that spends it
    (#189 retired the one process that used to print every ledger together)."""
    monkeypatch.setattr(
        cli,
        "load_platform_ai_settings",
        lambda: ("platform-key", AIQuotaSettings(rpm=10, tpm=250000, rpd=500), "gemini-test"),
    )
    monkeypatch.setattr(cli, "_build_client", lambda http: object())
    monkeypatch.setattr(cli, "PostgresJobStore", lambda client, ingestion=None: object())
    # `_extract_facets` re-imports these two names from their owning modules
    # at call time rather than using cli.py's module-level copies, so the
    # patch has to land on the source module, not on `cli`.
    monkeypatch.setattr(ai_gemini, "build_gemini_provider", lambda *args, **kwargs: object())

    class FakeTracker:
        def __init__(self, *args, **kwargs):
            pass

        def snapshot(self, now):
            return AIUsageSummary(
                requests_today=5,
                rpd_percent=1.0,
                rpm_peak_percent=2.0,
                tpm_peak_percent=3.0,
                input_tokens_today=100,
                output_tokens_today=50,
                thinking_tokens_today=0,
                cached_tokens_today=0,
                total_tokens_today=150,
                purpose_counts={"job_facets": 5},
                internal_budget_exhausted=False,
                provider_paused=False,
            )

    monkeypatch.setattr(ai_usage, "AIUsageTracker", FakeTracker)
    monkeypatch.setattr(
        extract_facets_stage,
        "drain_extract_facets",
        _drain_returning(ExtractFacetsDrain(stopped_because="queue_empty")),
    )

    with caplog.at_level("INFO"):
        assert cli.main(["extract-facets"]) == 0

    assert "ai_usage account=platform" in caplog.text
    assert "purposes=job_facets:5" in caplog.text
