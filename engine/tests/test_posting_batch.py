"""Persisting a crawl batch with one set-based merge (issue #182).

The unit tests here are about what the store sends and what it does when it
has no direct Postgres connection. The behaviour of the merge itself -- which
posting a batch resolves to, which description wins, what a re-merge does --
is SQL, and is asserted in `supabase/tests/pgtap/job_hunter_posting_batches.sql`
against a real database. The integration tests at the bottom of this file
assert the one thing neither of those can: that the two paths, batched and
per-listing, reach the same postings for the same input.
"""

from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager

import pytest

from engine import postgres_store
from engine.models import Job
from engine.pg import IngestionDatabase
from engine.postgres_store import (
    _POSTING_STAGING_COLUMNS,
    PostgresJobStore,
    PostingBatch,
)
from engine.resolve_persist import ResolvePersistStage
from engine.stage_queue import QueueMessage, Stage


def job_fingerprint(job: Job) -> str:
    """The fingerprint the store will actually compute for this job.

    Resolved through the module rather than imported from `normalize`,
    because conftest's `_postings_unique_to_this_test` salts
    `postgres_store.job_fingerprint` per test (#175): postings are shared and
    cannot be cleaned between tests, so each test gets fingerprints of its
    own. A test that imported the unsalted function would be asking the store
    about a posting it never wrote.
    """
    return postgres_store.job_fingerprint(job)


class RecordingClient:
    """Records every PostgREST call this store makes.

    Since #179 the job upsert is not one of them: it writes a posting, which
    is a shared row, so it goes over the privileged connection instead. This
    fake therefore refuses every RPC, which is the assertion -- a job payload
    that reaches PostgREST is a payload on the wrong transport.
    """

    user_id = "11111111-1111-1111-1111-111111111111"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def rpc(self, function, payload=None, *, retry=True):
        self.calls.append((function, payload or {}))
        raise AssertionError(
            f"{function} reached PostgREST; shared-table writes go over the "
            "direct connection since #179"
        )

    def select(self, table, params=None):
        return []


def _job(title: str = "Frontend Engineer", **overrides) -> Job:
    fields = {
        "source": "test",
        "source_job_id": f"sj-{title}",
        "title": title,
        "company": "Acme",
        "location": "Remote",
        "url": f"https://example.test/{title.replace(' ', '-')}",
        "description": f"description for {title}",
        "content_confidence": "source_detail_page",
    }
    fields.update(overrides)
    return Job(**fields)


# Without a direct connection ---------------------------------------------------


def test_merge_posting_batch_is_empty_without_a_direct_connection():
    store = PostgresJobStore(RecordingClient())

    batch = store.merge_posting_batch([_job()])

    assert batch == PostingBatch()
    assert batch.posting_ids == {}
    assert batch.newly_discovered == 0


def test_a_job_upsert_is_refused_outright_without_a_direct_connection():
    """Since #179 there is no per-listing fallback left to degrade to.

    A job upsert writes `job_hunter_postings`, which no user may write, so a
    store with no privileged connection cannot persist a listing at all. The
    pipeline is what keeps this from happening in a real run -- it skips
    ingestion entirely when `can_write_shared_rows` is false -- and this is
    what makes forgetting to ask fail loudly rather than half-writing.
    """
    store = PostgresJobStore(RecordingClient())

    assert store.can_write_shared_rows is False
    with pytest.raises(postgres_store.SharedWriteUnavailable):
        store.upsert_logical_job(_job())


def test_a_job_upsert_carries_no_posting_id_when_the_batch_resolved_none():
    database = QueueRecordingDatabase()
    store = PostgresJobStore(RecordingClient(), database)

    store.upsert_logical_jobs([_job()], posting_batch=PostingBatch())

    assert "posting_id" not in database.job_payloads[0]


def test_merging_an_empty_list_never_reaches_the_connection():
    class ExplodingDatabase:
        unavailable = False

        def connection(self):
            raise AssertionError("an empty batch must not open a connection")

    store = PostgresJobStore(RecordingClient(), ExplodingDatabase())

    assert store.merge_posting_batch([]) == PostingBatch()


