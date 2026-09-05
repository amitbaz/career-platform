from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

from job_hunter.gemini_usage import GeminiQuotaPaused
from job_hunter.gmail_client import GmailHistoryExpired, GmailHistoryPage, GmailPage
from job_hunter.gmail_matching import JobMatch
from job_hunter.gmail_models import ExtractedJob, GmailClassification, GmailMessage
from job_hunter.models import Job
from job_hunter.gmail_sync import GmailSyncService, build_backfill_query
from job_hunter.store import JobStore


NOW = datetime(2026, 8, 31, 12, tzinfo=UTC)


def _message(message_id: str, *, sent_at: datetime = NOW) -> GmailMessage:
    return GmailMessage(
        message_id=message_id,
        thread_id=f"thread-{message_id}",
        sender="newsletter@example.com",
        subject="Weekly newsletter",
        sent_at=sent_at,
        snippet="General product news",
        body="General product news",
    )


def _job_alert(message_id: str, *, sent_at: datetime) -> GmailMessage:
    return GmailMessage(
        message_id=message_id,
        thread_id=f"thread-{message_id}",
        sender="jobalerts-noreply@linkedin.com",
        subject="Job alert",
        sent_at=sent_at,
        snippet="A new frontend role",
        body="A new frontend role",
        links=[f"https://www.linkedin.com/jobs/view/{message_id}/"],
    )


class FakeGemini:
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
        raise AssertionError("irrelevant fixture must not call Gemini")


class ResponseGemini:
    def __init__(self, response: dict) -> None:
        self.response = response

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
        return json.dumps(self.response)


class SequencedGemini:
    """Returns/raises each entry of `responses` in order, one per call."""

    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.calls: list[str] = []

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
        self.calls.append(prompt)
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return json.dumps(response)


class FakeGmail:
    def __init__(
        self,
        *,
        profile: tuple[str, str] = ("candidate@example.com", "100"),
        message_ids: list[str] | None = None,
        messages: dict[str, GmailMessage | Exception] | None = None,
    ) -> None:
        self.profile = profile
        self.message_ids = message_ids or []
        self.messages = messages or {}
        self.profile_calls = 0
        self.history_pages: dict[str | None, GmailHistoryPage | Exception] = {}
        self.search_calls: list[tuple[str, str | None]] = []
        self.history_calls: list[tuple[str, str | None]] = []
        self.message_calls: list[str] = []

    def get_profile(self) -> tuple[str, str]:
        self.profile_calls += 1
        return self.profile

    def list_message_ids(self, query: str, page_token: str | None = None) -> GmailPage:
        self.search_calls.append((query, page_token))
        return GmailPage(self.message_ids, None)

    def list_history(
        self, start_history_id: str, page_token: str | None = None
    ) -> GmailHistoryPage:
        self.history_calls.append((start_history_id, page_token))
        result = self.history_pages.get(
            page_token, GmailHistoryPage([], start_history_id, None)
        )
        if isinstance(result, Exception):
            raise result
        return result

    def get_message(self, message_id: str) -> GmailMessage:
        self.message_calls.append(message_id)
        result = self.messages[message_id]
        if isinstance(result, Exception):
            raise result
        return result


def _service(tmp_path, gmail: FakeGmail) -> tuple[GmailSyncService, JobStore]:
    store = JobStore(tmp_path / "state.sqlite3")
    return GmailSyncService(gmail=gmail, gemini=FakeGemini(), store=store), store


def _save_completed_state(
    store: JobStore,
    *,
    history_id: str = "100",
    completed_at: datetime = NOW - timedelta(days=1),
) -> None:
    store.save_gmail_sync_state(
        account_id="candidate@example.com",
        history_id=history_id,
        last_successful_sync_at=completed_at.isoformat(),
        backfill_completed_at=completed_at.isoformat(),
    )


def test_first_sync_uses_profile_email_as_account_id(tmp_path):
    gmail = FakeGmail(profile=("real.account@example.com", "checkpoint-1"))
    service, store = _service(tmp_path, gmail)

    service.sync(NOW)

    assert store.get_gmail_sync_state("real.account@example.com") is not None
    assert store.get_gmail_sync_state("primary") is None


def test_first_sync_scans_120_days_and_marks_backfill_complete(tmp_path):
    gmail = FakeGmail(
        message_ids=["m1"],
        messages={"m1": _message("m1")},
    )
    service, store = _service(tmp_path, gmail)

    summary = service.sync(NOW)

    assert gmail.search_calls == [
        (
            'after:2026/05/03 {application interview recruiter hiring "job alert" '
            'position "technical assessment" "coding challenge" "job offer" '
            '"offer letter" "offer of employment" "pleased to offer you"}',
            None,
        )
    ]
    assert build_backfill_query(datetime(2024, 2, 29, tzinfo=UTC)) == (
        'after:2023/11/01 {application interview recruiter hiring "job alert" '
        'position "technical assessment" "coding challenge" "job offer" '
        '"offer letter" "offer of employment" "pleased to offer you"}'
    )
    state = store.get_gmail_sync_state("candidate@example.com")
    assert state is not None
    assert state["history_id"] == "100"
    assert state["last_successful_sync_at"] == NOW.isoformat()
    assert state["backfill_completed_at"] == NOW.isoformat()
    assert summary.fetched == 1
    assert summary.processed == 1


