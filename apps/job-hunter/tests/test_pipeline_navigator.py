import json

import pytest

from job_hunter.models import AIQuotaSettings, Job, SearchPolicy, Settings
from job_hunter.pipeline import run_pipeline
from job_hunter.telegram_navigation import parse_callback


@pytest.fixture(autouse=True)
def _use_default_search_profile(default_search_profile):
    """Opt this file into `conftest.default_search_profile` (#187, #188):
    every test here runs `run_pipeline` end-to-end and needs
    `job_hunter_match_jobs` to have something to rank against.
    """


class FakeGemini:
    model = "gemini-test"

    def generate_text(
        self,
        prompt,
        *,
        call_class,
        purpose=None,
        thinking_level=None,
        max_output_tokens=None,
        json_mode=False,
        json_schema=None,
        max_attempts=1,
    ):
        if purpose == "candidate_context":
            return json.dumps(
                {
                    "preferences": {
                        "preferred_roles": ["Senior Product Engineer"],
                        "preferred_seniority": ["senior"],
                        "must_have_signals": ["React"],
                        "nice_to_have_signals": ["TypeScript"],
                        "preferred_locations": ["Germany"],
                        "avoid_signals": [],
                        "summary": "Senior frontend/product engineer.",
                    },
                    "technical_skills": [],
                    "architecture_evidence": [],
                    "leadership_ownership": [],
                    "agentic_ai_evidence": [],
                    "product_domain_evidence": [],
                    "location_language_facts": [],
                    "career_direction": [],
                    "company_environment": [],
                    "career_evidence": [],
                    "evaluation_summary": "Senior frontend/product engineer.",
                }
            )
        if purpose == "job_facets":
            # Scoring is fed the posting's facets (#126), so a fake that
            # cannot answer this purpose cannot get a job scored at all.
            return json.dumps(
                {
                    "seniority": "senior",
                    "remote_policy": "remote",
                    "relocation_policy": "unknown",
                    "hiring_regions": ["europe"],
                    "stack": ["react"],
                    "compensation": {
                        "disclosed": False,
                        "currency": "",
                        "minimum": None,
                        "maximum": None,
                        "period": "",
                    },
                    "requirements": [
                        {"requirement": "React", "depth": "experience", "kind": "must_have"}
                    ],
                }
            )
        if json_mode:
            return json.dumps(
                {
                    "scores": {
                        "role_seniority": 25,
                        "technical": 20,
                        "product_architecture": 15,
                        "career_direction": 8,
                        "location_language": 7,
                        "company_environment": 5,
                    },
                    "total_score": 80,
                    "hard_blockers": [],
                    "strengths": ["React"],
                    "gaps": [],
                    "salary_note": "",
                    "location_note": "",
                    "decision": "possible_match",
                    "rationale": "Good fit",
                    "requirements": {
                        "must_have": [
                            {"requirement": "React", "candidate_support": "supported"}
                        ],
                        "preferred": [],
                    },
                }
            )
        raise AssertionError("cover letter should not be generated for possible_match")


class FakeSource:
    def __init__(self, jobs):
        self.jobs = jobs

    def discover(self):
        return self.jobs


class NavigatorTelegram:
    def __init__(self, *, card_result="nav-msg-1", message_result="msg-1"):
        self.cards = []
        self.messages = []
        self.documents = []
        self.events = []
        self.card_result = card_result
        self.message_result = message_result

    def send_job_card(self, text, keyboard):
        self.events.append("card")
        self.cards.append((text, keyboard))
        return self.card_result

    def send_message(self, text):
        self.events.append("message")
        self.messages.append(text)
        return self.message_result

    def send_document(self, path, caption):
        self.events.append("document")
        self.documents.append((path, caption))
        return "doc-1"


def _settings():
    return Settings(
        ai_api_key="key",
        candidate_profile="profile",
        cover_letter_template="template",
        timezone="Europe/Berlin",
        scheduled_hour=9,
        policy=SearchPolicy(
            target_titles=["senior product engineer"],
            positive_keywords=["react"],
            blocked_title_keywords=["junior"],
            salary_floor_eur=90000,
            thresholds={"package": 75, "possible": 65},
            max_jobs_per_run=35,
        ),
        ai_quota=AIQuotaSettings(rpm=10, tpm=250000, rpd=500),
        dry_run=False,
        telegram_bot_token="token",
        telegram_chat_id="chat",
    )


def _job(source_job_id, company, location):
    return Job(
        source="ashby",
        source_job_id=source_job_id,
        title="Senior Product Engineer",
        company=company,
        location=location,
        url=f"https://example.test/{source_job_id}",
        remote=True,
        description="React TypeScript product engineering role",
        content_confidence="official_ats",
    )