def test_a_failed_merge_degrades_to_the_per_listing_path(caplog):
    class BrokenDatabase:
        unavailable = False

        def connection(self):
            raise RuntimeError("no route to host")

    store = PostgresJobStore(RecordingClient(), BrokenDatabase())

    with caplog.at_level("ERROR"):
        batch = store.merge_posting_batch([_job()])

    assert batch == PostingBatch()
    assert "staged resolve_persist queue failed" in caplog.text


# What the batch changes about a job payload -------------------------------------


def test_a_resolved_posting_id_travels_with_the_job_that_resolved_it():
    database = QueueRecordingDatabase()
    store = PostgresJobStore(RecordingClient(), database)
    jobs = [_job("Frontend Engineer"), _job("Backend Engineer")]
    batch = PostingBatch(
        posting_ids={job_fingerprint(jobs[0]): "posting-a"}, newly_discovered=1
    )

    store.upsert_logical_jobs(jobs, posting_batch=batch)

    sent = database.job_payloads
    assert sent[0]["posting_id"] == "posting-a"
    # The second job's advertisement was not in the batch, so it resolves its
    # own posting inside the upsert, exactly as it did before #182.
    assert "posting_id" not in sent[1]


def test_every_job_upsert_names_this_store_s_own_user():
    """The user is an argument now, so it is worth asserting which one.

    `job_hunter_upsert_job` no longer reads `auth.uid()` -- it writes shared
    rows and runs as the privileged role, which has no user identity (#179).
    Nothing but this argument stops one user's crawl writing another user's
    membership rows, so a store must always pass its own.
    """
    client = RecordingClient()
    database = QueueRecordingDatabase()
    store = PostgresJobStore(client, database)

    store.upsert_logical_jobs([_job("Frontend Engineer")])
    store.upsert_logical_job(_job("Backend Engineer"))
    store.upsert_job(_job("Platform Engineer"))

    assert database.upsert_users == [client.user_id] * 3


def test_a_replayed_job_keeps_the_posting_its_batch_resolved(monkeypatch):
    database = QueueRecordingDatabase()
    store = PostgresJobStore(RecordingClient(), database)
    job = _job()
    batch = PostingBatch(posting_ids={job_fingerprint(job): "posting-a"})

    def explode(self, chunk, posting_batch=None):
        raise RuntimeError("chunk failed")

    monkeypatch.setattr(PostgresJobStore, "_upsert_job_chunk", explode)

    store.upsert_logical_jobs([job], posting_batch=batch)

    assert database.job_payloads[0]["posting_id"] == "posting-a"


def test_a_staging_row_says_the_same_thing_the_job_payload_says():
    job = _job(
        remote=True,
        canonical_url="https://boards.greenhouse.io/acme/jobs/1",
        ats_provider="greenhouse",
        ats_board="acme",
        ats_job_id="1",
    )
    row = dict(
        zip(_POSTING_STAGING_COLUMNS, PostgresJobStore._staging_row("batch", 3, job))
    )
    payload = PostgresJobStore._job_payload(job)

    assert row["batch_id"] == "batch"
    assert row["ordinal"] == 3
    shared = set(_POSTING_STAGING_COLUMNS) & set(payload)
    assert shared  # the two must overlap, or this asserts nothing
    for key in shared:
        assert row[key] == payload[key], key


class QueueRecordingCopy:
    def __init__(self, database):
        self._database = database

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def write_row(self, row):
        self._database.batch_id = row[0]


