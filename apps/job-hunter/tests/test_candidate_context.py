import json

import pytest

from job_hunter import candidate_context
from job_hunter.candidate_context import (
    CANDIDATE_CONTEXT_SCHEMA,
    CANDIDATE_CONTEXT_SCHEMA_VERSION,
    FALLBACK_CONTEXT_SUMMARY,
    get_candidate_context,
)
from job_hunter.gemini_usage import GeminiBudgetExceeded, GeminiQuotaPaused
from job_hunter.models import CandidateContext, SearchPolicy
from job_hunter.preferences import FALLBACK_PREFERENCES_SUMMARY
from job_hunter.store import JobStore


class FakeGemini:
    def __init__(self, responses, model="gemini-test", exceptions=None):
        self.model = model
        self._responses = list(responses)
        self._exceptions = list(exceptions or [])
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
        if self._exceptions:
            raise self._exceptions.pop(0)
        return self._responses.pop(0)


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


def valid_context_payload(**overrides) -> dict:
    payload = {
        "preferences": {
            "preferred_roles": ["Senior Product Engineer"],
            "preferred_seniority": ["senior"],
            "must_have_signals": ["React"],
            "nice_to_have_signals": ["mentorship"],
            "preferred_locations": ["Germany"],
            "avoid_signals": ["on-site"],
            "summary": "Senior frontend/product engineer focused on remote EU roles.",
        },
        "technical_skills": ["React", "TypeScript", "Node.js"],
        "architecture_evidence": ["Designed a micro-frontend platform"],
        "leadership_ownership": ["Led a team of 4 engineers"],
        "agentic_ai_evidence": ["Built an LLM-based support triage tool"],
        "product_domain_evidence": ["5 years in fintech"],
        "location_language_facts": ["Based in Berlin, fluent in English and German"],
        "career_direction": ["Seeking staff-level scope"],
        "company_environment": ["Prefers small, product-led teams"],
        "career_evidence": ["8 years of professional software engineering"],
        "evaluation_summary": "Senior full-stack engineer with product and leadership experience.",
    }
    payload.update(overrides)
    return payload


def valid_context_json(**overrides) -> str:
    return json.dumps(valid_context_payload(**overrides))


def test_get_candidate_context_extracts_valid_response():
    store = JobStore(":memory:")
    gemini = FakeGemini([valid_context_json()])

    context = get_candidate_context("candidate profile text", make_policy(), gemini, store)

    assert isinstance(context, CandidateContext)
    assert context.technical_skills == ["React", "TypeScript", "Node.js"]
    assert context.preferences.summary == "Senior frontend/product engineer focused on remote EU roles."
    assert context.evaluation_summary == "Senior full-stack engineer with product and leadership experience."
    assert len(gemini.calls) == 1


def test_get_candidate_context_uses_exact_gemini_request_control():
    store = JobStore(":memory:")
    gemini = FakeGemini([valid_context_json()])

    get_candidate_context("candidate profile text", make_policy(), gemini, store)

    call = gemini.calls[0]
    assert call["purpose"] == "candidate_context"
    assert call["thinking_level"] == "medium"
    assert call["max_output_tokens"] == 1800
    assert call["json_mode"] is True
    assert call["json_schema"] == CANDIDATE_CONTEXT_SCHEMA


def test_extraction_prompt_states_anti_hallucination_rule():
    store = JobStore(":memory:")
    gemini = FakeGemini([valid_context_json()])

    get_candidate_context("candidate profile text", make_policy(), gemini, store)

    prompt = gemini.calls[0]["prompt"]
    assert "candidate profile text" in prompt
    lowered = prompt.lower()
    assert "omit" in lowered
    assert "never infer" in lowered or "not infer" in lowered


def test_get_candidate_context_cache_hit_makes_zero_additional_gemini_calls():
    store = JobStore(":memory:")
    gemini = FakeGemini([valid_context_json()])
    policy = make_policy()

    first = get_candidate_context("candidate profile text", policy, gemini, store)
    second = get_candidate_context("candidate profile text", policy, gemini, store)

    assert len(gemini.calls) == 1
    assert second == first


def test_get_candidate_context_profile_change_triggers_new_extraction():
    store = JobStore(":memory:")
    gemini = FakeGemini([valid_context_json(), valid_context_json(evaluation_summary="Different summary.")])
    policy = make_policy()

    get_candidate_context("profile A", policy, gemini, store)
    second = get_candidate_context("profile B", policy, gemini, store)

    assert len(gemini.calls) == 2
    assert second.evaluation_summary == "Different summary."