def _seed_pending_activity(store, message_id="gmail-review-1"):
    store.record_gmail_message(
        message_id=message_id,
        thread_id=f"thread-{message_id}",
        sender="recruiter@example.com",
        subject="Montash role",
        occurred_at="2026-09-01T10:00:00+00:00",
        classification="REVIEW_NEEDED",
        confidence=1.0,
        rationale="deterministic recruiter template",
    )
    return store.save_application_event(
        job_id=None,
        event_type="RECRUITER_CONTACT",
        occurred_at="2026-09-01T10:00:00+00:00",
        source_message_id=message_id,
        source_thread_id=f"thread-{message_id}",
        confidence=1.0,
        company="Montash",
        role_title="Senior Frontend Engineer",
        rationale="deterministic recruiter template",
    )


def _session_id_from_keyboard(keyboard):
    parsed = parse_callback(keyboard[0][-1]["callback_data"])
    assert parsed is not None
    return parsed[1]


def test_pipeline_sends_one_sorted_navigator_and_persists_location(store):
    settings = _settings()
    telegram = NavigatorTelegram()
    jobs = [
        _job("2", "Beta", "Remote EU"),
        _job("1", "Acme", "Berlin"),
    ]

    run_pipeline(
        settings,
        sources=[FakeSource(jobs)],
        store=store,
        ai=FakeGemini(),
        telegram=telegram,
    )

    assert len(telegram.cards) == 1
    text, keyboard = telegram.cards[0]
    assert "Company: Acme" in text
    assert "Location: Berlin" in text
    assert "Match: 80%" in text
    assert "1 / 2" == keyboard[1][1]["text"]

    session = store.get_navigation_session(_session_id_from_keyboard(keyboard))
    assert session is not None
    assert session.telegram_message_id == "nav-msg-1"
    assert [card.company for card in session.cards] == ["Acme", "Beta"]
    assert [card.location for card in session.cards] == ["Berlin", "Remote EU"]

    for job in jobs:
        job_id, _, _ = store.upsert_job(job)
        assert store.has_delivery(job_id, "telegram_message") is True


def test_pipeline_failed_navigator_send_keeps_jobs_pending(store):
    settings = _settings()
    telegram = NavigatorTelegram(card_result=None)
    job = _job("1", "Acme", "Berlin")

    run_pipeline(
        settings,
        sources=[FakeSource([job])],
        store=store,
        ai=FakeGemini(),
        telegram=telegram,
    )

    job_id, _, _ = store.upsert_job(job)
    assert len(telegram.cards) == 1
    assert store.has_delivery(job_id, "telegram_message") is False
    assert job_id in store.pending_delivery_job_ids(settings.policy.match_score_floor)


def test_pipeline_no_deliverable_jobs_still_sends_nothing_when_the_run_finds_none(store):
    """Renamed from ...with_no_deliverable_jobs...: the fixture it used
    ("Junior QA Tester") is no longer an example of a job with nothing to
    deliver, since #243 removes the profession-title gate that used to keep
    it unscored -- it now scores and delivers like any other job (see
    test_pipeline_no_longer_prefilters_non_matching_jobs). This test keeps
    the *shape* being proven (a run that discovers nothing sends nothing at
    all) with a fixture that actually has nothing to deliver: no sources."""
    settings = _settings()
    telegram = NavigatorTelegram()

    run_pipeline(
        settings,
        sources=[FakeSource([])],
        store=store,
        ai=FakeGemini(),
        telegram=telegram,
    )

    assert telegram.cards == []
    assert telegram.messages == []


def test_pipeline_sends_gmail_activity_before_job_navigator(store):
    settings = _settings()
    event_id = _seed_pending_activity(store)
    telegram = NavigatorTelegram()

    run_pipeline(
        settings,
        sources=[FakeSource([_job("1", "Acme", "Berlin")])],
        store=store,
        ai=FakeGemini(),
        telegram=telegram,
    )

    assert telegram.events == ["message", "card"]
    assert len(telegram.messages) == 1
    assert "Gmail activity I couldn't link" in telegram.messages[0]
    assert "Montash — Senior Frontend Engineer" in telegram.messages[0]
    assert "deterministic recruiter template" not in telegram.messages[0]
    assert "https://mail.google.com/mail/u/0/#all/thread-gmail-review-1" in telegram.messages[0]
    assert store.pending_review_events() == []
    deliveries = store.client.select(
        "job_hunter_review_deliveries",
        params={"event_id": f"eq.{event_id}", "select": "telegram_message_id"},
    )
    assert len(deliveries) == 1
    assert deliveries[0]["telegram_message_id"] == "msg-1"


def test_pipeline_failed_gmail_activity_send_keeps_review_pending(store):
    settings = _settings()
    event_id = _seed_pending_activity(store)
    telegram = NavigatorTelegram(message_result=None)

    run_pipeline(
        settings,
        sources=[FakeSource([])],
        store=store,
        ai=FakeGemini(),
        telegram=telegram,
    )

    assert telegram.events == ["message"]
    pending = store.pending_review_events()
    assert [row["id"] for row in pending] == [event_id]
    assert store.client.select(
        "job_hunter_review_deliveries",
        params={"event_id": f"eq.{event_id}", "select": "event_id"},
    ) == []
