"""Worker-run telemetry and health for the ingestion workers (issue #258)."""

from __future__ import annotations

import dataclasses
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from job_hunter.models import Job
from job_hunter.postgres_stage_queue import PostgresStageQueue
from job_hunter.stage_queue import QueueDelays, QueueMessage, Stage
from job_hunter.worker_runs import (
    WORKERS,
    WorkerRun,
    read_worker_health,
    report_worker_health,
)


@dataclasses.dataclass
class _Drain:
    """The shape every stage drain reports, without a queue behind it."""

    claimed: int = 0
    outcomes: Counter = dataclasses.field(default_factory=Counter)
    stopped_because: str = ""
    queue_delays: QueueDelays = dataclasses.field(default_factory=QueueDelays)


class _Cursor:
    def __init__(self, database):
        self._database = database
        self._rows: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if self._database.fail:
            raise RuntimeError("database unreachable")
        self._database.executed.append((sql, params))
        if "returning id" in sql:
            self._rows = [("run-1",)]
        elif "job_hunter_worker_health" in sql:
            self._rows = list(self._database.health)
        else:
            self._rows = []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class _Connection:
    def __init__(self, database):
        self._database = database

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return _Cursor(self._database)


class _Database:
    def __init__(self, *, fail=False, health=()):
        self.fail = fail
        self.health = health
        self.executed: list = []

    def connection(self):
        return _Connection(self)


def _updates(database):
    return [(sql, params) for sql, params in database.executed if sql.startswith("update")]


# Queue delay -----------------------------------------------------------------


def test_queue_delay_runs_from_enqueue_to_claim():
    enqueued = datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc)
    delays = QueueDelays()
    for seconds in (2, 5):
        delays.observe(
            QueueMessage(Stage.CRAWL_SOURCE, seconds, {}, enqueued_at=enqueued),
            enqueued + timedelta(seconds=seconds),
        )
    assert (delays.total_ms, delays.max_ms) == (7000, 5000)


def test_a_message_with_no_enqueue_time_is_not_measured_as_zero():
    """Zero is a measurement: counting an unknown delay as zero would drag the mean down."""
    delays = QueueDelays()
    delays.observe(QueueMessage(Stage.CRAWL_SOURCE, 1, {}), datetime.now(timezone.utc))
    assert (delays.total_ms, delays.max_ms) == (0, 0)


# The run row -----------------------------------------------------------------


def test_a_run_is_recorded_before_its_drain_starts():
    database = _Database()
    run = WorkerRun(database, "crawl_source", stale_after_seconds=900)
    run.start()
    sql, params = database.executed[0]
    assert sql.startswith("insert into public.job_hunter_worker_runs")
    assert params == ("crawl_source", 900)
    assert run.id == "run-1"


def test_every_heartbeat_records_the_progress_so_far():
    database = _Database()
    run = WorkerRun(database, "extract_facets", stale_after_seconds=300)
    run.start()
    drain = _Drain(claimed=3, outcomes=Counter(extracted=3))
    drain.queue_delays.total_ms, drain.queue_delays.max_ms = 900, 400
    run.heartbeat(drain)
    sql, params = _updates(database)[-1]
    assert "heartbeat_at = now()" in sql
    assert params == (3, '{"extracted": 3}', 900, 400, "run-1")


def test_finishing_records_why_the_drain_stopped():
    database = _Database()
    run = WorkerRun(database, "crawl_source", stale_after_seconds=900)
    run.start()
    run.finish(_Drain(stopped_because="queue_empty"))
    sql, params = _updates(database)[-1]
    assert "finished_at = now()" in sql
    assert "queue_empty" in params


def test_a_drain_that_stopped_for_no_known_reason_is_recorded_as_an_error():
    database = _Database()
    run = WorkerRun(database, "crawl_source", stale_after_seconds=900)
    run.start()
    run.finish(_Drain(stopped_because=""))
    _, params = _updates(database)[-1]
    assert "error" in params
    assert any("without a known reason" in str(value) for value in params)


def test_telemetry_that_cannot_be_written_never_costs_the_drain_its_work():
    """A run that was never recorded still surfaces: health reports its worker missing."""
    run = WorkerRun(_Database(fail=True), "recheck_freshness", stale_after_seconds=900)
    run.start()
    run.heartbeat(_Drain())
    run.finish(_Drain(stopped_because="limit"))
    run.fail(RuntimeError("boom"))
    assert run.id is None


def test_an_unknown_worker_is_refused():
    with pytest.raises(ValueError):
        WorkerRun(_Database(), "crawl-source", stale_after_seconds=900)


# Health ----------------------------------------------------------------------


def test_health_is_false_when_any_worker_is_unhealthy(caplog):
    database = _Database(
        health=[
            ("crawl_source", "ok", ""),
            ("extract_facets", "missing", "last run started 2026-09-11 08:00"),
            ("recheck_freshness", "ok", ""),
        ]
    )
    assert report_worker_health(database) is False
    assert "worker=extract_facets status=missing" in caplog.text


def test_health_is_true_only_when_every_worker_is_ok():
    database = _Database(health=[(worker, "ok", "") for worker in WORKERS])
    assert report_worker_health(database) is True


