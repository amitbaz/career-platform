"""Tests for `PostgresJobStore.release_legacy_blank_linkedin_jobs`.

There was no test file for this behavior before issue #70 task 12 --
`gmail_linkedin_cleanup.py`'s original free function reached `store._conn`
directly and was untested at the store layer. These exercise the ported
method against the local Supabase stack.
"""

from __future__ import annotations

import uuid

from job_hunter.gmail_models import ExtractedJob
from job_hunter.models import Evaluation, Job


def _record_job_alert(store, message_id: str) -> None:
    store.record_gmail_message(
        message_id=message_id,
        thread_id=None,
        sender="jobs-noreply@linkedin.com",
        subject="New jobs for you",
        occurred_at="2026-08-01T00:00:00+00:00",
        classification="JOB_ALERT",
        confidence=0.9,
        rationale="",
    )


def _stage_linkedin_candidate(
    store, message_id: str, candidate_key: str, *, company: str = "", title: str = ""
) -> None:
    store.stage_inbound_job(
        message_id,
        candidate_key,
        ExtractedJob(
            source_platform="linkedin",
            source_job_id=candidate_key,
            company=company,
            title=title,
            url=f"https://linkedin.example/{candidate_key}",
        ),
    )


def _blank_linkedin_job(store, candidate_key: str, *, title: str = "") -> str:
    job_id, _, _ = store.upsert_job(
        Job(
            source="gmail:linkedin",
            source_job_id=candidate_key,
            company="",
            title=title,
            url=f"https://linkedin.example/{candidate_key}",
        )
    )
    return job_id


def test_releases_blank_candidate_job_and_message_together(store, supabase_client):
    _record_job_alert(store, "m1")
    _stage_linkedin_candidate(store, "m1", "cand1")
    job_id = _blank_linkedin_job(store, "cand1")

    assert store.release_legacy_blank_linkedin_jobs() == 1

    assert store.get_job(job_id) is None
    assert (
        supabase_client.select(
            "job_hunter_inbound_job_candidates",
            params={"source_message_id": "eq.m1", "select": "id"},
        )
        == []
    )
    assert (
        supabase_client.select(
            "job_hunter_gmail_messages",
            params={"message_id": "eq.m1", "select": "id"},
        )
        == []
    )


def test_releases_poisoned_sign_in_title_job(store):
    """A blank company with the historical `Sign in` scrape title is also safe."""
    _record_job_alert(store, "m1")
    _stage_linkedin_candidate(store, "m1", "cand1")
    job_id = _blank_linkedin_job(store, "cand1", title="Sign in")

    assert store.release_legacy_blank_linkedin_jobs() == 1
    assert store.get_job(job_id) is None


def test_a_populated_sibling_candidate_blocks_the_whole_message(store):
    """One real candidate on the message means none of it is safe to drop.

    Proves the anti-join direction: a wrong implementation that ignores the
    populated sibling (e.g. only ever checking the blank candidates it
    selected) would release job_id here, so this fails against that bug.
    """
    _record_job_alert(store, "m1")
    _stage_linkedin_candidate(store, "m1", "cand-blank")
    _stage_linkedin_candidate(store, "m1", "cand-real", company="Acme", title="Engineer")
    job_id = _blank_linkedin_job(store, "cand-blank")

    assert store.release_legacy_blank_linkedin_jobs() == 0
    assert store.get_job(job_id) is not None


def test_a_dependent_evaluation_blocks_release(store):
    """A job with a saved evaluation is never safe to delete."""
    _record_job_alert(store, "m1")
    _stage_linkedin_candidate(store, "m1", "cand1")
    job_id = _blank_linkedin_job(store, "cand1")
    store.save_evaluation(
        job_id,
        Evaluation(
            job_id=job_id,
            total_score=10,
            scores={},
            decision="reject",
            hard_blockers=[],
            strengths=[],
            gaps=[],
            salary_note="",
            location_note="",
            rationale="",
            model="m",
        ),
    )

    assert store.release_legacy_blank_linkedin_jobs() == 0
    assert store.get_job(job_id) is not None


def test_a_job_that_seeded_a_company_watch_no_longer_blocks_release(store):
    """A job that promoted a company watch is safe to delete since #204.

    Before #204, `job_hunter_company_watch.discovered_from_job_id`
    referenced `job_hunter_jobs (id, user_id)` with no `on delete cascade`
    (migration 202609060002:128), so `_job_has_dependencies` had to check it
    to avoid a 409 mid-loop. Automatic promotion now writes the shared
    `job_hunter_company_watch_health` instead, whose `discovered_from_job_id`
    is unenforced provenance with no foreign key at all -- so there is
    nothing left that can block this delete, and `_job_has_dependencies`
    no longer checks it.
    """
    _record_job_alert(store, "m1")
    _stage_linkedin_candidate(store, "m1", "cand1")
    job_id = _blank_linkedin_job(store, "cand1")
    # Automatic promotion writes the shared job_hunter_company_watch_health
    # (#204), which has no user_id -- a bare "Acme" here would race a
    # concurrently running test promoting the same literal name under
    # xdist (#236).
    store.upsert_company_watch(
        company_name=f"Acme {uuid.uuid4().hex[:12]}",
        careers_url="https://acme.example/careers",
        ats_provider=None,
        ats_identifier=None,
        discovered_from_job_id=job_id,
        promotion_source="automatic",
        confidence=0.9,
    )

    assert store.release_legacy_blank_linkedin_jobs() == 1
    assert store.get_job(job_id) is None


def test_non_poisoned_populated_job_is_left_alone(store):
    """A real (non-blank, non-`Sign in`) job under the same source is untouched."""
    _record_job_alert(store, "m1")
    _stage_linkedin_candidate(store, "m1", "cand1")
    job_id, _, _ = store.upsert_job(
        Job(
            source="gmail:linkedin",
            source_job_id="cand1",
            company="Acme",
            title="Staff Engineer",
            url="https://linkedin.example/cand1",
        )
    )

    assert store.release_legacy_blank_linkedin_jobs() == 0
    assert store.get_job(job_id) is not None


def test_message_not_classified_job_alert_is_skipped(store, supabase_client):
    """Only JOB_ALERT-classified messages are candidates for release."""
    store.record_gmail_message(
        message_id="m1",
        thread_id=None,
        sender="jobs-noreply@linkedin.com",
        subject="Not an alert",
        occurred_at="2026-08-01T00:00:00+00:00",
        classification="REVIEW_NEEDED",
        confidence=0.9,
        rationale="",
    )
    _stage_linkedin_candidate(store, "m1", "cand1")
    job_id = _blank_linkedin_job(store, "cand1")

    assert store.release_legacy_blank_linkedin_jobs() == 0
    assert store.get_job(job_id) is not None
    assert (
        supabase_client.select(
            "job_hunter_gmail_messages",
            params={"message_id": "eq.m1", "select": "id"},
        )
        != []
    )


def test_is_idempotent_on_repeat_calls(store):
    _record_job_alert(store, "m1")
    _stage_linkedin_candidate(store, "m1", "cand1")
    _blank_linkedin_job(store, "cand1")

    assert store.release_legacy_blank_linkedin_jobs() == 1
    assert store.release_legacy_blank_linkedin_jobs() == 0
