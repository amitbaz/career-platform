import json

import pytest

from job_hunter.content_confidence import AGGREGATOR_TEXT, OFFICIAL_ATS, PARTIAL_UNKNOWN
from job_hunter.evaluation import EvaluationError, evaluate_job
from job_hunter.models import CandidateContext, CandidatePreferences, Evaluation, Job, SearchPolicy
from tests.market_fixtures import make_market_policy

# A sentinel that would only appear in the prompt if some future change
# reintroduced sending the raw candidate profile wholesale. It is never
# passed into evaluate_job (the signature no longer accepts a profile
# string at all), so its absence proves the prompt is built solely from
# the compact CandidateContext.
_FULL_PROFILE_SENTINEL = "FULL_PROFILE_TEXT_MUST_NOT_LEAK_9f3a"


class FakeGemini:
    def __init__(self):
        self.text = ""
        self.model = "gemini-2.5-flash-lite"
        self.prompts = []

    def generate_text(
        self,
        prompt,
        *,
        purpose=None,
        thinking_level=None,
        max_output_tokens=None,
        json_mode=False,
        json_schema=None,
        max_attempts=1,
    ):
        self.prompts.append(
            (prompt, purpose, thinking_level, max_output_tokens, json_mode, max_attempts)
        )
        return self.text


@pytest.fixture
def fake_gemini():
    return FakeGemini()


@pytest.fixture
def policy():
    return SearchPolicy(
        target_titles=["senior product engineer"],
        positive_keywords=["react"],
        blocked_title_keywords=["junior"],
        salary_floor_eur=90000,
        thresholds={"package": 75, "possible": 65},
    )


@pytest.fixture
def job():
    return Job(
        source="ashby",
        title="Senior Product Engineer",
        description="React TypeScript remote",
        content_confidence="official_ats",
    )


@pytest.fixture
def context():
    return CandidateContext(
        preferences=CandidatePreferences(
            preferred_roles=["Senior Product Engineer"],
            preferred_seniority=["senior"],
            must_have_signals=["React"],
            nice_to_have_signals=["TypeScript"],
            preferred_locations=["Germany", "EU remote"],
            avoid_signals=["on-site only"],
            summary="Senior frontend/product engineer looking for remote roles.",
        ),
        technical_skills=["React", "TypeScript", "Node.js"],
        architecture_evidence=["Led migration to microservices at Acme"],
        leadership_ownership=["Managed a team of 3 engineers"],
        agentic_ai_evidence=["Built an LLM-based job evaluation pipeline"],
        product_domain_evidence=["5 years building B2B SaaS products"],
        location_language_facts=["Based in Germany", "Fluent in English and German"],
        career_direction=["Moving toward staff-level product engineering"],
        company_environment=["Prefers small, product-focused teams"],
        career_evidence=["Senior engineer at Acme for 5 years"],
        evaluation_summary="Strong senior product engineer with deep React and architecture experience.",
    )


def _valid_payload(**overrides):
    payload = {
        "scores": {
            "role_seniority": 28,
            "technical": 22,
            "product_architecture": 18,
            "career_direction": 8,
            "location_language": 9,
            "company_environment": 4,
        },
        "total_score": 89,
        "hard_blockers": [],
        "strengths": ["React expertise"],
        "gaps": ["No Rust experience"],
        "salary_note": "Not disclosed",
        "location_note": "Remote EU friendly",
        "decision": "high_priority",
        "rationale": "Strong fit",
        "requirements": {
            "must_have": [
                {"requirement": "React", "depth": "experience", "candidate_support": "supported"}
            ],
            "preferred": [
                {"requirement": "GraphQL", "depth": "familiarity", "candidate_support": "unknown"}
            ],
        },
    }
    payload.update(overrides)
    return payload


def test_evaluate_job_maps_high_priority_decision(fake_gemini, job, policy, context):
    fake_gemini.text = json.dumps(_valid_payload())
    evaluation = evaluate_job(job, context, policy, fake_gemini)
    assert evaluation.total_score == 89
    assert evaluation.decision == "high_priority"
    assert evaluation.model == "gemini-2.5-flash-lite"