def test_health_that_cannot_be_read_is_not_reported_as_healthy():
    assert report_worker_health(_Database(fail=True)) is False


def test_no_scheduled_workers_is_not_reported_as_healthy():
    assert report_worker_health(_Database(health=[])) is False


# What the evidence can and cannot report ---------------------------------------


def test_no_source_publication_time_is_captured_yet():
    """job_hunter_crawl_window_evidence reports publication-to-first-seen delay as
    null, with the reason `no_trusted_source_timestamp`, because no adapter
    captures a publication time. When Job gains one, this fails: report the
    delay for the sources whose timestamp can be trusted, instead of leaving
    the column null while the data exists."""
    names = {field.name for field in dataclasses.fields(Job)}
    assert not {name for name in names if "publish" in name or "posted" in name}


# Against the real tables -------------------------------------------------------


def _root() -> Path:
    return Path(__file__).resolve().parents[3]


def _interval_seconds(cron: str) -> int:
    minute, hour, day, month, weekday = cron.split()
    if (day, month, weekday) == ("*", "*", "*"):
        if minute.startswith("*/") and hour == "*":
            return int(minute[2:]) * 60
        if minute.isdigit() and hour.startswith("*/"):
            return int(hour[2:]) * 60 * 60
        if minute.isdigit() and hour == "*":
            return 60 * 60
        if minute.isdigit() and hour.isdigit():
            return 24 * 60 * 60
    raise AssertionError(
        f"no fixed interval can be read from {cron!r}; teach this test the new "
        "shape rather than dropping the worker from the comparison"
    )


def test_worker_schedules_match_render_yaml(ingestion_database, _stack_env):
    """Health judges a worker missing against job_hunter_worker_schedules, and
    Render runs it on render.yaml. If the two drift, a healthy worker is
    reported missing or a missing one is reported healthy."""
    blueprint = yaml.safe_load((_root() / "render.yaml").read_text(encoding="utf-8"))
    expected = {}
    for service in blueprint["services"]:
        if service.get("type") != "cron":
            continue
        command = service["startCommand"].split()
        worker = command[command.index("job_hunter") + 1].replace("-", "_")
        expected[worker] = _interval_seconds(service["schedule"])
    assert set(expected) == set(WORKERS)

    with ingestion_database.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "select worker, expected_interval_seconds "
                "from public.job_hunter_worker_schedules"
            )
            recorded = dict(cursor.fetchall())
    assert recorded == expected


def _empty_runs(database, worker: str) -> int:
    with database.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "select coalesce(sum(empty_runs), 0) "
                "from public.job_hunter_worker_run_evidence() where worker = %s",
                (worker,),
            )
            return int(cursor.fetchone()[0])


def test_an_empty_invocation_is_a_finished_run_and_counts_as_evidence(
    ingestion_database, _stack_env
):
    """The criterion this ticket exists for: a worker that woke to an empty
    queue leaves a durable row, and that row reaches the evidence."""
    before = _empty_runs(ingestion_database, "extract_facets")
    run = WorkerRun(ingestion_database, "extract_facets", stale_after_seconds=300)
    run.start()
    drain = _Drain(stopped_because="queue_empty")
    run.heartbeat(drain)
    run.finish(drain)

    with ingestion_database.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "select worker, claimed, stop_reason, finished_at is not null, "
                "outcomes, stale_after_seconds "
                "from public.job_hunter_worker_runs where id = %s",
                (run.id,),
            )
            row = cursor.fetchone()
    assert row == ("extract_facets", 0, "queue_empty", True, {}, 300)
    assert _empty_runs(ingestion_database, "extract_facets") >= before + 1


def test_a_drain_that_raised_is_finished_as_an_error(ingestion_database, _stack_env):
    run = WorkerRun(ingestion_database, "recheck_freshness", stale_after_seconds=900)
    run.start()
    run.fail(RuntimeError("stack unreachable"))

    with ingestion_database.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "select stop_reason, error, finished_at is not null "
                "from public.job_hunter_worker_runs where id = %s",
                (run.id,),
            )
            row = cursor.fetchone()
    assert row == ("error", "RuntimeError: stack unreachable", True)


def test_health_reads_one_row_per_scheduled_worker(ingestion_database, _stack_env):
    health = read_worker_health(ingestion_database)
    assert sorted(row.worker for row in health) == sorted(WORKERS)
    assert {row.status for row in health} <= {"ok", "missing", "unfinished"}


def test_a_claimed_message_carries_when_it_was_enqueued(
    ingestion_database, _clean_isolated_stage_queues
):
    queue = PostgresStageQueue(ingestion_database, _clean_isolated_stage_queues)
    queue.enqueue(Stage.CRAWL_SOURCE, {"crawl_key": "remotive"})
    [message] = queue.claim(
        Stage.CRAWL_SOURCE, visibility_timeout_seconds=30, batch_size=1
    )
    try:
        assert message.enqueued_at is not None
        assert message.enqueued_at.tzinfo is not None
        assert abs(datetime.now(timezone.utc) - message.enqueued_at) < timedelta(minutes=5)
    finally:
        queue.complete(message)
