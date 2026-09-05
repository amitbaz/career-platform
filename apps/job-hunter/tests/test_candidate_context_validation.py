import json

from job_hunter.candidate_context import _MAX_ITEM_LENGTH, get_candidate_context
from job_hunter.models import SearchPolicy
from job_hunter.store import JobStore


class _Gemini:
    model = "gemini-test"

    def __init__(self, payload: dict):
        self._payload = payload

    def generate_text(self, *args, **kwargs):
        return json.dumps(self._payload)


def _policy() -> SearchPolicy:
    return SearchPolicy(
        target_titles=["senior frontend engineer"],
        positive_keywords=["react"],
        blocked_title_keywords=[],
        salary_floor_eur=90000,
        thresholds={"package": 75, "possible": 65},
    )


def _payload() -> dict:
    return {
        "preferences": {
            "preferred_roles": ["Senior Frontend Engineer"],
            "preferred_seniority": ["senior"],
            "must_have_signals": ["React"],
            "nice_to_have_signals": ["TypeScript"],
            "preferred_locations": ["Berlin"],
            "avoid_signals": ["German required"],
            "summary": "Senior frontend engineer seeking English-speaking roles.",
        },
        "technical_skills": ["React", "TypeScript"],
        "architecture_evidence": ["Owned frontend architecture"],
        "leadership_ownership": ["Led frontend delivery"],
        "agentic_ai_evidence": [],
        "product_domain_evidence": [],
        "location_language_facts": ["Based in Berlin"],
        "career_direction": ["Senior frontend roles"],
        "company_environment": [],
        "career_evidence": ["Senior frontend experience"],
        "evaluation_summary": "Senior frontend engineer with strong React experience.",
    }


def test_candidate_context_ignores_unknown_structured_output_fields():
    payload = _payload()
    payload["provider_metadata"] = {"ignored": True}
    payload["preferences"]["provider_note"] = "ignored"

    context = get_candidate_context(
        "PRIVATE_PROFILE_MARKER",
        _policy(),
        _Gemini(payload),
        JobStore(":memory:"),
    )

    assert context.source == "gemini"
    assert context.technical_skills == ["React", "TypeScript"]


def test_candidate_context_accepts_evidence_string_at_max_length_boundary():
    payload = _payload()
    payload["agentic_ai_evidence"] = ["x" * _MAX_ITEM_LENGTH]

    context = get_candidate_context(
        "PRIVATE_PROFILE_MARKER",
        _policy(),
        _Gemini(payload),
        JobStore(":memory:"),
    )

    assert context.source == "gemini"
    assert context.agentic_ai_evidence == ["x" * _MAX_ITEM_LENGTH]


def test_candidate_context_rejects_evidence_string_over_max_length(caplog):
    payload = _payload()
    payload["agentic_ai_evidence"] = ["x" * (_MAX_ITEM_LENGTH + 1)]

    context = get_candidate_context(
        "PRIVATE_PROFILE_MARKER",
        _policy(),
        _Gemini(payload),
        JobStore(":memory:"),
    )

    assert context.source == "fallback_error"
    assert context.load_error == "ValueError"
    assert f"agentic_ai_evidence entries must be <= {_MAX_ITEM_LENGTH} characters" in caplog.text


def test_candidate_context_logs_safe_local_validation_reason(caplog):
    payload = _payload()
    payload["technical_skills"] = "React"

    context = get_candidate_context(
        "PRIVATE_PROFILE_MARKER",
        _policy(),
        _Gemini(payload),
        JobStore(":memory:"),
    )

    assert context.source == "fallback_error"
    assert context.load_error == "ValueError"
    assert "reason=technical_skills must be a list" in caplog.text
    assert "PRIVATE_PROFILE_MARKER" not in caplog.text
    assert json.dumps(payload) not in caplog.text
