"""Posting-level facts are read from the posting, not from the job row (#177).

Every one of these runs against the local Supabase stack through the
`store` fixture, and every one works the same way: write a job through the
real RPC (so a posting exists and the job row points at it), then drive the
two copies of a posting-level fact apart by writing directly to one table.
A test that only ever saw the two agree could not tell which one was read.

Fingerprints are unique per test. Postings are global and have no delete
policy, so a fixed fingerprint would leave a row behind that the next run
-- or a suite running at the same time under a different seed user --
would resolve to and read someone else's description out of.
"""

from __future__ import annotations

import uuid

from job_hunter.models import Evaluation, Job


def _make_job(fingerprint: str, *, description: str, content_confidence: str = "") -> Job:
    return Job(
        source="test",
        source_job_id=fingerprint,
        url=f"https://example.test/{fingerprint}",
        company="Acme",
        title="Engineer",
        location="Remote",
        remote=True,
        description=description,
        content_confidence=content_confidence,
    )


def _evaluation(job_id: str) -> Evaluation:
    return Evaluation(
        job_id=job_id,
        total_score=0,
        scores={},
        decision="pass",
        hard_blockers=[],
        strengths=[],
        gaps=[],
        salary_note="",
        location_note="",
        rationale="",
        model="test",
        status="ok",
    )


def _posting_id(store, job_id: str) -> str:
    row = store._client.select(
        "job_hunter_jobs", params={"id": f"eq.{job_id}", "select": "posting_id"}
    )[0]
    assert row["posting_id"] is not None, "the upsert RPC must point every job row at a posting"
    return row["posting_id"]


def _overwrite_job_row(store, job_id: str, values: dict) -> None:
    """Make the job row's duplicated copy disagree with the posting.

    Nothing in the application writes a job row this way -- the point is to
    prove which of the two copies the reader under test actually consults.
    """
    store._client.update("job_hunter_jobs", values, params={"id": f"eq.{job_id}"})


def _overwrite_posting(store, posting_id: str, values: dict) -> None:
    store._client.update("job_hunter_postings", values, params={"id": f"eq.{posting_id}"})


# get_job ---------------------------------------------------------------------


def test_get_job_reads_the_advertisement_from_the_posting(store):
    fingerprint = f"posting-read-{uuid.uuid4()}"
    job_id, _, _ = store.upsert_job(_make_job(fingerprint, description="the shared description"))
    _overwrite_job_row(
        store,
        job_id,
        {
            "company": "Stale Co",
            "title": "Stale Title",
            "location": "Stale City",
            "description": "stale description",
        },
    )

    job = store.get_job(job_id)

    assert job.company == "Acme"
    assert job.title == "Engineer"
    assert job.location == "Remote"
    assert job.description == "the shared description"


def test_get_job_keeps_the_merged_rows_url(store):
    """The URL is resolved across every posting the row merges, so it stays.

    `posting_id` names one of them, and reading its URL would put an
    aggregator link in the digest where the employer's own is known.
    """
    fingerprint = f"posting-url-{uuid.uuid4()}"
    job_id, _, _ = store.upsert_job(_make_job(fingerprint, description="desc"))
    _overwrite_job_row(store, job_id, {"url": "https://resolved.example/careers/1"})

    assert store.get_job(job_id).url == "https://resolved.example/careers/1"


def test_get_job_still_takes_the_market_from_the_membership_row(store):
    fingerprint = f"posting-market-{uuid.uuid4()}"
    job_id, _, _ = store.upsert_job(_make_job(fingerprint, description="desc"))
    store.set_job_market(job_id, "eu")

    assert store.get_job(job_id).market_id == "eu"


