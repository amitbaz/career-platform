from __future__ import annotations

import base64
import json
import logging
import math
import os
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .ai.limits import free_tier_quota
from .gmail_models import GmailSettings
from .normalize import ats_board_key
from .models import (
    DEFAULT_BACKEND_HEAVY_SIGNALS,
    DEFAULT_BLOCKED_PROFESSION_TITLE_PHRASES,
    DEFAULT_ENGINEERING_TITLE_KEYWORDS,
    DEFAULT_ENGINEERING_TITLE_PHRASES,
    DEFAULT_FRONTEND_SIGNALS,
    DEFAULT_SOURCE_TIME_BUDGET_SECONDS,
    DEFAULT_SPECIALIST_BOARD_HOSTS,
    CompanyWatchSeed,
    AIQuotaSettings,
    ProviderCredentials,
    SearchPolicy,
    Settings,
    MarketPolicy,
    SalaryPolicy,
)

if TYPE_CHECKING:
    from .postgres_store import PostgresJobStore


logger = logging.getLogger(__name__)

#: The model a run calls when `GEMINI_MODEL` is unset. It is a model the
#: free-tier limits table knows (see `job_hunter.ai.limits`), so a run that
#: configures nothing but an API key still gets that model's real limits
#: rather than the conservative fallback.
_DEFAULT_AI_MODEL = "gemini-3.5-flash-lite"

_REMOTE_POLICIES = {"preferred", "required", "allowed"}
_RELOCATION_POLICIES = {"none", "selective", "allowed"}
_SPONSORSHIP_POLICIES = {"not_required", "required"}


@dataclass(slots=True, frozen=True)
class WebhookSettings:
    telegram_bot_token: str
    telegram_webhook_secret: str
    github_repository: str
    github_dispatch_token: str


@dataclass(slots=True, frozen=True)
class SupabaseSettings:
    """Credentials for acting as one user against the shared Supabase project.

    ``signing_key_jwk`` is the private half of the project's ES256 signing key.
    It is held in memory only and must never be logged or written to disk.
    That promise is enforced by the type itself: the field is excluded from
    ``repr()`` (via ``field(repr=False)``), so a stray ``logger.info(settings)``,
    ``print(settings)``, or a future ``pytest --showlocals`` failure cannot
    print the key.
    """

    user_id: str
    url: str
    publishable_key: str
    signing_key_jwk: dict = field(repr=False)


class ProfileNotFoundError(RuntimeError):
    """Raised when the acting user has no row in job_hunter_search_profiles."""


class RuntimeConfigurationError(RuntimeError):
    """Raised when required per-user runtime material is absent."""


def load_provider_credentials(store: "PostgresJobStore") -> ProviderCredentials:
    """Load provider credentials through the authenticated user store."""
    return store.get_provider_credentials()


def _load_required_documents(store: "PostgresJobStore") -> tuple[str, str]:
    """Load candidate documents in the order used by runtime validation."""
    documents = store.get_source_documents()
    return documents.get("cv", ""), documents.get("cover_letter", "")


def load_gmail_settings(store: "PostgresJobStore") -> GmailSettings:
    credentials = load_provider_credentials(store)
    if not credentials.gemini_api_key:
        raise RuntimeConfigurationError(
            "Missing per-user Job Hunter configuration: gemini"
        )
    ai_model = _ai_model()
    return GmailSettings(
        client_id=_require_env("GMAIL_CLIENT_ID"),
        client_secret=_require_env("GMAIL_CLIENT_SECRET"),
        refresh_token=_require_env("GMAIL_REFRESH_TOKEN"),
        ai_api_key=credentials.gemini_api_key,
        ai_quota=_ai_quota(ai_model),
        ai_model=ai_model,
    )


