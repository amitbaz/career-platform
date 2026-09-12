import json
from datetime import date

import pytest

from job_hunter.cover_letter import cover_letter_output_dir, generate_cover_letter, generate_cover_letter_on_demand
from job_hunter.ai.gemini import AIIncompleteResponse
from job_hunter.ai.usage import AIBudgetExceeded
from job_hunter.models import (
    AIQuotaSettings,
    CandidateContext,
    CandidatePreferences,
    Evaluation,
    Job,
    Material,
    SearchPolicy,
    Settings,
)


class FakeGemini:
    def __init__(self):
        self.text = ""
        self.prompts = []
        self.read_timeouts = []

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
        read_timeout=None,
    ):
        self.prompts.append((prompt, purpose, thinking_level, max_output_tokens, json_mode))
        self.read_timeouts.append(read_timeout)
        return self.text


class TruncatingThenSucceedingGemini(FakeGemini):
    """Raises AIIncompleteResponse on the first call, then succeeds."""

    def __init__(self, final_text):
        super().__init__()
        self._final_text = final_text
        self.calls = 0

    def generate_text(self, prompt, **kwargs):
        self.calls += 1
        self.prompts.append(
            (prompt, kwargs.get("purpose"), kwargs.get("thinking_level"), kwargs.get("max_output_tokens"), kwargs.get("json_mode", False))
        )
        if self.calls == 1:
            raise AIIncompleteResponse("MAX_TOKENS")
        return self._final_text


class AlwaysTruncatingGemini(FakeGemini):
    """Raises AIIncompleteResponse on every call."""

    def __init__(self):
        super().__init__()
        self.calls = 0

    def generate_text(self, prompt, **kwargs):
        self.calls += 1
        self.prompts.append(
            (prompt, kwargs.get("purpose"), kwargs.get("thinking_level"), kwargs.get("max_output_tokens"), kwargs.get("json_mode", False))
        )
        raise AIIncompleteResponse("MAX_TOKENS")


@pytest.fixture
def fake_gemini():
    return FakeGemini()


@pytest.fixture
def job():
    return Job(source="ashby", title="Senior Product Engineer", company="Acme", description="React TypeScript remote")


@pytest.fixture
def evaluation():
    return Evaluation(
        job_id=1,
        total_score=89,
        scores={},
        decision="high_priority",
        hard_blockers=[],
        strengths=["React expertise"],
        gaps=["No Rust experience"],
        salary_note="",
        location_note="",
        rationale="",
        model="gemini-2.5-flash-lite",
    )


@pytest.fixture
def context():
    return CandidateContext(
        preferences=CandidatePreferences(
            preferred_roles=["Senior Product Engineer"],
            preferred_seniority=["senior"],
            must_have_signals=["React"],
            nice_to_have_signals=["TypeScript"],
            preferred_locations=["Germany"],
            avoid_signals=[],
            summary="Senior frontend/product engineer.",
        ),
        technical_skills=["React", "TypeScript"],
        architecture_evidence=[],
        leadership_ownership=[],
        agentic_ai_evidence=[],
        product_domain_evidence=[],
        location_language_facts=[],
        career_direction=[],
        company_environment=[],
        career_evidence=["Senior engineer at Acme for 5 years, shipped a React design system"],
        evaluation_summary="Strong senior product engineer.",
    )


def test_generate_cover_letter_returns_stripped_text(fake_gemini, job, evaluation, context):
    fake_gemini.text = "  Dear Hiring Team,\n\nI want to join Acme as Senior Product Engineer.\n\nBest,\nAmit  "
    result = generate_cover_letter(job, evaluation, context, "template", fake_gemini, date(2026, 8, 30))
    assert result.startswith("Dear Hiring Team,")
    assert result.endswith("Amit")


def test_cover_letter_rejects_unreplaced_placeholders(fake_gemini, job, evaluation, context):
    fake_gemini.text = "Dear Hiring Team, I want [Position] at [Company]."
    with pytest.raises(ValueError):
        generate_cover_letter(job, evaluation, context, "template", fake_gemini, date(2026, 8, 30))


def test_cover_letter_rejects_empty_output(fake_gemini, job, evaluation, context):
    fake_gemini.text = "   "
    with pytest.raises(ValueError):
        generate_cover_letter(job, evaluation, context, "template", fake_gemini, date(2026, 8, 30))


