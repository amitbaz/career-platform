from __future__ import annotations

import json
from typing import TYPE_CHECKING

from job_hunter import content_confidence
from job_hunter.market_eligibility import evaluate_market_eligibility
from job_hunter.market_policy import market_by_id, salary_floor_for_job
from job_hunter.models import CandidateContext, Evaluation, Job, MarketPolicy, SearchPolicy

if TYPE_CHECKING:
    from job_hunter.gemini import GeminiClient

SCORE_MAXIMA = {
    "role_seniority": 30,
    "technical": 25,
    "product_architecture": 20,
    "career_direction": 10,
    "location_language": 10,
    "company_environment": 5,
}

HIGH_PRIORITY_THRESHOLD = 85

# One retry for transient Gemini 5xx/timeout failures during evaluation; see
# GeminiClient.generate_text's max_attempts docstring for what qualifies.
_EVALUATION_MAX_ATTEMPTS = 2

_VALID_DEPTHS = {"familiarity", "experience", "deep_expert"}
_VALID_SUPPORT = {"supported", "partial", "unsupported", "unknown"}

_TIER_PROMPT_HINTS = {
    content_confidence.OFFICIAL_ATS: "This is the official employer/ATS posting text.",
    content_confidence.CANONICAL_EMPLOYER_PAGE: "This was extracted from the employer's own careers page.",
    content_confidence.SOURCE_DETAIL_PAGE: "This is a full detail-page scrape; likely complete but not confirmed authoritative.",
    content_confidence.AGGREGATOR_TEXT: "This is third-party aggregator or community text and may be incomplete or stale.",
    content_confidence.PARTIAL_UNKNOWN: "This content is thin or unverified. Prefer 'unknown' candidate_support over guessing when the posting doesn't clearly state a requirement.",
}

_REQUIREMENT_EXTRACTION_RULES = """Before scoring, extract the posting's explicit requirements:
- must_have: requirements the posting states or clearly implies are required.
- preferred: requirements stated as nice-to-have, a plus, or preferred.
For each, state its required depth (familiarity, experience, or deep_expert) and classify
candidate_support strictly from the candidate context evidence below: supported, partial,
unsupported, or unknown. Do not infer expertise from adjacent technology mentions alone
(for example, React experience is not backend expertise, and API collaboration is not
evidence of independently designing backend systems). Do not invent requirements that are
not stated or clearly implied by the posting."""


class EvaluationError(ValueError):
    pass


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text.strip()


def _format_evidence(label: str, values: list[str]) -> str:
    if not values:
        return f"{label}: none noted"
    return f"{label}: " + "; ".join(values)


def _serialize_context(context: CandidateContext) -> str:
    """Render a CandidateContext as compact factual text for a prompt.

    Replaces the old wholesale full-profile dump: only the extracted,
    validated evidence and preferences are sent, once per job instead of
    the entire raw candidate profile.
    """
    prefs = context.preferences
    lines = [
        f"Candidate summary: {context.evaluation_summary}",
        _format_evidence("Preferred roles", prefs.preferred_roles),
        _format_evidence("Preferred seniority", prefs.preferred_seniority),
        _format_evidence("Must-have signals", prefs.must_have_signals),
        _format_evidence("Nice-to-have signals", prefs.nice_to_have_signals),
        _format_evidence("Preferred locations", prefs.preferred_locations),
        _format_evidence("Signals to avoid", prefs.avoid_signals),
        _format_evidence("Technical skills", context.technical_skills),
        _format_evidence("Architecture evidence", context.architecture_evidence),
        _format_evidence("Leadership/ownership evidence", context.leadership_ownership),
        _format_evidence("Agentic AI evidence", context.agentic_ai_evidence),
        _format_evidence("Product/domain evidence", context.product_domain_evidence),
        _format_evidence("Location/language facts", context.location_language_facts),
        _format_evidence("Career direction", context.career_direction),
        _format_evidence("Company environment preferences", context.company_environment),
    ]
    return "\n".join(lines)


_FULL_STACK_TITLE_MARKERS = ("full stack", "full-stack")

# Verbatim per the market-driven search strategy plan: the candidate is a
# senior frontend engineer, not a senior backend engineer, and Gemini must
# not credit backend seniority it has no evidence for.
_FULL_STACK_BACKEND_RAMP_PARAGRAPH = (
    "The candidate is a senior frontend engineer but is earlier than junior-level "
    "in backend depth today. Treat React/Next.js/TypeScript ownership as senior "
    "evidence. Node.js/TypeScript APIs, REST/GraphQL, PostgreSQL/Supabase and "
    "similar product-backend work may be realistic ramp-up areas. Do not invent "
    "senior backend experience. Backend-dominant ownership is a gap and may make "
    "the role unsuitable."
)


def _is_full_stack_role(title: str) -> bool:
    normalized = (title or "").lower()
    return any(marker in normalized for marker in _FULL_STACK_TITLE_MARKERS)