def load_settings(store: "PostgresJobStore") -> Settings:
    result = store.get_search_profile()
    if result is None:
        raise ProfileNotFoundError(
            "no job_hunter_search_profiles row exists for this user's account; "
            "create one first, e.g. via PostgresJobStore.save_search_profile"
        )
    profile_row, market_row_list = result
    data = _profile_row_to_legacy_dict(profile_row, market_row_list)

    credentials = load_provider_credentials(store)
    candidate_profile, cover_letter_template = _load_required_documents(store)
    missing = [
        name
        for name, value in (
            ("gemini", credentials.gemini_api_key),
            ("cv", candidate_profile),
            ("cover_letter", cover_letter_template),
        )
        if not value
    ]
    if missing:
        raise RuntimeConfigurationError(
            f"Missing per-user Job Hunter configuration: {', '.join(missing)}"
        )
    dry_run = os.environ.get("JOB_HUNTER_DRY_RUN", "").strip().lower() in ("1", "true", "yes")

    if not dry_run:
        telegram_bot_token = _require_env("TELEGRAM_BOT_TOKEN")
        telegram_chat_id = _require_env("TELEGRAM_CHAT_ID")
    else:
        telegram_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
        telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    policy = SearchPolicy(
        target_titles=data.get("target_titles", []),
        positive_keywords=data.get("positive_keywords", []),
        blocked_title_keywords=data.get("blocked_title_keywords", []),
        salary_floor_eur=data.get("salary_floor_eur", 90000),
        thresholds=data.get("thresholds", {}),
        max_jobs_per_run=data.get("max_jobs_per_run", 35),
        daily_offer_limit=data.get("daily_offer_limit", 10),
        match_score_floor=data.get("match_score_floor", 80),
        source_minimum_per_run=data.get("source_minimum_per_run", 0),
        source_max_share=data.get("source_max_share", 0.5),
        search_queries=data.get("search_queries", []),
        ats=data.get("ats", {}),
        role_families=data.get("role_families", []),
        search_query_templates=data.get("search_query_templates", []),
        search_domains=data.get("search_domains", []),
        specialist_search_domains=data.get("specialist_search_domains", []),
        specialist_query_templates=data.get("specialist_query_templates", []),
        yc_job_pages=data.get("yc_job_pages", []),
        manual_company_watch=_parse_manual_company_watch(
            data.get("manual_company_watch", [])
        ),
        source_time_budget_seconds=_source_time_budget_seconds(),
        max_search_queries_per_run=data.get("max_search_queries_per_run", 30),
        max_canonical_resolutions_per_run=data.get(
            "max_canonical_resolutions_per_run", 80
        ),
        max_learned_ats_boards_per_run=_parse_max_learned_ats_boards_per_run(data),
        learned_ats_denylist=_parse_learned_ats_denylist(data),
        learned_ats_allowlist=_parse_learned_ats_allowlist(data),
        engineering_title_keywords=list(
            data.get("engineering_title_keywords") or DEFAULT_ENGINEERING_TITLE_KEYWORDS
        ),
        engineering_title_phrases=list(
            data.get("engineering_title_phrases") or DEFAULT_ENGINEERING_TITLE_PHRASES
        ),
        blocked_profession_title_phrases=list(
            data.get("blocked_profession_title_phrases")
            or DEFAULT_BLOCKED_PROFESSION_TITLE_PHRASES
        ),
        specialist_board_hosts=list(
            data.get("specialist_board_hosts") or DEFAULT_SPECIALIST_BOARD_HOSTS
        ),
        frontend_signals=list(data.get("frontend_signals") or DEFAULT_FRONTEND_SIGNALS),
        backend_heavy_signals=list(
            data.get("backend_heavy_signals") or DEFAULT_BACKEND_HEAVY_SIGNALS
        ),
        markets=_parse_markets(data.get("markets", [])),
    )

    ai_model = _ai_model()
    return Settings(
        ai_api_key=credentials.gemini_api_key,
        candidate_profile=candidate_profile,
        cover_letter_template=cover_letter_template,
        timezone=data.get("timezone", "Europe/Berlin"),
        scheduled_hour=data.get("scheduled_hour", 9),
        policy=policy,
        ai_quota=_ai_quota(ai_model),
        brave_search_api_key=credentials.brave_search_api_key,
        dry_run=dry_run,
        telegram_bot_token=telegram_bot_token,
        telegram_chat_id=telegram_chat_id,
        ai_model=ai_model,
        output_dir=os.environ.get("JOB_HUNTER_OUTPUT_DIR", "var"),
    )