def test_get_job_makes_one_request(store, monkeypatch):
    """The posting arrives as a PostgREST embed, not as a second round trip."""
    fingerprint = f"posting-trips-{uuid.uuid4()}"
    job_id, _, _ = store.upsert_job(_make_job(fingerprint, description="desc"))
    calls: list[str] = []
    original = store._client.select

    def counting_select(table, **kwargs):
        calls.append(table)
        return original(table, **kwargs)

    monkeypatch.setattr(store._client, "select", counting_select)

    store.get_job(job_id)

    assert calls == ["job_hunter_jobs"]


def test_get_job_falls_back_to_a_job_row_that_has_no_posting(store):
    """A direct insert bypasses the RPC, so `posting_id` can be null."""
    inserted = store._client.insert(
        "job_hunter_jobs",
        [
            {
                "user_id": store._client.user_id,
                "fingerprint": f"posting-none-{uuid.uuid4()}",
                "source": "test",
                "title": "Unpointed Engineer",
                "company": "Unpointed Co",
                "url": "https://unpointed.example/1",
                "description": "unpointed description",
                "first_seen_at": "2026-09-09T10:00:00+00:00",
                "last_seen_at": "2026-09-09T10:00:00+00:00",
            }
        ],
    )[0]

    job = store.get_job(inserted["id"])

    assert job.title == "Unpointed Engineer"
    assert job.company == "Unpointed Co"
    assert job.description == "unpointed description"


# Re-evaluation ---------------------------------------------------------------


def test_needs_evaluation_ignores_a_stale_hash_on_the_job_row(store):
    fingerprint = f"posting-eval-stale-{uuid.uuid4()}"
    job_id, _, _ = store.upsert_job(_make_job(fingerprint, description="the shared description"))
    store.save_evaluation(job_id, _evaluation(job_id))

    _overwrite_job_row(store, job_id, {"description_hash": "stale", "content_confidence": "stale"})

    assert store.needs_evaluation(job_id) is False
    assert store.needs_evaluation_bulk([job_id])[job_id] is False


def test_needs_evaluation_follows_the_postings_description_hash(store):
    fingerprint = f"posting-eval-changed-{uuid.uuid4()}"
    job_id, _, _ = store.upsert_job(_make_job(fingerprint, description="the shared description"))
    store.save_evaluation(job_id, _evaluation(job_id))

    _overwrite_posting(store, _posting_id(store, job_id), {"description_hash": "moved on"})

    assert store.needs_evaluation(job_id) is True
    assert store.needs_evaluation_bulk([job_id])[job_id] is True


def test_needs_evaluation_follows_the_postings_content_confidence(store):
    fingerprint = f"posting-eval-conf-{uuid.uuid4()}"
    job_id, _, _ = store.upsert_job(
        _make_job(fingerprint, description="desc", content_confidence="aggregator_text")
    )
    store.save_evaluation(job_id, _evaluation(job_id))

    _overwrite_posting(store, _posting_id(store, job_id), {"content_confidence": "official_ats"})

    assert store.needs_evaluation(job_id) is True
    assert store.needs_evaluation_bulk([job_id])[job_id] is True


def test_save_evaluation_stamps_the_postings_hash(store):
    """What was evaluated is the posting's text, so that is what is recorded."""
    fingerprint = f"posting-eval-stamp-{uuid.uuid4()}"
    job_id, _, _ = store.upsert_job(_make_job(fingerprint, description="the shared description"))
    _overwrite_job_row(store, job_id, {"description_hash": "stale", "content_confidence": "stale"})

    store.save_evaluation(job_id, _evaluation(job_id))

    posting = store._client.select(
        "job_hunter_postings",
        params={
            "id": f"eq.{_posting_id(store, job_id)}",
            "select": "description_hash,content_confidence",
        },
    )[0]
    stamped = store._client.select(
        "job_hunter_evaluations",
        params={
            "job_id": f"eq.{job_id}",
            "select": "description_hash_at_eval,content_confidence_at_eval",
        },
    )[0]
    assert stamped["description_hash_at_eval"] == posting["description_hash"]
    assert stamped["content_confidence_at_eval"] == posting["content_confidence"]