def test_backfill_limits_unprocessed_messages_and_defers_remaining(tmp_path):
    gmail = FakeGmail(
        message_ids=["m1", "m2", "m3"],
        messages={
            "m1": _message("m1"),
            "m2": _message("m2"),
            "m3": _message("m3"),
        },
    )
    store = JobStore(tmp_path / "state.sqlite3")
    service = GmailSyncService(
        gmail=gmail,
        gemini=FakeGemini(),
        store=store,
        backfill_batch_size=2,
    )

    summary = service.sync(NOW)

    assert gmail.message_calls == ["m1", "m2"]
    assert summary.fetched == 3
    assert summary.processed == 2
    assert store.has_processed_gmail_message("m1") is True
    assert store.has_processed_gmail_message("m2") is True
    assert store.has_processed_gmail_message("m3") is False
    assert store.get_gmail_sync_state("candidate@example.com") is None


def test_failed_backfill_attempt_consumes_the_sole_batch_slot(tmp_path):
    gmail = FakeGmail(
        message_ids=["broken", "ok"],
        messages={"broken": RuntimeError("decode failed"), "ok": _message("ok")},
    )
    store = JobStore(tmp_path / "state.sqlite3")
    service = GmailSyncService(
        gmail=gmail,
        gemini=FakeGemini(),
        store=store,
        backfill_batch_size=1,
    )

    summary = service.sync(NOW)

    assert gmail.message_calls == ["broken"]
    assert summary.errors == 1
    assert summary.processed == 0
    assert store.has_processed_gmail_message("ok") is False
    assert store.get_gmail_sync_state("candidate@example.com") is None


def test_default_backfill_processes_only_first_100_unprocessed_messages(tmp_path):
    message_ids = [f"message-{index}" for index in range(101)]
    gmail = FakeGmail(
        message_ids=message_ids,
        messages={message_id: _message(message_id) for message_id in message_ids},
    )
    store = JobStore(tmp_path / "state.sqlite3")
    service = GmailSyncService(gmail=gmail, gemini=FakeGemini(), store=store)

    summary = service.sync(NOW)

    assert len(gmail.message_calls) == 100
    assert summary.fetched == 101
    assert summary.processed == 100
    assert store.has_processed_gmail_message("message-99") is True
    assert store.has_processed_gmail_message("message-100") is False
    assert store.get_gmail_sync_state("candidate@example.com") is None


def test_backfill_resumes_and_marks_complete_after_final_batch(tmp_path):
    gmail = FakeGmail(
        message_ids=["m1", "m2", "m3"],
        messages={
            "m1": _message("m1"),
            "m2": _message("m2"),
            "m3": _message("m3"),
        },
    )
    store = JobStore(tmp_path / "state.sqlite3")
    service = GmailSyncService(
        gmail=gmail,
        gemini=FakeGemini(),
        store=store,
        backfill_batch_size=2,
    )

    first = service.sync(NOW)
    second = service.sync(NOW + timedelta(minutes=1))

    state = store.get_gmail_sync_state("candidate@example.com")
    assert first.processed == 2
    assert second.processed == 1
    assert gmail.message_calls == ["m1", "m2", "m3"]
    assert state is not None
    assert state["history_id"] == "100"
    assert state["backfill_completed_at"] == (
        NOW + timedelta(minutes=1)
    ).isoformat()


def test_processed_ids_do_not_consume_backfill_batch_allowance(tmp_path):
    gmail = FakeGmail(
        message_ids=["old", "new-1", "new-2"],
        messages={
            "new-1": _message("new-1"),
            "new-2": _message("new-2"),
        },
    )
    store = JobStore(tmp_path / "state.sqlite3")
    store.record_gmail_message(
        message_id="old",
        thread_id="thread-old",
        sender="newsletter@example.com",
        subject="Old",
        occurred_at=NOW.isoformat(),
        classification="IRRELEVANT",
        confidence=1.0,
        rationale="already processed",
    )
    service = GmailSyncService(
        gmail=gmail,
        gemini=FakeGemini(),
        store=store,
        backfill_batch_size=2,
    )

    summary = service.sync(NOW)

    assert gmail.message_calls == ["new-1", "new-2"]
    assert summary.fetched == 3
    assert summary.processed == 2
    assert store.get_gmail_sync_state("candidate@example.com") is not None


