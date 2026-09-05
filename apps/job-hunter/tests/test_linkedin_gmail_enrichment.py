from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from job_hunter.gmail_classifier import classify_email, source_candidate_key
from job_hunter.gmail_client import GmailHistoryPage, GmailPage
from job_hunter.gmail_linkedin_cleanup import release_legacy_blank_linkedin_jobs
from job_hunter.gmail_models import ExtractedJob, GmailMessage
from job_hunter.gmail_sync import GmailSyncService
from job_hunter.models import Evaluation, Job
from job_hunter.store import JobStore


NOW = datetime(2026, 9, 1, 10, tzinfo=UTC)


class ResponseGemini:
    def __init__(self, response: dict) -> None:
        self.response = response
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
        return json.dumps(self.response)


class EmptyGmail:
    def __init__(self) -> None:
        self.search_calls: list[str] = []
        self.history_calls: list[str] = []

    def get_profile(self) -> tuple[str, str]:
        return "candidate@example.com", "200"

    def list_message_ids(self, query: str, page_token: str | None = None) -> GmailPage:
        self.search_calls.append(query)
        return GmailPage([], None)

    def list_history(
        self, start_history_id: str, page_token: str | None = None
    ) -> GmailHistoryPage:
        self.history_calls.append(start_history_id)
        return GmailHistoryPage([], "201", None)

    def get_message(self, message_id: str) -> GmailMessage:
        raise AssertionError("empty Gmail fixture must not fetch a message")


def _linkedin_alert(*links: str) -> GmailMessage:
    return GmailMessage(
        message_id="linkedin-alert",
        thread_id="linkedin-thread",
        sender="LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>",
        subject="Frontend Engineer at Hired",
        sent_at=NOW,
        snippet="Hired Frontend Engineer: Role: Frontend Engineer Location: France (Remote)",
        body="A new job matches your alert: Hired Frontend Engineer in France (Remote).",
        links=list(links),
    )


def _semantic_linkedin_response(url: str) -> dict:
    return {
        "kind": "JOB_ALERT",
        "confidence": 0.98,
        "company": "Hired",
        "role_title": "Frontend Engineer",
        "source_job_id": "4461012343",
        "job_urls": [url],
        "jobs": [
            {
                "source_platform": "linkedin",
                "source_job_id": "4461012343",
                "url": url,
                "company": "Hired",
                "title": "Frontend Engineer",
                "location": "France (Remote)",
                "remote": True,
                "description": "Frontend Engineer role advertised by Hired.",
            }
        ],
        "rationale": "LinkedIn job alert for one frontend opening.",
    }


def test_linkedin_alert_is_semantically_enriched_and_tracking_links_are_deduped():
    tracked_a = (
        "https://www.linkedin.com/comm/jobs/view/4461012343/"
        "?trackingId=first&trk=email_job_alert"
    )
    tracked_b = (
        "https://www.linkedin.com/comm/jobs/view/4461012343/"
        "?trackingId=second&refId=other"
    )
    normalized = "https://www.linkedin.com/jobs/view/4461012343/"
    gemini = ResponseGemini(_semantic_linkedin_response(normalized))

    result = classify_email(_linkedin_alert(tracked_a, tracked_b), gemini)

    assert len(gemini.calls) == 1
    assert result.kind == "JOB_ALERT"
    assert len(result.jobs) == 1
    assert len(result.job_urls) == 1
    job = result.jobs[0]
    assert job.source_platform == "linkedin"
    assert job.source_job_id == "4461012343"
    assert job.company == "Hired"
    assert job.title == "Frontend Engineer"
    assert job.location == "France (Remote)"
    assert "/jobs/view/4461012343/" in job.url
    assert source_candidate_key(job) == "id:linkedin:4461012343"


def test_linkedin_candidate_key_uses_job_id_from_tracking_url_without_semantic_id():
    job = ExtractedJob(
        source_platform="linkedin",
        url=(
            "https://www.linkedin.com/comm/jobs/view/4461012343/"
            "?trackingId=first&trk=email_job_alert"
        ),
    )

    assert source_candidate_key(job) == "id:linkedin:4461012343"


def _record_alert(store: JobStore, message_id: str) -> None:
    store.record_gmail_message(
        message_id=message_id,
        thread_id=f"thread-{message_id}",
        sender="jobalerts-noreply@linkedin.com",
        subject="Frontend Engineer at Example",
        occurred_at=NOW.isoformat(),
        classification="JOB_ALERT",
        confidence=1.0,
        rationale="deterministic job alert sender or template",
    )