def test_evaluate_job_recomputes_total_from_components(fake_gemini, job, policy, context):
    payload = _valid_payload(total_score=89)
    payload["scores"]["company_environment"] = 3
    fake_gemini.text = json.dumps(payload)
    with pytest.raises(EvaluationError):
        evaluate_job(job, context, policy, fake_gemini)


def test_evaluate_job_strips_markdown_code_fences(fake_gemini, job, policy, context):
    fake_gemini.text = "```json\n" + json.dumps(_valid_payload()) + "\n```"
    evaluation = evaluate_job(job, context, policy, fake_gemini)
    assert evaluation.total_score == 89


def test_evaluation_rejects_component_over_max(fake_gemini, job, policy, context):
    payload = _valid_payload()
    payload["scores"]["role_seniority"] = 31
    payload["total_score"] = 92
    fake_gemini.text = json.dumps(payload)
    with pytest.raises(EvaluationError):
        evaluate_job(job, context, policy, fake_gemini)


def test_evaluation_rejects_unknown_score_key(fake_gemini, job, policy, context):
    payload = _valid_payload()
    payload["scores"]["extra_key"] = 1
    fake_gemini.text = json.dumps(payload)
    with pytest.raises(EvaluationError):
        evaluate_job(job, context, policy, fake_gemini)


def test_evaluation_rejects_missing_score_key(fake_gemini, job, policy, context):
    payload = _valid_payload()
    del payload["scores"]["company_environment"]
    fake_gemini.text = json.dumps(payload)
    with pytest.raises(EvaluationError):
        evaluate_job(job, context, policy, fake_gemini)


def test_evaluation_hard_blocker_forces_blocked_decision(fake_gemini, job, policy, context):
    payload = _valid_payload(hard_blockers=["Not remote"])
    fake_gemini.text = json.dumps(payload)
    evaluation = evaluate_job(job, context, policy, fake_gemini)
    assert evaluation.decision == "blocked"


def test_evaluation_maps_skip_band(fake_gemini, job, policy, context):
    payload = _valid_payload(
        scores={
            "role_seniority": 15,
            "technical": 15,
            "product_architecture": 10,
            "career_direction": 5,
            "location_language": 5,
            "company_environment": 2,
        },
        total_score=52,
    )
    fake_gemini.text = json.dumps(payload)
    evaluation = evaluate_job(job, context, policy, fake_gemini)
    assert evaluation.decision == "skip"


def test_evaluation_rejects_invalid_json(fake_gemini, job, policy, context):
    fake_gemini.text = "not json"
    with pytest.raises(EvaluationError):
        evaluate_job(job, context, policy, fake_gemini)


def test_evaluation_prompt_uses_compact_context_not_full_profile(fake_gemini, job, policy, context):
    fake_gemini.text = json.dumps(_valid_payload())
    evaluate_job(job, context, policy, fake_gemini)
    prompt, _purpose, _thinking, _max_tokens, _json_mode, _max_attempts = fake_gemini.prompts[0]

    # Compact evidence/summary from the context must reach the prompt...
    assert context.evaluation_summary in prompt
    assert "Led migration to microservices at Acme" in prompt
    assert "Managed a team of 3 engineers" in prompt

    # ...and an arbitrary full-profile sentinel must never appear, guarding
    # against a regression that reintroduces sending the raw profile.
    assert _FULL_PROFILE_SENTINEL not in prompt


def test_evaluation_prompt_preserves_hard_blockers_and_thresholds(fake_gemini, job, policy, context):
    fake_gemini.text = json.dumps(_valid_payload())
    evaluate_job(job, context, policy, fake_gemini)
    prompt, _purpose, _thinking, _max_tokens, _json_mode, _max_attempts = fake_gemini.prompts[0]

    assert str(policy.salary_floor_eur) in prompt
    assert "hard blocker" in prompt.lower()
    assert "remote" in prompt.lower()
    assert "relocation" in prompt.lower()


def test_evaluation_uses_expected_resource_controls(fake_gemini, job, policy, context):
    fake_gemini.text = json.dumps(_valid_payload())
    evaluate_job(job, context, policy, fake_gemini)
    _prompt, purpose, thinking_level, max_output_tokens, json_mode, max_attempts = fake_gemini.prompts[0]

    assert purpose == "job_evaluation"
    assert thinking_level == "medium"
    assert max_output_tokens == 5000
    assert json_mode is True
    assert max_attempts == 2