def test_incremental_sync_is_not_limited_by_backfill_batch_size(tmp_path):
    gmail = FakeGmail(
        messages={
            "m1": _message("m1"),
            "m2": _message("m2"),
            "m3": _message("m3"),
        }
    )
    gmail.history_pages = {
        None: GmailHistoryPage(["m1", "m2", "m3"], "103", None)
    }
    store = JobStore(tmp_path / "state.sqlite3")
    _save_completed_state(store, history_id="100")
    service = GmailSyncService(
        gmail=gmail,
        gemini=FakeGemini(),
        store=store,
        backfill_batch_size=1,
    )

    summary = service.sync(NOW)

    assert gmail.message_calls == ["m1", "m2", "m3"]
    assert summary.processed == 3
    assert store.get_gmail_sync_state("candidate@example.com")["history_id"] == "103"


def test_forced_backfill_uses_same_batch_limit(tmp_path):
    gmail = FakeGmail(
        message_ids=["m1", "m2", "m3"],
        messages={
            "m1": _message("m1"),
            "m2": _message("m2"),
            "m3": _message("m3"),
        },
    )
    store = JobStore(tmp_path / "state.sqlite3")
    _save_completed_state(store)
    service = GmailSyncService(
        gmail=gmail,
        gemini=FakeGemini(),
        store=store,
        backfill_batch_size=2,
    )

    summary = service.sync(NOW, force_backfill=True)

    state = store.get_gmail_sync_state("candidate@example.com")
    assert summary.processed == 2
    assert gmail.message_calls == ["m1", "m2"]
    assert state is not None
    assert state["backfill_completed_at"] is None


def test_backfill_rerun_skips_processed_message_ids(tmp_path):
    gmail = FakeGmail(
        message_ids=["m1"],
        messages={"m1": _message("m1")},
    )
    service, store = _service(tmp_path, gmail)
    service.sync(NOW)

    summary = service.sync(NOW, force_backfill=True)

    assert gmail.message_calls == ["m1"]
    assert store.has_processed_gmail_message("m1") is True
    assert summary.fetched == 1
    assert summary.processed == 0


def test_backfill_error_prevents_completion_marker(tmp_path):
    gmail = FakeGmail(
        message_ids=["broken", "ok"],
        messages={"broken": RuntimeError("decode failed"), "ok": _message("ok")},
    )
    service, store = _service(tmp_path, gmail)

    summary = service.sync(NOW)

    assert summary.errors == 1
    assert summary.processed == 1
    assert store.has_processed_gmail_message("ok") is True
    assert store.get_gmail_sync_state("candidate@example.com") is None


def test_failed_forced_backfill_is_retried_by_following_ordinary_sync(tmp_path):
    gmail = FakeGmail(
        profile=("candidate@example.com", "force-start-checkpoint"),
        message_ids=["historical-message"],
        messages={"historical-message": RuntimeError("decode failed")},
    )
    service, store = _service(tmp_path, gmail)
    _save_completed_state(store, history_id="completed-cursor")

    failed_summary = service.sync(NOW, force_backfill=True)

    incomplete_state = store.get_gmail_sync_state("candidate@example.com")
    assert failed_summary.errors == 1
    assert incomplete_state["backfill_completed_at"] is None

    gmail.messages["historical-message"] = _message("historical-message")
    retry_summary = service.sync(NOW + timedelta(minutes=1))

    state = store.get_gmail_sync_state("candidate@example.com")
    expected_query = (
        'after:2026/05/03 {application interview recruiter hiring "job alert" '
        'position "technical assessment" "coding challenge" "job offer" '
        '"offer letter" "offer of employment" "pleased to offer you"}'
    )
    assert gmail.search_calls == [
        (expected_query, None),
        (expected_query, None),
    ]
    assert gmail.history_calls == []
    assert retry_summary.processed == 1
    assert state["history_id"] == "force-start-checkpoint"
    assert state["backfill_completed_at"] == (NOW + timedelta(minutes=1)).isoformat()


def test_message_arriving_during_backfill_is_not_skipped_by_saved_history_checkpoint(
    tmp_path,
):
    gmail = FakeGmail(
        profile=("candidate@example.com", "before-scan"),
        messages={"arrived-during-scan": _message("arrived-during-scan")},
    )
    service, store = _service(tmp_path, gmail)

    service.sync(NOW)
    gmail.history_pages = {
        None: GmailHistoryPage(["arrived-during-scan"], "after-scan", None)
    }
    second_summary = service.sync(NOW + timedelta(minutes=1))

    state = store.get_gmail_sync_state("candidate@example.com")
    assert gmail.history_calls == [("before-scan", None)]
    assert second_summary.processed == 1
    assert store.has_processed_gmail_message("arrived-during-scan") is True
    assert state is not None
    assert state["history_id"] == "after-scan"


