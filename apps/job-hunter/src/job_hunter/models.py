from __future__ import annotations
from dataclasses import dataclass, field

from job_hunter.availability import UNCHECKED

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

#: Wall-clock seconds any one job source may spend before discovery cuts it
#: off. Deliberately loose. No source's normal cost is known yet -- that is
#: what the per-source timing exists to establish -- and a tight default would
#: silently truncate healthy sources on the very first run, producing data
#: about the budget rather than about the sources. 1800s sits above every gap
#: seen in the 47-minute discovery of the reference run (34138786671) while
#: still stopping one source from consuming a whole run. Tightening it belongs
#: to a later ticket, decided from the numbers rather than guessed at now.
DEFAULT_SOURCE_TIME_BUDGET_SECONDS = 1800.0

DEFAULT_SPECIALIST_BOARD_HOSTS = [
    "wellfound.com", "jobs.techaviv.com", "devjobs.co.il", "workvisajobs.co.uk",
    "nodeflair.com", "sg.jobstreet.com", "mycareersfuture.gov.sg", "builtin.com",
    "startup.jobs", "ycombinator.com",
]

DEFAULT_FRONTEND_SIGNALS = [
    "react", "next.js", "nextjs", "frontend", "front-end", "typescript", "design system",
]

DEFAULT_BACKEND_HEAVY_SIGNALS = [
    "distributed systems", "kubernetes", "golang", "java",
    "event-driven architecture", "backend architecture", "high-throughput", "message queues",
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
    #: Deterministic posting-availability status. Not persisted -- set fresh
    #: each run from whatever page fetch already happened. See availability.py.
    availability: str = UNCHECKED


@dataclass(slots=True)
class Compensation:
    """What a posting discloses about pay, and nothing more.

    `disclosed` is False whenever the posting says nothing determinate; the
    other fields are then empty. A posting that states only one end of a range
    carries that end and leaves the other `None` -- an absent bound is not
    zero.
    """

    disclosed: bool = False
    currency: str = ""
    minimum: int | None = None
    maximum: int | None = None
    #: One of "hour", "day", "month", "year", or "" when undisclosed.
    period: str = ""


@dataclass(slots=True)
class JobFacets:
    """The objective facts a posting states about itself (issue #125).

    A facet is identical for every user -- it is a property of the posting,
    not of anyone reading it -- so facets are extracted once per posting and
    reused by every later run. See `facets.py` for the extraction, which is
    deliberately unable to see anything per-user, and CONTEXT.md for the
    objective-extraction / subjective-scoring split this is one half of.

    Every field has an "it did not say" value: "unknown", an empty list, or
    undisclosed compensation. None of them means "no", and callers must not
    read them that way.
    """

    seniority: str = "unknown"
    remote_policy: str = "unknown"
    relocation_policy: str = "unknown"
    #: Regions the posting states it will hire in, in `hiring_scope`'s
    #: vocabulary. Empty means it stated none, never "eligible nowhere".
    hiring_regions: list[str] = field(default_factory=list)
    stack: list[str] = field(default_factory=list)
    compensation: Compensation = field(default_factory=Compensation)
    #: [{"requirement": str, "depth": str, "kind": "must_have"|"preferred"}]
    requirements: list[dict[str, str]] = field(default_factory=list)
    #: Facet names taken from structured source data or deterministic code
    #: rather than from the model, so the split can be measured.
    source_supplied: list[str] = field(default_factory=list)
    #: The description hash these facets were read at. Stamped by the store
    #: from the job row, so invalidation reuses exactly the mechanism that
    #: gates re-evaluation rather than inventing a second one.
    description_hash_at_extraction: str = ""
    model: str = ""


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
    rejected_reason: str | None = None


@dataclass(slots=True)
class CompanyWatchSeed:
    company_name: str
    careers_url: str = ""
    ats_provider: str | None = None
    ats_identifier: str | None = None


@dataclass(slots=True)
class Evaluation:
    job_id: str
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
    job_id: str
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
    #: How many job offers a run may deliver, counting the ready-to-apply and
    #: possible-match outcomes the user actually receives. It is the run's
    #: budget, not a filter: evaluation stops once it is met, and the
    #: candidates never reached stay unevaluated and eligible tomorrow.
    #: `max_jobs_per_run` remains a hard ceiling above it, so a day where
    #: almost nothing passes still terminates. The allowed values (5, 10, 20)
    #: are enforced where the setting is written, on `SearchProfile`.
    daily_offer_limit: int = 10
    #: Minimum match score that can become an offer. Decision thresholds keep
    #: their classification meaning; this separate floor controls delivery.
    #: The allowed 50..95 range is enforced on `SearchProfile`.
    match_score_floor: int = 80
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
    #: Wall-clock seconds any one source may spend before it is cut off,
    #: checked between the units of work it iterates over. See
    #: DEFAULT_SOURCE_TIME_BUDGET_SECONDS for why the default is loose.
    #:
    #: A zero or negative value here disables the budget, so that a caller
    #: constructing a policy directly can opt out. That is *not* how the
    #: environment variable behaves: `JOB_HUNTER_SOURCE_TIME_BUDGET_SECONDS=0`
    #: is treated as a misconfiguration and replaced by the default, because a
    #: zeroed setting is far more often a mistake than a deliberate opt-out.
    #: `config._source_time_budget_seconds` is where that decision lives.
    source_time_budget_seconds: float = DEFAULT_SOURCE_TIME_BUDGET_SECONDS
    max_search_queries_per_run: int = 30
    max_canonical_resolutions_per_run: int = 80
    max_learned_ats_boards_per_run: int = 75
    #: Normalized `ats_board_key` values, e.g. "lever:jobgether". See
    #: aggregator_detection.py for how this relates to detection.
    learned_ats_denylist: list[str] = field(default_factory=list)
    #: Normalized `ats_board_key` values that aggregator detection may never
    #: reject -- the inverse of `learned_ats_denylist`, and the operator's
    #: only way to reverse a rejection. See aggregator_detection.py.
    learned_ats_allowlist: list[str] = field(default_factory=list)
    engineering_title_keywords: list[str] = field(
        default_factory=lambda: list(DEFAULT_ENGINEERING_TITLE_KEYWORDS)
    )
    engineering_title_phrases: list[str] = field(
        default_factory=lambda: list(DEFAULT_ENGINEERING_TITLE_PHRASES)
    )
    blocked_profession_title_phrases: list[str] = field(
        default_factory=lambda: list(DEFAULT_BLOCKED_PROFESSION_TITLE_PHRASES)
    )
    specialist_board_hosts: list[str] = field(default_factory=list)
    frontend_signals: list[str] = field(default_factory=list)
    backend_heavy_signals: list[str] = field(default_factory=list)
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
    gemini_api_key: str = field(repr=False)
    candidate_profile: str = field(repr=False)
    cover_letter_template: str = field(repr=False)
    timezone: str
    scheduled_hour: int
    policy: SearchPolicy
    gemini_quota: GeminiQuotaSettings
    brave_search_api_key: str | None = field(default=None, repr=False)
    dry_run: bool = False
    telegram_bot_token: str | None = field(default=None, repr=False)
    telegram_chat_id: str | None = field(default=None, repr=False)
    gemini_model: str = "gemini-3.6-flash"
    output_dir: str = "var"


@dataclass(slots=True, frozen=True)
class ProviderCredentials:
    gemini_api_key: str | None = field(default=None, repr=False)
    brave_search_api_key: str | None = field(default=None, repr=False)


@dataclass(slots=True)
class DigestItem:
    job_id: str
    company: str
    title: str
    score: int
    decision: str
    url: str
    hard_blockers: list
    location: str = ""
    market_id: str = ""
    market_note: str = ""
    availability_note: str = ""


@dataclass(slots=True, frozen=True)
class NavigationCard:
    job_id: str
    title: str
    company: str
    location: str
    score: int
    url: str
    market_id: str = ""
    market_note: str = ""
    availability_note: str = ""


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
    withheld_by_score_floor: int = 0
    skipped: int = 0
    errors: int = 0
    # Core-evaluation health, distinct from `errors` (which also counts
    # unrelated post-decision failures like company-watch promotion).
    # `evaluation_attempted` counts jobs where a fresh Gemini evaluation was
    # actually made (excludes already-evaluated and quota-deferred jobs);
    # `evaluated` counts how many of those produced a decision. The run is
    # catastrophic when jobs were attempted but none produced a decision.
    evaluation_attempted: int = 0
    evaluated: int = 0
    # Objective facet extraction (issue #125), counted separately from
    # evaluation on purpose: the two are different calls against different
    # jobs' worth of work, and a run where facets are failing while
    # evaluation is healthy needs telling apart from the reverse. A failed
    # extraction is not an `errors` entry either -- it leaves the job
    # unenriched and the next run retries it, which is recovery, not damage.
    facet_extraction_attempted: int = 0
    facet_extraction_failed: int = 0
    # Jobs the stored facets disqualified for this user before any scoring
    # call was dispatched (issue #127). Deliberately not part of
    # `evaluation_attempted`/`evaluated`: no provider call was made, and a
    # deterministic block must not be able to mask a run where every fresh
    # evaluation failed.
    blocked_by_facets: int = 0