# --- Market-aware prompt content (Task 6) -----------------------------------


def test_market_aware_prompt_drops_the_legacy_remote_only_framing(fake_gemini, context):
    """The market-aware prompt must not open by declaring a remote-only search
    while its market rules block says hybrid/onsite is allowed."""
    policy = make_market_policy()
    job = Job(
        source="ashby",
        title="Senior Frontend Engineer",
        location="London",
        description="Hybrid role, 2 days a week in our London office. React and TypeScript.",
        market_id="london",
    )
    fake_gemini.text = json.dumps(_valid_payload())
    evaluate_job(job, context, policy, fake_gemini)
    prompt, _purpose, _thinking, _max_tokens, _json_mode, _max_attempts = fake_gemini.prompts[0]

    assert "for a remote-only job search" not in prompt
    assert "for a market-driven job search" in prompt
    assert "not by a single global remote-only rule" in prompt


def test_legacy_prompt_keeps_the_remote_only_framing(fake_gemini, job, policy, context):
    """The no-market (legacy) path's opening sentence must stay unchanged."""
    fake_gemini.text = json.dumps(_valid_payload())
    evaluate_job(job, context, policy, fake_gemini)
    prompt, _purpose, _thinking, _max_tokens, _json_mode, _max_attempts = fake_gemini.prompts[0]

    assert prompt.startswith(
        "You are evaluating a job posting against a candidate profile "
        "for a remote-only job search."
    )


def test_evaluation_prompt_includes_london_market_details(fake_gemini, context):
    policy = make_market_policy()
    job = Job(
        source="ashby",
        title="Senior Frontend Engineer",
        location="London",
        description="Hybrid role, 2 days a week in our London office. React and TypeScript.",
        market_id="london",
    )
    fake_gemini.text = json.dumps(_valid_payload())
    evaluate_job(job, context, policy, fake_gemini)
    prompt, _purpose, _thinking, _max_tokens, _json_mode, _max_attempts = fake_gemini.prompts[0]
    lower = prompt.lower()

    assert "GBP" in prompt
    assert "90000" in prompt
    assert "relocation policy: allowed" in lower
    assert "sponsorship policy: required" in lower
    assert "deterministic sponsorship status: unknown" in lower
    assert "omission is unknown" in lower


def test_evaluation_prompt_includes_sf_market_salary_floor(fake_gemini, context):
    policy = make_market_policy()
    job = Job(
        source="ashby",
        title="Senior Product Engineer",
        location="San Francisco",
        description="React and TypeScript, remote friendly.",
        market_id="us_nyc_sf",
    )
    fake_gemini.text = json.dumps(_valid_payload())
    evaluate_job(job, context, policy, fake_gemini)
    prompt, _purpose, _thinking, _max_tokens, _json_mode, _max_attempts = fake_gemini.prompts[0]

    assert "USD" in prompt
    assert "200000" in prompt


def test_evaluation_prompt_full_stack_adds_backend_ramp_language(fake_gemini, context):
    policy = make_market_policy()
    job = Job(
        source="ashby",
        title="Full-Stack Engineer",
        location="Berlin",
        description="React, Node.js, PostgreSQL.",
        market_id="germany_eu",
    )
    fake_gemini.text = json.dumps(_valid_payload())
    evaluate_job(job, context, policy, fake_gemini)
    prompt, _purpose, _thinking, _max_tokens, _json_mode, _max_attempts = fake_gemini.prompts[0]
    lower = prompt.lower()

    assert "senior frontend engineer" in lower
    assert "do not invent senior backend experience" in lower


def test_evaluation_prompt_full_stack_hyphenated_title_also_matches(fake_gemini, context):
    policy = make_market_policy()
    job = Job(
        source="ashby",
        title="Full Stack Engineer",
        location="Berlin",
        description="React, Node.js, PostgreSQL.",
        market_id="germany_eu",
    )
    fake_gemini.text = json.dumps(_valid_payload())
    evaluate_job(job, context, policy, fake_gemini)
    prompt, _purpose, _thinking, _max_tokens, _json_mode, _max_attempts = fake_gemini.prompts[0]

    assert "do not invent senior backend experience" in prompt.lower()


