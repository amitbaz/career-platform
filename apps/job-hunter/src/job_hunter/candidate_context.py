from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict
from typing import TYPE_CHECKING

from job_hunter.gemini import GeminiIncompleteResponse
from job_hunter.gemini_usage import GeminiBudgetExceeded, GeminiQuotaPaused
from job_hunter.models import CandidateContext, CandidatePreferences, SearchPolicy
from job_hunter.normalize import normalize_text
from job_hunter.preferences import _build_fallback_preferences

if TYPE_CHECKING:
    from job_hunter.gemini import GeminiClient
    from job_hunter.store import JobStore

logger = logging.getLogger(__name__)

CANDIDATE_CONTEXT_SCHEMA_VERSION = "1"
FALLBACK_CONTEXT_SUMMARY = "Fallback candidate context derived from search policy."

_EVIDENCE_FIELDS = (
    "technical_skills",
    "architecture_evidence",
    "leadership_ownership",
    "agentic_ai_evidence",
    "product_domain_evidence",
    "location_language_facts",
    "career_direction",
    "company_environment",
    "career_evidence",
)
_TOP_LEVEL_FIELDS = ("preferences", *_EVIDENCE_FIELDS, "evaluation_summary")
_MAX_LIST_ITEMS = 20
_MAX_ITEM_LENGTH = 180
_MAX_SUMMARY_LENGTH = 1500
_INITIAL_OUTPUT_TOKENS = 1800
_RETRY_OUTPUT_TOKENS = 3600

_PREFERENCES_FIELDS = (
    "preferred_roles",
    "preferred_seniority",
    "must_have_signals",
    "nice_to_have_signals",
    "preferred_locations",
    "avoid_signals",
    "summary",
)
_PREFERENCES_LIST_FIELDS = tuple(f for f in _PREFERENCES_FIELDS if f != "summary")
_PREFERENCES_MAX_LIST_ITEMS = 8
_PREFERENCES_MAX_ITEM_LENGTH = 80
_PREFERENCES_MAX_SUMMARY_LENGTH = 280

_PREFERENCES_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        **{
            field: {
                "type": "ARRAY",
                "items": {"type": "STRING", "maxLength": _PREFERENCES_MAX_ITEM_LENGTH},
                "maxItems": _PREFERENCES_MAX_LIST_ITEMS,
            }
            for field in _PREFERENCES_LIST_FIELDS
        },
        "summary": {"type": "STRING", "maxLength": _PREFERENCES_MAX_SUMMARY_LENGTH},
    },
    "required": list(_PREFERENCES_FIELDS),
}

