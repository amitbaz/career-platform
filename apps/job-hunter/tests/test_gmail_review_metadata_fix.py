import json
from datetime import UTC, datetime

from job_hunter.gmail_classifier import classify_email
from job_hunter.gmail_models import GmailMessage
from job_hunter.models import ReviewItem
from job_hunter.telegram import build_gmail_review_digest


NOW = datetime(2026, 9, 1, tzinfo=UTC)


class FakeGemini:
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


def _message() -> GmailMessage:
    return GmailMessage(
        message_id="m1",
        thread_id="t1",
        sender="recruiter@example.com",
        subject="Hiring process update",
        sent_at=NOW,
        snippet="Can we find time to chat about the role?",
        body="Can we find time to chat about the role?",
        links=["https://jobs.acme.example/frontend"],
    )


def test_low_confidence_review_preserves_semantic_metadata():
    result = classify_email(
        _message(),
        FakeGemini(
            {
                "kind": "INTERVIEW",
                "confidence": 0.72,
                "company": "Acme",
                "role_title": "Frontend Engineer",
                "source_job_id": "frontend-42",
                "job_urls": ["https://jobs.acme.example/frontend"],
                "jobs": [
                    {
                        "source_platform": "acme",
                        "source_job_id": "frontend-42",
                        "url": "https://jobs.acme.example/frontend",
                        "company": "Acme",
                        "title": "Frontend Engineer",
                        "location": "Remote",
                        "remote": True,
                        "description": "Frontend role",
                    }
                ],
                "rationale": "Possibly scheduling an interview.",
            }
        ),
    )

    assert result.kind == "REVIEW_NEEDED"
    assert result.confidence == 0.72
    assert result.company == "Acme"
    assert result.role_title == "Frontend Engineer"
    assert result.source_job_id == "frontend-42"
    assert result.job_urls == ["https://jobs.acme.example/frontend"]
    assert result.jobs[0].title == "Frontend Engineer"
    assert result.rationale == "Possibly scheduling an interview."


def test_review_without_company_or_role_falls_back_to_subject():
    digest = build_gmail_review_digest(
        [
            ReviewItem(
                event_id=1,
                company="",
                role_title="",
                occurred_at=NOW.isoformat(),
                subject="Interview invitation from Acme",
                rationale="semantic classification requires review",
                event_type="REVIEW_NEEDED",
                source_message_id="m1",
                source_thread_id="t1",
            )
        ]
    )

    assert digest == (
        "Gmail activity I couldn't link\n\n"
        "Interview invitation from Acme\n"
        "This looks job-related, but I couldn't classify or link it confidently.\n"
        "Open email: https://mail.google.com/mail/u/0/#all/t1"
    )
    assert "semantic classification requires review" not in digest
