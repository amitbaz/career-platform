import json

import pytest

from job_hunter.ai import AIQuotaPaused
from job_hunter.content_confidence import AGGREGATOR_TEXT, OFFICIAL_ATS, PARTIAL_UNKNOWN
from job_hunter.evaluation import SCORE_MAXIMA, EvaluationError, evaluate_job
from job_hunter.models import (
    CandidateContext,
    CandidatePreferences,
    Compensation,
    Evaluation,
    Job,
    SearchPolicy,
)
from tests.facet_fixtures import make_facets
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
        self.schemas = []

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
        self.prompts.append(
            (prompt, purpose, thinking_level, max_output_tokens, json_mode, max_attempts)
        )
        self.schemas.append(json_schema)
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
def facets():
    return make_facets()


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
            "must_have": [{"requirement": "React", "candidate_support": "supported"}],
            "preferred": [{"requirement": "GraphQL", "candidate_support": "unknown"}],
        },
    }
    payload.update(overrides)
    return payload


def test_evaluate_job_maps_high_priority_decision(fake_gemini, job, facets, policy, context):
    fake_gemini.text = json.dumps(_valid_payload())
    evaluation = evaluate_job(job, facets, context, policy, fake_gemini)
    assert evaluation.total_score == 89
    assert evaluation.decision == "high_priority"
    assert evaluation.model == "gemini-2.5-flash-lite"


def test_evaluate_job_recomputes_total_from_components(fake_gemini, job, facets, policy, context):
    payload = _valid_payload(total_score=89)
    payload["scores"]["company_environment"] = 3
    fake_gemini.text = json.dumps(payload)
    with pytest.raises(EvaluationError):
        evaluate_job(job, facets, context, policy, fake_gemini)


def test_evaluate_job_strips_markdown_code_fences(fake_gemini, job, facets, policy, context):
    fake_gemini.text = "```json\n" + json.dumps(_valid_payload()) + "\n```"
    evaluation = evaluate_job(job, facets, context, policy, fake_gemini)
    assert evaluation.total_score == 89


def test_evaluation_rejects_component_over_max(fake_gemini, job, facets, policy, context):
    payload = _valid_payload()
    payload["scores"]["role_seniority"] = 31
    payload["total_score"] = 92
    fake_gemini.text = json.dumps(payload)
    with pytest.raises(EvaluationError):
        evaluate_job(job, facets, context, policy, fake_gemini)


def test_evaluation_rejects_unknown_score_key(fake_gemini, job, facets, policy, context):
    payload = _valid_payload()
    payload["scores"]["extra_key"] = 1
    fake_gemini.text = json.dumps(payload)
    with pytest.raises(EvaluationError):
        evaluate_job(job, facets, context, policy, fake_gemini)


def test_evaluation_rejects_missing_score_key(fake_gemini, job, facets, policy, context):
    payload = _valid_payload()
    del payload["scores"]["company_environment"]
    fake_gemini.text = json.dumps(payload)
    with pytest.raises(EvaluationError):
        evaluate_job(job, facets, context, policy, fake_gemini)


def test_evaluation_hard_blocker_forces_blocked_decision(fake_gemini, job, facets, policy, context):
    payload = _valid_payload(hard_blockers=["Not remote"])
    fake_gemini.text = json.dumps(payload)
    evaluation = evaluate_job(job, facets, context, policy, fake_gemini)
    assert evaluation.decision == "blocked"


def test_evaluation_maps_skip_band(fake_gemini, job, facets, policy, context):
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
    evaluation = evaluate_job(job, facets, context, policy, fake_gemini)
    assert evaluation.decision == "skip"


def test_evaluation_rejects_invalid_json(fake_gemini, job, facets, policy, context):
    fake_gemini.text = "not json"
    with pytest.raises(EvaluationError):
        evaluate_job(job, facets, context, policy, fake_gemini)