def test_cover_letter_prompt_includes_job_and_date(fake_gemini, job, evaluation, context):
    fake_gemini.text = "Dear Hiring Team, I am excited to apply."
    generate_cover_letter(job, evaluation, context, "template", fake_gemini, date(2026, 8, 30))
    prompt, _purpose, _thinking, _max_tokens, _json_mode = fake_gemini.prompts[0]
    assert "Acme" in prompt
    assert "Senior Product Engineer" in prompt
    assert "2026-08-30" in prompt


def test_cover_letter_prompt_uses_context_career_evidence_and_evaluation(fake_gemini, job, evaluation, context):
    fake_gemini.text = "Dear Hiring Team, I am excited to apply."
    generate_cover_letter(job, evaluation, context, "template", fake_gemini, date(2026, 8, 30))
    prompt, _purpose, _thinking, _max_tokens, _json_mode = fake_gemini.prompts[0]

    assert "Senior engineer at Acme for 5 years, shipped a React design system" in prompt
    assert "React expertise" in prompt
    assert "No Rust experience" in prompt
    assert "template" in prompt


def test_cover_letter_prompt_forbids_inventing_facts(fake_gemini, job, evaluation, context):
    fake_gemini.text = "Dear Hiring Team, I am excited to apply."
    generate_cover_letter(job, evaluation, context, "template", fake_gemini, date(2026, 8, 30))
    prompt, _purpose, _thinking, _max_tokens, _json_mode = fake_gemini.prompts[0]

    assert "NEVER invent facts about the candidate" in prompt


def test_cover_letter_uses_expected_resource_controls(fake_gemini, job, evaluation, context):
    fake_gemini.text = "Dear Hiring Team, I am excited to apply."
    generate_cover_letter(job, evaluation, context, "template", fake_gemini, date(2026, 8, 30))
    _prompt, purpose, thinking_level, max_output_tokens, _json_mode = fake_gemini.prompts[0]

    assert purpose == "cover_letter"
    assert thinking_level == "low"
    assert max_output_tokens == 800


def test_cover_letter_recovers_from_max_tokens_with_larger_budget(job, evaluation, context):
    gemini = TruncatingThenSucceedingGemini("Dear Hiring Team, I am excited to apply.")

    result = generate_cover_letter(job, evaluation, context, "template", gemini, date(2026, 8, 30))

    assert result == "Dear Hiring Team, I am excited to apply."
    assert gemini.calls == 2
    first_budget = gemini.prompts[0][3]
    second_budget = gemini.prompts[1][3]
    assert first_budget == 800
    assert second_budget > first_budget


def test_cover_letter_repeated_max_tokens_raises_cleanly(job, evaluation, context):
    gemini = AlwaysTruncatingGemini()

    with pytest.raises(AIIncompleteResponse):
        generate_cover_letter(job, evaluation, context, "template", gemini, date(2026, 8, 30))

    # Bounded: exactly two attempts, never unbounded retrying.
    assert gemini.calls == 2


def test_cover_letter_generation_gets_a_longer_read_budget(
    fake_gemini, job, evaluation, context
):
    """A cover letter must not die on the default 25s read timeout.

    It is the longest single generation this app makes, and a slow reply
    means the model is still writing rather than that something is broken.
    Every other call keeps the short default, where a slow reply really is
    a fault worth failing fast on.
    """
    fake_gemini.text = "Dear team,\n\nI am writing about the role.\n\nRegards"

    generate_cover_letter(job, evaluation, context, "template", fake_gemini, date(2026, 9, 7))

    assert fake_gemini.read_timeouts == [120]


# --- generate_cover_letter_on_demand (moved from the retired pipeline.py, #189) ---


