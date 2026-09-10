"""The cursor store and the per-run crawl recorder, without a database.

Both are best-effort by design: a cursor is an optimisation and telemetry is
telemetry, and neither may fail a crawl that already succeeded. These tests
pin that, and pin the two places where writing a plausible-looking number
would corrupt the crawl cadence.
"""

from __future__ import annotations

from job_hunter.discovery import DiscoveryStats
from job_hunter.http import Validators
from job_hunter.source_cursors import SourceCursorStore, record_run_crawls


class _Cursor:
    def __init__(self, rows=None, fail=False):
        self.rows = rows
        self.fail = fail
        self.executed = []
        self.many = []

    def execute(self, sql, params=()):
        if self.fail:
            raise RuntimeError("connection lost")
        self.executed.append((sql, params))

    def executemany(self, sql, rows):
        if self.fail:
            raise RuntimeError("connection lost")
        self.many.append((sql, rows))

    def fetchone(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Database:
    def __init__(self, cursor):
        self._cursor = cursor

    def connection(self):
        return _Connection(self._cursor)


def test_a_missing_row_reads_as_no_cursor_rather_than_raising():
    store = SourceCursorStore(_Database(_Cursor(rows=None)))
    assert store.read("remotive") == ("", Validators())


def test_a_stored_row_comes_back_as_validators():
    store = SourceCursorStore(_Database(_Cursor(rows=("https://f", '"e"', "Wed"))))
    assert store.read("remotive") == (
        "https://f",
        Validators(etag='"e"', last_modified="Wed"),
    )


def test_an_unreadable_cursor_degrades_to_a_full_crawl():
    """Losing a cursor costs one full fetch. Raising here would cost the
    whole crawl, which is the more expensive of the two."""
    store = SourceCursorStore(_Database(_Cursor(fail=True)))
    assert store.read("remotive") == ("", Validators())


def test_an_unwritable_cursor_does_not_raise():
    store = SourceCursorStore(_Database(_Cursor(fail=True)))
    store.write("remotive", "https://f", Validators(etag='"e"'))


def test_a_write_without_a_url_is_skipped():
    """No URL means no request was made through the scope, so there is
    nothing to be conditional about next time."""
    cursor = _Cursor()
    SourceCursorStore(_Database(cursor)).write("remotive", "", Validators(etag='"e"'))
    assert cursor.executed == []


def _stats():
    stats = DiscoveryStats()
    stats.source_outcomes.update(
        {"remotive": "completed", "lever:acme": "not_modified", "jobicy": "failed"}
    )
    stats.raw_by_label.update({"remotive": 120, "jobicy": 0})
    stats.new_to_corpus_by_label.update({"remotive": 7})
    stats.requests_by_source.update({"remotive": 3, "lever:acme": 1})
    stats.elapsed_by_source.update({"remotive": 1.5})
    return stats


def test_one_row_per_source_the_run_read():
    cursor = _Cursor()
    written = record_run_crawls(_Database(cursor), _stats(), {})
    assert written == 3
    assert len(cursor.many[1][1]) == 3


def test_an_unchanged_board_records_not_modified_not_an_empty_fetch():
    cursor = _Cursor()
    record_run_crawls(_Database(cursor), _stats(), {})
    rows = {row[0]: row for row in cursor.many[1][1]}
    assert rows["lever:acme"][1] == "not_modified"
    assert rows["jobicy"][1] == "failed"
    assert rows["remotive"][1] == "fetched"


def test_a_source_cut_off_by_its_budget_is_a_fetch_not_a_failure():
    """Banding it down for being large would visit it less and cut it off
    sooner -- a source that is working, punished for its size."""
    stats = DiscoveryStats()
    stats.source_outcomes["remotive"] = "cut_off"
    stats.raw_by_label["remotive"] = 40
    cursor = _Cursor()
    record_run_crawls(_Database(cursor), stats, {})
    assert cursor.many[1][1][0][1] == "fetched"


def test_changed_is_never_reported_by_the_inline_path():
    """The band sums new_to_corpus + changed. The inline pipeline runs no
    description-hash short-circuit, so reporting `fetched` as `changed` would
    read every source as maximally productive and peg the portfolio to the
    fastest band."""
    cursor = _Cursor()
    record_run_crawls(_Database(cursor), _stats(), {})
    sql = cursor.many[1][0]
    assert "changed" not in sql
    remotive = next(row for row in cursor.many[1][1] if row[0] == "remotive")
    assert remotive[2] == 120, "fetched"
    assert remotive[3] == 7, "new_to_corpus"


def test_rows_are_keyed_by_the_durable_source_key_not_the_label():
    """The scheduler bands on this key. A label is de-duplicated per run and
    would silently split one source's history in two."""
    cursor = _Cursor()
    record_run_crawls(_Database(cursor), _stats(), {"lever:acme": "lever:acme-board"})
    keys = {row[0] for row in cursor.many[1][1]}
    assert "lever:acme-board" in keys
    assert "lever:acme" not in keys


def test_a_failed_write_does_not_raise_into_the_run():
    assert record_run_crawls(_Database(_Cursor(fail=True)), _stats(), {}) == 0


def test_a_run_that_read_no_sources_writes_nothing():
    assert record_run_crawls(_Database(_Cursor()), DiscoveryStats(), {}) == 0


def test_every_source_is_registered_as_a_crawl_target():
    """The scheduler loops over job_hunter_crawl_targets. Writing yield rows
    for keys that table has never heard of means it iterates nothing and
    installs no cron entry at all."""
    cursor = _Cursor()
    record_run_crawls(_Database(cursor), _stats(), {})
    targets_sql, targets = cursor.many[0]
    assert "job_hunter_crawl_targets" in targets_sql
    assert {row[0] for row in targets} == {"remotive", "lever:acme", "jobicy"}


def test_a_run_that_measured_no_novelty_records_nothing():
    """new_to_corpus=0 means 'nothing was new'. A run whose staged batch fell
    back counted nothing, which is a different claim -- and six of them would
    walk the whole portfolio to the weekly band over a queue hiccup."""
    cursor = _Cursor()
    assert record_run_crawls(
        _Database(cursor), _stats(), {}, novelty_measured=False
    ) == 0
    assert cursor.many == []
