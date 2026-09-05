from datetime import datetime, timedelta, timezone

from job_hunter.gmail_matching import derive_application_state, match_job
from job_hunter.gmail_models import GmailClassification, GmailMessage
from job_hunter.models import Job
from job_hunter.store import JobStore


_SENT_AT = datetime(2026, 8, 31, 12, tzinfo=timezone.utc)


def _message(sent_at: datetime = _SENT_AT) -> GmailMessage:
    return GmailMessage(
        message_id="message-1",
        thread_id="thread-1",
        sender="jobs@example.com",
        subject="Application update",
        sent_at=sent_at,
        snippet="",
        body="",
    )


def _classification(**overrides) -> GmailClassification:
    values = {
        "kind": "APPLIED",
        "confidence": 1.0,
        "company": "",
        "role_title": "",
        "source_job_id": None,
        "job_urls": [],
        "rationale": "test",
    }
    values.update(overrides)
    return GmailClassification(**values)


def _job(store: JobStore, **overrides) -> int:
    values = {
        "source": "lever",
        "source_job_id": None,
        "url": "",
        "company": "",
        "title": "",
    }
    values.update(overrides)
    job_id, _, _ = store.upsert_job(Job(**values))
    return job_id


def test_exact_canonical_url_beats_company_title(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    url_job_id = _job(
        store,
        source_job_id="url-job",
        url="https://jobs.example.com/frontend?utm_source=mail",
        company="Different Company",
        title="Different Title",
    )
    _job(
        store,
        source_job_id="company-title-job",
        company="Acme",
        title="Frontend Engineer",
    )

    result = match_job(
        store,
        _classification(
            company="Acme",
            role_title="Frontend Engineer",
            job_urls=["https://jobs.example.com/frontend"],
        ),
        _message(),
    )

    assert (result.job_id, result.reason, result.ambiguous) == (
        url_job_id,
        "canonical_url",
        False,
    )


def test_source_job_id_is_second_priority(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    source_id_job_id = _job(
        store,
        source_job_id="job-42",
        company="Different Company",
        title="Different Title",
    )
    _job(
        store,
        source_job_id="other-job",
        company="Acme",
        title="Frontend Engineer",
    )

    result = match_job(
        store,
        _classification(
            company="Acme",
            role_title="Frontend Engineer",
            source_job_id="job-42",
        ),
        _message(),
    )

    assert (result.job_id, result.reason, result.ambiguous) == (
        source_id_job_id,
        "source_job_id",
        False,
    )


def test_company_and_normalized_title_matches_when_unique(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    job_id = _job(
        store,
        source_job_id="job-1",
        company="  ACME  ",
        title="Senior   Frontend Engineer",
    )

    result = match_job(
        store,
        _classification(company="acme", role_title=" senior frontend engineer "),
        _message(),
    )

    assert (result.job_id, result.reason, result.ambiguous) == (
        job_id,
        "company_and_title",
        False,
    )


def test_company_only_recent_match_requires_single_candidate(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    job_id = _job(store, source_job_id="job-1", company="Acme")
    first_seen_at = (_SENT_AT - timedelta(days=120)).isoformat()
    store._conn.execute(
        "UPDATE jobs SET first_seen_at = ?, last_seen_at = ? WHERE id = ?",
        (first_seen_at, first_seen_at, job_id),
    )
    store._conn.commit()

    result = match_job(store, _classification(company="  acme "), _message())

    assert (result.job_id, result.reason, result.ambiguous) == (
        job_id,
        "recent_company",
        False,
    )


def test_ambiguous_company_match_returns_no_job(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    _job(store, source_job_id="job-1", company="Acme")
    _job(store, source_job_id="job-2", company="Acme")

    result = match_job(store, _classification(company="Acme"), _message())

    assert (result.job_id, result.reason, result.ambiguous) == (
        None,
        "ambiguous_company",
        True,
    )


def _event(event_id: int, event_type: str, occurred_at: str, **overrides):
    values = {
        "id": event_id,
        "job_id": 1,
        "event_type": event_type,
        "occurred_at": occurred_at,
        "confidence": 0.95,
    }
    values.update(overrides)
    return values


def test_latest_event_wins_even_if_earlier_event_has_higher_stage():
    state = derive_application_state(
        [
            _event(1, "OFFER", "2026-08-01T12:00:00+00:00"),
            _event(2, "APPLIED", "2026-08-02T12:00:00+00:00"),
        ]
    )

    assert state == "APPLIED"


def test_same_timestamp_uses_offer_before_rejected():
    state = derive_application_state(
        [
            _event(1, "REJECTED", "2026-08-01T12:00:00+00:00"),
            _event(2, "OFFER", "2026-08-01T12:00:00+00:00"),
        ]
    )

    assert state == "OFFER"


def test_review_needed_never_becomes_current_state():
    state = derive_application_state(
        [
            _event(1, "APPLIED", "2026-08-01T12:00:00+00:00"),
            _event(2, "REVIEW_NEEDED", "2026-08-02T12:00:00+00:00"),
        ]
    )

    assert state == "APPLIED"


def test_unresolved_event_never_becomes_current_state():
    state = derive_application_state(
        [
            _event(1, "APPLIED", "2026-08-01T12:00:00+00:00"),
            _event(
                2,
                "INTERVIEW",
                "2026-08-02T12:00:00+00:00",
                job_id=None,
            ),
        ]
    )

    assert state == "APPLIED"


def test_low_confidence_event_never_becomes_current_state():
    state = derive_application_state(
        [
            _event(1, "APPLIED", "2026-08-01T12:00:00+00:00"),
            _event(
                2,
                "INTERVIEW",
                "2026-08-02T12:00:00+00:00",
                confidence=0.89,
            ),
        ]
    )

    assert state == "APPLIED"
