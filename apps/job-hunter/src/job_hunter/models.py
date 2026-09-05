from __future__ import annotations
from dataclasses import dataclass, field

DEFAULT_ENGINEERING_TITLE_KEYWORDS = [
    "engineer",
    "developer",
]

DEFAULT_ENGINEERING_TITLE_PHRASES = [
    "technical lead",
    "frontend lead",
    "front-end lead",
    "software lead",
    "engineering lead",
    "software architect",
    "frontend architect",
    "front-end architect",
    "web architect",
]

DEFAULT_BLOCKED_PROFESSION_TITLE_PHRASES = [
    "product manager",
    "platform product manager",
    "technical product manager",
    "product designer",
    "ux designer",
    "ui designer",
    "product marketing manager",
    "program manager",
    "project manager",
    "customer success manager",
    "solutions consultant",
    "sales engineer",
    "solutions engineer",
    "support engineer",
    "data engineer",
    "machine learning engineer",
    "ml engineer",
    "data scientist",
    "ml researcher",
    "machine learning researcher",
    "ios engineer",
    "android engineer",
    "mobile engineer",
    "embedded engineer",
]


@dataclass(slots=True)
class SalaryPolicy:
    currency: str
    gross_base_floor: int
    location_floors: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class MarketPolicy:
    id: str
    query_share: float
    locations: list[str]
    allowed_languages: list[str]
    salary: SalaryPolicy
    remote_policy: str
    relocation_policy: str
    sponsorship_policy: str
    direct_sources: list[str] = field(default_factory=list)
    discovery_domains: list[str] = field(default_factory=list)
    query_templates: list[str] = field(default_factory=list)
    role_families: list[str] = field(default_factory=list)
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class SearchQuery:
    text: str
    market_id: str | None = None


@dataclass(slots=True)
class Job:
    source: str
    title: str
    company: str = ""
    location: str = ""
    url: str = ""
    description: str = ""
    source_job_id: str | None = None
    remote: bool | None = None
    original_url: str = ""
    canonical_url: str = ""
    ats_provider: str | None = None
    ats_board: str | None = None
    ats_job_id: str | None = None
    market_hint: str | None = None
    market_id: str | None = None
    source_page_html: str = ""
    content_confidence: str = ""


@dataclass(slots=True)
class AtsReference:
    provider: str
    board: str
    job_id: str | None


@dataclass(slots=True)
class CanonicalResolution:
    url: str
    ats: AtsReference | None
    confidence: float
    method: str


@dataclass(frozen=True, slots=True)
class AtsRegistryEntry:
    provider: str
    board_identifier: str
    company_name: str
    market_hint: str
    first_seen_at: str
    last_seen_at: str
    last_checked_at: str | None
    last_success_at: str | None
    last_eligible_at: str | None
    last_job_count: int
    eligible_jobs_seen: int
    consecutive_failures: int
    active: bool
    paused_until: str | None


@dataclass(slots=True)
class CompanyWatchSeed:
    company_name: str
    careers_url: str = ""
    ats_provider: str | None = None
    ats_identifier: str | None = None


@dataclass(slots=True)
class Evaluation:
    job_id: int
    total_score: int
    scores: dict
    decision: str
    hard_blockers: list
    strengths: list
    gaps: list
    salary_note: str
    location_note: str
    rationale: str
    model: str
    status: str = "ok"
    market_id: str = ""
    content_confidence: str = ""
    requirements: dict = field(default_factory=dict)
    #: Gemini's raw component sum, before any deterministic cap. Diagnostics
    #: only — `total_score` is the number every consumer should use.
    raw_model_score: int = 0


@dataclass(slots=True)
class Material:
    job_id: int
    cover_letter_text: str


@dataclass(slots=True)
class PrefilterResult:
    should_evaluate: bool
    hard_blocker: bool
    reason: str
    reason_code: str = ""


@dataclass(slots=True)
class SearchPolicy:
    target_titles: list
    positive_keywords: list
    blocked_title_keywords: list
    salary_floor_eur: int
    thresholds: dict
    max_jobs_per_run: int = 35
    source_minimum_per_run: int = 0
    source_max_share: float = 0.5
    search_queries: list = field(default_factory=list)
    ats: dict = field(default_factory=dict)
    role_families: list[str] = field(default_factory=list)
    search_query_templates: list[str] = field(default_factory=list)
    search_domains: list[str] = field(default_factory=list)
    specialist_search_domains: list[str] = field(default_factory=list)
    specialist_query_templates: list[str] = field(default_factory=list)
    yc_job_pages: list[str] = field(default_factory=list)
    manual_company_watch: list[CompanyWatchSeed] = field(default_factory=list)
    max_search_queries_per_run: int = 30
    max_canonical_resolutions_per_run: int = 80
    max_learned_ats_boards_per_run: int = 75
    engineering_title_keywords: list[str] = field(
        default_factory=lambda: list(DEFAULT_ENGINEERING_TITLE_KEYWORDS)
    )
    engineering_title_phrases: list[str] = field(
        default_factory=lambda: list(DEFAULT_ENGINEERING_TITLE_PHRASES)
    )
    blocked_profession_title_phrases: list[str] = field(
        default_factory=lambda: list(DEFAULT_BLOCKED_PROFESSION_TITLE_PHRASES)
    )
    markets: list[MarketPolicy] = field(default_factory=list)


