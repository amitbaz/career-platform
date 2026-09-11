from __future__ import annotations

import uuid

import pytest

from job_hunter.crawl_source import CrawlSourceStage, description_hash
from job_hunter.models import Job
from job_hunter.stage_queue import PermanentStageFailure, QueueMessage, Stage


class _FakeCursor:
    def __init__(self, database):
        self._database = database

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._database.executed.append((sql, params))
        if "job_hunter_source_cursors" in sql and sql.strip().startswith("select"):
            self._rows = [(self._database.etag, "", None)]
        elif "description_hash" in sql:
            self._rows = list(self._database.known_hashes)
        else:
            self._rows = []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConnection:
    def __init__(self, database):
        self._database = database

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return _FakeCursor(self._database)


class _FakeDatabase:
    def __init__(self, *, etag="", known_hashes=()):
        self.etag = etag
        self.known_hashes = known_hashes
        self.executed: list = []

    def connection(self):
        return _FakeConnection(self)


def _job(source: str, source_job_id: str, description: str) -> Job:
    return Job(
        source=source,
        source_job_id=source_job_id,
        title="Engineer",
        company="Acme",
        location="Remote",
        url=f"https://example.test/{source_job_id}",
        description=description,
    )


class _StubSource:
    source_label = "remotive"
    source_key = "remotive"

    def __init__(self, jobs, *, raises=None):
        self._jobs = jobs
        self._raises = raises

    def discover(self):
        if self._raises is not None:
            raise self._raises
        yield from self._jobs


def _message(source_key="remotive") -> QueueMessage:
    return QueueMessage(
        stage=Stage.CRAWL_SOURCE, message_id=1, payload={"crawl_key": source_key}
    )


def test_a_listing_whose_hash_is_unchanged_never_reaches_persistence():
    """Criterion 3. The hash check must happen before staging, not inside the upsert."""
    unchanged = _job("remotive", "1", "same words")
    fresh = _job("remotive", "2", "new words")
    # Ask the stage what fingerprint it will compute rather than recomputing
    # one here. conftest's `_postings_unique_to_this_test` salts the
    # fingerprint per test and patches it on every module that computes one,
    # `crawl_source` included -- so a second, independently-derived
    # fingerprint would be the unsalted value and would never match.
    from job_hunter import crawl_source as _stage

    database = _FakeDatabase(
        known_hashes=[
            (_stage.job_fingerprint(unchanged), description_hash("same words"))
        ]
    )
    persisted: list[list[Job]] = []

    def _persist(jobs):
        persisted.append(jobs)
        return None

    stage = CrawlSourceStage(
        database,
        build_source=lambda key: _StubSource([unchanged, fresh]),
        persist=_persist,
    )
    outcome = stage(_message())

    assert [job.source_job_id for job in persisted[0]] == ["2"]
    assert outcome.unchanged_by_hash == 1
    assert outcome.fetched == 2


def test_the_stage_registers_its_crawl_target_on_first_crawl():
    """job_hunter_reschedule_sources loops job_hunter_crawl_targets, keyed by
    the fine string build_source() answers to -- not job_hunter_sources,
    which is keyed by the coarse posting source and has no row for an ATS
    adapter's fine key. Nothing registers that row except this stage."""
    database = _FakeDatabase()
    stage = CrawlSourceStage(
        database, build_source=lambda key: _StubSource([]), persist=lambda jobs: None
    )
    stage(_message())

    inserts = [
        (sql, params)
        for sql, params in database.executed
        if "job_hunter_crawl_targets" in sql
    ]
    assert len(inserts) == 1
    sql, params = inserts[0]
    assert "on conflict" in sql.lower()
    assert params == ("remotive",)


def test_registering_the_crawl_target_is_idempotent_on_the_second_crawl():
    """The second crawl of the same source must not fail or duplicate the row."""
    database = _FakeDatabase()
    stage = CrawlSourceStage(
        database, build_source=lambda key: _StubSource([]), persist=lambda jobs: None
    )
    stage(_message())
    stage(_message())

    inserts = [
        (sql, params)
        for sql, params in database.executed
        if "job_hunter_crawl_targets" in sql
    ]
    assert len(inserts) == 2, "each crawl attempts the on-conflict-do-nothing insert"
    assert all(params == ("remotive",) for _, params in inserts)


def test_an_unchanged_board_records_not_modified_rather_than_an_empty_fetch():
    """Criterion 2, and the charter rule that an empty result carries its reason."""
    from job_hunter.http import NOT_MODIFIED

    seen: list = []
    database = _FakeDatabase(etag='"abc"')
    stage = CrawlSourceStage(
        database,
        build_source=lambda key: _StubSource([]),
        persist=lambda jobs: seen.append(jobs),
        probe=lambda source, validators: NOT_MODIFIED,
    )
    outcome = stage(_message())

    assert outcome.outcome == "not_modified"
    assert outcome.fetched == 0
    assert seen == [], "a 304 must cost no downstream work at all"


