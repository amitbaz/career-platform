import json
from datetime import UTC, datetime

import pytest

from job_hunter.gmail_classifier import (
    classify_deterministically,
    classify_email,
    is_probably_job_related,
    source_candidate_key,
)
from job_hunter.gmail_models import ExtractedJob, GmailMessage


def message(subject: str, body: str, *, sender: str = "jobs@example.com", links: list[str] | None = None) -> GmailMessage:
    return GmailMessage(
        message_id="m1",
        thread_id="t1",
        sender=sender,
        subject=subject,
        sent_at=datetime(2026, 8, 31, tzinfo=UTC),
        snippet=body[:80],
        body=body,
        links=links or [],
    )


@pytest.mark.parametrize(
    ("subject", "body", "expected"),
    [
        ("Thanks for applying", "We received your application for Frontend Engineer.", "APPLIED"),
        ("Update on your application", "We will not be moving forward with your application.", "REJECTED"),
        ("Interview invitation", "Choose a time for your interview with our team.", "INTERVIEW"),
        ("Technical assessment", "Please complete this coding challenge by Friday.", "TECHNICAL"),
        ("Offer", "We are pleased to offer you the position.", "OFFER"),
    ],
)
def test_high_signal_templates_are_deterministic(subject, body, expected):
    result = classify_deterministically(message(subject, body))

    assert result is not None
    assert result.kind == expected
    assert result.confidence == 1.0


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            "Thanks for applying. We received your application. "
            "Your interview invitation is ready; choose a time for your interview.",
            "INTERVIEW",
        ),
        (
            "We are pleased to offer you the position. "
            "However, we will not be moving forward with your application.",
            "OFFER",
        ),
    ],
)
def test_multiple_lifecycle_signals_use_deterministic_precedence(body, expected):
    result = classify_deterministically(message("Application update", body))

    assert result is not None
    assert result.kind == expected


def test_linkedin_job_alert_extracts_known_job_url():
    result = classify_deterministically(
        message(
            "New jobs for you",
            "See jobs matching your preferences.",
            sender="jobalerts-noreply@linkedin.com",
            links=["https://www.linkedin.com/jobs/view/1234567890/"],
        )
    )

    assert result is not None
    assert result.kind == "JOB_ALERT"
    assert result.confidence == 1.0
    assert [job.url for job in result.jobs] == ["https://www.linkedin.com/jobs/view/1234567890/"]