def test_evaluation_retries_one_unparseable_response_and_uses_the_second_sample(
    fake_gemini, job, facets, policy, context
):
    responses = ["not json", json.dumps(_valid_payload())]

    def generate_text(prompt, **kwargs):
        fake_gemini.text = responses.pop(0)
        return FakeGemini.generate_text(fake_gemini, prompt, **kwargs)

    fake_gemini.generate_text = generate_text

    result = evaluate_job(job, facets, context, policy, fake_gemini)

    assert result.total_score == 89
    assert len(fake_gemini.prompts) == 2
    assert fake_gemini.schemas[0] == fake_gemini.schemas[1]


def test_evaluation_does_not_retry_a_quota_refusal(job, facets, policy, context):
    class QuotaRefusingGemini(FakeGemini):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def generate_text(self, prompt, **kwargs):
            self.calls += 1
            raise AIQuotaPaused(
                "paused",
                paused_until="2026-09-10T00:00:00+00:00",
                reason="daily_quota",
            )

    gemini = QuotaRefusingGemini()

    with pytest.raises(AIQuotaPaused):
        evaluate_job(job, facets, context, policy, gemini)

    assert gemini.calls == 1


def test_evaluation_prompt_uses_compact_context_not_full_profile(fake_gemini, job, facets, policy, context):
    fake_gemini.text = json.dumps(_valid_payload())
    evaluate_job(job, facets, context, policy, fake_gemini)
    prompt, _purpose, _thinking, _max_tokens, _json_mode, _max_attempts = fake_gemini.prompts[0]

    # Compact evidence/summary from the context must reach the prompt...
    assert context.evaluation_summary in prompt
    assert "Led migration to microservices at Acme" in prompt
    assert "Managed a team of 3 engineers" in prompt

    # ...and an arbitrary full-profile sentinel must never appear, guarding
    # against a regression that reintroduces sending the raw profile.
    assert _FULL_PROFILE_SENTINEL not in prompt


def test_evaluation_prompt_preserves_hard_blockers_and_thresholds(fake_gemini, job, facets, policy, context):
    fake_gemini.text = json.dumps(_valid_payload())
    evaluate_job(job, facets, context, policy, fake_gemini)
    prompt, _purpose, _thinking, _max_tokens, _json_mode, _max_attempts = fake_gemini.prompts[0]

    assert str(policy.salary_floor_eur) in prompt
    assert "hard blocker" in prompt.lower()
    assert "remote" in prompt.lower()
    assert "relocation" in prompt.lower()


def test_evaluation_uses_expected_resource_controls(fake_gemini, job, facets, policy, context):
    fake_gemini.text = json.dumps(_valid_payload())
    evaluate_job(job, facets, context, policy, fake_gemini)
    _prompt, purpose, thinking_level, max_output_tokens, json_mode, max_attempts = fake_gemini.prompts[0]

    assert purpose == "job_evaluation"
    assert thinking_level == "medium"
    assert max_output_tokens == 5000
    assert json_mode is True
    assert max_attempts == 2


def test_evaluation_declares_the_complete_parser_schema(
    fake_gemini, job, facets, policy, context
):
    fake_gemini.text = json.dumps(_valid_payload())

    evaluate_job(job, facets, context, policy, fake_gemini)

    schema = fake_gemini.schemas[0]
    assert schema["type"] == "OBJECT"
    assert schema["required"] == list(schema["properties"])
    scores = schema["properties"]["scores"]
    assert scores["required"] == list(SCORE_MAXIMA)
    assert scores["properties"]["role_seniority"] == {
        "type": "INTEGER",
        "minimum": 0,
        "maximum": 30,
    }
    requirements = schema["properties"]["requirements"]["properties"]
    assert requirements["must_have"]["minItems"] == 1
    assert requirements["must_have"]["maxItems"] == 1
    assert requirements["preferred"]["minItems"] == 1
    assert requirements["preferred"]["maxItems"] == 1
    support = requirements["must_have"]["items"]["properties"]["candidate_support"]
    assert support["enum"] == sorted({"supported", "partial", "unsupported", "unknown"})


