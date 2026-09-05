from __future__ import annotations

import math
from collections import defaultdict

from job_hunter.models import CandidatePreferences, Job, SearchPolicy
from job_hunter.normalize import normalize_text

_ATS_HOSTS = ("jobs.ashbyhq.com", "jobs.lever.co", "boards.greenhouse.io")

# Approved market-specialist boards (config/search.yml markets[*].source_domains):
# hand-picked per market, but not the canonical direct-employer link an ATS
# host is, so they rank below ATS and above generic web/aggregator results.
_SPECIALIST_BOARD_HOSTS = (
    "wellfound.com",
    "jobs.techaviv.com",
    "devjobs.co.il",
    "workvisajobs.co.uk",
    "nodeflair.com",
    "sg.jobstreet.com",
    "mycareersfuture.gov.sg",
    "builtin.com",
    "startup.jobs",
    "ycombinator.com",
)

_CAREER_SIGNALS = (
    "ownership",
    "architecture",
    "mentorship",
    "roadmap",
    "strategy",
    "leadership",
    "end-to-end",
    "cross-functional",
    "autonomy",
    "greenfield",
)

_REGION_WORDS = frozenset({
    "europe", "emea", "germany", "netherlands", "spain", "portugal",
    "poland", "france", "italy", "austria", "ireland", "switzerland", "uk",
})

_REMOTE_FRIENDLY_WORDS = frozenset({"remote", "worldwide", "global", "anywhere", "distributed"})


def source_quality(job: Job) -> int:
    url = (job.url or "").lower()
    if any(host in url for host in _ATS_HOSTS):
        return 10
    if job.source in {"ashby", "lever", "greenhouse"}:
        return 10
    if any(host in url for host in _SPECIALIST_BOARD_HOSTS):
        return 8
    if job.source in {"remoteok", "remotive", "weworkremotely", "arbeitnow"}:
        return 7
    if job.source == "hackernews":
        return 5
    return 3


def _normalized_words(text: str) -> set[str]:
    return set(normalize_text(text or "").split())


def _normalized_phrases(values: list[str]) -> list[str]:
    normalized = []
    seen = set()
    for value in values:
        phrase = normalize_text(value or "")
        if phrase and phrase not in seen:
            seen.add(phrase)
            normalized.append(phrase)
    return normalized


def _title_fit(title: str, policy: SearchPolicy) -> int:
    normalized_title = normalize_text(title or "")
    if any(kw in normalized_title for kw in policy.blocked_title_keywords):
        return 0
    if not policy.target_titles:
        return 0
    title_words = set(normalized_title.split())
    best_ratio = 0.0
    for target in policy.target_titles:
        normalized_target = normalize_text(target)
        if normalized_target == normalized_title:
            return 40
        target_words = set(normalized_target.split())
        if not target_words:
            continue
        overlap = len(title_words & target_words) / len(target_words)
        best_ratio = max(best_ratio, overlap)
    return round(40 * best_ratio)


def _strength_evidence(description: str, policy: SearchPolicy) -> int:
    keywords = policy.positive_keywords or []
    if not keywords:
        return 0
    normalized_description = normalize_text(description or "")
    matched = {kw for kw in keywords if normalize_text(kw) in normalized_description}
    return round(25 * len(matched) / len(keywords))


def _career_direction_evidence(description: str) -> int:
    normalized_description = normalize_text(description or "")
    matched = {signal for signal in _CAREER_SIGNALS if signal in normalized_description}
    return round(15 * len(matched) / len(_CAREER_SIGNALS))


def _location_evidence(job: Job) -> int:
    if not job.remote:
        return 0
    location_words = set(normalize_text(job.location or "").split())
    if not location_words:
        return 3
    if location_words & _REGION_WORDS:
        return 10
    return 5


def _role_seniority_fit(job: Job, preferences: CandidatePreferences) -> int:
    normalized_title = normalize_text(job.title or "")
    title_words = set(normalized_title.split())

    best_role_ratio = 0.0
    for role in _normalized_phrases(preferences.preferred_roles):
        if role == normalized_title:
            best_role_ratio = 1.0
            break
        role_words = set(role.split())
        if not role_words:
            continue
        best_role_ratio = max(best_role_ratio, len(title_words & role_words) / len(role_words))

    role_score = round(25 * best_role_ratio)
    seniority_words = _normalized_phrases(preferences.preferred_seniority)
    seniority_score = 10 if any(word in title_words for word in seniority_words) else 0
    return min(35, role_score + seniority_score)


def _signal_coverage(job: Job, preferences: CandidatePreferences) -> int:
    haystack = normalize_text(" ".join([job.title or "", job.description or ""]))
    must_have = _normalized_phrases(preferences.must_have_signals)
    nice_to_have = _normalized_phrases(preferences.nice_to_have_signals)
    must_matches = {signal for signal in must_have if signal in haystack}
    nice_matches = {signal for signal in nice_to_have if signal in haystack}
    must_score = round(20 * len(must_matches) / len(must_have)) if must_have else 0
    nice_score = round(10 * len(nice_matches) / len(nice_to_have)) if nice_to_have else 0
    return min(30, must_score + nice_score)


def _profile_location_fit(job: Job, preferences: CandidatePreferences) -> int:
    if not job.remote:
        return 0

    preferred_locations = _normalized_phrases(preferences.preferred_locations)
    if not preferred_locations:
        return 15

    location_text = normalize_text(" ".join([job.location or "", job.description or ""]))
    if any(location in location_text for location in preferred_locations):
        return 15
    if not location_text:
        return 8

    location_words = set(location_text.split())
    if location_words & _REMOTE_FRIENDLY_WORDS:
        return 10
    return 5