def test_get_candidate_context_model_change_triggers_new_extraction():
    store = JobStore(":memory:")
    policy = make_policy()
    gemini_a = FakeGemini([valid_context_json()], model="gemini-model-a")
    gemini_b = FakeGemini(
        [valid_context_json(evaluation_summary="Different summary.")], model="gemini-model-b"
    )

    get_candidate_context("candidate profile text", policy, gemini_a, store)
    second = get_candidate_context("candidate profile text", policy, gemini_b, store)

    assert len(gemini_a.calls) == 1
    assert len(gemini_b.calls) == 1
    assert second.evaluation_summary == "Different summary."


def test_get_candidate_context_schema_version_change_triggers_new_extraction(monkeypatch):
    store = JobStore(":memory:")
    policy = make_policy()
    gemini = FakeGemini([valid_context_json(), valid_context_json(evaluation_summary="Different summary.")])

    get_candidate_context("candidate profile text", policy, gemini, store)
    monkeypatch.setattr(candidate_context, "CANDIDATE_CONTEXT_SCHEMA_VERSION", "2")
    second = get_candidate_context("candidate profile text", policy, gemini, store)

    assert len(gemini.calls) == 2
    assert second.evaluation_summary == "Different summary."


def test_get_candidate_context_empty_profile_uses_fallback_without_gemini_call():
    store = JobStore(":memory:")
    gemini = FakeGemini([valid_context_json()])
    policy = make_policy()

    context = get_candidate_context("", policy, gemini, store)

    assert gemini.calls == []
    assert context.preferences.summary == FALLBACK_PREFERENCES_SUMMARY
    assert context.evaluation_summary == FALLBACK_CONTEXT_SUMMARY
    assert context.technical_skills == []


def test_get_candidate_context_falls_back_on_malformed_json_and_does_not_cache():
    store = JobStore(":memory:")
    policy = make_policy()
    gemini = FakeGemini(["not json", valid_context_json()])

    context = get_candidate_context("candidate profile text", policy, gemini, store)

    assert context.preferences.summary == FALLBACK_PREFERENCES_SUMMARY
    assert context.evaluation_summary == FALLBACK_CONTEXT_SUMMARY

    # A malformed payload must never be cached: the next call re-extracts.
    get_candidate_context("candidate profile text", policy, gemini, store)
    assert len(gemini.calls) == 2


def test_get_candidate_context_falls_back_when_evidence_list_exceeds_bound():
    store = JobStore(":memory:")
    policy = make_policy()
    oversized = [f"skill {i}" for i in range(21)]
    gemini = FakeGemini([valid_context_json(technical_skills=oversized)])

    context = get_candidate_context("candidate profile text", policy, gemini, store)

    assert context.preferences.summary == FALLBACK_PREFERENCES_SUMMARY
    assert context.technical_skills == []


def test_get_candidate_context_falls_back_when_evidence_item_exceeds_length_bound():
    store = JobStore(":memory:")
    policy = make_policy()
    gemini = FakeGemini([valid_context_json(technical_skills=["x" * 181])])

    context = get_candidate_context("candidate profile text", policy, gemini, store)

    assert context.preferences.summary == FALLBACK_PREFERENCES_SUMMARY


def test_get_candidate_context_propagates_gemini_budget_exceeded():
    store = JobStore(":memory:")
    policy = make_policy()
    gemini = FakeGemini([], exceptions=[GeminiBudgetExceeded("over budget")])

    with pytest.raises(GeminiBudgetExceeded):
        get_candidate_context("candidate profile text", policy, gemini, store)


def test_get_candidate_context_propagates_gemini_quota_paused():
    store = JobStore(":memory:")
    policy = make_policy()
    gemini = FakeGemini(
        [],
        exceptions=[
            GeminiQuotaPaused("paused", paused_until="2026-01-01T00:00:00+00:00", reason="daily_quota")
        ],
    )

    with pytest.raises(GeminiQuotaPaused):
        get_candidate_context("candidate profile text", policy, gemini, store)


def test_candidate_context_schema_version_constant_is_stable():
    assert CANDIDATE_CONTEXT_SCHEMA_VERSION == "1"