def test_related_write_failure_does_not_record_message_or_advance_backfill(
    tmp_path, monkeypatch
):
    gmail = FakeGmail(
        message_ids=["alert"],
        messages={"alert": _job_alert("alert", sent_at=NOW)},
    )
    service, store = _service(tmp_path, gmail)

    def fail_staging(*args, **kwargs):
        raise RuntimeError("staging failed")

    monkeypatch.setattr(store, "stage_inbound_job", fail_staging)

    summary = service.sync(NOW)

    assert summary.errors == 1
    assert store.has_processed_gmail_message("alert") is False
    assert store.get_gmail_sync_state("candidate@example.com") is None


def test_six_month_old_job_alert_is_not_staged(tmp_path):
    gmail = FakeGmail(
        message_ids=["old-alert"],
        messages={
            "old-alert": _job_alert("old-alert", sent_at=NOW - timedelta(days=180))
        },
    )
    service, store = _service(tmp_path, gmail)

    summary = service.sync(NOW)

    assert store.has_processed_gmail_message("old-alert") is True
    assert (
        store._conn.execute("SELECT COUNT(*) FROM inbound_job_candidates").fetchone()[0]
        == 0
    )
    assert summary.job_alerts == 1


def test_three_day_old_job_alert_is_staged(tmp_path):
    gmail = FakeGmail(
        message_ids=["recent-alert"],
        messages={
            "recent-alert": _job_alert("recent-alert", sent_at=NOW - timedelta(days=3))
        },
    )
    service, store = _service(tmp_path, gmail)

    service.sync(NOW)

    candidate = store._conn.execute(
        "SELECT source_message_id FROM inbound_job_candidates"
    ).fetchone()
    assert candidate is not None
    assert candidate["source_message_id"] == "recent-alert"


def test_generic_job_board_alert_is_semantically_extracted_and_staged(tmp_path):
    job_url = "https://talentboard.example/jobs/frontend-42"
    alert = GmailMessage(
        message_id="generic-alert",
        thread_id="thread-generic-alert",
        sender="alerts@talentboard.example",
        subject="Job alert",
        sent_at=NOW,
        snippet="A new frontend role matches your preferences.",
        body="A new frontend role matches your preferences.",
        links=[job_url],
    )
    gmail = FakeGmail(message_ids=[alert.message_id], messages={alert.message_id: alert})
    store = JobStore(tmp_path / "state.sqlite3")
    gemini = ResponseGemini(
        {
            "kind": "JOB_ALERT",
            "confidence": 0.96,
            "company": "Acme",
            "role_title": "Frontend Engineer",
            "source_job_id": "frontend-42",
            "job_urls": [job_url],
            "jobs": [
                {
                    "source_platform": "talentboard",
                    "source_job_id": "frontend-42",
                    "url": job_url,
                    "company": "Acme",
                    "title": "Frontend Engineer",
                    "location": "Remote",
                    "remote": True,
                    "description": "Frontend role from the alert.",
                }
            ],
            "rationale": "Job-board alert with one frontend opening.",
        }
    )

    summary = GmailSyncService(gmail=gmail, gemini=gemini, store=store).sync(NOW)

    candidate = store._conn.execute(
        """
        SELECT source_platform, source_job_id, url
        FROM inbound_job_candidates
        WHERE source_message_id = 'generic-alert'
        """
    ).fetchone()
    assert summary.job_alerts == 1
    assert tuple(candidate) == ("talentboard", "frontend-42", job_url)


def _generic_job_alert(message_id: str, *, sent_at: datetime, job_url: str) -> GmailMessage:
    return GmailMessage(
        message_id=message_id,
        thread_id=f"thread-{message_id}",
        sender="alerts@talentboard.example",
        subject="Job alert",
        sent_at=sent_at,
        snippet="A new frontend role matches your preferences.",
        body="A new frontend role matches your preferences.",
        links=[job_url],
    )


def test_stale_backfill_job_alert_skips_semantic_extraction_and_stages_nothing(tmp_path):
    job_url = "https://talentboard.example/jobs/frontend-42"
    alert = _generic_job_alert(
        "stale-alert", sent_at=NOW - timedelta(days=15), job_url=job_url
    )
    gmail = FakeGmail(message_ids=[alert.message_id], messages={alert.message_id: alert})
    # FakeGemini raises if generate_text is ever called: proves zero Gemini
    # calls for a 15+ day backfill job alert.
    service, store = _service(tmp_path, gmail)

    summary = service.sync(NOW)

    assert store.has_processed_gmail_message("stale-alert") is True
    assert summary.job_alerts == 1
    assert (
        store._conn.execute("SELECT COUNT(*) FROM inbound_job_candidates").fetchone()[0]
        == 0
    )