# Markets where remote work is the primary/preferred mode of engagement
# (config/search.yml remote_policy: "preferred" or "required") count as the
# candidate's "home" market for location scoring - remote roles there score
# highest, ahead of remote roles in relocation-style markets.
_HOME_MARKET_REMOTE_POLICIES = frozenset({"preferred", "required"})


def _market_location_fit(job: Job, preferences: CandidatePreferences, policy: SearchPolicy) -> int:
    if not job.market_id:
        return _profile_location_fit(job, preferences)

    market = next((m for m in policy.markets if m.id == job.market_id), None)
    if market is None:
        return _profile_location_fit(job, preferences)

    location_text = normalize_text(" ".join([job.location or "", job.description or ""]))
    target_city_match = any(normalize_text(city) in location_text for city in market.locations)
    home_market = market.remote_policy in _HOME_MARKET_REMOTE_POLICIES

    if job.remote:
        if home_market:
            return 15
        return 10 if target_city_match else 8
    if target_city_match:
        return 12 if home_market else 10
    return 0


_FULL_STACK_TITLE_PHRASES = ("full stack", "full-stack")

_FRONTEND_SIGNALS = (
    "react",
    "next.js",
    "nextjs",
    "frontend",
    "front-end",
    "typescript",
    "design system",
)

_BACKEND_HEAVY_SIGNALS = (
    "distributed systems",
    "kubernetes",
    "golang",
    "java",
    "event-driven architecture",
    "backend architecture",
    "high-throughput",
    "message queues",
)


def _backend_transition_penalty(job: Job) -> int:
    normalized_title = normalize_text(job.title or "")
    if not any(phrase in normalized_title for phrase in _FULL_STACK_TITLE_PHRASES):
        return 0

    haystack = normalize_text(" ".join([job.title or "", job.description or ""]))
    frontend_matches = sum(1 for signal in _FRONTEND_SIGNALS if signal in haystack)
    backend_matches = sum(1 for signal in _BACKEND_HEAVY_SIGNALS if signal in haystack)

    if backend_matches < 2:
        return 0
    if frontend_matches == 0:
        return 15
    return 6


def market_priority_bonus(job: Job, policy: SearchPolicy) -> int:
    if not job.market_id:
        return 0
    for index, market in enumerate(policy.markets):
        if market.id == job.market_id:
            return max(0, len(policy.markets) - index)
    return 0


def _avoid_signal_penalty(job: Job, preferences: CandidatePreferences) -> int:
    avoid_signals = _normalized_phrases(preferences.avoid_signals)
    if not avoid_signals:
        return 0

    haystack = normalize_text(" ".join([job.title or "", job.location or "", job.description or ""]))
    matches = {signal for signal in avoid_signals if signal in haystack}
    return round(10 * len(matches) / len(avoid_signals))


def profile_priority_score(job: Job, preferences: CandidatePreferences, policy: SearchPolicy) -> int:
    total = (
        _role_seniority_fit(job, preferences)
        + _signal_coverage(job, preferences)
        + _market_location_fit(job, preferences, policy)
        + source_quality(job)
        + market_priority_bonus(job, policy)
        - _avoid_signal_penalty(job, preferences)
        - _backend_transition_penalty(job)
    )
    return max(0, min(100, total))


def priority_score(job: Job, policy: SearchPolicy) -> int:
    total = (
        _title_fit(job.title, policy)
        + _strength_evidence(job.description, policy)
        + _career_direction_evidence(job.description)
        + _location_evidence(job)
        + source_quality(job)
    )
    return max(0, min(100, total))


def rank_jobs(
    jobs: list[tuple[int, Job]],
    policy: SearchPolicy,
    preferences: CandidatePreferences | None = None,
) -> list[tuple[int, Job, int]]:
    scorer = priority_score if preferences is None else lambda job, current_policy: profile_priority_score(job, preferences, current_policy)
    scored = [(job_id, job, scorer(job, policy)) for job_id, job in jobs]
    return sorted(
        scored,
        key=lambda item: (
            -item[2],
            (item[1].company or "").lower(),
            (item[1].title or "").lower(),
            item[0],
        ),
    )


def _source_share_cap(limit: int, minimum_per_source: int, max_share: float) -> int:
    if limit <= 0:
        return 0
    if max_share >= 1:
        share_cap = limit
    else:
        share_cap = max(1, math.floor(limit * max_share))
    return min(limit, max(minimum_per_source, share_cap))


def select_diverse_candidates(
    ranked: list[tuple[int, Job, int]],
    limit: int,
    minimum_per_source: int,
    max_share: float,
) -> list[tuple[int, Job, int]]:
    if limit <= 0 or not ranked:
        return []

    per_source_limit = _source_share_cap(limit, max(0, minimum_per_source), max_share)
    grouped: dict[str, list[tuple[int, Job, int]]] = defaultdict(list)
    source_order: list[str] = []
    for item in ranked:
        source = item[1].source
        if source not in grouped:
            source_order.append(source)
        grouped[source].append(item)

    selected: list[tuple[int, Job, int]] = []
    selected_ids: set[int] = set()
    source_counts: dict[str, int] = defaultdict(int)

    for source in source_order:
        for item in grouped[source][: min(max(0, minimum_per_source), per_source_limit)]:
            if len(selected) >= limit:
                return selected
            selected.append(item)
            selected_ids.add(item[0])
            source_counts[source] += 1

    for item in ranked:
        if len(selected) >= limit:
            break
        job_id, job, _score = item
        if job_id in selected_ids:
            continue
        if source_counts[job.source] >= per_source_limit:
            continue
        selected.append(item)
        selected_ids.add(job_id)
        source_counts[job.source] += 1

    for item in ranked:
        if len(selected) >= limit:
            break
        if item[0] in selected_ids:
            continue
        selected.append(item)
        selected_ids.add(item[0])

    return selected