def test_evaluation_prompt_falls_back_to_legacy_without_market_id(fake_gemini, context):
    """A market-enabled policy with a job that has no attributed market must
    still produce the exact legacy global prompt, not a market-shaped one."""
    policy = make_market_policy()
    job = Job(source="ashby", title="Senior Product Engineer", description="React TypeScript remote")
    fake_gemini.text = json.dumps(_valid_payload())
    evaluate_job(job, context, policy, fake_gemini)
    prompt, _purpose, _thinking, _max_tokens, _json_mode, _max_attempts = fake_gemini.prompts[0]
    lower = prompt.lower()

    assert f"eur {policy.salary_floor_eur}" in lower
    assert "not remote, or requires relocation" in lower


def test_evaluate_job_sets_market_id_from_job(fake_gemini, context):
    policy = make_market_policy()
    job = Job(
        source="ashby",
        title="Senior Frontend Engineer",
        location="London",
        market_id="london",
    )
    fake_gemini.text = json.dumps(_valid_payload())
    evaluation = evaluate_job(job, context, policy, fake_gemini)

    assert evaluation.market_id == "london"


def test_evaluate_job_market_id_defaults_to_empty_string(fake_gemini, job, policy, context):
    fake_gemini.text = json.dumps(_valid_payload())
    evaluation = evaluate_job(job, context, policy, fake_gemini)

    assert evaluation.market_id == ""


# --- Requirement-aware gating (Task 7) --------------------------------------


def test_missing_requirements_field_is_rejected(fake_gemini, job, policy, context):
    payload = _valid_payload()
    del payload["requirements"]
    fake_gemini.text = json.dumps(payload)
    with pytest.raises(EvaluationError, match="requirements"):
        evaluate_job(job, context, policy, fake_gemini)


def test_invalid_requirement_depth_is_rejected(fake_gemini, job, policy, context):
    payload = _valid_payload()
    payload["requirements"]["must_have"][0]["depth"] = "nonsense"
    fake_gemini.text = json.dumps(payload)
    with pytest.raises(EvaluationError, match="depth"):
        evaluate_job(job, context, policy, fake_gemini)


def test_major_unsupported_must_have_caps_score_below_possible(fake_gemini, job, policy, context):
    payload = _valid_payload(total_score=89)
    payload["requirements"]["must_have"] = [
        {"requirement": "Deep PostgreSQL expertise", "depth": "deep_expert", "candidate_support": "unsupported"}
    ]
    fake_gemini.text = json.dumps(payload)
    evaluation = evaluate_job(job, context, policy, fake_gemini)
    assert evaluation.raw_model_score == 89
    assert evaluation.total_score == policy.thresholds["possible"] - 1
    assert evaluation.total_score < policy.thresholds["possible"]
    assert evaluation.decision == "skip"


def test_familiarity_depth_unsupported_must_have_does_not_gate(fake_gemini, job, policy, context):
    payload = _valid_payload(total_score=89)
    payload["requirements"]["must_have"] = [
        {"requirement": "Basic SQL familiarity", "depth": "familiarity", "candidate_support": "unsupported"}
    ]
    fake_gemini.text = json.dumps(payload)
    evaluation = evaluate_job(job, context, policy, fake_gemini)
    assert evaluation.decision == "high_priority"
    assert evaluation.total_score == 89


def test_unsupported_preferred_requirement_does_not_gate(fake_gemini, job, policy, context):
    payload = _valid_payload(total_score=89)
    payload["requirements"]["preferred"] = [
        {"requirement": "PostgreSQL", "depth": "deep_expert", "candidate_support": "unsupported"}
    ]
    fake_gemini.text = json.dumps(payload)
    evaluation = evaluate_job(job, context, policy, fake_gemini)
    assert evaluation.decision == "high_priority"
    assert evaluation.total_score == 89


def test_insufficient_content_confidence_caps_below_high_priority(fake_gemini, job, policy, context):
    job.content_confidence = PARTIAL_UNKNOWN
    payload = _valid_payload(total_score=89)
    fake_gemini.text = json.dumps(payload)
    evaluation = evaluate_job(job, context, policy, fake_gemini)
    assert evaluation.decision == "possible_match"