class OnDemandFakeGemini:
    """A fuller fake than `FakeGemini` above: on-demand generation goes through
    `get_candidate_context` first, so it must answer that purpose too.
    """

    def __init__(self):
        self.model = "gemini-test"
        self.preference_calls = 0
        self.eval_calls = 0
        self.cover_letter_calls = 0

    def generate_text(self, prompt, *, call_class, purpose=None, thinking_level=None,
                       max_output_tokens=None, json_mode=False, json_schema=None,
                       max_attempts=1, read_timeout=None):
        if purpose == "candidate_context":
            self.preference_calls += 1
            return json.dumps({
                "preferences": {
                    "preferred_roles": ["Senior Product Engineer"],
                    "preferred_seniority": ["senior"],
                    "must_have_signals": ["React"],
                    "nice_to_have_signals": ["TypeScript"],
                    "preferred_locations": ["Germany"],
                    "avoid_signals": [],
                    "summary": "Remote product-oriented frontend engineer.",
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
                "evaluation_summary": "Remote product-oriented frontend engineer.",
            })
        self.cover_letter_calls += 1
        return "Dear Hiring Team,\n\nI would love to join Acme as Senior Product Engineer.\n\nBest,\nAmit"


class RaisingGemini(OnDemandFakeGemini):
    """Raises `exception` the first time `generate_text` is called for `raise_on_purpose`."""

    def __init__(self, *, raise_on_purpose, exception):
        super().__init__()
        self._raise_on_purpose = raise_on_purpose
        self._exception = exception

    def generate_text(self, prompt, **kwargs):
        if kwargs.get("purpose") == self._raise_on_purpose:
            raise self._exception
        return super().generate_text(prompt, **kwargs)


def _budget_exceeded():
    return AIBudgetExceeded("Gemini gemini-test budget exceeded for purpose 'job_evaluation'")


class FakeTelegram:
    def __init__(self):
        self.messages = []
        self.documents = []

    def send_message(self, text):
        self.messages.append(text)
        return "msg-1"

    def send_document(self, path, caption):
        self.documents.append((path, caption))
        return "doc-1"


def _job(**overrides):
    defaults = dict(
        source="ashby",
        source_job_id="job-1",
        title="Senior Product Engineer",
        company="Acme",
        location="Remote",
        remote=True,
        description="React TypeScript remote role",
        content_confidence="official_ats",
    )
    defaults.update(overrides)
    return Job(**defaults)


def _on_demand_evaluation(job_id, *, decision="high_priority", total_score=90):
    return Evaluation(
        job_id=job_id,
        total_score=total_score,
        scores={},
        decision=decision,
        hard_blockers=[],
        strengths=["React expertise"],
        gaps=[],
        salary_note="Not disclosed",
        location_note="Remote EU friendly",
        rationale="Strong fit",
        model="gemini-test",
    )


def _record_application_history(store, job_id):
    """Give the intended merge survivor history stronger than a card delivery."""
    store.save_application_event(
        job_id=job_id,
        event_type="INTERVIEW",
        occurred_at="2026-09-08T10:00:00+00:00",
        source_message_id=f"application-{job_id}",
        source_thread_id=None,
        confidence=1.0,
        company="Acme Survivor",
        role_title="Senior Product Engineer",
        rationale="interview invitation",
    )


@pytest.fixture
def on_demand_policy():
    return SearchPolicy(
        target_titles=["senior product engineer"],
        positive_keywords=["react"],
        blocked_title_keywords=["junior"],
        salary_floor_eur=90000,
        thresholds={"package": 75, "possible": 65},
        max_jobs_per_run=100,
    )


@pytest.fixture
def on_demand_settings(on_demand_policy):
    return Settings(
        ai_api_key="key",
        candidate_profile="profile",
        cover_letter_template="template",
        timezone="Europe/Berlin",
        scheduled_hour=9,
        policy=on_demand_policy,
        ai_quota=AIQuotaSettings(rpm=10, tpm=250000, rpd=500),
        dry_run=False,
        telegram_bot_token="token",
        telegram_chat_id="chat",
    )


def test_generate_cover_letter_on_demand_calls_gemini_when_no_material(store, on_demand_settings):
    job = _job()
    job_id, _, _ = store.upsert_job(job)
    store.save_evaluation(job_id, _on_demand_evaluation(job_id))
    gemini = OnDemandFakeGemini()
    telegram = FakeTelegram()

    delivered = generate_cover_letter_on_demand(
        on_demand_settings, job_id, store=store, ai=gemini, telegram=telegram
    )

    assert delivered is True
    assert gemini.cover_letter_calls == 1
    assert len(telegram.documents) == 1
    assert store.get_material(job_id) is not None
    assert store.has_delivery(job_id, "telegram_document")


def test_generate_cover_letter_on_demand_resends_without_regenerating(store, on_demand_settings):
    job = _job()
    job_id, _, _ = store.upsert_job(job)
    store.save_evaluation(job_id, _on_demand_evaluation(job_id))
    store.save_material(job_id, Material(job_id=job_id, cover_letter_text="Existing letter text"))
    gemini = OnDemandFakeGemini()
    telegram = FakeTelegram()

    delivered = generate_cover_letter_on_demand(
        on_demand_settings, job_id, store=store, ai=gemini, telegram=telegram
    )

    assert delivered is True
    assert gemini.cover_letter_calls == 0
    assert len(telegram.documents) == 1


def test_generate_cover_letter_on_demand_missing_job_returns_false(store, on_demand_settings):
    gemini = OnDemandFakeGemini()
    telegram = FakeTelegram()

    delivered = generate_cover_letter_on_demand(
        on_demand_settings,
        "00000000-0000-0000-0000-000000000999",
        store=store,
        ai=gemini,
        telegram=telegram,
    )

    assert delivered is False
    assert len(telegram.documents) == 0


def test_generate_cover_letter_on_demand_follows_a_job_merged_since_delivery(
    store, on_demand_settings
):
    """A card outlives the job it names; the button must still produce a letter.

    Cards sit in Telegram for days and every discovery run merges duplicates
    away, so the id a card carries can name a row that no longer exists by the
    time it is tapped (#146). The redirect written by the merge says which job
    replaced it, and the letter belongs to that one.
    """
    duplicate_id, _, _ = store.upsert_job(
        _job(source_job_id="duplicate", company="Acme Duplicate")
    )
    survivor_id, _, _ = store.upsert_job(
        _job(source_job_id="survivor", company="Acme Survivor")
    )
    store.save_evaluation(survivor_id, _on_demand_evaluation(survivor_id))
    _record_application_history(store, survivor_id)
    store.mark_delivered(duplicate_id, "telegram_message", "card-message")
    assert store.merge_jobs(survivor_id, duplicate_id) == survivor_id
    gemini = OnDemandFakeGemini()
    telegram = FakeTelegram()

    delivered = generate_cover_letter_on_demand(
        on_demand_settings, duplicate_id, store=store, ai=gemini, telegram=telegram
    )

    assert delivered is True
    assert gemini.cover_letter_calls == 1
    assert len(telegram.documents) == 1
    # The letter and its delivery belong to the surviving job, not the dead id.
    assert store.get_material(survivor_id) is not None
    assert store.has_delivery(survivor_id, "telegram_message")
    assert store.has_delivery(survivor_id, "telegram_document")


def test_generate_cover_letter_on_demand_resends_a_merged_job_letter_for_free(
    store, on_demand_settings
):
    """The free-resend path still applies once the merge has been followed."""
    duplicate_id, _, _ = store.upsert_job(
        _job(source_job_id="duplicate", company="Acme Duplicate")
    )
    survivor_id, _, _ = store.upsert_job(
        _job(source_job_id="survivor", company="Acme Survivor")
    )
    store.save_evaluation(survivor_id, _on_demand_evaluation(survivor_id))
    store.save_material(
        survivor_id,
        Material(job_id=survivor_id, cover_letter_text="Existing letter text"),
    )
    _record_application_history(store, survivor_id)
    store.mark_delivered(duplicate_id, "telegram_message", "card-message")
    assert store.merge_jobs(survivor_id, duplicate_id) == survivor_id
    gemini = OnDemandFakeGemini()
    telegram = FakeTelegram()

    delivered = generate_cover_letter_on_demand(
        on_demand_settings, duplicate_id, store=store, ai=gemini, telegram=telegram
    )

    assert delivered is True
    assert (gemini.preference_calls, gemini.eval_calls, gemini.cover_letter_calls) == (0, 0, 0)
    assert len(telegram.documents) == 1


def test_generate_cover_letter_on_demand_tells_the_user_when_the_job_is_gone(store, on_demand_settings):
    """An id that is neither live nor merged gets a reply, not silence.

    A button that does nothing is indistinguishable from a broken bot, so the
    dead-id branch says so the way the quota and failure branches already do.
    """
    gemini = OnDemandFakeGemini()
    telegram = FakeTelegram()

    delivered = generate_cover_letter_on_demand(
        on_demand_settings,
        "00000000-0000-0000-0000-000000000999",
        store=store,
        ai=gemini,
        telegram=telegram,
    )

    assert delivered is False
    assert (gemini.preference_calls, gemini.eval_calls, gemini.cover_letter_calls) == (0, 0, 0)
    assert telegram.documents == []
    assert len(telegram.messages) == 1
    assert "no longer available" in telegram.messages[0].lower()


def test_generate_cover_letter_on_demand_notifies_on_quota_block(store, on_demand_settings):
    job = _job()
    job_id, _, _ = store.upsert_job(job)
    store.save_evaluation(job_id, _on_demand_evaluation(job_id))
    gemini = RaisingGemini(raise_on_purpose="cover_letter", exception=_budget_exceeded())
    telegram = FakeTelegram()

    delivered = generate_cover_letter_on_demand(
        on_demand_settings, job_id, store=store, ai=gemini, telegram=telegram
    )

    assert delivered is False
    assert len(telegram.documents) == 0
    assert len(telegram.messages) == 1
    assert "quota" in telegram.messages[0].lower()
