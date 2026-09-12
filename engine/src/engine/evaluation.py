"""Subjective scoring: how well one posting fits one person (issue #126).

This is the per-user half of the objective-extraction / subjective-scoring
split described in [CONTEXT.md](../../../../CONTEXT.md). It is handed the
posting's **facets** -- the requirements it states and the depth each demands,
its disclosed pay, where it will hire, its remote and relocation policy, its
seniority and its stack -- already read once by `facets.py`, and it never sees
the posting text. Judging whether *this* candidate satisfies each stated
requirement stays here, because that answer is different for every user and
cannot be shared.

The module, the stored row and the provider purpose keep the name
"evaluation" because the artefact they persist is still an `Evaluation`; the
combined operation that read the posting and scored it in one call is gone.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from engine.ai import CallClass
from engine import content_confidence
from engine.market_eligibility import evaluate_market_eligibility
from engine.market_policy import market_by_id, salary_floor_for_job
from engine.models import (
    CandidateContext,
    CompanyFacets,
    CompanyPreferences,
    Compensation,
    Evaluation,
    Job,
    JobFacets,
    MarketPolicy,
    SearchPolicy,
)

if TYPE_CHECKING:
    from engine.ai import AIProvider

SCORE_MAXIMA = {
    "role_seniority": 30,
    "technical": 25,
    "product_architecture": 20,
    "career_direction": 10,
    "location_language": 10,
    "company_environment": 5,
}

HIGH_PRIORITY_THRESHOLD = 85

# One retry for transient provider 5xx/timeout failures during evaluation; see
# the adapter's generate_text max_attempts docstring for what qualifies.
_EVALUATION_MAX_ATTEMPTS = 2
# One fresh sample when a completed response still fails the evaluation parser.
_EVALUATION_PARSE_MAX_ATTEMPTS = 2

_VALID_SUPPORT = {"supported", "partial", "unsupported", "unknown"}

_TIER_PROMPT_HINTS = {
    content_confidence.OFFICIAL_ATS: "This is the official employer/ATS posting text.",
    content_confidence.CANONICAL_EMPLOYER_PAGE: "This was extracted from the employer's own careers page.",
    content_confidence.SOURCE_DETAIL_PAGE: "This is a full detail-page scrape; likely complete but not confirmed authoritative.",
    content_confidence.AGGREGATOR_TEXT: "This is third-party aggregator or community text and may be incomplete or stale.",
    content_confidence.PARTIAL_UNKNOWN: "This content is thin or unverified. Prefer 'unknown' candidate_support over guessing when the posting doesn't clearly state a requirement.",
}

_REQUIREMENT_SUPPORT_RULES = """The posting's requirements have already been read from it and are listed below,
each with the depth it demands. Do not add to them, drop any, merge them, or reword them.
For each one, in the order given, judge candidate_support strictly from the candidate
context evidence below: supported, partial, unsupported, or unknown. Do not infer expertise
from adjacent technology mentions alone (for example, React experience is not backend
expertise, and API collaboration is not evidence of independently designing backend
systems)."""


#: The response contract, identical on both prompt paths. The support lists
#: are answered positionally against the requirements the posting block lists,
#: so the ordering instruction is part of the shape rather than advice.
_RESPONSE_SHAPE = """Return ONLY JSON with this exact shape and no markdown fences:
{"scores": {"role_seniority": int, "technical": int, "product_architecture": int, "career_direction": int, "location_language": int, "company_environment": int}, "total_score": int, "hard_blockers": [string], "strengths": [string], "gaps": [string], "salary_note": string, "location_note": string, "decision": string, "rationale": string, "requirements": {"must_have": [{"requirement": string, "candidate_support": string}], "preferred": [{"requirement": string, "candidate_support": string}]}}