@dataclass(slots=True)
class CandidatePreferences:
    preferred_roles: list[str]
    preferred_seniority: list[str]
    must_have_signals: list[str]
    nice_to_have_signals: list[str]
    preferred_locations: list[str]
    avoid_signals: list[str]
    summary: str


@dataclass(frozen=True, slots=True)
class CandidateContext:
    """A single rich extraction of the candidate profile, reused everywhere.

    Replaces repeated full-profile prompts across evaluation and cover-letter
    generation: extracted once per (profile, model, schema version) and
    cached by job_hunter.candidate_context.get_candidate_context.
    """

    preferences: CandidatePreferences
    technical_skills: list[str]
    architecture_evidence: list[str]
    leadership_ownership: list[str]
    agentic_ai_evidence: list[str]
    product_domain_evidence: list[str]
    location_language_facts: list[str]
    career_direction: list[str]
    company_environment: list[str]
    career_evidence: list[str]
    evaluation_summary: str
    source: str = field(default="unknown", compare=False)
    load_error: str = field(default="", compare=False)


@dataclass(frozen=True, slots=True)
class CandidateContextCacheEntry:
    """A cached, JSON-backed candidate context and its cache identity."""

    cache_key: str
    profile_hash: str
    model: str
    schema_version: str
    context: dict
    created_at: str


@dataclass(frozen=True, slots=True)
class GeminiQuotaSettings:
    rpm: int
    tpm: int
    rpd: int
    ceiling_ratio: float = 0.80
    core_reserve_ratio: float = 0.25
    rate_pause_seconds: int = 90

    def __post_init__(self) -> None:
        # config.py's _require_positive_int_env already rejects a non-positive
        # rpm/tpm/rpd from the environment; this guard closes the same gap for
        # any other construction path (tests, future callers) so it can never
        # contradict that validation, only extend it. rate_pause_seconds has
        # no env-level guard at all today, and a non-positive value is the
        # root cause of a real correctness bug: a zero-length rate-limit pause
        # (`paused_until == now`) makes GeminiUsageTracker.record_429's caller
        # look paused-and-already-expired in the same instant, so a 429 could
        # surface as the wrong exception type. See gemini.py's 429 handling.
        for field_name in ("rpm", "tpm", "rpd", "rate_pause_seconds"):
            if getattr(self, field_name) <= 0:
                raise ValueError(
                    f"GeminiQuotaSettings.{field_name} must be a positive integer"
                )


@dataclass(frozen=True, slots=True)
class GeminiUsageSummary:
    """A point-in-time rollup of Gemini usage against configured free-tier quotas.

    Percentages are against the configured provider limit, not the internal
    80% ceiling. Token totals and `requests_today` cover only attempts that
    reached the provider (blocked_budget rows never happened at Google).

    `cached_tokens_today` is a subset of `input_tokens_today` (Google's
    `cachedContentTokenCount` is part of `promptTokenCount`, not additional to
    it), so `total_tokens_today` is Google's own `totalTokenCount` for each
    attempt (falling back to a reconstructed input+output+thinking estimate
    only when a row has no `usageMetadata` at all) rather than a naive sum of
    the four token fields above, which would double-count the cached portion.
    """

    requests_today: int
    rpd_percent: float
    rpm_peak_percent: float
    tpm_peak_percent: float
    input_tokens_today: int
    output_tokens_today: int
    thinking_tokens_today: int
    cached_tokens_today: int
    total_tokens_today: int
    purpose_counts: dict[str, int]
    internal_budget_exhausted: bool
    provider_paused: bool


@dataclass(slots=True)
class Settings:
    gemini_api_key: str
    candidate_profile: str
    cover_letter_template: str
    timezone: str
    scheduled_hour: int
    policy: SearchPolicy
    gemini_quota: GeminiQuotaSettings
    dry_run: bool = False
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    gemini_model: str = "gemini-3.6-flash"
    db_path: str = "var/job_hunter.sqlite3"


@dataclass(slots=True)
class DigestItem:
    job_id: int
    company: str
    title: str
    score: int
    decision: str
    url: str
    hard_blockers: list
    location: str = ""
    market_id: str = ""
    market_note: str = ""


@dataclass(slots=True, frozen=True)
class NavigationCard:
    job_id: int
    title: str
    company: str
    location: str
    score: int
    url: str
    market_id: str = ""
    market_note: str = ""


@dataclass(slots=True, frozen=True)
class NavigationSession:
    session_id: str
    cards: list[NavigationCard]
    telegram_message_id: str | None
    created_at: str
    expires_at: str


@dataclass(slots=True)
class ReviewItem:
    """Compact, privacy-minimized representation of unresolved Gmail activity."""

    event_id: int
    company: str
    role_title: str
    occurred_at: str
    subject: str
    rationale: str
    event_type: str
    source_message_id: str
    source_thread_id: str | None


@dataclass(slots=True)
class RunSummary:
    ready_to_apply: int = 0
    possible_matches: int = 0
    skipped: int = 0
    errors: int = 0