def test_insufficient_content_still_allows_possible_match_and_skip(fake_gemini, job, policy, context):
    job.content_confidence = PARTIAL_UNKNOWN
    payload = _valid_payload(
        scores={
            "role_seniority": 15,
            "technical": 13,
            "product_architecture": 10,
            "career_direction": 5,
            "location_language": 5,
            "company_environment": 2,
        },
        total_score=50,
    )
    fake_gemini.text = json.dumps(payload)
    evaluation = evaluate_job(job, context, policy, fake_gemini)
    assert evaluation.decision == "skip"  # below possible threshold on its own merits


def test_hard_blockers_still_force_blocked_regardless_of_requirements(fake_gemini, job, policy, context):
    payload = _valid_payload(total_score=89, hard_blockers=["Below salary floor"])
    fake_gemini.text = json.dumps(payload)
    evaluation = evaluate_job(job, context, policy, fake_gemini)
    assert evaluation.decision == "blocked"


def test_evaluation_persists_content_confidence_and_requirements(fake_gemini, job, policy, context):
    fake_gemini.text = json.dumps(_valid_payload())
    evaluation = evaluate_job(job, context, policy, fake_gemini)
    assert evaluation.content_confidence == "official_ats"
    assert evaluation.requirements["must_have"][0]["requirement"] == "React"


def test_prompt_includes_content_confidence_tier(fake_gemini, job, policy, context):
    fake_gemini.text = json.dumps(_valid_payload())
    evaluate_job(job, context, policy, fake_gemini)
    prompt = fake_gemini.prompts[0][0]
    assert "official_ats" in prompt


def test_prompt_instructs_requirement_extraction(fake_gemini, job, policy, context):
    fake_gemini.text = json.dumps(_valid_payload())
    evaluate_job(job, context, policy, fake_gemini)
    prompt = fake_gemini.prompts[0][0]
    assert "must-have" in prompt.lower() or "must_have" in prompt


def test_evaluation_defaults_raw_model_score_to_zero():
    evaluation = Evaluation(
        job_id=1,
        total_score=70,
        scores={},
        decision="possible_match",
        hard_blockers=[],
        strengths=[],
        gaps=[],
        salary_note="",
        location_note="",
        rationale="",
        model="m",
    )
    assert evaluation.raw_model_score == 0


def test_experience_depth_unsupported_must_have_is_also_capped(fake_gemini, job, policy, context):
    payload = _valid_payload(total_score=89)
    payload["requirements"]["must_have"] = [
        {"requirement": "End-to-end forecasting", "depth": "experience", "candidate_support": "unsupported"}
    ]
    fake_gemini.text = json.dumps(payload)
    evaluation = evaluate_job(job, context, policy, fake_gemini)
    assert evaluation.total_score == policy.thresholds["possible"] - 1
    assert evaluation.decision == "skip"


def test_partial_support_must_have_is_not_capped(fake_gemini, job, policy, context):
    payload = _valid_payload(total_score=89)
    payload["requirements"]["must_have"] = [
        {"requirement": "Deep PostgreSQL expertise", "depth": "deep_expert", "candidate_support": "partial"}
    ]
    fake_gemini.text = json.dumps(payload)
    evaluation = evaluate_job(job, context, policy, fake_gemini)
    assert evaluation.total_score == 89
    assert evaluation.raw_model_score == 89
    assert evaluation.decision == "high_priority"


def test_hard_blocker_takes_precedence_over_cap(fake_gemini, job, policy, context):
    payload = _valid_payload(total_score=89, hard_blockers=["Requires on-site in the US"])
    payload["requirements"]["must_have"] = [
        {"requirement": "Deep PostgreSQL expertise", "depth": "deep_expert", "candidate_support": "unsupported"}
    ]
    fake_gemini.text = json.dumps(payload)
    evaluation = evaluate_job(job, context, policy, fake_gemini)
    assert evaluation.decision == "blocked"
    assert evaluation.total_score == policy.thresholds["possible"] - 1
    assert evaluation.raw_model_score == 89


def test_uncapped_evaluation_keeps_raw_and_total_in_sync(fake_gemini, job, policy, context):
    fake_gemini.text = json.dumps(_valid_payload())
    evaluation = evaluate_job(job, context, policy, fake_gemini)
    assert evaluation.total_score == 89
    assert evaluation.raw_model_score == 89