def _profile_row_to_legacy_dict(
    profile_row: dict, market_rows: list[dict]
) -> dict:
    """Reshape Postgres rows into the dict shape `_parse_*` already expects.

    Keeps every existing YAML-era parsing/validation function (`_parse_markets`,
    `_parse_manual_company_watch`, the ATS-list parsers) unchanged: they were
    written against `yaml.safe_load`'s output, and a Postgres row reshaped into
    the same shape is a drop-in replacement for it.
    """
    data = dict(profile_row)
    data["markets"] = [
        {
            "id": row["market_id"],
            "query_share": row["query_share"],
            "locations": row["locations"],
            "allowed_languages": row["allowed_languages"],
            "salary": {
                "currency": row["currency"],
                "gross_base_floor": row["gross_base_floor"],
                "location_floors": row["location_floors"],
            },
            "remote_policy": row["remote_policy"],
            "relocation_policy": row["relocation_policy"],
            "sponsorship_policy": row["sponsorship_policy"],
            "direct_sources": row["direct_sources"],
            "discovery_domains": row["discovery_domains"],
            "query_templates": row["query_templates"],
            "role_families": row["role_families"],
            "enabled": row["enabled"],
        }
        for row in market_rows
    ]
    return data


def load_webhook_settings() -> WebhookSettings:
    return WebhookSettings(
        telegram_bot_token=_require_env("TELEGRAM_BOT_TOKEN"),
        telegram_webhook_secret=_require_env("TELEGRAM_WEBHOOK_SECRET"),
        github_repository=_require_env("GITHUB_REPOSITORY"),
        github_dispatch_token=_require_env("GITHUB_DISPATCH_TOKEN"),
    )


def load_supabase_settings() -> SupabaseSettings:
    raw_user_id = _require_env("JOB_HUNTER_USER_ID")
    try:
        uuid.UUID(raw_user_id)
    except ValueError as exc:
        raise ValueError("JOB_HUNTER_USER_ID must be a UUID") from exc

    return SupabaseSettings(
        user_id=raw_user_id,
        url=_require_env("SUPABASE_URL").rstrip("/"),
        publishable_key=_require_env("SUPABASE_PUBLISHABLE_KEY"),
        signing_key_jwk=_decode_signing_key(_require_env("SUPABASE_SIGNING_KEY_B64")),
    )


def _decode_signing_key(encoded: str) -> dict:
    """Decode the base64-encoded private JWK.

    Error messages deliberately omit the offending value: it is key material.
    """
    try:
        jwk = json.loads(base64.b64decode(encoded))
    except Exception:  # never surface the key material in the message
        raise ValueError(
            "SUPABASE_SIGNING_KEY_B64 must be base64-encoded JSON"
        ) from None
    if not isinstance(jwk, dict) or not jwk.get("kid") or not jwk.get("kty"):
        raise ValueError(
            "SUPABASE_SIGNING_KEY_B64 must decode to a JWK object with 'kid' and 'kty'"
        )
    return jwk


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise ValueError(f"Required environment variable {name!r} is not set")
    return val


def _ai_model() -> str:
    """The model this run calls. `gemini-3.5-flash-lite` is the free-tier default.

    An empty value is treated as unset: the workflows pass
    `GEMINI_MODEL: ${{ vars.GEMINI_MODEL }}`, which expands to an empty string
    when the repository variable is not set, and an empty model would otherwise
    reach Google as `.../models/:generateContent`.
    """
    return os.environ.get("GEMINI_MODEL", "").strip() or _DEFAULT_AI_MODEL


def _ai_quota(model: str) -> AIQuotaSettings:
    """The model's published free-tier limits, with any environment override.

    A run needs an API key and nothing else: the limits are defaults in code
    (`job_hunter.ai.limits`), keyed by model. The three `GEMINI_FREE_*`
    variables remain as an optional per-user override for a project whose
    limits are not the published ones, or for a table entry that has gone
    stale; each overrides only the dimension it names. An empty value is
    treated as unset, because that is what an unconfigured GitHub Actions
    variable expands to.
    """
    return free_tier_quota(
        model,
        rpm=_optional_positive_int_env("GEMINI_FREE_RPM"),
        tpm=_optional_positive_int_env("GEMINI_FREE_TPM"),
        rpd=_optional_positive_int_env("GEMINI_FREE_RPD"),
    )


