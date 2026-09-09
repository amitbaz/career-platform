"""Posting-level facts are read from the posting, not from the job row (#177, #178).

Every one of these runs against the local Supabase stack through the
`store` fixture, and every one works the same way: write a job through the
real RPC (so a posting exists and the job row points at it), then move the
posting's copy of a fact and check that the reader followed it.

#177 could drive two copies of a fact apart, because the job row still had
one. #178 removed the job row's copy, so what these prove now is that the
reader consults the posting at all and that nothing else is left to consult.

Fingerprints are unique per test. Postings are global and have no delete
policy, so a fixed fingerprint would leave a row behind that the next run
-- or a suite running at the same time under a different seed user --
would resolve to and read someone else's description out of.
"""

from __future__ import annotations

import uuid

import pytest

from job_hunter.models import Evaluation, Job
from job_hunter.supabase_client import SupabaseRequestError


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


def _overwrite_posting(store, posting_id: str, values: dict) -> None:
    """Drive the posting and the job row apart, as ingestion could.

    Over the privileged connection since #179: a posting is not writable by a
    user at all, so a fixture that wrote one through PostgREST would be
    testing a path that no longer exists. Nothing about what these tests
    assert changes -- they are about which of the two rows the readers
    follow.
    """
    assignments = ", ".join(f"{column} = %s" for column in values)
    store._shared_write(
        f"update public.job_hunter_postings set {assignments} where id = %s::uuid",
        (*values.values(), posting_id),
    )


# get_job ---------------------------------------------------------------------


def test_get_job_reads_the_advertisement_from_the_posting(store):
    fingerprint = f"posting-read-{uuid.uuid4()}"
    job_id, _, _ = store.upsert_job(_make_job(fingerprint, description="the shared description"))
    _overwrite_posting(
        store,
        _posting_id(store, job_id),
        {
            "company": "Moved Co",
            "title": "Moved Title",
            "location": "Moved City",
            "description": "the moved description",
        },
    )

    job = store.get_job(job_id)

    assert job.company == "Moved Co"
    assert job.title == "Moved Title"
    assert job.location == "Moved City"
    assert job.description == "the moved description"


def test_get_job_reads_the_url_from_the_posting(store):
    """The link is the advertisement's since #178.

    #177 kept `url` on the job row because a merged row was the only row that
    had seen every posting behind it. #176 made merging a posting-level
    decision, so the surviving posting carries the resolved link and every
    user reads the same one.
    """
    fingerprint = f"posting-url-{uuid.uuid4()}"
    job_id, _, _ = store.upsert_job(_make_job(fingerprint, description="desc"))
    _overwrite_posting(
        store, _posting_id(store, job_id), {"url": "https://resolved.example/careers/1"}
    )

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


def test_a_job_row_without_a_posting_cannot_be_written(store):
    """The fallback #177 needed is gone, because the case it covered is gone.

    A direct insert used to be able to make a job row with no posting, which
    is why `get_job` fell back to the row's own copy of the advertisement.
    #178 made `posting_id` `not null`: there is no copy to fall back to and no
    row to fall back for.
    """
    with pytest.raises(SupabaseRequestError):
        store._client.insert(
            "job_hunter_jobs",
            [
                {
                    "user_id": store._client.user_id,
                    "first_seen_at": "2026-09-09T10:00:00+00:00",
                    "last_seen_at": "2026-09-09T10:00:00+00:00",
                }
            ],
        )


# Re-evaluation ---------------------------------------------------------------


def test_needs_evaluation_is_current_against_the_posting(store):
    """An evaluation stamped from the posting stays current while it does.

    There is no second copy of the description state left to disagree with it
    (#178): both readers consult the posting and nothing else.
    """
    fingerprint = f"posting-eval-stale-{uuid.uuid4()}"
    job_id, _, _ = store.upsert_job(_make_job(fingerprint, description="the shared description"))
    store.save_evaluation(job_id, _evaluation(job_id))

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