class QueueRecordingCursor:
    def __init__(self, database):
        self._database = database
        self._rows = []
        self._row = None
        # psycopg leaves this None for a statement with no result set, and
        # `_shared_write` reads it to decide whether there is anything to
        # fetch. A tuple stands in for the column descriptions it would carry.
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def copy(self, statement):
        self._database.statements.append(statement)
        return QueueRecordingCopy(self._database)

    def execute(self, statement, params=None):
        self._database.statements.append(statement)
        self._row = None
        self._rows = []
        self.description = ("result",)
        if "job_hunter_upsert_jobs" in statement:
            payloads = json.loads(params[0])
            self._database.job_payloads.extend(payloads)
            self._database.upsert_users.append(params[1])
            self._rows = [
                (index, f"job-{index}", True, False)
                for index, _payload in enumerate(payloads)
            ]
        elif "job_hunter_upsert_job(" in statement:
            payload = json.loads(params[0])
            self._database.job_payloads.append(payload)
            self._database.upsert_users.append(params[1])
            self._rows = [("job-0", True, False)]
        elif "pgmq.send" in statement:
            self._row = (7,)
        elif "pgmq.read" in statement:
            # msg_id, payload, attempt count, enqueued_at, claimed_at -- the
            # claim's columns.
            self._rows = [(7, {"batch_id": self._database.batch_id}, 0, None, None)]
        elif "job_hunter_merge_posting_batch" in statement:
            self._rows = [(self._database.fingerprint, "posting-a", True)]
        elif "job_hunter_job_facets" in statement:
            # The _enqueue_needing_facets select: every posting id passed in
            # is treated as missing current facets, for a fake simple enough
            # to make "does merge_posting_batch enqueue extraction" testable
            # without a real database.
            self._rows = [(posting_id,) for posting_id in params[0]]
        elif "job_hunter_stage_queue_metrics" in statement:
            self._rows = [
                (stage, 0, 0, 0)
                for stage in (
                    "crawl_source",
                    "extract_facets",
                    "recheck_freshness",
                    "resolve_persist",
                )
            ]

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class QueueRecordingConnection:
    def __init__(self, database):
        self._database = database

    def cursor(self):
        return QueueRecordingCursor(self._database)


class QueueRecordingDatabase:
    #: `IngestionDatabase` latches this when a lease fails, and
    #: `can_write_shared_rows` reads it. A fake is always reachable.
    unavailable = False

    def __init__(self, fingerprint=None):
        self.fingerprint = fingerprint
        self.batch_id = None
        self.statements: list[str] = []
        #: The job payloads that reached `job_hunter_upsert_job(s)`, in order,
        #: and the user id each call named. Since #179 that argument is the
        #: only thing telling the database whose membership row to write.
        self.job_payloads: list[dict] = []
        self.upsert_users: list[str] = []

    @contextmanager
    def connection(self):
        yield QueueRecordingConnection(self)


def test_resolve_persist_is_reached_as_a_queue_consumer():
    job = _job()
    database = QueueRecordingDatabase(job_fingerprint(job))
    store = PostgresJobStore(RecordingClient(), database)

    batch = store.merge_posting_batch([job])

    assert batch == PostingBatch(
        posting_ids={job_fingerprint(job): "posting-a"},
        newly_discovered=1,
        new_fingerprints=frozenset({job_fingerprint(job)}),
    )
    assert any("pgmq.send" in statement for statement in database.statements)
    assert any("pgmq.read" in statement for statement in database.statements)
    assert any("pgmq.delete" in statement for statement in database.statements)


def test_merge_posting_batch_enqueues_extraction_for_what_it_resolved():
    """crawl_source's persist path never calls upsert_logical_jobs (that
    writes job_hunter_jobs, a per-user table this user-free stage must not
    touch), so merge_posting_batch itself is the only place a Render
    crawl-only deployment can enqueue extraction for what it just merged."""
    job = _job()
    database = QueueRecordingDatabase(job_fingerprint(job))
    store = PostgresJobStore(RecordingClient(), database)

    store.merge_posting_batch([job])

    sends = [s for s in database.statements if "pgmq.send" in s]
    assert len(sends) == 2, "one send for resolve_persist, one for extract_facets"


# Variant grouping (issue #61) -----------------------------------------------------
#
# resolve_persist calls job_hunter_assign_variant_groups right after the merge
# resolves, over the same connection, and reports how many of the batch's
# postings joined a group that already existed. These fakes are deliberately
# separate from QueueRecordingCursor above: that fake's "job_hunter_merge_
# posting_batch" branch is shared by many unrelated tests whose exact-equality
# assertions on PostingBatch would break if it started returning nonzero
# variant-group rows by default.


class _VariantGroupCursor:
    def __init__(self, merge_rows, group_rows):
        self._merge_rows = merge_rows
        self._group_rows = group_rows
        self.description = None
        self._last_statement = ""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, statement, params=None):
        self._last_statement = statement
        self.description = ("result",)

    def fetchall(self):
        if "job_hunter_merge_posting_batch" in self._last_statement:
            return self._merge_rows
        if "job_hunter_assign_variant_groups" in self._last_statement:
            return self._group_rows
        return []


class _VariantGroupConnection:
    def __init__(self, merge_rows, group_rows):
        self._merge_rows = merge_rows
        self._group_rows = group_rows

    def cursor(self):
        return _VariantGroupCursor(self._merge_rows, self._group_rows)