def _optional_positive_int_env(name: str) -> int | None:
    """Read an optional positive-integer override, or `None` when unset.

    A value that is set but unusable still raises: it was typed on purpose,
    and silently ignoring it would run under limits the operator believes are
    not in force.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _source_time_budget_seconds() -> float:
    """Read the per-source wall-clock budget from the environment.

    Environment rather than the search profile on purpose: the budget bounds
    what ingestion costs the platform, and says nothing about what jobs this
    user wants. Putting it in the search profile would make an infrastructure
    limit look like a search preference and would have to be answered by every
    future user.

    An unusable value falls back to the default rather than raising, and never
    to zero: a budget is a safeguard, and a run that refuses to start -- or one
    that cuts every source off at its first unit -- is a worse outcome than a
    run bounded by the default. "Unusable" includes the non-finite floats
    `float()` accepts: `nan` compares False against everything, so a budget of
    nan would disable the budget for every source in the run while looking like
    a configured one.
    """
    raw = os.environ.get("JOB_HUNTER_SOURCE_TIME_BUDGET_SECONDS", "").strip()
    if not raw:
        return DEFAULT_SOURCE_TIME_BUDGET_SECONDS
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "ignoring unparseable JOB_HUNTER_SOURCE_TIME_BUDGET_SECONDS=%r; "
            "using the default of %ss",
            raw,
            DEFAULT_SOURCE_TIME_BUDGET_SECONDS,
        )
        return DEFAULT_SOURCE_TIME_BUDGET_SECONDS
    if not math.isfinite(value) or value <= 0:
        logger.warning(
            "ignoring unusable JOB_HUNTER_SOURCE_TIME_BUDGET_SECONDS=%r; "
            "using the default of %ss",
            raw,
            DEFAULT_SOURCE_TIME_BUDGET_SECONDS,
        )
        return DEFAULT_SOURCE_TIME_BUDGET_SECONDS
    return value


def _parse_max_learned_ats_boards_per_run(data: dict) -> int:
    value = data.get("max_learned_ats_boards_per_run", 75)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("max_learned_ats_boards_per_run must be a positive integer")
    return value


def _parse_ats_board_key_list(data: dict, key: str) -> list[str]:
    """Normalize one `<provider>:<board>` policy list once, for every consumer.

    A bare `<key>:` (the state left behind by commenting out its only entry)
    parses as None, which must read as an empty list rather than aborting
    the run.
    """
    entries = data.get(key) or []
    if not isinstance(entries, list):
        raise ValueError(f"{key} must be a list")

    board_keys: list[str] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, str) or entry.count(":") != 1:
            raise ValueError(f"{key}[{index}] must be a \"<provider>:<board>\" string")
        provider, board_identifier = entry.split(":")
        if not provider.strip() or not board_identifier.strip():
            raise ValueError(f"{key}[{index}] must be a \"<provider>:<board>\" string")
        board_keys.append(ats_board_key(provider, board_identifier))
    return board_keys


def _parse_learned_ats_denylist(data: dict) -> list[str]:
    """Boards that must be kept out of the learned ATS registry."""
    return _parse_ats_board_key_list(data, "learned_ats_denylist")


def _parse_learned_ats_allowlist(data: dict) -> list[str]:
    """Boards that aggregator detection may never reject.

    Both lists normalize to the same key form, so a board named by both is a
    contradiction the operator has to resolve: there is no correct way to
    honour "always reject" and "never reject" for one board, and preferring
    either silently would hide the edit that caused it.
    """
    allowlist = _parse_ats_board_key_list(data, "learned_ats_allowlist")
    denylist = set(_parse_learned_ats_denylist(data))
    for board_key in allowlist:
        if board_key in denylist:
            raise ValueError(
                f"{board_key} is in both learned_ats_denylist and "
                "learned_ats_allowlist; remove it from one"
            )
    return allowlist


def _parse_markets(entries: object) -> list[MarketPolicy]:
    if not isinstance(entries, list):
        raise ValueError("markets must be a list")

    markets: list[MarketPolicy] = []
    seen_ids = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"markets[{index}] must be a mapping")

        market_id = entry.get("id")
        if not isinstance(market_id, str) or not market_id:
            raise ValueError(f"markets[{index}].id must be a non-empty string")

        if market_id in seen_ids:
            raise ValueError(f"duplicate market id: {market_id}")
        seen_ids.add(market_id)

        query_share = entry.get("query_share", 0.0)
        if not isinstance(query_share, (int, float)) or query_share < 0:
            raise ValueError(f"markets[{index}].query_share cannot be negative")

        salary_dict = entry.get("salary", {})
        if not isinstance(salary_dict, dict):
            raise ValueError(f"markets[{index}].salary must be a mapping")

        currency = salary_dict.get("currency", "")
        if not currency:
            raise ValueError(f"markets[{index}].salary.currency cannot be empty")

        gross_base_floor = salary_dict.get("gross_base_floor", 0)
        if not isinstance(gross_base_floor, int) or gross_base_floor <= 0:
            raise ValueError(f"markets[{index}].salary.gross_base_floor must be positive")

        location_floors = salary_dict.get("location_floors", {})
        for k, v in location_floors.items():
            if not isinstance(v, int) or v <= 0:
                raise ValueError(f"markets[{index}].salary.location_floors.{k} must be positive")

        remote_policy = entry.get("remote_policy", "allowed")
        if remote_policy not in _REMOTE_POLICIES:
            raise ValueError(f"markets[{index}] invalid remote_policy: {remote_policy}")

        relocation_policy = entry.get("relocation_policy", "allowed")
        if relocation_policy not in _RELOCATION_POLICIES:
            raise ValueError(f"markets[{index}] invalid relocation_policy: {relocation_policy}")

        sponsorship_policy = entry.get("sponsorship_policy", "not_required")
        if sponsorship_policy not in _SPONSORSHIP_POLICIES:
            raise ValueError(f"markets[{index}] invalid sponsorship_policy: {sponsorship_policy}")

        allowed_fields = {
            "id", "query_share", "locations", "allowed_languages", "salary",
            "remote_policy", "relocation_policy", "sponsorship_policy",
            "direct_sources", "discovery_domains", "source_domains",
            "query_templates", "role_families", "enabled"
        }
        unknown_fields = sorted(set(entry) - allowed_fields, key=str)
        if unknown_fields:
            raise ValueError(f"markets[{index}].{unknown_fields[0]} is not allowed")

        if "source_domains" in entry and "discovery_domains" in entry:
            raise ValueError(
                f"markets[{index}] cannot define both source_domains and discovery_domains"
            )
        discovery_domains = entry.get(
            "discovery_domains",
            entry.get("source_domains", []),
        )

        markets.append(MarketPolicy(
            id=market_id,
            query_share=float(query_share),
            locations=entry.get("locations", []),
            allowed_languages=entry.get("allowed_languages", []),
            salary=SalaryPolicy(
                currency=currency,
                gross_base_floor=gross_base_floor,
                location_floors=location_floors,
            ),
            remote_policy=remote_policy,
            relocation_policy=relocation_policy,
            sponsorship_policy=sponsorship_policy,
            direct_sources=entry.get("direct_sources", []),
            discovery_domains=discovery_domains,
            query_templates=entry.get("query_templates", []),
            role_families=entry.get("role_families", []),
            enabled=entry.get("enabled", True),
        ))

    return markets


def _parse_manual_company_watch(entries: object) -> list[CompanyWatchSeed]:
    if not isinstance(entries, list):
        raise ValueError("manual_company_watch must be a list")

    seeds: list[CompanyWatchSeed] = []
    for index, entry in enumerate(entries):
        if isinstance(entry, str):
            company_name = entry.strip()
            if not company_name:
                raise ValueError(
                    f"manual_company_watch[{index}].company_name "
                    "must be a non-empty string"
                )
            seeds.append(CompanyWatchSeed(company_name=company_name))
            continue
        if not isinstance(entry, dict):
            raise ValueError(
                f"manual_company_watch[{index}] must be a non-empty string or mapping"
            )

        allowed_fields = {
            "company_name",
            "careers_url",
            "ats_provider",
            "ats_identifier",
        }
        unknown_fields = sorted(set(entry) - allowed_fields, key=str)
        if unknown_fields:
            raise ValueError(
                f"manual_company_watch[{index}].{unknown_fields[0]} is not allowed"
            )

        company_name = entry.get("company_name")
        if not isinstance(company_name, str) or not company_name.strip():
            raise ValueError(
                f"manual_company_watch[{index}].company_name "
                "must be a non-empty string"
            )
        for field in ("careers_url", "ats_provider", "ats_identifier"):
            value = entry.get(field)
            if value is not None and not isinstance(value, str):
                raise ValueError(
                    f"manual_company_watch[{index}].{field} must be a string or null"
                )
        seeds.append(
            CompanyWatchSeed(
                company_name=company_name.strip(),
                careers_url=entry.get("careers_url", "") or "",
                ats_provider=entry.get("ats_provider"),
                ats_identifier=entry.get("ats_identifier"),
            )
        )
    return seeds