def test_a_zero_job_fetch_is_not_reported_as_not_modified():
    """A board that answered in full and had nothing is a different fact."""
    database = _FakeDatabase()
    stage = CrawlSourceStage(
        database, build_source=lambda key: _StubSource([]), persist=lambda jobs: None
    )
    outcome = stage(_message())
    assert outcome.outcome == "fetched"
    assert outcome.fetched == 0


def test_new_to_corpus_comes_back_from_the_persist_call():
    """The scheduler bands on novelty, so the count must survive the round trip."""
    from job_hunter.resolve_persist import PostingBatch

    fresh = _job("remotive", "2", "new words")
    database = _FakeDatabase()
    stage = CrawlSourceStage(
        database,
        build_source=lambda key: _StubSource([fresh]),
        persist=lambda jobs: PostingBatch(
            posting_ids={"fp": "id"}, newly_discovered=1
        ),
    )
    outcome = stage(_message())
    assert outcome.new_to_corpus == 1
    assert outcome.changed == 1


def test_new_to_corpus_does_not_track_changed():
    """The distinction the scheduler bands on, pinned so a regression fails.

    A source re-advertising jobs the corpus already holds, with edited
    descriptions, is *changed* but not *new*. Deriving `new_to_corpus`
    from `len(fresh)` would earn such a source a faster cadence forever
    while it adds nothing.
    """
    from job_hunter.resolve_persist import PostingBatch

    edited = _job("remotive", "3", "same job, reworded description")
    database = _FakeDatabase()
    stage = CrawlSourceStage(
        database,
        build_source=lambda key: _StubSource([edited]),
        persist=lambda jobs: PostingBatch(posting_ids={"fp": "id"}, newly_discovered=0),
    )
    outcome = stage(_message())

    assert outcome.changed == 1, "the listing did reach persistence"
    assert outcome.new_to_corpus == 0, (
        "the corpus already held it -- banding on this must not reward a "
        "source that only reworded what we already have"
    )


def test_joined_variant_group_comes_back_from_the_persist_call():
    """issue #61: the batch's variant-group count survives the round trip,
    the same way new_to_corpus does."""
    from job_hunter.resolve_persist import PostingBatch

    fresh = _job("remotive", "4", "new words")
    database = _FakeDatabase()
    stage = CrawlSourceStage(
        database,
        build_source=lambda key: _StubSource([fresh]),
        persist=lambda jobs: PostingBatch(
            posting_ids={"fp": "id"}, newly_discovered=1, joined_existing_group=3
        ),
    )
    outcome = stage(_message())
    assert outcome.joined_variant_group == 3


def test_joined_variant_group_is_zero_and_still_recorded_when_nothing_joined():
    """AGENTS.md rule 5: an empty result must carry its reason, including a
    variant-group count of exactly zero."""
    database = _FakeDatabase()
    stage = CrawlSourceStage(
        database, build_source=lambda key: _StubSource([]), persist=lambda jobs: None
    )
    outcome = stage(_message())

    assert outcome.joined_variant_group == 0
    inserts = [
        (sql, params)
        for sql, params in database.executed
        if "job_hunter_source_crawls" in sql
    ]
    assert len(inserts) == 1
    sql, params = inserts[0]
    assert "joined_variant_group" in sql
    assert params[-1] == 0


def test_a_rate_limited_source_records_rate_limited_and_does_not_raise():
    """Criterion 5. This source stalls; nothing else may be affected."""
    import requests

    response = requests.Response()
    response.status_code = 429
    error = requests.HTTPError(response=response)

    database = _FakeDatabase()
    stage = CrawlSourceStage(
        database,
        build_source=lambda key: _StubSource([], raises=error),
        persist=lambda jobs: None,
    )
    outcome = stage(_message())

    assert outcome.outcome == "rate_limited"


def test_a_failing_source_records_failed():
    database = _FakeDatabase()
    stage = CrawlSourceStage(
        database,
        build_source=lambda key: _StubSource([], raises=RuntimeError("boom")),
        persist=lambda jobs: None,
    )
    assert stage(_message()).outcome == "failed"


def test_the_wrong_stage_is_a_permanent_failure():
    database = _FakeDatabase()
    stage = CrawlSourceStage(
        database, build_source=lambda key: _StubSource([]), persist=lambda jobs: None
    )
    wrong = QueueMessage(
        stage=Stage.RESOLVE_PERSIST, message_id=1, payload={"source_key": "remotive"}
    )
    with pytest.raises(PermanentStageFailure):
        stage(wrong)


def test_an_unexpected_payload_key_is_a_permanent_failure():
    database = _FakeDatabase()
    stage = CrawlSourceStage(
        database, build_source=lambda key: _StubSource([]), persist=lambda jobs: None
    )
    message = QueueMessage(
        stage=Stage.CRAWL_SOURCE,
        message_id=1,
        payload={"crawl_key": "remotive", "user_id": "someone"},
    )
    with pytest.raises(PermanentStageFailure):
        stage(message)