# --- Market-aware prompt content (Task 6) -----------------------------------


def test_market_aware_prompt_drops_the_legacy_remote_only_framing(fake_gemini, facets, context):
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
    evaluate_job(job, facets, context, policy, fake_gemini)
    prompt, _purpose, _thinking, _max_tokens, _json_mode, _max_attempts = fake_gemini.prompts[0]

    assert "for a remote-only job search" not in prompt
    assert "for a market-driven job search" in prompt
    assert "not by a single global remote-only rule" in prompt


def test_legacy_prompt_keeps_the_remote_only_framing(fake_gemini, job, facets, policy, context):
    """The no-market (legacy) path's opening sentence must stay unchanged."""
    fake_gemini.text = json.dumps(_valid_payload())
    evaluate_job(job, facets, context, policy, fake_gemini)
    prompt, _purpose, _thinking, _max_tokens, _json_mode, _max_attempts = fake_gemini.prompts[0]

    assert prompt.startswith(
        "You are evaluating a job posting against a candidate profile "
        "for a remote-only job search."
    )


def test_evaluation_prompt_includes_london_market_details(fake_gemini, facets, context):
    policy = make_market_policy()
    job = Job(
        source="ashby",
        title="Senior Frontend Engineer",
        location="London",
        description="Hybrid role, 2 days a week in our London office. React and TypeScript.",
        market_id="london",
    )
    fake_gemini.text = json.dumps(_valid_payload())
    evaluate_job(job, facets, context, policy, fake_gemini)
    prompt, _purpose, _thinking, _max_tokens, _json_mode, _max_attempts = fake_gemini.prompts[0]
    lower = prompt.lower()

    assert "GBP" in prompt
    assert "90000" in prompt
    assert "relocation policy: allowed" in lower
    assert "sponsorship policy: required" in lower
    assert "deterministic sponsorship status: unknown" in lower
    assert "omission is unknown" in lower


def test_evaluation_prompt_includes_sf_market_salary_floor(fake_gemini, facets, context):
    policy = make_market_policy()
    job = Job(
        source="ashby",
        title="Senior Product Engineer",
        location="San Francisco",
        description="React and TypeScript, remote friendly.",
        market_id="us_nyc_sf",
    )
    fake_gemini.text = json.dumps(_valid_payload())
    evaluate_job(job, facets, context, policy, fake_gemini)
    prompt, _purpose, _thinking, _max_tokens, _json_mode, _max_attempts = fake_gemini.prompts[0]

    assert "USD" in prompt
    assert "200000" in prompt


def test_evaluation_prompt_full_stack_adds_backend_ramp_language(fake_gemini, facets, context):
    policy = make_market_policy()
    job = Job(
        source="ashby",
        title="Full-Stack Engineer",
        location="Berlin",
        description="React, Node.js, PostgreSQL.",
        market_id="germany_eu",
    )
    fake_gemini.text = json.dumps(_valid_payload())
    evaluate_job(job, facets, context, policy, fake_gemini)
    prompt, _purpose, _thinking, _max_tokens, _json_mode, _max_attempts = fake_gemini.prompts[0]
    lower = prompt.lower()

    assert "senior frontend engineer" in lower
    assert "do not invent senior backend experience" in lower


def test_evaluation_prompt_full_stack_hyphenated_title_also_matches(fake_gemini, facets, context):
    policy = make_market_policy()
    job = Job(
        source="ashby",
        title="Full Stack Engineer",
        location="Berlin",
        description="React, Node.js, PostgreSQL.",
        market_id="germany_eu",
    )
    fake_gemini.text = json.dumps(_valid_payload())
    evaluate_job(job, facets, context, policy, fake_gemini)
    prompt, _purpose, _thinking, _max_tokens, _json_mode, _max_attempts = fake_gemini.prompts[0]

    assert "do not invent senior backend experience" in prompt.lower()