requirements.must_have and requirements.preferred must each carry exactly one entry per requirement listed under "Stated must-have requirements" and "Stated preferred requirements" below, in the same order, echoing the requirement text unchanged. Never add a requirement, drop one, or reorder them."""


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
# senior frontend engineer, not a senior backend engineer, and the model must
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
- Only use evidence present in the candidate context and the posting facts below. Never invent candidate facts.
- A fact the posting did not state arrives as "unknown", an empty list, or undisclosed pay. None of those means "no".
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


def _format_compensation(compensation: Compensation) -> str:
    """Render disclosed pay, or say plainly that the posting disclosed none.

    "not disclosed" is spelled out rather than left blank: an empty field
    invites the model to read silence as zero, and a zero salary is a hard
    blocker under every market policy.

    A row claiming disclosure but carrying neither bound is read as no
    disclosure. `facets.py` cannot produce that, but `job_facets_from_row`
    reads the columns straight, so a row written before that rule existed can
    -- and rendering it would put "up to None" in front of the model.
    """
    if not compensation.disclosed:
        return "not disclosed"
    if compensation.minimum is None and compensation.maximum is None:
        return "not disclosed"
    if compensation.minimum is not None and compensation.maximum is not None:
        amount = f"{compensation.minimum}-{compensation.maximum}"
    elif compensation.minimum is not None:
        amount = f"from {compensation.minimum}"
    else:
        amount = f"up to {compensation.maximum}"
    period = f" per {compensation.period}" if compensation.period else ""
    return f"{compensation.currency} {amount}{period}".strip()


def _stated_requirements(facets: JobFacets, kind: str) -> list[dict[str, str]]:
    """The posting's requirements of one kind, in the order they were read.

    Order is the contract between the prompt and the response: support
    verdicts come back positionally, so the same list has to be rendered and
    validated against.
    """
    return [item for item in facets.requirements if item.get("kind") == kind]


def _support_list_schema(item_count: int) -> dict:
    return {
        "type": "ARRAY",
        "items": {
            "type": "OBJECT",
            "properties": {
                "requirement": {"type": "STRING"},
                "candidate_support": {"type": "STRING", "enum": sorted(_VALID_SUPPORT)},
            },
            "required": ["requirement", "candidate_support"],
        },
        "minItems": item_count,
        "maxItems": item_count,
    }


def _evaluation_response_schema(facets: JobFacets) -> dict:
    """Describe the complete response consumed by the scoring parser."""
    properties = {
        "scores": {
            "type": "OBJECT",
            "properties": {
                name: {"type": "INTEGER", "minimum": 0, "maximum": maximum}
                for name, maximum in SCORE_MAXIMA.items()
            },
            "required": list(SCORE_MAXIMA),
        },
        "total_score": {"type": "INTEGER", "minimum": 0, "maximum": sum(SCORE_MAXIMA.values())},
        "hard_blockers": {"type": "ARRAY", "items": {"type": "STRING"}},
        "strengths": {"type": "ARRAY", "items": {"type": "STRING"}},
        "gaps": {"type": "ARRAY", "items": {"type": "STRING"}},
        "salary_note": {"type": "STRING"},
        "location_note": {"type": "STRING"},
        "decision": {"type": "STRING"},
        "rationale": {"type": "STRING"},
        "requirements": {
            "type": "OBJECT",
            "properties": {
                kind: _support_list_schema(len(_stated_requirements(facets, kind)))
                for kind in ("must_have", "preferred")
            },
            "required": ["must_have", "preferred"],
        },
    }
    return {"type": "OBJECT", "properties": properties, "required": list(properties)}


def _render_requirements(label: str, items: list[dict[str, str]]) -> str:
    if not items:
        return f"Stated {label} requirements: none stated"
    lines = [f"Stated {label} requirements, in order:"]
    lines += [
        f"{index}. {item['requirement']} (required depth: {item['depth']})"
        for index, item in enumerate(items, start=1)
    ]
    return "\n".join(lines)


def _posting_block(job: Job, facets: JobFacets) -> str:
    """Everything the scoring call learns about the posting.

    Deliberately not the posting text. It was read once by `facets.py` and
    turned into these facts; re-sending it for every user, every day, for a
    posting that has not changed is the cost this split exists to remove.
    """
    confidence = job.content_confidence or content_confidence.PARTIAL_UNKNOWN
    hint = _TIER_PROMPT_HINTS.get(confidence, _TIER_PROMPT_HINTS[content_confidence.PARTIAL_UNKNOWN])
    return "\n".join(
        [
            f"Job title: {job.title}",
            f"Company: {job.company}",
            f"Location: {job.location}",
            f"Remote: {job.remote}",
            f"Job content confidence: {confidence} — {hint}",
            "",
            "Posting facts, read from the posting itself. The posting text is not repeated here:",
            f"- Seniority: {facets.seniority}",
            f"- Remote policy: {facets.remote_policy}",
            f"- Relocation policy: {facets.relocation_policy}",
            f"- Hiring regions: {', '.join(facets.hiring_regions) or 'none stated'}",
            f"- Stack: {', '.join(facets.stack) or 'none named'}",
            f"- Disclosed compensation: {_format_compensation(facets.compensation)}",
            "",
            _render_requirements("must-have", _stated_requirements(facets, "must_have")),
            "",
            _render_requirements("preferred", _stated_requirements(facets, "preferred")),
        ]
    )


#: How each company dimension is labelled in the prompt. Rendered in a fixed
#: order so the same company always produces the same block.
_COMPANY_DIMENSIONS = (
    ("industry", "Industry"),
    ("business_model", "Business model"),
    ("stage", "Stage"),
    ("size_band", "Approximate size"),
    ("headquarters_region", "Headquarters region"),
)


def _company_block(company: CompanyFacets | None, preferences: CompanyPreferences) -> str:
    """What the scoring call learns about the employer, and what to do with it.

    Both halves are needed for the call to mean anything. The facts alone
    would not tell the model which of them this candidate cares about, and the
    stated preferences alone would have nothing to compare against.

    A company nobody has read yet is described as exactly that, with an
    instruction to score the dimension neutrally. Saying nothing instead would
    leave the model to fill the silence, and it fills it by inferring an
    employer from the posting -- which is a guess presented as a fact, and the
    one failure mode this whole feature must not have (#198, user story 6).
    """
    lines = ["Company facts, read once from the employer rather than from this posting:"]
    if company is None or not company.is_known():
        lines.append(
            "- Nothing has been established about this employer yet. Score "
            "company fit neutrally: this is a gap in what has been read, not "
            "evidence that the company is a poor fit, and it must neither "
            "raise nor lower the score."
        )
    else:
        lines += [
            f"- {label}: {getattr(company, name)}"
            for name, label in _COMPANY_DIMENSIONS
        ]
        lines.append(
            'A dimension recorded as "unknown" was not established. Treat it '
            "as absent, never as a mismatch."
        )

    stated = [
        (label, values)
        for label, values in (
            ("prefers companies in", preferences.preferred_industries),
            ("will not work in", preferences.excluded_industries),
            ("prefers the business model", preferences.preferred_business_models),
            ("will not work for", preferences.excluded_business_models),
            ("prefers companies at stage", preferences.preferred_stages),
            ("prefers company size", preferences.preferred_size_bands),
        )
        if values
    ]
    if stated:
        lines.append("")
        lines.append("What this candidate has stated about the kind of employer they want:")
        lines += [f"- {label}: {', '.join(values)}" for label, values in stated]

    return "\n".join(lines)


def _build_evaluation_prompt(
    job: Job,
    facets: JobFacets,
    context: CandidateContext,
    policy: SearchPolicy,
    market: MarketPolicy | None = None,
    company: CompanyFacets | None = None,
) -> str:
    maxima_lines = "\n".join(f"- {key}: max {value}" for key, value in SCORE_MAXIMA.items())

    if market is None:
        return f"""You are evaluating a job posting against a candidate profile for a remote-only job search.

Score EXACTLY these components, each an integer from 0 up to its stated maximum:
{maxima_lines}

{_REQUIREMENT_SUPPORT_RULES}

Rules:
- Only use evidence present in the candidate context and the posting facts below. Never invent candidate facts.
- A fact the posting did not state arrives as "unknown", an empty list, or undisclosed pay. None of those means "no".
- Compensation floor is EUR {policy.salary_floor_eur}. A disclosed maximum below the floor is a hard blocker.
- A role that is not remote, or requires relocation, is a hard blocker.
- List every hard blocker in hard_blockers; otherwise leave it empty.

{_RESPONSE_SHAPE}

Candidate context:
{_serialize_context(context)}

{_posting_block(job, facets)}

{_company_block(company, policy.company_preferences)}
"""

    return f"""You are evaluating a job posting against a candidate profile for a market-driven job search. Remote, hybrid, onsite, and relocation compatibility is governed by the specific market policy below, not by a single global remote-only rule.

Score EXACTLY these components, each an integer from 0 up to its stated maximum:
{maxima_lines}

{_REQUIREMENT_SUPPORT_RULES}

{_market_rules_block(job)}

Market policy:
{_market_policy_block(job, market)}

{_RESPONSE_SHAPE}

Candidate context:
{_serialize_context(context)}

{_posting_block(job, facets)}

{_company_block(company, policy.company_preferences)}
"""


def _validate_support_list(
    items: object, label: str, stated: list[dict[str, str]]
) -> list[dict]:
    """Pair each stated requirement with the verdict the scoring call gave it.

    The requirement text and its depth are the posting's, taken from the
    facets, and only `candidate_support` comes from the model -- so the call
    can neither invent a requirement nor quietly restate one at a depth the
    posting never demanded.

    A response that does not carry exactly one verdict per stated requirement
    cannot be lined up with them at all, and is rejected rather than read as
    far as it goes: dropping the verdict the model failed to give would let an
    unjudged must-have pass as though it had been judged.
    """
    if not isinstance(items, list):
        raise EvaluationError(f"requirements.{label} must be a list")
    if len(items) != len(stated):
        raise EvaluationError(
            f"requirements.{label} must carry exactly one candidate_support verdict per "
            f"stated requirement ({len(stated)}), got {len(items)}"
        )
    validated = []
    for item, requirement in zip(items, stated):
        if not isinstance(item, dict):
            raise EvaluationError(f"each requirements.{label} entry must be an object")
        support = item.get("candidate_support")
        if support not in _VALID_SUPPORT:
            raise EvaluationError(
                f"requirements.{label}.candidate_support {support!r} must be one of {sorted(_VALID_SUPPORT)}"
            )
        validated.append(
            {
                "requirement": requirement["requirement"],
                "depth": requirement["depth"],
                "candidate_support": support,
            }
        )
    return validated


def _capped_score(total: int, possible_threshold: int) -> int:
    """Lower `total` so it cannot sit in the `possible_match` band or above.

    Applied when the candidate has no support for a core requirement. The cap
    is derived from configuration rather than hardcoded so it tracks
    `policy.thresholds["possible"]`; `max(0, ...)` guards a threshold of 0.
    """
    return min(total, max(0, possible_threshold - 1))


def _parse_evaluation_response(
    raw: str,
    job: Job,
    facets: JobFacets,
    policy: SearchPolicy,
    model: str,
) -> Evaluation:
    cleaned = _strip_code_fences(raw)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise EvaluationError(f"the model returned invalid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise EvaluationError("the model's response must be a JSON object")

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
    must_have = _validate_support_list(
        requirements["must_have"], "must_have", _stated_requirements(facets, "must_have")
    )
    preferred = _validate_support_list(
        requirements["preferred"], "preferred", _stated_requirements(facets, "preferred")
    )

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
        model=model,
        market_id=job.market_id or "",
        content_confidence=job.content_confidence or content_confidence.PARTIAL_UNKNOWN,
        requirements={"must_have": must_have, "preferred": preferred},
        raw_model_score=raw_total,
    )


def evaluate_job(
    job: Job,
    facets: JobFacets | None,
    context: CandidateContext,
    policy: SearchPolicy,
    ai: "AIProvider",
    company: CompanyFacets | None = None,
) -> Evaluation:
    """Score `job` for this candidate from the facets already read from it.

    `facets` is required. A job whose facets are missing -- extraction has not
    reached it yet, or failed -- must not be scored: an absent requirements
    list is indistinguishable, inside the prompt, from a posting that demands
    nothing, and scoring it that way inflates exactly the jobs nothing is known
    about. The caller leaves such a job for a later run.

    `company` is optional and stays optional (#198). The employer's facts are
    extra evidence, not a precondition: a company nobody has read yet is
    described to the model as unread and scored neutrally, so a gap in the
    company corpus can never cost a posting its evaluation the way missing
    facets do.

    A completed response that the parser rejects gets one fresh provider call.
    Provider and quota errors are not caught here, so a refusal can never be
    mistaken for a parse failure or consume the parse-retry allowance.

    Raises `EvaluationError` on missing facets and when both response samples
    fail the complete-result parser.
    """
    if facets is None:
        raise EvaluationError(
            f"cannot score job {job.url or job.title!r} without its extracted facets"
        )

    market = market_by_id(policy, job.market_id) if job.market_id and policy.markets else None
    prompt = _build_evaluation_prompt(job, facets, context, policy, market, company)
    schema = _evaluation_response_schema(facets)

    for parse_attempt in range(1, _EVALUATION_PARSE_MAX_ATTEMPTS + 1):
        raw = ai.generate_text(
            prompt,
            call_class=CallClass.USER_SUBJECTIVE,
            purpose="job_evaluation",
            thinking_level="medium",
            max_output_tokens=5000,
            json_mode=True,
            json_schema=schema,
            max_attempts=_EVALUATION_MAX_ATTEMPTS,
        )
        try:
            return _parse_evaluation_response(raw, job, facets, policy, ai.model)
        except EvaluationError:
            if parse_attempt == _EVALUATION_PARSE_MAX_ATTEMPTS:
                raise

    raise AssertionError("unreachable")