def test_fresh_backfill_job_alert_still_uses_semantic_extraction(tmp_path):
    job_url = "https://talentboard.example/jobs/frontend-42"
    alert = _generic_job_alert(
        "fresh-alert", sent_at=NOW - timedelta(days=13), job_url=job_url
    )
    gmail = FakeGmail(message_ids=[alert.message_id], messages={alert.message_id: alert})
    store = JobStore(tmp_path / "state.sqlite3")
    gemini = ResponseGemini(
        {
            "kind": "JOB_ALERT",
            "confidence": 0.96,
            "company": "Acme",
            "role_title": "Frontend Engineer",
            "source_job_id": "frontend-42",
            "job_urls": [job_url],
            "jobs": [
                {
                    "source_platform": "talentboard",
                    "source_job_id": "frontend-42",
                    "url": job_url,
                    "company": "Acme",
                    "title": "Frontend Engineer",
                    "location": "Remote",
                    "remote": True,
                    "description": "Frontend role from the alert.",
                }
            ],
            "rationale": "Job-board alert with one frontend opening.",
        }
    )
    service = GmailSyncService(gmail=gmail, gemini=gemini, store=store)

    summary = service.sync(NOW)

    assert summary.job_alerts == 1
    candidate = store._conn.execute(
        "SELECT source_message_id FROM inbound_job_candidates"
    ).fetchone()
    assert candidate is not None
    assert candidate["source_message_id"] == "fresh-alert"


def _semantic_job_alert(message_id: str) -> GmailMessage:
    return GmailMessage(
        message_id=message_id,
        thread_id=f"thread-{message_id}",
        sender="alerts@talentboard.example",
        subject="Job alert",
        sent_at=NOW,
        snippet="A new frontend role matches your preferences.",
        body="A new frontend role matches your preferences.",
        links=[],
    )


def test_quota_pause_stops_backfill_batch_and_leaves_remainder_unprocessed(tmp_path):
    gmail = FakeGmail(
        message_ids=["m1", "m2", "m3"],
        messages={
            "m1": _semantic_job_alert("m1"),
            "m2": _semantic_job_alert("m2"),
            "m3": _semantic_job_alert("m3"),
        },
    )
    gemini = SequencedGemini(
        [
            {
                "kind": "JOB_ALERT",
                "confidence": 0.9,
                "company": "",
                "role_title": "",
                "source_job_id": None,
                "job_urls": [],
                "jobs": [],
                "rationale": "generic alert",
            },
            GeminiQuotaPaused(
                "paused",
                paused_until="2026-09-02T00:00:00+00:00",
                reason="daily_quota",
            ),
        ]
    )
    store = JobStore(tmp_path / "state.sqlite3")
    service = GmailSyncService(gmail=gmail, gemini=gemini, store=store)

    summary = service.sync(NOW)

    # m3 is never even fetched: the batch stops attempting further messages
    # as soon as the pause is raised.
    assert gmail.message_calls == ["m1", "m2"]
    assert summary.processed == 1
    assert summary.errors == 0
    assert store.has_processed_gmail_message("m1") is True
    assert store.has_processed_gmail_message("m2") is False
    assert store.has_processed_gmail_message("m3") is False
    # Not counted as a permanent malformed-email review item, and backfill
    # is not marked complete while messages remain unprocessed.
    assert store.get_gmail_sync_state("candidate@example.com") is None


def test_quota_pause_during_incremental_sync_does_not_advance_history_cursor(tmp_path):
    gmail = FakeGmail(
        messages={
            "m1": _semantic_job_alert("m1"),
            "m2": _semantic_job_alert("m2"),
        }
    )
    gmail.history_pages = {
        None: GmailHistoryPage(["m1", "m2"], "new-cursor", None)
    }
    gemini = SequencedGemini(
        [
            {
                "kind": "JOB_ALERT",
                "confidence": 0.9,
                "company": "",
                "role_title": "",
                "source_job_id": None,
                "job_urls": [],
                "jobs": [],
                "rationale": "generic alert",
            },
            GeminiQuotaPaused(
                "paused",
                paused_until="2026-09-02T00:00:00+00:00",
                reason="daily_quota",
            ),
        ]
    )
    store = JobStore(tmp_path / "state.sqlite3")
    _save_completed_state(store, history_id="old-cursor")
    service = GmailSyncService(gmail=gmail, gemini=gemini, store=store)

    summary = service.sync(NOW)

    state = store.get_gmail_sync_state("candidate@example.com")
    assert gmail.message_calls == ["m1", "m2"]
    assert summary.processed == 1
    assert state["history_id"] == "old-cursor"
    assert store.has_processed_gmail_message("m1") is True
    assert store.has_processed_gmail_message("m2") is False