def test_evaluation_prompt_falls_back_to_legacy_without_market_id(fake_gemini, facets, context):
    """A market-enabled policy with a job that has no attributed market must
    still produce the exact legacy global prompt, not a market-shaped one."""
    policy = make_market_policy()
    job = Job(source="ashby", title="Senior Product Engineer", description="React TypeScript remote")
    fake_gemini.text = json.dumps(_valid_payload())
    evaluate_job(job, facets, context, policy, fake_gemini)
    prompt, _purpose, _thinking, _max_tokens, _json_mode, _max_attempts = fake_gemini.prompts[0]
    lower = prompt.lower()

    assert f"eur {policy.salary_floor_eur}" in lower
    assert "not remote, or requires relocation" in lower


def test_evaluate_job_sets_market_id_from_job(fake_gemini, facets, context):
    policy = make_market_policy()
    job = Job(
        source="ashby",
        title="Senior Frontend Engineer",
        location="London",
        market_id="london",
    )
    fake_gemini.text = json.dumps(_valid_payload())
    evaluation = evaluate_job(job, facets, context, policy, fake_gemini)

    assert evaluation.market_id == "london"


def test_evaluate_job_market_id_defaults_to_empty_string(fake_gemini, job, facets, policy, context):
    fake_gemini.text = json.dumps(_valid_payload())
    evaluation = evaluate_job(job, facets, context, policy, fake_gemini)

    assert evaluation.market_id == ""


# --- Requirement-aware gating (Task 7) --------------------------------------


def test_missing_requirements_field_is_rejected(fake_gemini, job, facets, policy, context):
    payload = _valid_payload()
    del payload["requirements"]
    fake_gemini.text = json.dumps(payload)
    with pytest.raises(EvaluationError, match="requirements"):
        evaluate_job(job, facets, context, policy, fake_gemini)


def test_invalid_candidate_support_is_rejected(fake_gemini, job, facets, policy, context):
    payload = _valid_payload()
    payload["requirements"]["must_have"][0]["candidate_support"] = "nonsense"
    fake_gemini.text = json.dumps(payload)
    with pytest.raises(EvaluationError, match="candidate_support"):
        evaluate_job(job, facets, context, policy, fake_gemini)


def test_a_support_verdict_per_stated_requirement_is_required(
    fake_gemini, job, facets, policy, context
):
    """Support is answered positionally against the requirements supplied.

    A response with a different number of verdicts cannot be lined up with the
    posting's requirements at all, so it is rejected rather than partially read
    -- silently dropping the one the model failed to answer would let a
    must-have nobody judged pass as if it had been.
    """
    payload = _valid_payload()
    payload["requirements"]["must_have"] = []
    fake_gemini.text = json.dumps(payload)
    with pytest.raises(EvaluationError, match="must_have"):
        evaluate_job(job, facets, context, policy, fake_gemini)


def test_the_scoring_call_may_not_invent_requirements(
    fake_gemini, job, facets, policy, context
):
    payload = _valid_payload()
    payload["requirements"]["preferred"].append(
        {"requirement": "Kubernetes", "candidate_support": "unsupported"}
    )
    fake_gemini.text = json.dumps(payload)
    with pytest.raises(EvaluationError, match="preferred"):
        evaluate_job(job, facets, context, policy, fake_gemini)