class _VariantGroupDatabase:
    unavailable = False

    def __init__(self, merge_rows, group_rows):
        self._merge_rows = merge_rows
        self._group_rows = group_rows

    @contextmanager
    def connection(self):
        yield _VariantGroupConnection(self._merge_rows, self._group_rows)


def test_resolve_persist_counts_postings_that_joined_an_existing_variant_group():
    merge_rows = [
        ("fp-a", "posting-a", True),
        ("fp-b", "posting-b", True),
        ("fp-c", "posting-c", True),
    ]
    group_rows = [
        ("posting-a", "posting-a", False),  # founded its own group
        ("posting-b", "posting-a", True),  # joined posting-a's group
        ("posting-c", None, False),  # no ATS board: left ungrouped
    ]
    database = _VariantGroupDatabase(merge_rows, group_rows)
    stage = ResolvePersistStage(database)

    batch = stage(
        QueueMessage(
            stage=Stage.RESOLVE_PERSIST,
            message_id=1,
            payload={"batch_id": str(uuid.uuid4())},
        )
    )

    assert batch.joined_existing_group == 1


def test_resolve_persist_reports_zero_when_nothing_joined_a_group():
    merge_rows = [("fp-a", "posting-a", True)]
    group_rows = [("posting-a", "posting-a", False)]
    database = _VariantGroupDatabase(merge_rows, group_rows)
    stage = ResolvePersistStage(database)

    batch = stage(
        QueueMessage(
            stage=Stage.RESOLVE_PERSIST,
            message_id=1,
            payload={"batch_id": str(uuid.uuid4())},
        )
    )

    assert batch.joined_existing_group == 0


# Against a real database ---------------------------------------------------------


@pytest.fixture
def ingestion_store(supabase_client, _clean_isolated_stage_queues):
    """A store holding both transports: PostgREST and a direct connection.

    Skips rather than fails without `SUPABASE_TEST_DB_URL`, like every other
    stack-backed fixture in this suite: the direct connection is optional in
    production too, and a developer without one still gets a green run of
    everything else.
    """
    dsn = os.environ.get("SUPABASE_TEST_DB_URL")
    if not dsn:
        pytest.skip("no SUPABASE_TEST_DB_URL; ingestion has no direct connection")
    store = PostgresJobStore(
        supabase_client,
        IngestionDatabase(dsn),
        stage_queue_names=_clean_isolated_stage_queues,
    )
    try:
        yield store
    finally:
        store.close()


def _unique(prefix: str) -> str:
    """A source job id nothing else on the shared local stack will collide with.

    Postings have no user column and no delete policy -- a posting is shared,
    so no single user may remove one -- which means rows written here outlive
    the test that wrote them. Every fingerprint below is therefore made unique
    per run rather than cleaned up afterwards.
    """
    return f"{prefix}-{uuid.uuid4()}"


def test_a_staged_batch_resolves_every_listing_to_a_posting(ingestion_store):
    jobs = [
        _job("Frontend Engineer", source_job_id=_unique("batch")),
        _job("Backend Engineer", source_job_id=_unique("batch")),
    ]

    batch = ingestion_store.merge_posting_batch(jobs)

    assert batch.newly_discovered == 2
    assert set(batch.posting_ids) == {job_fingerprint(job) for job in jobs}
    assert len(set(batch.posting_ids.values())) == 2


def test_duplicate_listings_in_one_batch_collapse_to_one_posting(ingestion_store):
    source_job_id = _unique("dup")
    jobs = [
        _job("Frontend Engineer", source_job_id=source_job_id),
        _job("Frontend Engineer", source_job_id=source_job_id, description="again"),
    ]

    batch = ingestion_store.merge_posting_batch(jobs)

    assert batch.newly_discovered == 1
    assert len(batch.posting_ids) == 1


def test_re_merging_an_already_merged_batch_discovers_nothing(ingestion_store):
    jobs = [_job("Frontend Engineer", source_job_id=_unique("remerge"))]

    first = ingestion_store.merge_posting_batch(jobs)
    second = ingestion_store.merge_posting_batch(jobs)

    assert first.newly_discovered == 1
    assert second.newly_discovered == 0
    assert second.posting_ids == first.posting_ids