def test_semantic_gmail_job_description_is_not_persisted(tmp_path, monkeypatch):
    private_body = "PRIVATE EMAIL BODY THAT MUST NOT ENTER SQLITE"
    alert = GmailMessage(
        message_id="private-alert",
        thread_id="thread-private-alert",
        sender="alerts@talentboard.example",
        subject="Job alert",
        sent_at=NOW,
        snippet="A private role summary.",
        body=private_body,
        links=["https://talentboard.example/jobs/private-42"],
    )
    classification = GmailClassification(
        kind="JOB_ALERT",
        confidence=0.96,
        jobs=[
            ExtractedJob(
                source_platform="talentboard",
                source_job_id="private-42",
                url="https://talentboard.example/jobs/private-42",
                company="Acme",
                title="Frontend Engineer",
                description=private_body,
            )
        ],
        rationale="Job-board alert with one opening.",
    )
    gmail = FakeGmail(message_ids=[alert.message_id], messages={alert.message_id: alert})
    database_path = tmp_path / "state.sqlite3"
    store = JobStore(database_path)
    monkeypatch.setattr("job_hunter.gmail_sync.classify_email", lambda *_, **__: classification)

    GmailSyncService(gmail=gmail, gemini=FakeGemini(), store=store).sync(NOW)

    persisted_description = store._conn.execute(
        "SELECT description FROM inbound_job_candidates WHERE source_message_id = ?",
        (alert.message_id,),
    ).fetchone()["description"]
    store.close()
    assert persisted_description == ""
    assert private_body.encode() not in database_path.read_bytes()


def test_second_sync_uses_saved_history_id(tmp_path):
    gmail = FakeGmail(messages={"m1": _message("m1"), "m2": _message("m2")})
    gmail.history_pages = {
        None: GmailHistoryPage(["m1"], "110", "page-2"),
        "page-2": GmailHistoryPage(["m2"], "112", None),
    }
    service, store = _service(tmp_path, gmail)
    _save_completed_state(store, history_id="100")

    summary = service.sync(NOW)

    assert gmail.history_calls == [("100", None), ("100", "page-2")]
    assert store.get_gmail_sync_state("candidate@example.com")["history_id"] == "112"
    assert summary.processed == 2


def test_history_message_ids_are_idempotent(tmp_path):
    gmail = FakeGmail(messages={"new": _message("new")})
    gmail.history_pages = {
        None: GmailHistoryPage(["already-processed", "new"], "101", None)
    }
    service, store = _service(tmp_path, gmail)
    _save_completed_state(store)
    store.record_gmail_message(
        message_id="already-processed",
        thread_id="thread-old",
        sender="newsletter@example.com",
        subject="Old",
        occurred_at=NOW.isoformat(),
        classification="IRRELEVANT",
        confidence=1.0,
        rationale="test fixture",
    )

    summary = service.sync(NOW)

    assert gmail.message_calls == ["new"]
    assert summary.fetched == 2
    assert summary.processed == 1
    assert (
        store._conn.execute("SELECT COUNT(*) FROM gmail_messages").fetchone()[0]
        == 2
    )


def test_history_hard_error_does_not_advance_cursor(tmp_path):
    gmail = FakeGmail(
        messages={
            "broken": RuntimeError("decode failed"),
            "ok": _message("ok"),
        }
    )
    gmail.history_pages = {
        None: GmailHistoryPage(["broken", "ok"], "new-cursor", None)
    }
    service, store = _service(tmp_path, gmail)
    original_sync_at = NOW - timedelta(days=1)
    _save_completed_state(store, history_id="old-cursor", completed_at=original_sync_at)

    summary = service.sync(NOW)

    state = store.get_gmail_sync_state("candidate@example.com")
    assert summary.errors == 1
    assert summary.processed == 1
    assert state["history_id"] == "old-cursor"
    assert state["last_successful_sync_at"] == original_sync_at.isoformat()


def test_processed_message_lookup_failure_is_counted_and_continues_batch(
    tmp_path, monkeypatch, caplog
):
    gmail = FakeGmail(
        messages={"lookup-broken": _message("lookup-broken"), "ok": _message("ok")}
    )
    gmail.history_pages = {
        None: GmailHistoryPage(["lookup-broken", "ok"], "new-cursor", None)
    }
    service, store = _service(tmp_path, gmail)
    _save_completed_state(store, history_id="old-cursor")
    original_lookup = store.has_processed_gmail_message

    def fail_one_lookup(message_id: str) -> bool:
        if message_id == "lookup-broken":
            raise RuntimeError("processed-message lookup failed")
        return original_lookup(message_id)

    monkeypatch.setattr(store, "has_processed_gmail_message", fail_one_lookup)
    caplog.set_level(logging.INFO, logger="job_hunter.gmail_sync")

    summary = service.sync(NOW)

    state = store.get_gmail_sync_state("candidate@example.com")
    assert summary.errors == 1
    assert summary.processed == 1
    assert gmail.message_calls == ["ok"]
    assert state["history_id"] == "old-cursor"
    assert caplog.messages[-1] == (
        "gmail_fetched=2 gmail_processed=1 gmail_job_alerts=0 "
        "gmail_application_events=0 gmail_review_needed=0 "
        "gmail_irrelevant=1 gmail_errors=1"
    )