def test_major_unsupported_must_have_caps_score_below_possible(fake_gemini, job, policy, context):
    facets = make_facets(
        requirements=[
            {"requirement": "Deep PostgreSQL expertise", "depth": "deep_expert", "kind": "must_have"}
        ]
    )
    payload = _valid_payload(total_score=89)
    payload["requirements"] = {
        "must_have": [
            {"requirement": "Deep PostgreSQL expertise", "candidate_support": "unsupported"}
        ],
        "preferred": [],
    }
    fake_gemini.text = json.dumps(payload)
    evaluation = evaluate_job(job, facets, context, policy, fake_gemini)
    assert evaluation.raw_model_score == 89
    assert evaluation.total_score == policy.thresholds["possible"] - 1
    assert evaluation.total_score < policy.thresholds["possible"]
    assert evaluation.decision == "skip"


def test_familiarity_depth_unsupported_must_have_does_not_gate(fake_gemini, job, policy, context):
    facets = make_facets(
        requirements=[
            {"requirement": "Basic SQL familiarity", "depth": "familiarity", "kind": "must_have"}
        ]
    )
    payload = _valid_payload(total_score=89)
    payload["requirements"] = {
        "must_have": [
            {"requirement": "Basic SQL familiarity", "candidate_support": "unsupported"}
        ],
        "preferred": [],
    }
    fake_gemini.text = json.dumps(payload)
    evaluation = evaluate_job(job, facets, context, policy, fake_gemini)
    assert evaluation.decision == "high_priority"
    assert evaluation.total_score == 89


def test_unsupported_preferred_requirement_does_not_gate(fake_gemini, job, policy, context):
    facets = make_facets(
        requirements=[
            {"requirement": "React", "depth": "experience", "kind": "must_have"},
            {"requirement": "PostgreSQL", "depth": "deep_expert", "kind": "preferred"},
        ]
    )
    payload = _valid_payload(total_score=89)
    payload["requirements"] = {
        "must_have": [{"requirement": "React", "candidate_support": "supported"}],
        "preferred": [{"requirement": "PostgreSQL", "candidate_support": "unsupported"}],
    }
    fake_gemini.text = json.dumps(payload)
    evaluation = evaluate_job(job, facets, context, policy, fake_gemini)
    assert evaluation.decision == "high_priority"
    assert evaluation.total_score == 89


def test_insufficient_content_confidence_caps_below_high_priority(fake_gemini, job, facets, policy, context):
    job.content_confidence = PARTIAL_UNKNOWN
    payload = _valid_payload(total_score=89)
    fake_gemini.text = json.dumps(payload)
    evaluation = evaluate_job(job, facets, context, policy, fake_gemini)
    assert evaluation.decision == "possible_match"


def test_insufficient_content_still_allows_possible_match_and_skip(fake_gemini, job, facets, policy, context):
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
    evaluation = evaluate_job(job, facets, context, policy, fake_gemini)
    assert evaluation.decision == "skip"  # below possible threshold on its own merits


def test_hard_blockers_still_force_blocked_regardless_of_requirements(fake_gemini, job, facets, policy, context):
    payload = _valid_payload(total_score=89, hard_blockers=["Below salary floor"])
    fake_gemini.text = json.dumps(payload)
    evaluation = evaluate_job(job, facets, context, policy, fake_gemini)
    assert evaluation.decision == "blocked"


def test_evaluation_persists_content_confidence_and_requirements(fake_gemini, job, facets, policy, context):
    fake_gemini.text = json.dumps(_valid_payload())
    evaluation = evaluate_job(job, facets, context, policy, fake_gemini)
    assert evaluation.content_confidence == "official_ats"
    # The stored requirement keeps the posting's own text and depth, from the
    # facets, with only the support verdict coming from the scoring call.
    assert evaluation.requirements["must_have"] == [
        {"requirement": "React", "depth": "experience", "candidate_support": "supported"}
    ]
    assert evaluation.requirements["preferred"] == [
        {"requirement": "GraphQL", "depth": "familiarity", "candidate_support": "unknown"}
    ]


def test_prompt_includes_content_confidence_tier(fake_gemini, job, facets, policy, context):
    fake_gemini.text = json.dumps(_valid_payload())
    evaluate_job(job, facets, context, policy, fake_gemini)
    prompt = fake_gemini.prompts[0][0]
    assert "official_ats" in prompt


