from __future__ import annotations

from typing import TYPE_CHECKING

from job_hunter.models import CandidatePreferences, SearchPolicy
from job_hunter.normalize import normalize_text

if TYPE_CHECKING:
    from job_hunter.gemini import GeminiClient
    from job_hunter.store import JobStore

_SENIORITY_WORDS = ("intern", "junior", "mid", "senior", "staff", "lead", "principal", "head")
_LOCATION_HINT_WORDS = frozenset(
    {
        "remote",
        "europe",
        "emea",
        "germany",
        "netherlands",
        "spain",
        "portugal",
        "poland",
        "france",
        "italy",
        "austria",
        "ireland",
        "switzerland",
        "uk",
    }
)
_MAX_LIST_ITEMS = 8
FALLBACK_PREFERENCES_SUMMARY = "Fallback preferences derived from search policy."


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


def _fallback_seniority(policy: SearchPolicy) -> list[str]:
    values: list[str] = []
    for source in [*policy.role_families, *policy.target_titles]:
        words = set(normalize_text(source).split())
        for word in _SENIORITY_WORDS:
            if word in words and word not in values:
                values.append(word)
    return values


def _fallback_locations(policy: SearchPolicy) -> list[str]:
    values: list[str] = []
    sources = [*policy.search_queries, *policy.search_query_templates]
    for source in sources:
        for word in normalize_text(source).replace('"', " ").split():
            if word in _LOCATION_HINT_WORDS and word not in values:
                values.append(word)
    return values[:_MAX_LIST_ITEMS]


def _build_fallback_preferences(policy: SearchPolicy) -> CandidatePreferences:
    preferred_roles = _dedupe_preserve_order([*policy.role_families, *policy.target_titles])[:_MAX_LIST_ITEMS]
    must_have_signals = _dedupe_preserve_order(list(policy.positive_keywords))[:_MAX_LIST_ITEMS]
    avoid_signals = _dedupe_preserve_order(list(policy.blocked_profession_title_phrases))[:_MAX_LIST_ITEMS]
    return CandidatePreferences(
        preferred_roles=preferred_roles,
        preferred_seniority=_fallback_seniority(policy),
        must_have_signals=must_have_signals,
        nice_to_have_signals=[],
        preferred_locations=_fallback_locations(policy),
        avoid_signals=avoid_signals,
        summary=FALLBACK_PREFERENCES_SUMMARY,
    )


def extract_candidate_preferences(
    profile: str,
    gemini: "GeminiClient",
    policy: SearchPolicy,
    store: "JobStore",
) -> CandidatePreferences:
    """Compatibility helper: the preferences slice of the cached CandidateContext.

    No longer runs its own per-run Gemini extraction — it delegates to
    job_hunter.candidate_context.get_candidate_context, which extracts once
    per (profile, model, schema version) and caches the result in `store`.
    Imported lazily to avoid a circular import (candidate_context reuses
    _build_fallback_preferences from this module).
    """
    from job_hunter.candidate_context import get_candidate_context

    return get_candidate_context(profile, policy, gemini, store).preferences


def preferences_source(preferences: CandidatePreferences) -> str:
    if preferences.summary == FALLBACK_PREFERENCES_SUMMARY:
        return "fallback"
    return "gemini"
