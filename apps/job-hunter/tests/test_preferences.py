import json

import pytest

from job_hunter.gemini_usage import GeminiBudgetExceeded, GeminiQuotaPaused
from job_hunter.models import CandidatePreferences, SearchPolicy
from job_hunter.preferences import extract_candidate_preferences
from job_hunter.store import JobStore


class FakeGemini:
    def __init__(self, response=None, exception=None):
        self.model = "gemini-test"
        self.response = response
        self.exception = exception
        self.calls = []

    def generate_text(
        self,
        prompt,
        *,
        purpose=None,
        thinking_level=None,
        max_output_tokens=None,
        json_mode=False,
        json_schema=None,
    ):
        self.calls.append(
            {
                "prompt": prompt,
                "purpose": purpose,
                "thinking_level": thinking_level,
                "max_output_tokens": max_output_tokens,
                "json_mode": json_mode,
                "json_schema": json_schema,
            }
        )
        if self.exception is not None:
            raise self.exception
        return self.response


def make_policy() -> SearchPolicy:
    return SearchPolicy(
        target_titles=["senior product engineer", "staff frontend engineer"],
        positive_keywords=["react", "typescript", "system design"],
        blocked_title_keywords=["junior"],
        salary_floor_eur=90000,
        thresholds={"package": 75, "possible": 65},
        role_families=["frontend engineering", "product engineering"],
        blocked_profession_title_phrases=["product manager", "designer"],
    )


def full_context_payload(**preference_overrides) -> str:
    preferences = {
        "preferred_roles": ["Senior Product Engineer", "Staff Frontend Engineer"],
        "preferred_seniority": ["senior", "staff"],
        "must_have_signals": ["React", "TypeScript"],
        "nice_to_have_signals": ["system design", "mentorship"],
        "preferred_locations": ["Germany", "EU"],
        "avoid_signals": ["on-site", "manager"],
        "summary": "Senior frontend/product engineer focused on remote EU roles.",
    }
    preferences.update(preference_overrides)
    return json.dumps(
        {
            "preferences": preferences,
            "technical_skills": [],
            "architecture_evidence": [],
            "leadership_ownership": [],
            "agentic_ai_evidence": [],
            "product_domain_evidence": [],
            "location_language_facts": [],
            "career_direction": [],
            "company_environment": [],
            "career_evidence": [],
            "evaluation_summary": "Senior full-stack engineer.",
        }
    )


def test_extract_candidate_preferences_returns_valid_json_fields():
    store = JobStore(":memory:")
    gemini = FakeGemini(full_context_payload())

    preferences = extract_candidate_preferences("candidate profile text", gemini, make_policy(), store)

    assert preferences == CandidatePreferences(
        preferred_roles=["Senior Product Engineer", "Staff Frontend Engineer"],
        preferred_seniority=["senior", "staff"],
        must_have_signals=["React", "TypeScript"],
        nice_to_have_signals=["system design", "mentorship"],
        preferred_locations=["Germany", "EU"],
        avoid_signals=["on-site", "manager"],
        summary="Senior frontend/product engineer focused on remote EU roles.",
    )
    assert len(gemini.calls) == 1
    assert "candidate profile text" in gemini.calls[0]["prompt"]


def test_extract_candidate_preferences_falls_back_on_malformed_json():
    store = JobStore(":memory:")
    policy = make_policy()
    gemini = FakeGemini('{"preferred_roles": "not-a-list"}')

    preferences = extract_candidate_preferences("candidate profile text", gemini, policy, store)

    assert preferences == CandidatePreferences(
        preferred_roles=["frontend engineering", "product engineering", "senior product engineer", "staff frontend engineer"],
        preferred_seniority=["senior", "staff"],
        must_have_signals=["react", "typescript", "system design"],
        nice_to_have_signals=[],
        preferred_locations=[],
        avoid_signals=["product manager", "designer"],
        summary="Fallback preferences derived from search policy.",
    )


def test_extract_candidate_preferences_empty_profile_uses_policy_fallback():
    store = JobStore(":memory:")
    policy = make_policy()
    gemini = FakeGemini("{}")

    preferences = extract_candidate_preferences("", gemini, policy, store)

    assert preferences.preferred_roles == [
        "frontend engineering",
        "product engineering",
        "senior product engineer",
        "staff frontend engineer",
    ]
    assert preferences.must_have_signals == ["react", "typescript", "system design"]
    assert preferences.avoid_signals == ["product manager", "designer"]
    assert preferences.summary == "Fallback preferences derived from search policy."
    assert gemini.calls == []


def test_extract_candidate_preferences_caches_across_calls():
    store = JobStore(":memory:")
    policy = make_policy()
    gemini = FakeGemini(full_context_payload())

    first = extract_candidate_preferences("candidate profile text", gemini, policy, store)
    second = extract_candidate_preferences("candidate profile text", gemini, policy, store)

    assert first == second
    assert len(gemini.calls) == 1


def test_extract_candidate_preferences_propagates_gemini_budget_exceeded():
    store = JobStore(":memory:")
    policy = make_policy()
    gemini = FakeGemini(exception=GeminiBudgetExceeded("over budget"))

    with pytest.raises(GeminiBudgetExceeded):
        extract_candidate_preferences("candidate profile text", gemini, policy, store)


def test_extract_candidate_preferences_propagates_gemini_quota_paused():
    store = JobStore(":memory:")
    policy = make_policy()
    gemini = FakeGemini(
        exception=GeminiQuotaPaused("paused", paused_until="2026-01-01T00:00:00+00:00", reason="daily_quota")
    )

    with pytest.raises(GeminiQuotaPaused):
        extract_candidate_preferences("candidate profile text", gemini, policy, store)