def test_the_batched_path_reaches_the_posting_the_per_listing_path_reaches(
    ingestion_store, supabase_client
):
    """The acceptance criterion, stated as a test.

    One listing goes through `job_hunter_upsert_job`'s own posting resolution;
    an identical listing goes through the staged batch merge. The two postings
    must agree about everything that describes the advertisement.
    """
    columns = (
        "source,source_job_id,url,canonical_url,company,title,location,remote,"
        "description,description_hash,content_confidence,ats_provider,ats_board,"
        "ats_job_id"
    )
    shared = {
        "title": "Staff Engineer",
        "company": "Globex",
        "location": "Berlin",
        "remote": False,
        "description": "the posting's own words",
        "content_confidence": "official_ats",
        "ats_provider": "greenhouse",
        "ats_board": "globex",
        "ats_job_id": "42",
        "url": "https://boards.greenhouse.io/globex/jobs/42",
    }
    per_listing = _job(source_job_id=_unique("loop"), **shared)
    batched = _job(source_job_id=_unique("merge"), **shared)

    ingestion_store.upsert_logical_job(per_listing)
    ingestion_store.merge_posting_batch([batched])

    def posting(job):
        rows = supabase_client.select(
            "job_hunter_postings",
            params={"fingerprint": f"eq.{job_fingerprint(job)}", "select": columns},
        )
        assert len(rows) == 1
        return rows[0]

    left, right = posting(per_listing), posting(batched)
    # source_job_id is the one column that must differ: it is what made the
    # two fingerprints distinct in the first place.
    left.pop("source_job_id")
    right.pop("source_job_id")
    assert left == right


def test_a_batched_job_upsert_points_at_the_posting_the_merge_resolved(
    ingestion_store, supabase_client
):
    jobs = [_job("Frontend Engineer", source_job_id=_unique("pointer"))]

    batch = ingestion_store.merge_posting_batch(jobs)
    results = ingestion_store.upsert_logical_jobs(jobs, posting_batch=batch)

    job_id = results[0][0]
    rows = supabase_client.select(
        "job_hunter_jobs", params={"id": f"eq.{job_id}", "select": "posting_id"}
    )
    # Through the redirect, because the batch's posting is not necessarily
    # still a survivor by the time the membership row is written. Since #178
    # the job upsert resolves identity against the whole corpus of postings --
    # canonical URL, ATS triple, normalized company/title/location -- and
    # merges what it finds, so a listing whose identity matches a posting
    # somebody else already holds ends up on that survivor. What must hold is
    # that the row points at whatever the batch's posting resolves to, which
    # is the same advertisement either way.
    resolved = supabase_client.rpc(
        "job_hunter_resolve_posting",
        {"p_posting_id": batch.posting_ids[job_fingerprint(jobs[0])]},
    )
    assert rows[0]["posting_id"] == resolved[0]


# Which transport carried the write (#179) -----------------------------------------
#
# The acceptance criterion these serve is deliberately narrow: a test that
# passes because CI happens to supply a DB URL, while never asserting which
# connection carried the write, does not cover the ticket. These name the
# transport.


def test_no_shared_table_write_reaches_postgrest():
    """Every write to a table with no user goes over the direct connection.

    `RecordingClient.rpc` raises, and the client is handed no write methods it
    would answer, so anything that reached PostgREST here would fail loudly
    rather than pass quietly. The assertions are on the SQL the privileged
    connection actually saw.
    """
    database = QueueRecordingDatabase()
    store = PostgresJobStore(RecordingClient(), database)

    store.upsert_logical_job(_job())

    assert any(
        "job_hunter_upsert_job(" in statement for statement in database.statements
    )
    assert database.job_payloads, "the payload never reached the direct connection"


def test_a_store_without_the_connection_says_so_rather_than_writing():
    """The degraded mode is a question a caller can ask, not an exception to catch.

    `run_pipeline` asks this before it crawls or enriches, which is what keeps
    a deployment with no SUPABASE_DB_URL from spending a crawl's worth of
    network on rows the database will refuse.
    """
    with_connection = PostgresJobStore(RecordingClient(), QueueRecordingDatabase())
    without = PostgresJobStore(RecordingClient())

    assert with_connection.can_write_shared_rows is True
    assert without.can_write_shared_rows is False