def _market_policy_block(job: Job, market: MarketPolicy) -> str:
    """Render the market's configured policy plus deterministic eligibility
    signals (reusing evaluate_market_eligibility rather than reimplementing
    sponsorship/remote/warning detection) for the prompt."""
    eligibility = evaluate_market_eligibility(job, market)
    salary_floor = salary_floor_for_job(job, market)
    allowed_languages = ", ".join(market.allowed_languages) or "none configured"
    warnings_text = "; ".join(eligibility.warnings) if eligibility.warnings else "none noted"

    return "\n".join(
        [
            f"Market ID: {market.id}",
            f"Gross base salary floor: {market.salary.currency} {salary_floor}",
            f"Allowed required languages: {allowed_languages}",
            f"Remote policy: {market.remote_policy}",
            f"Relocation policy: {market.relocation_policy}",
            f"Sponsorship policy: {market.sponsorship_policy}",
            f"Deterministic sponsorship status: {eligibility.sponsorship_status}",
            f"Deterministic international-remote status: {eligibility.international_remote_status}",
            f"Deterministic warnings: {warnings_text}",
        ]
    )


def _market_rules_block(job: Job) -> str:
    rules = """Rules:
- Only use evidence present in the candidate context and job description below. Never invent candidate facts.
- Unstated or unclear requirements are gaps, not invented facts.
- Missing salary is unknown, not a blocker.
- Disclosed gross base max below market floor is a blocker.
- Hybrid/onsite/relocation is not a blocker when market policy allows it.
- Explicit no-sponsorship is a blocker when sponsorship is required; omission is unknown.
- Disallowed language blocks only when explicitly required; nice-to-have does not.
- Time-zone overlap is informational and must be preserved in location_note.
- Sponsorship/international-remote uncertainty must be preserved in location_note.
- List every hard blocker in hard_blockers; otherwise leave it empty."""

    if _is_full_stack_role(job.title):
        rules += "\n\n" + _FULL_STACK_BACKEND_RAMP_PARAGRAPH

    return rules


def _build_evaluation_prompt(
    job: Job,
    context: CandidateContext,
    policy: SearchPolicy,
    market: MarketPolicy | None = None,
) -> str:
    maxima_lines = "\n".join(f"- {key}: max {value}" for key, value in SCORE_MAXIMA.items())

    if market is None:
        return f"""You are evaluating a job posting against a candidate profile for a remote-only job search.

Score EXACTLY these components, each an integer from 0 up to its stated maximum:
{maxima_lines}

{_REQUIREMENT_EXTRACTION_RULES}

Rules:
- Only use evidence present in the candidate context and job description below. Never invent candidate facts.
- Unstated or unclear requirements are gaps, not invented facts.
- Compensation floor is EUR {policy.salary_floor_eur}. A disclosed maximum below the floor is a hard blocker.
- A role that is not remote, or requires relocation, is a hard blocker.
- List every hard blocker in hard_blockers; otherwise leave it empty.

Return ONLY JSON with this exact shape and no markdown fences:
{{"scores": {{"role_seniority": int, "technical": int, "product_architecture": int, "career_direction": int, "location_language": int, "company_environment": int}}, "total_score": int, "hard_blockers": [string], "strengths": [string], "gaps": [string], "salary_note": string, "location_note": string, "decision": string, "rationale": string, "requirements": {{"must_have": [{{"requirement": string, "depth": string, "candidate_support": string}}], "preferred": [{{"requirement": string, "depth": string, "candidate_support": string}}]}}}}

Candidate context:
{_serialize_context(context)}

Job title: {job.title}
Company: {job.company}
Location: {job.location}
Remote: {job.remote}
Job content confidence: {job.content_confidence or content_confidence.PARTIAL_UNKNOWN} — {_TIER_PROMPT_HINTS.get(job.content_confidence, _TIER_PROMPT_HINTS[content_confidence.PARTIAL_UNKNOWN])}
Job description:
{job.description}
"""

    return f"""You are evaluating a job posting against a candidate profile for a market-driven job search. Remote, hybrid, onsite, and relocation compatibility is governed by the specific market policy below, not by a single global remote-only rule.

Score EXACTLY these components, each an integer from 0 up to its stated maximum:
{maxima_lines}

{_REQUIREMENT_EXTRACTION_RULES}

{_market_rules_block(job)}

Market policy:
{_market_policy_block(job, market)}

Return ONLY JSON with this exact shape and no markdown fences:
{{"scores": {{"role_seniority": int, "technical": int, "product_architecture": int, "career_direction": int, "location_language": int, "company_environment": int}}, "total_score": int, "hard_blockers": [string], "strengths": [string], "gaps": [string], "salary_note": string, "location_note": string, "decision": string, "rationale": string, "requirements": {{"must_have": [{{"requirement": string, "depth": string, "candidate_support": string}}], "preferred": [{{"requirement": string, "depth": string, "candidate_support": string}}]}}}}

Candidate context:
{_serialize_context(context)}

Job title: {job.title}
Company: {job.company}
Location: {job.location}
Remote: {job.remote}
Job content confidence: {job.content_confidence or content_confidence.PARTIAL_UNKNOWN} — {_TIER_PROMPT_HINTS.get(job.content_confidence, _TIER_PROMPT_HINTS[content_confidence.PARTIAL_UNKNOWN])}
Job description:
{job.description}
"""


