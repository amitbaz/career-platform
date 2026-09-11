"""The ingestion worker commands record every invocation and surface health (#258).

Exercised through `recheck-freshness`, the worker with the fewest
dependencies. `crawl-source` and `extract-facets` go through the same
`cli._recorded_drain`.
"""

from __future__ import annotations

import pytest

from job_hunter import cli
from job_hunter import recheck_freshness_stage
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