def test_prompt_states_the_requirements_rather_than_asking_for_them(
    fake_gemini, job, facets, policy, context
):
    fake_gemini.text = json.dumps(_valid_payload())
    evaluate_job(job, facets, context, policy, fake_gemini)
    prompt = fake_gemini.prompts[0][0]

    # The requirements arrive already read, with their depth...
    assert "React" in prompt
    assert "experience" in prompt
    assert "GraphQL" in prompt
    # ...and the call is asked to judge support, not to extract them again.
    assert "candidate_support" in prompt
    assert "Before scoring, extract the posting" not in prompt


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
    facets = make_facets(
        requirements=[
            {"requirement": "End-to-end forecasting", "depth": "experience", "kind": "must_have"}
        ]
    )
    payload = _valid_payload(total_score=89)
    payload["requirements"] = {
        "must_have": [
            {"requirement": "End-to-end forecasting", "candidate_support": "unsupported"}
        ],
        "preferred": [],
    }
    fake_gemini.text = json.dumps(payload)
    evaluation = evaluate_job(job, facets, context, policy, fake_gemini)
    assert evaluation.total_score == policy.thresholds["possible"] - 1
    assert evaluation.decision == "skip"


def test_partial_support_must_have_is_not_capped(fake_gemini, job, policy, context):
    facets = make_facets(
        requirements=[
            {"requirement": "Deep PostgreSQL expertise", "depth": "deep_expert", "kind": "must_have"}
        ]
    )
    payload = _valid_payload(total_score=89)
    payload["requirements"] = {
        "must_have": [
            {"requirement": "Deep PostgreSQL expertise", "candidate_support": "partial"}
        ],
        "preferred": [],
    }
    fake_gemini.text = json.dumps(payload)
    evaluation = evaluate_job(job, facets, context, policy, fake_gemini)
    assert evaluation.total_score == 89
    assert evaluation.raw_model_score == 89
    assert evaluation.decision == "high_priority"


def test_hard_blocker_takes_precedence_over_cap(fake_gemini, job, policy, context):
    facets = make_facets(
        requirements=[
            {"requirement": "Deep PostgreSQL expertise", "depth": "deep_expert", "kind": "must_have"}
        ]
    )
    payload = _valid_payload(total_score=89, hard_blockers=["Requires on-site in the US"])
    payload["requirements"] = {
        "must_have": [
            {"requirement": "Deep PostgreSQL expertise", "candidate_support": "unsupported"}
        ],
        "preferred": [],
    }
    fake_gemini.text = json.dumps(payload)
    evaluation = evaluate_job(job, facets, context, policy, fake_gemini)
    assert evaluation.decision == "blocked"
    assert evaluation.total_score == policy.thresholds["possible"] - 1
    assert evaluation.raw_model_score == 89


def test_uncapped_evaluation_keeps_raw_and_total_in_sync(fake_gemini, job, facets, policy, context):
    fake_gemini.text = json.dumps(_valid_payload())
    evaluation = evaluate_job(job, facets, context, policy, fake_gemini)
    assert evaluation.total_score == 89
    assert evaluation.raw_model_score == 89


# --- Scoring from facets, not from the description (issue #126) --------------