def test_resolved_lifecycle_event_persists_real_event_type(tmp_path):
    message = GmailMessage(
        message_id="interview-message",
        thread_id="interview-thread",
        sender="jobs@example.com",
        subject="Interview invitation",
        sent_at=NOW,
        snippet="Choose an interview time",
        body="Interview invitation: choose a time.",
        links=["https://jobs.example.com/frontend"],
    )
    gmail = FakeGmail(message_ids=[message.message_id], messages={message.message_id: message})
    service, store = _service(tmp_path, gmail)
    job_id, _, _ = store.upsert_job(
        Job(
            source="public",
            source_job_id="frontend-1",
            url="https://jobs.example.com/frontend",
            company="Acme",
            title="Frontend Engineer",
        )
    )

    summary = service.sync(NOW)

    event = store._conn.execute(
        "SELECT job_id, event_type FROM application_events WHERE source_message_id = ?",
        (message.message_id,),
    ).fetchone()
    assert summary.application_events == 1
    assert (event["job_id"], event["event_type"]) == (job_id, "INTERVIEW")


def test_unresolved_lifecycle_event_is_review_needed_without_job_association(tmp_path):
    message = GmailMessage(
        message_id="unresolved-interview",
        thread_id="unresolved-thread",
        sender="jobs@example.com",
        subject="Interview invitation",
        sent_at=NOW,
        snippet="Choose an interview time",
        body="Interview invitation: choose a time.",
    )
    gmail = FakeGmail(message_ids=[message.message_id], messages={message.message_id: message})
    service, store = _service(tmp_path, gmail)

    summary = service.sync(NOW)

    event = store._conn.execute(
        "SELECT job_id, event_type FROM application_events WHERE source_message_id = ?",
        (message.message_id,),
    ).fetchone()
    assert summary.review_needed == 1
    assert (event["job_id"], event["event_type"]) == (None, "INTERVIEW")


def test_ambiguous_lifecycle_event_is_review_needed_without_job_association(
    tmp_path, monkeypatch
):
    message = _message("ambiguous-interview")
    gmail = FakeGmail(message_ids=[message.message_id], messages={message.message_id: message})
    service, store = _service(tmp_path, gmail)
    job_id, _, _ = store.upsert_job(
        Job(
            source="public",
            source_job_id="frontend-1",
            company="Acme",
            title="Frontend Engineer",
        )
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
        lambda *_: JobMatch(job_id=job_id, reason="ambiguous_title", ambiguous=True),
    )

    summary = service.sync(NOW)

    event = store._conn.execute(
        "SELECT job_id, event_type FROM application_events WHERE source_message_id = ?",
        (message.message_id,),
    ).fetchone()
    assert summary.review_needed == 1
    assert (event["job_id"], event["event_type"]) == (None, "INTERVIEW")


def test_low_confidence_lifecycle_event_is_review_needed(tmp_path, monkeypatch):
    message = _message("low-confidence")
    gmail = FakeGmail(message_ids=[message.message_id], messages={message.message_id: message})
    service, store = _service(tmp_path, gmail)
    job_id, _, _ = store.upsert_job(
        Job(
            source="public",
            source_job_id="frontend-1",
            url="https://jobs.example.com/frontend",
            company="Acme",
            title="Frontend Engineer",
        )
    )
    classification = GmailClassification(
        kind="INTERVIEW",
        confidence=0.89,
        company="Acme",
        role_title="Frontend Engineer",
        rationale="model was uncertain",
    )
    monkeypatch.setattr("job_hunter.gmail_sync.classify_email", lambda *_, **__: classification)

    summary = service.sync(NOW)

    event = store._conn.execute(
        "SELECT job_id, event_type FROM application_events WHERE source_message_id = ?",
        (message.message_id,),
    ).fetchone()
    assert job_id is not None
    assert summary.review_needed == 1
    assert (event["job_id"], event["event_type"]) == (job_id, "INTERVIEW")


