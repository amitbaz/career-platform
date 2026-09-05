from datetime import UTC, datetime

from job_hunter.gmail_classifier import classify_email
from job_hunter.gmail_matching import JobMatch
from job_hunter.gmail_models import (
    AUTO_CONFIDENCE_THRESHOLD,
    GmailClassification,
    GmailMessage,
)
from job_hunter.gmail_sync import GmailSyncService, build_backfill_query
from job_hunter.models import Job
from job_hunter.store import JobStore


NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)


class NoCallGemini:
    def generate_text(self, *args, **kwargs):
        raise AssertionError("deterministic classification should not call Gemini")


def _message(subject: str, body: str, *, sender: str = "jobs@example.com") -> GmailMessage:
    return GmailMessage(
        message_id="m1",
        thread_id="t1",
        sender=sender,
        subject=subject,
        sent_at=NOW,
        snippet=body[:80],
        body=body,
        links=[],
    )


def test_non_job_pleased_to_offer_promotion_is_irrelevant():
    result = classify_email(
        _message(
            "Travel together, Save up to 15%!",
            "Vietnam Airlines is pleased to offer exclusive fares for friends and family.",
            sender="no-reply@e-news.vietnamairlines.com",
        ),
        NoCallGemini(),
    )

    assert result.kind == "IRRELEVANT"


def test_employment_offer_phrase_remains_deterministic_offer():
    result = classify_email(
        _message(
            "Your offer",
            "We are pleased to offer you the position of Senior Frontend Engineer.",
        ),
        NoCallGemini(),
    )

    assert result.kind == "OFFER"


def test_linkedin_recruiter_message_remains_recruiter_activity():
    result = classify_email(
        _message(
            "Ofer just messaged you",
            "1 new message awaits your response. Senior Talent Acquisition Manager | IT recruiter",
            sender="messaging-digest-noreply@linkedin.com",
        ),
        NoCallGemini(),
    )

    assert result.kind == "RECRUITER_CONTACT"


def test_backfill_query_uses_only_employment_specific_offer_terms():
    query = build_backfill_query(NOW)

    assert '"job offer"' in query
    assert '"offer letter"' in query
    assert '"offer of employment"' in query
    assert '"pleased to offer you"' in query
    assert " coding challenge\" offer}" not in query


def test_unresolved_lifecycle_persists_original_event_type(tmp_path, monkeypatch):
    store = JobStore(tmp_path / "state.sqlite3")
    message = GmailMessage(
        message_id="unresolved-interview",
        thread_id="thread-unresolved-interview",
        sender="jobs@example.com",
        subject="Interview invitation",
        sent_at=NOW,
        snippet="Choose an interview time",
        body="Interview invitation: choose a time.",
        links=[],
    )
    classification = GmailClassification(
        kind="INTERVIEW",
        confidence=1.0,
        company="Acme",
        role_title="Frontend Engineer",
        rationale="interview details",
    )
    monkeypatch.setattr("job_hunter.gmail_sync.classify_email", lambda *_, **__: classification)
    monkeypatch.setattr(
        "job_hunter.gmail_sync.match_job",
        lambda *_: JobMatch(job_id=None, reason="unresolved", ambiguous=False),
    )

    result = GmailSyncService(gmail=None, gemini=None, store=store).process_message(
        message, dry_run=False
    )

    event = store._conn.execute(
        "SELECT job_id, event_type FROM application_events WHERE source_message_id = ?",
        (message.message_id,),
    ).fetchone()
    assert result.kind == "REVIEW_NEEDED"
    assert (event["job_id"], event["event_type"]) == (None, "INTERVIEW")


def test_low_confidence_lifecycle_persists_original_event_type(tmp_path, monkeypatch):
    store = JobStore(tmp_path / "state.sqlite3")
    job_id, _, _ = store.upsert_job(
        Job(source="test", title="Frontend Engineer", company="Acme")
    )
    message = GmailMessage(
        message_id="low-confidence-interview",
        thread_id="thread-low-confidence-interview",
        sender="jobs@example.com",
        subject="Interview invitation",
        sent_at=NOW,
        snippet="Interview details",
        body="Interview details",
        links=[],
    )
    classification = GmailClassification(
        kind="INTERVIEW",
        confidence=AUTO_CONFIDENCE_THRESHOLD - 0.01,
        company="Acme",
        role_title="Frontend Engineer",
        rationale="model was uncertain",
    )
    monkeypatch.setattr("job_hunter.gmail_sync.classify_email", lambda *_, **__: classification)
    monkeypatch.setattr(
        "job_hunter.gmail_sync.match_job",
        lambda *_: JobMatch(job_id=job_id, reason="company_and_title", ambiguous=False),
    )

    result = GmailSyncService(gmail=None, gemini=None, store=store).process_message(
        message, dry_run=False
    )

    event = store._conn.execute(
        "SELECT job_id, event_type FROM application_events WHERE source_message_id = ?",
        (message.message_id,),
    ).fetchone()
    assert result.kind == "REVIEW_NEEDED"
    assert (event["job_id"], event["event_type"]) == (job_id, "INTERVIEW")


def test_pending_review_events_are_derived_from_linkage_and_confidence(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    job_id, _, _ = store.upsert_job(
        Job(source="test", title="Frontend Engineer", company="Acme")
    )

    cases = [
        ("unresolved", None, "RECRUITER_CONTACT", 1.0),
        (
            "low-confidence",
            job_id,
            "INTERVIEW",
            AUTO_CONFIDENCE_THRESHOLD - 0.01,
        ),
        ("resolved", job_id, "INTERVIEW", AUTO_CONFIDENCE_THRESHOLD),
        ("legacy-review", None, "REVIEW_NEEDED", 1.0),
    ]

    for message_id, linked_job_id, event_type, confidence in cases:
        store.record_gmail_message(
            message_id=message_id,
            thread_id=f"thread-{message_id}",
            sender="jobs@example.com",
            subject=f"subject-{message_id}",
            occurred_at=NOW.isoformat(),
            classification="REVIEW_NEEDED",
            confidence=confidence,
            rationale="fixture",
        )
        store.save_application_event(
            job_id=linked_job_id,
            event_type=event_type,
            occurred_at=NOW.isoformat(),
            source_message_id=message_id,
            source_thread_id=f"thread-{message_id}",
            confidence=confidence,
            company="Acme",
            role_title="Frontend Engineer",
            rationale="fixture",
        )

    pending_ids = {row["source_message_id"] for row in store.pending_review_events()}
    assert pending_ids == {"unresolved", "low-confidence", "legacy-review"}
