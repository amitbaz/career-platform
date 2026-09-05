import json
import logging

from job_hunter.candidate_context import get_candidate_context
from job_hunter.gemini import GeminiIncompleteResponse
from job_hunter.models import SearchPolicy
from job_hunter.store import JobStore


class FakeGemini:
    model = "gemini-test"

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def generate_text(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _policy():
    return SearchPolicy(
        target_titles=["senior frontend engineer"],
        positive_keywords=["react"],
        blocked_title_keywords=["junior"],
        salary_floor_eur=90000,
        thresholds={"package": 75, "possible": 65},
        role_families=["frontend engineering"],
        blocked_profession_title_phrases=["designer"],
    )


def _valid_json():
    return json.dumps(
        {
            "preferences": {
                "preferred_roles": ["Senior Frontend Engineer"],
                "preferred_seniority": ["senior"],
                "must_have_signals": ["React"],
                "nice_to_have_signals": [],
                "preferred_locations": ["Germany"],
                "avoid_signals": [],
                "summary": "Senior frontend engineer seeking React roles.",
            },
            "technical_skills": ["React", "TypeScript"],
            "architecture_evidence": [],
            "leadership_ownership": [],
            "agentic_ai_evidence": [],
            "product_domain_evidence": [],
            "location_language_facts": ["Based in Berlin"],
            "career_direction": [],
            "company_environment": [],
            "career_evidence": [],
            "evaluation_summary": "Senior frontend engineer with React and TypeScript experience.",
        }
    )


def test_candidate_context_retries_once_after_provider_truncation_and_caches(caplog):
    store = JobStore(":memory:")
    gemini = FakeGemini([GeminiIncompleteResponse("MAX_TOKENS"), _valid_json()])

    with caplog.at_level(logging.WARNING):
        first = get_candidate_context("candidate profile", _policy(), gemini, store)
        second = get_candidate_context("candidate profile", _policy(), gemini, store)

    assert first.source == "gemini"
    assert second.source == "cache"
    assert len(gemini.calls) == 2
    assert gemini.calls[0][1]["max_output_tokens"] == 1800
    assert gemini.calls[1][1]["max_output_tokens"] == 3600
    assert "category=provider_truncation" in caplog.text
    assert "candidate profile" not in caplog.text


def test_candidate_context_falls_back_after_second_provider_truncation(caplog):
    store = JobStore(":memory:")
    gemini = FakeGemini(
        [GeminiIncompleteResponse("MAX_TOKENS"), GeminiIncompleteResponse("MAX_TOKENS")]
    )

    with caplog.at_level(logging.WARNING):
        context = get_candidate_context("candidate profile", _policy(), gemini, store)

    assert context.source == "fallback_error"
    assert context.load_error == "GeminiIncompleteResponse"
    assert len(gemini.calls) == 2
    assert "retry_exhausted=true" in caplog.text
    assert "candidate profile" not in caplog.text


def test_candidate_context_malformed_json_is_not_retried_as_truncation(caplog):
    store = JobStore(":memory:")
    gemini = FakeGemini(['{"preferences":'])

    with caplog.at_level(logging.WARNING):
        context = get_candidate_context("candidate profile", _policy(), gemini, store)

    assert context.source == "fallback_error"
    assert len(gemini.calls) == 1
    assert "category=malformed_structured_output" in caplog.text
    assert "category=provider_truncation" not in caplog.text
    assert "candidate profile" not in caplog.text