def test_old_recruiter_mail_without_extracted_job_is_not_staged_but_concrete_role_is(
    tmp_path, monkeypatch
):
    no_job = _message("old-recruiter-no-job", sent_at=NOW - timedelta(days=180))
    concrete_role = _message("old-recruiter-role", sent_at=NOW - timedelta(days=180))
    gmail = FakeGmail(
        message_ids=[no_job.message_id, concrete_role.message_id],
        messages={no_job.message_id: no_job, concrete_role.message_id: concrete_role},
    )
    service, store = _service(tmp_path, gmail)
    classifications = iter(
        [
            GmailClassification(
                kind="RECRUITER_CONTACT",
                confidence=1.0,
                rationale="general outreach without a role",
            ),
            GmailClassification(
                kind="RECRUITER_CONTACT",
                confidence=1.0,
                jobs=[
                    ExtractedJob(
                        source_platform="linkedin",
                        url="https://www.linkedin.com/jobs/view/42/",
                        company="Acme",
                        title="Frontend Engineer",
                    )
                ],
                rationale="concrete recruiter opportunity",
            ),
        ]
    )
    monkeypatch.setattr(
        "job_hunter.gmail_sync.classify_email", lambda *_, **__: next(classifications)
    )

    service.sync(NOW)

    rows = store._conn.execute(
        "SELECT source_message_id FROM inbound_job_candidates ORDER BY source_message_id"
    ).fetchall()
    assert [row["source_message_id"] for row in rows] == ["old-recruiter-role"]


def test_expired_history_uses_one_day_overlap_search(tmp_path):
    gmail = FakeGmail(
        profile=("candidate@example.com", "recovery-start-checkpoint"),
        message_ids=["overlap-message"],
        messages={"overlap-message": _message("overlap-message")},
    )
    gmail.history_pages = {None: GmailHistoryExpired()}
    service, store = _service(tmp_path, gmail)
    _save_completed_state(
        store,
        history_id="expired-cursor",
        completed_at=datetime(2026, 8, 30, 12, tzinfo=UTC),
    )

    service.sync(NOW)

    assert gmail.search_calls == [
        (
            'after:2026/08/29 {application interview recruiter hiring "job alert" '
            'position "technical assessment" "coding challenge" "job offer" '
            '"offer letter" "offer of employment" "pleased to offer you"}',
            None,
        )
    ]
    state = store.get_gmail_sync_state("candidate@example.com")
    assert state["history_id"] == "recovery-start-checkpoint"
    assert state["last_successful_sync_at"] == NOW.isoformat()


def test_dry_run_writes_nothing_and_does_not_advance_state(tmp_path):
    gmail = FakeGmail(
        profile=("candidate@example.com", "dry-run-checkpoint"),
        messages={"dry-alert": _job_alert("dry-alert", sent_at=NOW)},
    )
    gmail.history_pages = {
        None: GmailHistoryPage(["dry-alert"], "dry-run-new-cursor", None)
    }
    service, store = _service(tmp_path, gmail)
    _save_completed_state(store, history_id="dry-run-old-cursor")
    state_before = dict(store.get_gmail_sync_state("candidate@example.com"))

    summary = service.sync(NOW, dry_run=True)

    state = store.get_gmail_sync_state("candidate@example.com")
    assert summary.processed == 1
    assert dict(state) == state_before
    for table in (
        "gmail_messages",
        "inbound_job_candidates",
        "application_events",
        "review_deliveries",
    ):
        assert store._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_force_backfill_is_idempotent_and_non_destructive(tmp_path):
    recruiter = GmailMessage(
        message_id="recruiter-message",
        thread_id="recruiter-thread",
        sender="recruiter@example.com",
        subject="Recruiter outreach",
        sent_at=NOW,
        snippet="Recruiter role",
        body="Recruiter role",
        links=["https://www.linkedin.com/jobs/view/42/"],
    )
    gmail = FakeGmail(
        message_ids=["recruiter-message"],
        messages={"recruiter-message": recruiter},
    )
    service, store = _service(tmp_path, gmail)
    service.sync(NOW)
    gmail.profile = ("candidate@example.com", "fresh-force-checkpoint")

    service.sync(NOW + timedelta(hours=1), force_backfill=True)

    assert (
        store._conn.execute("SELECT COUNT(*) FROM gmail_messages").fetchone()[0]
        == 1
    )
    assert (
        store._conn.execute("SELECT COUNT(*) FROM inbound_job_candidates").fetchone()[0]
        == 1
    )
    assert (
        store._conn.execute("SELECT COUNT(*) FROM application_events").fetchone()[0]
        == 1
    )
    assert (
        store.get_gmail_sync_state("candidate@example.com")["history_id"]
        == "fresh-force-checkpoint"
    )


def test_sync_logs_exact_compact_metrics(tmp_path, caplog):
    gmail = FakeGmail(message_ids=["m1"], messages={"m1": _message("m1")})
    service, _ = _service(tmp_path, gmail)
    caplog.set_level(logging.INFO, logger="job_hunter.gmail_sync")

    service.sync(NOW)

    assert caplog.messages[-1] == (
        "gmail_fetched=1 gmail_processed=1 gmail_job_alerts=0 "
        "gmail_application_events=0 gmail_review_needed=0 "
        "gmail_irrelevant=1 gmail_errors=0"
    )
