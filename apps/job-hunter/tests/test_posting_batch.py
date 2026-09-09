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

import os
import uuid

import pytest

from job_hunter.models import Job
from job_hunter.normalize import job_fingerprint
from job_hunter.pg import IngestionDatabase
from job_hunter.postgres_store import (
    _POSTING_STAGING_COLUMNS,
    PostgresJobStore,
    PostingBatch,
)


class RecordingClient:
    """Records every RPC and answers `job_hunter_upsert_jobs` plausibly."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def rpc(self, function, payload=None, *, retry=True):
        self.calls.append((function, payload or {}))
        if function == "job_hunter_upsert_jobs":
            return [
                {
                    "input_index": index,
                    "id": f"job-{index}",
                    "is_new": True,
                    "description_changed": False,
                }
                for index, _job in enumerate(payload["p_jobs"])
            ]
        if function == "job_hunter_upsert_job":
            return [{"id": "job-0", "is_new": True, "description_changed": False}]
        raise AssertionError(f"unexpected rpc: {function}")

    @property
    def sent_jobs(self) -> list[dict]:
        return [
            job
            for function, payload in self.calls
            if function == "job_hunter_upsert_jobs"
            for job in payload["p_jobs"]
        ]


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


def test_a_job_upsert_carries_no_posting_id_when_the_batch_resolved_none():
    client = RecordingClient()
    store = PostgresJobStore(client)

    store.upsert_logical_jobs(
        [_job()], posting_batch=store.merge_posting_batch([_job()])
    )

    assert "posting_id" not in client.sent_jobs[0]


def test_merging_an_empty_list_never_reaches_the_connection():
    class ExplodingDatabase:
        def connection(self):
            raise AssertionError("an empty batch must not open a connection")

    store = PostgresJobStore(RecordingClient(), ExplodingDatabase())

    assert store.merge_posting_batch([]) == PostingBatch()


def test_a_failed_merge_degrades_to_the_per_listing_path(caplog):
    class BrokenDatabase:
        def connection(self):
            raise RuntimeError("no route to host")

    store = PostgresJobStore(RecordingClient(), BrokenDatabase())

    with caplog.at_level("ERROR"):
        batch = store.merge_posting_batch([_job()])

    assert batch == PostingBatch()
    assert "staged posting merge failed" in caplog.text


# What the batch changes about a job payload -------------------------------------


def test_a_resolved_posting_id_travels_with_the_job_that_resolved_it():
    client = RecordingClient()
    store = PostgresJobStore(client)
    jobs = [_job("Frontend Engineer"), _job("Backend Engineer")]
    batch = PostingBatch(
        posting_ids={job_fingerprint(jobs[0]): "posting-a"}, newly_discovered=1
    )

    store.upsert_logical_jobs(jobs, posting_batch=batch)

    sent = client.sent_jobs
    assert sent[0]["posting_id"] == "posting-a"
    # The second job's advertisement was not in the batch, so it resolves its
    # own posting inside the upsert, exactly as it did before #182.
    assert "posting_id" not in sent[1]


def test_a_replayed_job_keeps_the_posting_its_batch_resolved(monkeypatch):
    client = RecordingClient()
    store = PostgresJobStore(client)
    job = _job()
    batch = PostingBatch(posting_ids={job_fingerprint(job): "posting-a"})

    def explode(self, chunk, posting_batch=None):
        raise RuntimeError("chunk failed")

    monkeypatch.setattr(PostgresJobStore, "_upsert_job_chunk", explode)

    store.upsert_logical_jobs([job], posting_batch=batch)

    replayed = [
        payload["p_job"]
        for function, payload in client.calls
        if function == "job_hunter_upsert_job"
    ]
    assert replayed[0]["posting_id"] == "posting-a"


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


# Against a real database ---------------------------------------------------------


@pytest.fixture
def ingestion_store(supabase_client):
    """A store holding both transports: PostgREST and a direct connection.

    Skips rather than fails without `SUPABASE_TEST_DB_URL`, like every other
    stack-backed fixture in this suite: the direct connection is optional in
    production too, and a developer without one still gets a green run of
    everything else.
    """
    dsn = os.environ.get("SUPABASE_TEST_DB_URL")
    if not dsn:
        pytest.skip("no SUPABASE_TEST_DB_URL; ingestion has no direct connection")
    store = PostgresJobStore(supabase_client, IngestionDatabase(dsn))
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
    assert rows[0]["posting_id"] == batch.posting_ids[job_fingerprint(jobs[0])]
