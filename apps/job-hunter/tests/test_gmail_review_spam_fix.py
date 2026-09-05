import json
import logging
from datetime import UTC, datetime, timedelta

from job_hunter.gmail_client import GmailPage
from job_hunter.gmail_models import GmailMessage
from job_hunter.gmail_sync import GmailSyncService
from job_hunter.store import JobStore


NOW = datetime(2026, 9, 1, 8, tzinfo=UTC)
LEGACY_RATIONALE = "semantic classification unavailable or invalid"


class FailingGemini:
    def generate_text(
        self,
        prompt: str,
        *,
        purpose: str | None = None,
        thinking_level: str | None = None,
        max_output_tokens: int | None = None,
        json_mode: bool = False,
        json_schema: dict | None = None,
    ) -> str:
        raise RuntimeError("provider unavailable")


class InvalidSemanticGemini:
    def generate_text(
        self,
        prompt: str,
        *,
        purpose: str | None = None,
        thinking_level: str | None = None,
        max_output_tokens: int | None = None,
        json_mode: bool = False,
        json_schema: dict | None = None,
    ) -> str:
        return json.dumps(
            {
                "kind": "RECRUITER_CONTACT",
                "confidence": 0.96,
                "company": "Acme",
                "role_title": "Frontend Engineer",
                "source_job_id": None,
                "job_urls": [],
                "jobs": [
                    {
                        "source_platform": "linkedin",
                        "source_job_id": None,
                        "url": "",
                        "company": "Acme",
                        "title": "Frontend Engineer",
                        "location": "Remote",
                        "remote": "yes",
                        "description": "RAW_MODEL_SENTINEL",
                    }
                ],
                "rationale": "Recruiter outreach about a frontend role.",
            }
        )


class OneMessageGmail:
    def __init__(self, message: GmailMessage) -> None:
        self.message = message

    def get_profile(self) -> tuple[str, str]:
        return "candidate@example.com", "100"

    def list_message_ids(self, query: str, page_token: str | None = None) -> GmailPage:
        return GmailPage([self.message.message_id], None)

    def get_message(self, message_id: str) -> GmailMessage:
        return self.message


def _record_review(store: JobStore, message_id: str, rationale: str) -> int:
    store.record_gmail_message(
        message_id=message_id,
        thread_id=f"thread-{message_id}",
        sender="recruiter@example.com",
        subject="Application update",
        occurred_at=NOW.isoformat(),
        classification="REVIEW_NEEDED",
        confidence=1.0,
        rationale=rationale,
    )
    return store.save_application_event(
        job_id=None,
        event_type="REVIEW_NEEDED",
        occurred_at=NOW.isoformat(),
        source_message_id=message_id,
        source_thread_id=f"thread-{message_id}",
        confidence=1.0,
        company="",
        role_title="",
        rationale=rationale,
    )


def test_semantic_provider_failure_is_error_not_review_and_remains_retryable(tmp_path):
    message = GmailMessage(
        message_id="m1",
        thread_id="t1",
        sender="updates@example.com",
        subject="Hiring conversation",
        sent_at=NOW,
        snippet="Can we discuss an engineering role?",
        body="Can we discuss an engineering role?",
    )
    store = JobStore(tmp_path / "state.sqlite3")
    service = GmailSyncService(
        gmail=OneMessageGmail(message),
        gemini=FailingGemini(),
        store=store,
    )

    summary = service.sync(NOW)

    assert summary.errors == 1
    assert summary.review_needed == 0
    assert store.has_processed_gmail_message("m1") is False
    assert store.pending_review_events() == []


def test_invalid_semantic_response_logs_safe_validation_detail_only(tmp_path, caplog):
    message = GmailMessage(
        message_id="invalid-semantic",
        thread_id="invalid-thread",
        sender="recruiter@linkedin.com",
        subject="Hiring conversation",
        sent_at=NOW,
        snippet="Can we discuss a frontend role?",
        body="Can we discuss a frontend role?",
    )
    store = JobStore(tmp_path / "state.sqlite3")
    service = GmailSyncService(
        gmail=OneMessageGmail(message),
        gemini=InvalidSemanticGemini(),
        store=store,
    )
    caplog.set_level(logging.WARNING, logger="job_hunter.gmail_sync")

    summary = service.sync(NOW)

    assert summary.errors == 1
    assert "reason=invalid_semantic_response" in caplog.text
    assert "detail=remote must be a boolean or null" in caplog.text
    assert "RAW_MODEL_SENTINEL" not in caplog.text


def test_release_legacy_semantic_failures_removes_only_exact_technical_artifacts(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")

    legacy_event = _record_review(store, "legacy", LEGACY_RATIONALE)
    real_event = _record_review(store, "real-review", "ambiguous scheduling language")
    store.mark_review_delivered([legacy_event, real_event], "telegram-1")

    released = store.release_legacy_gmail_semantic_failures()

    assert released == 1
    assert store.has_processed_gmail_message("legacy") is False
    assert store.has_processed_gmail_message("real-review") is True
    remaining = store._conn.execute(
        "SELECT source_message_id, rationale FROM application_events ORDER BY source_message_id"
    ).fetchall()
    assert [(row["source_message_id"], row["rationale"]) for row in remaining] == [
        ("real-review", "ambiguous scheduling language")
    ]
    assert store.release_legacy_gmail_semantic_failures() == 0


def test_writable_sync_reopens_completed_backfill_to_reprocess_legacy_failure(tmp_path):
    message = GmailMessage(
        message_id="legacy",
        thread_id="thread-legacy",
        sender="newsletter@example.com",
        subject="Weekly newsletter",
        sent_at=NOW,
        snippet="General product news",
        body="General product news",
    )
    store = JobStore(tmp_path / "state.sqlite3")
    _record_review(store, "legacy", LEGACY_RATIONALE)
    completed_at = NOW - timedelta(days=1)
    store.save_gmail_sync_state(
        account_id="candidate@example.com",
        history_id="90",
        last_successful_sync_at=completed_at.isoformat(),
        backfill_completed_at=completed_at.isoformat(),
    )
    service = GmailSyncService(
        gmail=OneMessageGmail(message),
        gemini=FailingGemini(),
        store=store,
    )

    summary = service.sync(NOW)

    row = store._conn.execute(
        "SELECT classification, rationale FROM gmail_messages WHERE message_id = 'legacy'"
    ).fetchone()
    state = store.get_gmail_sync_state("candidate@example.com")
    assert summary.processed == 1
    assert summary.review_needed == 0
    assert summary.errors == 0
    assert tuple(row) == ("IRRELEVANT", "no deterministic job signal")
    assert store.pending_review_events() == []
    assert state["backfill_completed_at"] == NOW.isoformat()