CANDIDATE_CONTEXT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "preferences": _PREFERENCES_SCHEMA,
        **{
            field: {
                "type": "ARRAY",
                "items": {"type": "STRING", "maxLength": _MAX_ITEM_LENGTH},
                "maxItems": _MAX_LIST_ITEMS,
            }
            for field in _EVIDENCE_FIELDS
        },
        "evaluation_summary": {"type": "STRING", "maxLength": _MAX_SUMMARY_LENGTH},
    },
    "required": list(_TOP_LEVEL_FIELDS),
}


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


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        normalized = normalize_text(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(value.strip())
    return deduped


def _validate_string_list(data: dict, key: str, *, max_items: int, max_length: int) -> list[str]:
    values = data.get(key)
    if not isinstance(values, list):
        raise ValueError(f"{key} must be a list")
    if len(values) > max_items:
        raise ValueError(f"{key} exceeds {max_items} items")
    cleaned: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError(f"{key} entries must be strings")
        item = value.strip()
        if not item:
            raise ValueError(f"{key} entries must be non-empty")
        if len(item) > max_length:
            raise ValueError(f"{key} entries must be <= {max_length} characters")
        cleaned.append(item)
    return _dedupe_preserve_order(cleaned)


def _build_extraction_prompt(profile: str) -> str:
    return f"""Extract a compact, rich candidate context from the candidate profile below,
for reuse across every job evaluation and cover letter in this job search.

Return ONLY JSON with exactly these fields and no markdown fences:
{{"preferences": {{"preferred_roles": [string], "preferred_seniority": [string], "must_have_signals": [string], "nice_to_have_signals": [string], "preferred_locations": [string], "avoid_signals": [string], "summary": string}}, "technical_skills": [string], "architecture_evidence": [string], "leadership_ownership": [string], "agentic_ai_evidence": [string], "product_domain_evidence": [string], "location_language_facts": [string], "career_direction": [string], "company_environment": [string], "career_evidence": [string], "evaluation_summary": string}}

Rules:
- Every fact you output must be directly supported by the candidate profile below. Never infer, guess, or
  extrapolate a fact that is not stated. If a fact is unknown or not mentioned in the profile, omit it
  entirely rather than inventing or assuming it.
- Do not invent credentials, employers, skills, degrees, certifications, locations, or achievements.
- Keep every list compact and deduplicated.
- preferences.summary must be one short sentence. evaluation_summary must be at most a few sentences
  summarizing the candidate for downstream job evaluation.

Candidate profile:
{profile}
"""


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _cache_key(profile_hash: str, model: str, schema_version: str) -> str:
    return _hash(f"{profile_hash}\x1f{model}\x1f{schema_version}")


def _missing_fields(data: dict, required: tuple[str, ...]) -> list[str]:
    return [field for field in required if field not in data]


def _parse_preferences(data: object) -> CandidatePreferences:
    if not isinstance(data, dict):
        raise ValueError("preferences must be a JSON object")
    missing = _missing_fields(data, _PREFERENCES_FIELDS)
    if missing:
        raise ValueError(f"preferences missing required fields: {', '.join(missing)}")

    summary = data.get("summary")
    if not isinstance(summary, str):
        raise ValueError("preferences.summary must be a string")
    summary = summary.strip()
    if not summary:
        raise ValueError("preferences.summary must be non-empty")
    if len(summary) > _PREFERENCES_MAX_SUMMARY_LENGTH:
        raise ValueError(f"preferences.summary must be <= {_PREFERENCES_MAX_SUMMARY_LENGTH} characters")

    fields = {
        name: _validate_string_list(
            data, name, max_items=_PREFERENCES_MAX_LIST_ITEMS, max_length=_PREFERENCES_MAX_ITEM_LENGTH
        )
        for name in _PREFERENCES_LIST_FIELDS
    }
    return CandidatePreferences(summary=summary, **fields)


def _parse_context(raw: str) -> CandidateContext:
    try:
        data = json.loads(_strip_code_fences(raw))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("response must be a JSON object")
    missing = _missing_fields(data, _TOP_LEVEL_FIELDS)
    if missing:
        raise ValueError(f"response missing required fields: {', '.join(missing)}")

    evaluation_summary = data.get("evaluation_summary")
    if not isinstance(evaluation_summary, str):
        raise ValueError("evaluation_summary must be a string")
    evaluation_summary = evaluation_summary.strip()
    if not evaluation_summary:
        raise ValueError("evaluation_summary must be non-empty")
    if len(evaluation_summary) > _MAX_SUMMARY_LENGTH:
        raise ValueError(f"evaluation_summary must be <= {_MAX_SUMMARY_LENGTH} characters")

    evidence = {
        field: _validate_string_list(data, field, max_items=_MAX_LIST_ITEMS, max_length=_MAX_ITEM_LENGTH)
        for field in _EVIDENCE_FIELDS
    }

    return CandidateContext(
        preferences=_parse_preferences(data.get("preferences")),
        evaluation_summary=evaluation_summary,
        source="gemini",
        **evidence,
    )


def _fallback_context(policy: SearchPolicy, *, source: str, load_error: str = "") -> CandidateContext:
    return CandidateContext(
        preferences=_build_fallback_preferences(policy),
        technical_skills=[],
        architecture_evidence=[],
        leadership_ownership=[],
        agentic_ai_evidence=[],
        product_domain_evidence=[],
        location_language_facts=[],
        career_direction=[],
        company_environment=[],
        career_evidence=[],
        evaluation_summary=FALLBACK_CONTEXT_SUMMARY,
        source=source,
        load_error=load_error,
    )


def _context_from_dict(data: dict) -> CandidateContext:
    preferences_data = data["preferences"]
    return CandidateContext(
        preferences=CandidatePreferences(**{name: preferences_data[name] for name in _PREFERENCES_FIELDS}),
        **{field: list(data[field]) for field in _EVIDENCE_FIELDS},
        evaluation_summary=data["evaluation_summary"],
        source="cache",
    )


def _fallback_after_error(policy: SearchPolicy, exc: Exception, *, reason: str = "") -> CandidateContext:
    error_name = type(exc).__name__
    if reason:
        logger.warning("candidate context extraction failed; using fallback: error=%s reason=%s", error_name, reason)
    else:
        logger.warning("candidate context extraction failed; using fallback: error=%s", error_name)
    return _fallback_context(policy, source="fallback_error", load_error=error_name)


def _extract(gemini: "GeminiClient", prompt: str, max_output_tokens: int) -> str:
    return gemini.generate_text(
        prompt,
        purpose="candidate_context",
        thinking_level="medium",
        max_output_tokens=max_output_tokens,
        json_mode=True,
        json_schema=CANDIDATE_CONTEXT_SCHEMA,
    )


def get_candidate_context(
    profile: str,
    policy: SearchPolicy,
    gemini: "GeminiClient",
    store: "JobStore",
) -> CandidateContext:
    """Return the cached candidate context, extracting and caching it once if needed."""
    if not profile.strip():
        return _fallback_context(policy, source="fallback_empty_profile")

    profile_hash = _hash(profile)
    cache_key = _cache_key(profile_hash, gemini.model, CANDIDATE_CONTEXT_SCHEMA_VERSION)

    cached = store.get_candidate_context(cache_key)
    if cached is not None:
        return _context_from_dict(cached.context)

    prompt = _build_extraction_prompt(profile)
    try:
        try:
            raw = _extract(gemini, prompt, _INITIAL_OUTPUT_TOKENS)
        except GeminiIncompleteResponse as exc:
            logger.warning(
                "candidate context generation incomplete; retrying once: category=provider_truncation finish_reason=%s",
                exc.finish_reason,
            )
            raw = _extract(gemini, prompt, _RETRY_OUTPUT_TOKENS)
    except (GeminiBudgetExceeded, GeminiQuotaPaused):
        raise
    except GeminiIncompleteResponse as exc:
        return _fallback_after_error(
            policy,
            exc,
            reason=f"category=provider_truncation finish_reason={exc.finish_reason} retry_exhausted=true",
        )
    except Exception as exc:
        return _fallback_after_error(policy, exc)

    try:
        context = _parse_context(raw)
    except ValueError as exc:
        return _fallback_after_error(
            policy,
            exc,
            reason=f"{' '.join(str(exc).split())[:180]} category=malformed_structured_output",
        )
    except Exception as exc:
        return _fallback_after_error(policy, exc)

    store.save_candidate_context(
        cache_key=cache_key,
        profile_hash=profile_hash,
        model=gemini.model,
        schema_version=CANDIDATE_CONTEXT_SCHEMA_VERSION,
        context=asdict(context),
    )
    return context