def _seed_linkedin_candidate(
    store: JobStore,
    *,
    message_id: str,
    key: str,
    company: str,
    title: str,
    job_company: str | None = None,
    job_title: str | None = None,
    with_evaluation: bool = False,
) -> int:
    url = f"https://www.linkedin.com/jobs/view/{message_id}/?trackingId=legacy"
    materialized_company = company if job_company is None else job_company
    materialized_title = title if job_title is None else job_title
    _record_alert(store, message_id)
    store.stage_inbound_job(
        message_id,
        key,
        ExtractedJob(
            source_platform="linkedin",
            url=url,
            company=company,
            title=title,
        ),
    )
    job_id, _, _ = store.upsert_job(
        Job(
            source="gmail:linkedin",
            source_job_id=key,
            url=url,
            company=materialized_company,
            title=materialized_title,
        )
    )
    if with_evaluation:
        store.save_evaluation(
            job_id,
            Evaluation(
                job_id=job_id,
                total_score=50,
                scores={},
                decision="skip",
                hard_blockers=[],
                strengths=[],
                gaps=[],
                salary_note="",
                location_note="",
                rationale="existing dependent record",
                model="test",
            ),
        )
    return job_id


def test_legacy_blank_linkedin_cleanup_releases_only_safe_blank_artifacts(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    legacy_job_id = _seed_linkedin_candidate(
        store,
        message_id="4461012343",
        key="url:legacy-blank",
        company="",
        title="",
    )
    good_job_id = _seed_linkedin_candidate(
        store,
        message_id="4461012344",
        key="id:linkedin:4461012344",
        company="Hired",
        title="Frontend Engineer",
    )
    dependent_job_id = _seed_linkedin_candidate(
        store,
        message_id="4461012345",
        key="url:dependent-blank",
        company="",
        title="",
        with_evaluation=True,
    )

    released = release_legacy_blank_linkedin_jobs(store)

    assert released == 1
    assert store.has_processed_gmail_message("4461012343") is False
    assert store.has_processed_gmail_message("4461012344") is True
    assert store.has_processed_gmail_message("4461012345") is True
    assert store.get_job(legacy_job_id) is None
    assert store.get_job(good_job_id) is not None
    assert store.get_job(dependent_job_id) is not None
    remaining_messages = {
        row["source_message_id"]
        for row in store._conn.execute(
            "SELECT source_message_id FROM inbound_job_candidates"
        )
    }
    assert remaining_messages == {"4461012344", "4461012345"}


def test_legacy_sign_in_linkedin_cleanup_releases_safe_poisoned_artifact(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    job_id = _seed_linkedin_candidate(
        store,
        message_id="4461012350",
        key="url:legacy-sign-in",
        company="",
        title="",
        job_company="",
        job_title="  SIGN IN  ",
    )

    released = release_legacy_blank_linkedin_jobs(store)

    assert released == 1
    assert store.has_processed_gmail_message("4461012350") is False
    assert store.get_job(job_id) is None


def test_legacy_sign_in_linkedin_cleanup_preserves_dependent_artifact(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    job_id = _seed_linkedin_candidate(
        store,
        message_id="4461012351",
        key="url:dependent-sign-in",
        company="",
        title="",
        job_company="",
        job_title="Sign in",
        with_evaluation=True,
    )

    released = release_legacy_blank_linkedin_jobs(store)

    assert released == 0
    assert store.has_processed_gmail_message("4461012351") is True
    assert store.get_job(job_id) is not None


def test_legacy_sign_in_linkedin_cleanup_preserves_nonempty_company(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    job_id = _seed_linkedin_candidate(
        store,
        message_id="4461012352",
        key="url:company-sign-in",
        company="",
        title="",
        job_company="Example",
        job_title="Sign in",
    )

    released = release_legacy_blank_linkedin_jobs(store)

    assert released == 0
    assert store.has_processed_gmail_message("4461012352") is True
    assert store.get_job(job_id) is not None


def test_writable_sync_reopens_completed_backfill_after_blank_linkedin_cleanup(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    _seed_linkedin_candidate(
        store,
        message_id="4461012343",
        key="url:legacy-blank",
        company="",
        title="",
    )
    completed_at = NOW - timedelta(days=1)
    store.save_gmail_sync_state(
        account_id="candidate@example.com",
        history_id="100",
        last_successful_sync_at=completed_at.isoformat(),
        backfill_completed_at=completed_at.isoformat(),
    )
    gmail = EmptyGmail()
    service = GmailSyncService(
        gmail=gmail,
        gemini=ResponseGemini(_semantic_linkedin_response("https://example.com/unused")),
        store=store,
    )

    service.sync(NOW)

    assert gmail.search_calls
    assert gmail.history_calls == []
    assert store.has_processed_gmail_message("4461012343") is False


def test_dry_run_does_not_release_legacy_blank_linkedin_state(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    _seed_linkedin_candidate(
        store,
        message_id="4461012343",
        key="url:legacy-blank",
        company="",
        title="",
    )
    completed_at = NOW - timedelta(days=1)
    store.save_gmail_sync_state(
        account_id="candidate@example.com",
        history_id="100",
        last_successful_sync_at=completed_at.isoformat(),
        backfill_completed_at=completed_at.isoformat(),
    )
    gmail = EmptyGmail()
    service = GmailSyncService(
        gmail=gmail,
        gemini=ResponseGemini(_semantic_linkedin_response("https://example.com/unused")),
        store=store,
    )

    service.sync(NOW, dry_run=True)

    assert gmail.search_calls == []
    assert gmail.history_calls == ["100"]
    assert store.has_processed_gmail_message("4461012343") is True