def test_generic_job_alert_without_known_candidate_uses_semantic_extraction():
    job_url = "https://talentboard.example/jobs/frontend-42"
    gemini = FakeGemini(
        semantic_response(
            kind="JOB_ALERT",
            company="Acme",
            role_title="Frontend Engineer",
            job_urls=[job_url],
            jobs=[
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
            rationale="Job-board alert with one frontend opening.",
        )
    )

    result = classify_email(
        message(
            "Job alert",
            "A new frontend role matches your preferences.",
            sender="alerts@talentboard.example",
            links=[job_url],
        ),
        gemini,
    )

    assert result.kind == "JOB_ALERT"
    assert [job.url for job in result.jobs] == [job_url]
    assert len(gemini.calls) == 1


def test_generic_job_alert_with_no_safe_candidate_remains_job_alert():
    result = classify_email(
        message(
            "Job alert",
            "A new role matches your saved search.",
            sender="alerts@talentboard.example",
        ),
        FakeGemini(
            semantic_response(
                kind="JOB_ALERT",
                company="",
                role_title="",
                source_job_id=None,
                job_urls=[],
                jobs=[],
                rationale="Saved-search job alert.",
            )
        ),
    )

    assert result.kind == "JOB_ALERT"
    assert result.jobs == []


def test_linkedin_security_notification_uses_semantic_fallback_not_job_alert():
    gemini = FakeGemini(
        semantic_response(
            kind="IRRELEVANT",
            confidence=0.96,
            company="",
            role_title="",
            source_job_id=None,
            job_urls=[],
            jobs=[],
            rationale="Account security notification.",
        )
    )

    result = classify_email(
        message(
            "New sign-in to LinkedIn",
            "We noticed a new sign-in to your account.",
            sender="security-noreply@linkedin.com",
        ),
        gemini,
    )

    assert result.kind == "IRRELEVANT"
    assert len(gemini.calls) == 1


def test_platform_sender_and_url_require_hostname_boundaries():
    spoofed_sender = message("Account notification", "Your preferences changed.", sender="alerts@notlinkedin.com")
    spoofed_url = classify_deterministically(
        message(
            "Job alert",
            "A new role may interest you.",
            links=["https://notlinkedin.com/jobs/view/42"],
        )
    )

    assert not is_probably_job_related(spoofed_sender)
    assert spoofed_url is not None
    assert spoofed_url.jobs == []


@pytest.mark.parametrize(
    ("subject", "body"),
    [
        ("Special offer", "Save 20% on your next purchase."),
        ("Your position in the queue", "Your order is being processed."),
    ],
)
def test_bare_marketing_offer_or_position_is_not_probably_job_related(subject, body):
    assert is_probably_job_related(message(subject, body)) is False


def test_job_offer_phrase_is_probably_job_related():
    assert is_probably_job_related(
        message("Job offer", "We would like to discuss your job offer.")
    ) is True


@pytest.mark.parametrize(
    ("job", "expected"),
    [
        (ExtractedJob(source_platform="LinkedIn", url="https://www.linkedin.com/jobs/view/42/?utm_source=email"), "url:https://www.linkedin.com/jobs/view/42/"),
        (ExtractedJob(source_platform="Greenhouse", source_job_id="ABC-42"), "id:greenhouse:ABC-42"),
        (ExtractedJob(source_platform="email", company="Acme", title="Frontend Engineer", index=3), "fallback:acme|frontend engineer|3"),
    ],
)
def test_source_candidate_key_uses_url_id_then_normalized_fallback(job, expected):
    assert source_candidate_key(job) == expected


class FakeGemini:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[tuple[str, bool, dict | None]] = []
        self.kwargs: list[dict] = []

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
        self.calls.append((prompt, json_mode, json_schema))
        self.kwargs.append(
            {
                "purpose": purpose,
                "thinking_level": thinking_level,
                "max_output_tokens": max_output_tokens,
            }
        )
        return self.response


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


def semantic_response(**overrides) -> str:
    data = {
        "kind": "RECRUITER_CONTACT",
        "confidence": 0.96,
        "company": "Acme",
        "role_title": "Frontend Engineer",
        "source_job_id": None,
        "job_urls": ["https://jobs.acme.example/frontend"],
        "jobs": [
            {
                "source_platform": "acme",
                "source_job_id": None,
                "url": "https://jobs.acme.example/frontend",
                "company": "Acme",
                "title": "Frontend Engineer",
                "location": "Remote",
                "remote": True,
                "description": "Frontend role",
            }
        ],
        "rationale": "Recruiter outreach about a frontend role.",
    }
    data.update(overrides)
    import json

    return json.dumps(data)


def test_confident_semantic_recruiter_outreach_accepts_gemini_identified_job_url():
    gemini = FakeGemini(semantic_response())

    result = classify_email(
        message(
            "Hiring conversation",
            "I would like to discuss a Frontend Engineer role with you.",
            links=["https://jobs.acme.example/frontend"],
        ),
        gemini,
    )

    assert result.kind == "RECRUITER_CONTACT"
    assert result.confidence == 0.96
    assert result.job_urls == ["https://jobs.acme.example/frontend"]
    assert [job.url for job in result.jobs] == ["https://jobs.acme.example/frontend"]


def test_semantic_classification_requests_response_schema():
    gemini = FakeGemini(semantic_response(job_urls=[], jobs=[]))

    classify_email(
        message("Hiring conversation", "Can we discuss a frontend role?"),
        gemini,
    )

    _, json_mode, schema = gemini.calls[0]
    assert json_mode is True
    assert schema is not None
    assert schema["type"] == "OBJECT"
    assert "JOB_ALERT" in schema["properties"]["kind"]["enum"]
    assert schema["properties"]["jobs"]["type"] == "ARRAY"


def test_semantic_gmail_call_uses_bounded_resource_controls():
    gemini = FakeGemini(semantic_response(job_urls=[], jobs=[]))

    classify_email(
        message("Hiring conversation", "Can we discuss a frontend role?"),
        gemini,
    )

    call_kwargs = gemini.kwargs[0]
    assert call_kwargs["purpose"] == "gmail_semantic"
    assert call_kwargs["thinking_level"] == "minimal"
    assert call_kwargs["max_output_tokens"] == 5000
    _, json_mode, schema = gemini.calls[0]
    assert json_mode is True
    assert schema is not None


def test_semantic_prompt_caps_body_and_link_count():
    long_body = "x" * 20_000
    links = [f"https://example.com/jobs/{index}" for index in range(25)]
    gemini = FakeGemini(semantic_response(job_urls=[], jobs=[]))

    classify_email(
        message("Hiring conversation", long_body, links=links),
        gemini,
    )

    prompt = gemini.calls[0][0]
    email_data = json.loads(prompt.split("Email data:\n", 1)[1])
    assert len(email_data["body"]) <= 6_000
    assert len(email_data["links"]) <= 20


def test_semantic_job_optional_text_may_be_null():
    job_url = "https://www.linkedin.com/jobs/view/4461012343/"
    result = classify_email(
        message(
            "Frontend Engineer at Hired",
            "A new job matches your alert.",
            sender="jobalerts-noreply@linkedin.com",
            links=[job_url],
        ),
        FakeGemini(
            semantic_response(
                kind="JOB_ALERT",
                company="Hired",
                role_title="Frontend Engineer",
                source_job_id="4461012343",
                job_urls=[job_url],
                jobs=[
                    {
                        "source_platform": "linkedin",
                        "source_job_id": "4461012343",
                        "url": job_url,
                        "company": None,
                        "title": None,
                        "location": None,
                        "remote": None,
                        "description": None,
                    }
                ],
                rationale="LinkedIn job alert.",
            )
        ),
    )

    assert result.kind == "JOB_ALERT"
    assert len(result.jobs) == 1
    job = result.jobs[0]
    assert job.company == ""
    assert job.title == ""
    assert job.location == ""
    assert job.description == ""
    assert job.remote is None


def test_semantic_job_invalid_remote_type_still_fails():
    with pytest.raises(RuntimeError):
        classify_email(
            message("Hiring conversation", "Can we discuss this role?"),
            FakeGemini(
                semantic_response(
                    jobs=[
                        {
                            "source_platform": "acme",
                            "source_job_id": None,
                            "url": "https://jobs.acme.example/frontend",
                            "company": "Acme",
                            "title": "Frontend Engineer",
                            "location": "Remote",
                            "remote": "yes",
                            "description": "Frontend role",
                        }
                    ]
                )
            ),
        )


def test_semantic_generic_urls_not_in_email_are_dropped():
    result = classify_email(
        message("Hiring conversation", "Can we discuss a role?"),
        FakeGemini(semantic_response()),
    )

    assert result.kind == "RECRUITER_CONTACT"
    assert result.job_urls == []
    assert result.jobs[0].url == ""


def test_semantic_classification_retains_known_email_job_urls():
    result = classify_email(
        message(
            "Hiring conversation",
            "Can we discuss a role?",
            links=["https://www.linkedin.com/jobs/view/1234567890/"],
        ),
        FakeGemini(semantic_response(job_urls=[], jobs=[])),
    )

    assert result.kind == "RECRUITER_CONTACT"
    assert result.job_urls == ["https://www.linkedin.com/jobs/view/1234567890/"]
    assert [job.url for job in result.jobs] == ["https://www.linkedin.com/jobs/view/1234567890/"]


def test_conflicting_semantic_job_urls_and_jobs_do_not_force_review():
    result = classify_email(
        message(
            "Hiring conversation",
            "Can we discuss a role?",
            links=["https://jobs.acme.example/frontend"],
        ),
        FakeGemini(semantic_response(jobs=[])),
    )

    assert result.kind == "RECRUITER_CONTACT"
    assert result.job_urls == ["https://jobs.acme.example/frontend"]


def test_semantic_irrelevant_discards_harmless_extracted_metadata():
    result = classify_email(
        message("Application special offer", "A consumer promotion unrelated to jobs."),
        FakeGemini(
            semantic_response(
                kind="IRRELEVANT",
                confidence=0.98,
                company="Example Brand",
                role_title="",
                source_job_id=None,
                job_urls=[],
                jobs=[],
                rationale="Consumer marketing email.",
            )
        ),
    )

    assert result.kind == "IRRELEVANT"
    assert result.company == ""
    assert result.role_title == ""
    assert result.job_urls == []
    assert result.jobs == []


def test_semantic_optional_extraction_fields_may_be_null_or_missing():
    import json

    response = json.dumps(
        {
            "kind": "INTERVIEW",
            "confidence": 0.96,
            "company": None,
            "role_title": None,
            "rationale": "Scheduling an interview.",
        }
    )

    result = classify_email(
        message("Hiring process", "Can we find a time for an interview?"),
        FakeGemini(response),
    )

    assert result.kind == "INTERVIEW"
    assert result.company == ""
    assert result.role_title == ""
    assert result.source_job_id is None
    assert result.job_urls == []
    assert result.jobs == []


def test_low_confidence_semantic_lifecycle_becomes_review_needed():
    gemini = FakeGemini(
        semantic_response(
            kind="INTERVIEW",
            confidence=0.72,
            company="Acme",
            role_title="Frontend Engineer",
            source_job_id=None,
            job_urls=[],
            jobs=[],
            rationale="possibly scheduling",
        )
    )

    result = classify_email(message("Hiring process", "Can we find time to chat?"), gemini)

    assert result.kind == "REVIEW_NEEDED"


def test_malformed_semantic_json_raises_technical_failure():
    with pytest.raises(RuntimeError):
        classify_email(
            message("Hiring conversation", "Can we discuss this role?"),
            FakeGemini("not json"),
        )


def test_unsupported_semantic_kind_raises_technical_failure():
    with pytest.raises(RuntimeError):
        classify_email(
            message("Hiring conversation", "Can we discuss this role?"),
            FakeGemini(semantic_response(kind="FOLLOW_UP")),
        )


def test_gemini_failure_raises_technical_failure():
    with pytest.raises(RuntimeError):
        classify_email(
            message("Hiring conversation", "Can we discuss this role?"),
            FailingGemini(),
        )


def test_low_confidence_semantic_irrelevant_becomes_review_needed():
    result = classify_email(
        message("Hiring conversation", "Can we discuss this?"),
        FakeGemini(
            semantic_response(
                kind="IRRELEVANT",
                confidence=0.55,
                company="",
                role_title="",
                source_job_id=None,
                job_urls=[],
                jobs=[],
                rationale="possibly unrelated",
            )
        ),
    )

    assert result.kind == "REVIEW_NEEDED"


def test_semantic_prompt_includes_only_allowed_message_content_and_body_prefix():
    body_prefix = "body-prefix " + "x" * (20_000 - len("body-prefix "))
    gemini = FakeGemini(semantic_response(job_urls=[], jobs=[]))

    result = classify_email(
        message(
            "Hiring conversation",
            body_prefix + " SECRET_AFTER_LIMIT",
            sender="updates@company.example",
            links=["https://example.com/jobs/42"],
        ),
        gemini,
    )

    assert result.kind == "RECRUITER_CONTACT"
    assert gemini.calls[0][1] is True
    prompt = gemini.calls[0][0]
    assert "updates@company.example" in prompt
    assert "Hiring conversation" in prompt
    assert "body-prefix" in prompt
    assert "https://example.com/jobs/42" in prompt
    assert "SECRET_AFTER_LIMIT" not in prompt

    
def test_semantic_gmail_parses_large_structured_job_alert_response():
    jobs = [
        {
            "source_platform": "linkedin",
            "source_job_id": str(1000 + index),
            "url": f"https://www.linkedin.com/jobs/view/{1000 + index}/",
            "company": f"Company {index}",
            "title": f"Senior Frontend Engineer {index}",
            "location": "Berlin, Germany",
            "remote": True,
            "description": (
                "Senior frontend engineering role focused on React, TypeScript, "
                "architecture, performance, testing, and cross-functional product "
                "development. " * 8
            ),
        }
        for index in range(5)
    ]

    job_urls = [job["url"] for job in jobs]

    gemini = FakeGemini(
        semantic_response(
            kind="JOB_ALERT",
            company="",
            role_title="",
            source_job_id=None,
            job_urls=job_urls,
            jobs=jobs,
            rationale="Job alert containing multiple relevant frontend roles.",
        )
    )

    result = classify_email(
        message(
            "Hiring opportunities for you",
            "Several frontend roles may match your preferences.",
            links=job_urls,
        ),
        gemini,
    )

    assert len(gemini.calls) == 1
    assert result.kind == "JOB_ALERT"
    assert len(result.jobs) == 5
    assert [job.url for job in result.jobs] == job_urls
    assert all(job.description for job in result.jobs)