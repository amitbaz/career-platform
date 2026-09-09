"""Ingestion's direct Postgres connection (issue #182).

The connection is a saving, not a dependency: a run that cannot open one
persists each posting inside its own job upsert and still delivers its
digest. These tests pin that property, because every part of it is a
failure path that a green run never exercises.
"""

from __future__ import annotations

import pytest

from job_hunter import cli
from job_hunter.pg import IngestionDatabase


class _WedgedPool:
    """A pool that never hands out a connection, and counts the attempts."""

    def __init__(self) -> None:
        self.attempts = 0
        self.closed = False

    def open(self) -> None:
        pass

    def connection(self):
        self.attempts += 1
        raise TimeoutError("couldn't get a connection after 30.0 sec")

    def close(self) -> None:
        self.closed = True


def _database_with(pool) -> IngestionDatabase:
    database = IngestionDatabase.__new__(IngestionDatabase)
    database._pool = pool
    database._opened = False
    database._unavailable = False
    return database


def test_an_unreachable_pool_is_waited_for_once_per_run():
    """A crawl leases at least three connections; a wedged pooler costs one wait.

    Each wait is `_POOL_TIMEOUT_SECONDS`, and the caller's fallback is the
    same every time, so re-learning the same failure is pure delay added to a
    run that is already taking the slow path.
    """
    pool = _WedgedPool()
    database = _database_with(pool)

    with pytest.raises(TimeoutError):
        with database.connection():
            pass  # pragma: no cover - the lease never opens

    for _ in range(2):
        with pytest.raises(RuntimeError, match="already found unreachable"):
            with database.connection():
                pass  # pragma: no cover - the lease never opens

    assert pool.attempts == 1


def test_a_failure_in_the_callers_own_work_does_not_condemn_the_pool():
    """The connection worked; what ran on it did not. Those are different."""

    class _Leasing:
        def __init__(self) -> None:
            self.attempts = 0

        def open(self) -> None:
            pass

        def connection(self):
            self.attempts += 1
            from contextlib import contextmanager

            @contextmanager
            def lease():
                yield object()

            return lease()

        def close(self) -> None:
            pass

    pool = _Leasing()
    database = _database_with(pool)

    with pytest.raises(ValueError):
        with database.connection():
            raise ValueError("the merge statement was rejected")

    with database.connection():
        pass

    assert pool.attempts == 2


def test_closing_a_pool_that_never_opened_is_safe():
    pool = _WedgedPool()
    database = _database_with(pool)

    database.close()

    assert pool.closed is False


def test_a_driver_that_will_not_load_costs_the_run_its_speed_not_its_digest(
    monkeypatch, caplog
):
    """`psycopg` failing to import must not be the end of the run.

    `load_ingestion_dsn` returning None already degrades cleanly; this is the
    other half -- a DSN that is set, and a pool that cannot be built.
    """
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://postgres@127.0.0.1:1/postgres")

    def explode(dsn):
        raise RuntimeError("psycopg is not installed")

    monkeypatch.setattr(cli, "IngestionDatabase", explode)

    with caplog.at_level("ERROR"):
        assert cli._build_ingestion_database() is None

    assert "could not open a direct Postgres connection" in caplog.text


def test_no_dsn_means_no_connection_and_no_complaint(monkeypatch, caplog):
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)

    with caplog.at_level("ERROR"):
        assert cli._build_ingestion_database() is None

    assert caplog.text == ""