def test_the_crawl_row_carries_what_the_crawl_cost():
    """`requests` is the cost half of the yield figure; always-zero is a lie."""

    class _CountingHttp:
        request_count = 0

    http = _CountingHttp()

    class _RequestingSource:
        source_label = "remotive"
        source_key = "remotive"

        def discover(self):
            http.request_count += 3
            yield from ()

    database = _FakeDatabase()
    stage = CrawlSourceStage(
        database,
        build_source=lambda key: _RequestingSource(),
        persist=lambda jobs: None,
        http=http,
    )
    assert stage(_message()).requests == 3


def test_description_hash_matches_the_sql_definition():
    """job_hunter_upsert_posting computes sha256 over the UTF-8 description."""
    assert description_hash("hello") == (
        "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )


@pytest.mark.integration
def test_the_stage_reads_and_writes_the_real_tables(store):
    """The three queries the fakes above cannot check.

    Guards the gap #179 exposed: a fixture that withholds the ingestion
    connection tests a configuration nobody deploys.
    """
    from job_hunter.normalize import job_fingerprint

    assert store._ingestion is not None, (
        "the store fixture must supply an ingestion connection; a fake here "
        "would test a configuration nobody runs"
    )

    # Use store.upsert_job rather than the `seed_postings` fixture #179 added.
    # seed_postings inserts a posting row directly over the privileged
    # connection, which leaves `description_hash` at its '' default --
    # the hash is computed inside `job_hunter_upsert_posting`. This test is
    # about the hash short-circuit, so it needs the path that populates it.

    crawl_key = f"remotive:test-{uuid.uuid4()}"
    existing = _job("remotive", "int-1", "unchanged body")
    store.upsert_job(existing)

    stage = CrawlSourceStage(
        store._ingestion,
        build_source=lambda key: _StubSource([existing, _job("remotive", "int-2", "new body")]),
        persist=lambda jobs: store.merge_posting_batch(jobs),
    )
    outcome = stage(_message(crawl_key))

    # The hash short-circuit resolved against a row that is really there.
    assert outcome.unchanged_by_hash == 1
    assert outcome.fetched == 2

    with store._ingestion.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "select outcome, fetched, unchanged_by_hash "
                "from public.job_hunter_source_crawls where source_key = %s",
                (crawl_key,),
            )
            rows = cursor.fetchall()

    assert rows == [("fetched", 2, 1)], "the crawl row is written, not just logged"


@pytest.mark.integration
def test_a_crawl_that_produced_nothing_still_leaves_a_row(store):
    """An empty result must carry its reason, in the table and not only in a log."""
    crawl_key = f"arbeitnow:test-{uuid.uuid4()}"
    stage = CrawlSourceStage(
        store._ingestion,
        build_source=lambda key: _StubSource([], raises=RuntimeError("upstream down")),
        persist=lambda jobs: None,
    )
    stage(_message(crawl_key))

    with store._ingestion.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "select outcome, error from public.job_hunter_source_crawls "
                "where source_key = %s",
                (crawl_key,),
            )
            row = cursor.fetchone()

    assert row[0] == "failed"
    assert "upstream down" in row[1]


@pytest.mark.integration
def test_the_stage_registers_the_target_row_idempotently_against_the_real_table(store):
    """The insert this stage issues really lands, and a second crawl of the
    same fine key does not error or duplicate the row -- which is what makes
    first_seen_at mean what its name says."""
    stage = CrawlSourceStage(
        store._ingestion,
        build_source=lambda key: _StubSource([]),
        persist=lambda jobs: None,
    )
    crawl_key = f"greenhouse:int-registers-{uuid.uuid4()}"
    stage(_message(crawl_key))
    stage(_message(crawl_key))

    with store._ingestion.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "select count(*) from public.job_hunter_crawl_targets "
                "where crawl_key = %s",
                (crawl_key,),
            )
            row = cursor.fetchone()

    assert row[0] == 1, "one row, no matter how many times this key is crawled"


def test_a_closed_posting_listed_again_is_not_short_circuited(store, ingestion_database):
    """Issue #186. A crawl is what reopens a posting a re-check closed, so an
    unchanged re-listing of a closed posting has to reach the merge: dropping
    it on its unchanged hash would leave it closed while its board lists it."""
    import uuid

    job = _job("remotive", f"reopen-{uuid.uuid4()}", "same words")
    job_id, _, _ = store.upsert_job(job)
    with ingestion_database.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "update public.job_hunter_postings "
                "   set closed_at = now(), closed_reason = 'http_404' "
                " where id = (select posting_id from public.job_hunter_jobs where id = %s)",
                (job_id,),
            )
    persisted: list[list[Job]] = []

    stage = CrawlSourceStage(
        ingestion_database,
        build_source=lambda key: _StubSource([job]),
        persist=persisted.append,
    )
    outcome = stage(_message())

    assert [listed.source_job_id for listed in persisted[0]] == [job.source_job_id]
    assert outcome.unchanged_by_hash == 0