def test_the_scoring_prompt_does_not_carry_the_job_description(
    fake_gemini, facets, policy, context
):
    """The description is read once by extraction (#125) and never again.

    This is where the token saving lands: the posting text is the largest part
    of the old combined prompt and is re-sent for every user, every day, for a
    posting that has not changed.
    """
    description = "SECRET_DESCRIPTION_MARKER " + ("responsibilities and benefits " * 400)
    job = Job(
        source="ashby",
        title="Senior Product Engineer",
        description=description,
        content_confidence="official_ats",
    )
    fake_gemini.text = json.dumps(_valid_payload())
    evaluate_job(job, facets, context, policy, fake_gemini)
    prompt = fake_gemini.prompts[0][0]

    assert "SECRET_DESCRIPTION_MARKER" not in prompt
    assert description not in prompt
    # The combined prompt was this prompt's material plus the whole posting,
    # so that sum is the fair baseline to measure the fall against. A typical
    # posting dominates it, and the fall has to be material rather than
    # incidental -- more than half of what the combined call carried.
    combined_prompt_size = len(prompt) + len(description)
    assert len(prompt) < combined_prompt_size / 2


def test_the_scoring_prompt_carries_the_facets_instead(
    fake_gemini, job, policy, context
):
    facets = make_facets(
        seniority="staff",
        remote_policy="hybrid",
        relocation_policy="offered",
        hiring_regions=["europe", "north_america"],
        stack=["rust", "postgres"],
        compensation=Compensation(
            disclosed=True, currency="EUR", minimum=90000, maximum=110000, period="year"
        ),
    )
    fake_gemini.text = json.dumps(_valid_payload())
    evaluate_job(job, facets, context, policy, fake_gemini)
    prompt = fake_gemini.prompts[0][0]
    lower = prompt.lower()

    assert "staff" in lower
    assert "hybrid" in lower
    assert "offered" in lower
    assert "north_america" in lower
    assert "rust" in lower
    assert "eur" in lower
    assert "90000" in prompt
    assert "110000" in prompt


def test_undisclosed_compensation_reaches_the_prompt_as_undisclosed(
    fake_gemini, job, facets, policy, context
):
    """Silence must not read as zero: an undisclosed salary is unknown, and the
    prompt has to say so rather than leaving the field blank."""
    fake_gemini.text = json.dumps(_valid_payload())
    evaluate_job(job, facets, context, policy, fake_gemini)
    prompt = fake_gemini.prompts[0][0]

    assert "not disclosed" in prompt.lower()


def test_compensation_claiming_disclosure_without_a_figure_reads_as_undisclosed(
    fake_gemini, job, policy, context
):
    """A row `facets.py` cannot write, but `job_facets_from_row` can read.

    Rendering it literally would put "up to None" in front of the model, and
    an unreadable pay figure is exactly the case that must read as silence.
    """
    facets = make_facets(
        compensation=Compensation(disclosed=True, currency="EUR", period="year")
    )
    fake_gemini.text = json.dumps(_valid_payload())
    evaluate_job(job, facets, context, policy, fake_gemini)
    prompt = fake_gemini.prompts[0][0]

    assert "Disclosed compensation: not disclosed" in prompt
    assert "up to None" not in prompt


def test_a_posting_that_states_no_requirements_is_scored_without_any(
    fake_gemini, job, policy, context
):
    """Empty facets requirements are a fact about the posting, not a failure.

    A job whose facets are *missing* is a different case and is refused; see
    `test_scoring_without_facets_is_refused`.
    """
    facets = make_facets(requirements=[])
    payload = _valid_payload()
    payload["requirements"] = {"must_have": [], "preferred": []}
    fake_gemini.text = json.dumps(payload)

    evaluation = evaluate_job(job, facets, context, policy, fake_gemini)

    assert evaluation.requirements == {"must_have": [], "preferred": []}
    assert evaluation.decision == "high_priority"


def test_scoring_without_facets_is_refused(fake_gemini, job, policy, context):
    """No facets is handled explicitly rather than scored against nothing.

    Scoring a job as if the posting stated no requirements would silently turn
    "we have not read this posting" into "this posting demands nothing", which
    inflates the score of exactly the jobs nothing is known about.
    """
    fake_gemini.text = json.dumps(_valid_payload())
    with pytest.raises(EvaluationError, match="facets"):
        evaluate_job(job, None, context, policy, fake_gemini)

    assert fake_gemini.prompts == []