def _validate_requirement_list(items: object, label: str) -> list[dict]:
    if not isinstance(items, list):
        raise EvaluationError(f"requirements.{label} must be a list")
    validated = []
    for item in items:
        if not isinstance(item, dict):
            raise EvaluationError(f"each requirements.{label} entry must be an object")
        requirement = item.get("requirement")
        depth = item.get("depth")
        support = item.get("candidate_support")
        if not isinstance(requirement, str) or not requirement:
            raise EvaluationError(f"requirements.{label}.requirement must be a non-empty string")
        if depth not in _VALID_DEPTHS:
            raise EvaluationError(f"requirements.{label}.depth {depth!r} must be one of {sorted(_VALID_DEPTHS)}")
        if support not in _VALID_SUPPORT:
            raise EvaluationError(
                f"requirements.{label}.candidate_support {support!r} must be one of {sorted(_VALID_SUPPORT)}"
            )
        validated.append({"requirement": requirement, "depth": depth, "candidate_support": support})
    return validated


def _capped_score(total: int, possible_threshold: int) -> int:
    """Lower `total` so it cannot sit in the `possible_match` band or above.

    Applied when the candidate has no support for a core requirement. The cap
    is derived from configuration rather than hardcoded so it tracks
    `policy.thresholds["possible"]`; `max(0, ...)` guards a threshold of 0.
    """
    return min(total, max(0, possible_threshold - 1))


def evaluate_job(job: Job, context: CandidateContext, policy: SearchPolicy, gemini: "GeminiClient") -> Evaluation:
    market = market_by_id(policy, job.market_id) if job.market_id and policy.markets else None

    raw = gemini.generate_text(
        _build_evaluation_prompt(job, context, policy, market),
        purpose="job_evaluation",
        thinking_level="medium",
        max_output_tokens=5000,
        json_mode=True,
        max_attempts=_EVALUATION_MAX_ATTEMPTS,
    )
    cleaned = _strip_code_fences(raw)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise EvaluationError(f"Gemini returned invalid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise EvaluationError("Gemini response must be a JSON object")

    scores = data.get("scores")
    if not isinstance(scores, dict) or set(scores.keys()) != set(SCORE_MAXIMA.keys()):
        raise EvaluationError(f"scores must contain exactly the keys {sorted(SCORE_MAXIMA)}")

    total = 0
    for key, maximum in SCORE_MAXIMA.items():
        value = scores[key]
        if not isinstance(value, int) or isinstance(value, bool):
            raise EvaluationError(f"score {key!r} must be an integer")
        if value < 0 or value > maximum:
            raise EvaluationError(f"score {key!r}={value} is outside 0..{maximum}")
        total += value

    declared_total = data.get("total_score")
    if declared_total != total:
        raise EvaluationError(f"total_score {declared_total!r} does not match component sum {total}")

    hard_blockers = data.get("hard_blockers") or []
    if not isinstance(hard_blockers, list):
        raise EvaluationError("hard_blockers must be a list")

    requirements = data.get("requirements")
    if not isinstance(requirements, dict) or "must_have" not in requirements or "preferred" not in requirements:
        raise EvaluationError("requirements must be an object with 'must_have' and 'preferred' lists")
    must_have = _validate_requirement_list(requirements["must_have"], "must_have")
    preferred = _validate_requirement_list(requirements["preferred"], "preferred")

    major_unsupported_must_have = any(
        item["candidate_support"] == "unsupported" and item["depth"] != "familiarity"
        for item in must_have
    )
    insufficient_content = not content_confidence.is_sufficient(job.content_confidence)
    # A major unsupported must-have no longer needs to gate the decision ladder:
    # capping the score below `possible` already puts it out of reach of the
    # `package_match` and `high_priority` rungs. Thin postings still gate here,
    # because failing to read a description is not evidence of a poor fit.
    confident_decision_available = not insufficient_content

    possible_threshold = policy.thresholds.get("possible", 65)
    raw_total = total
    if major_unsupported_must_have:
        total = _capped_score(total, possible_threshold)

    if hard_blockers:
        decision = "blocked"
    elif total >= HIGH_PRIORITY_THRESHOLD and confident_decision_available:
        decision = "high_priority"
    elif total >= policy.thresholds.get("package", 75) and confident_decision_available:
        decision = "package_match"
    elif total >= possible_threshold:
        decision = "possible_match"
    else:
        decision = "skip"

    return Evaluation(
        job_id=0,
        total_score=total,
        scores=scores,
        decision=decision,
        hard_blockers=hard_blockers,
        strengths=data.get("strengths") or [],
        gaps=data.get("gaps") or [],
        salary_note=data.get("salary_note", "") or "",
        location_note=data.get("location_note", "") or "",
        rationale=data.get("rationale", "") or "",
        model=gemini.model,
        market_id=job.market_id or "",
        content_confidence=job.content_confidence or content_confidence.PARTIAL_UNKNOWN,
        requirements={"must_have": must_have, "preferred": preferred},
        raw_model_score=raw_total,
    )
