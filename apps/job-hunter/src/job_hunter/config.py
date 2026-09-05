from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from .gmail_models import GmailSettings
from .models import (
    DEFAULT_BLOCKED_PROFESSION_TITLE_PHRASES,
    DEFAULT_ENGINEERING_TITLE_KEYWORDS,
    DEFAULT_ENGINEERING_TITLE_PHRASES,
    CompanyWatchSeed,
    GeminiQuotaSettings,
    SearchPolicy,
    Settings,
    MarketPolicy,
    SalaryPolicy,
)


_REMOTE_POLICIES = {"preferred", "required", "allowed"}
_RELOCATION_POLICIES = {"none", "selective", "allowed"}
_SPONSORSHIP_POLICIES = {"not_required", "required"}


@dataclass(slots=True, frozen=True)
class WebhookSettings:
    telegram_bot_token: str
    telegram_webhook_secret: str
    github_repository: str
    github_state_token: str
    github_dispatch_token: str
    github_state_artifact_name: str = "job-hunter-state"
    github_state_cache_dir: str = "/tmp/job-hunter-state"


def load_gmail_settings() -> GmailSettings:
    return GmailSettings(
        client_id=_require_env("GMAIL_CLIENT_ID"),
        client_secret=_require_env("GMAIL_CLIENT_SECRET"),
        refresh_token=_require_env("GMAIL_REFRESH_TOKEN"),
        gemini_api_key=_require_env("GEMINI_API_KEY"),
        gemini_quota=GeminiQuotaSettings(
            rpm=_require_positive_int_env("GEMINI_FREE_RPM"),
            tpm=_require_positive_int_env("GEMINI_FREE_TPM"),
            rpd=_require_positive_int_env("GEMINI_FREE_RPD"),
        ),
        gemini_model=os.environ.get("GEMINI_MODEL", "gemini-3.6-flash"),
        db_path=os.environ.get("JOB_HUNTER_DB_PATH", "var/job_hunter.sqlite3"),
    )


def load_settings(config_path: Path) -> Settings:
    with open(config_path) as f:
        data = yaml.safe_load(f)

    gemini_api_key = _require_env("GEMINI_API_KEY")
    candidate_profile = base64.b64decode(_require_env("CANDIDATE_PROFILE_B64")).decode("utf-8")
    cover_letter_template = base64.b64decode(_require_env("COVER_LETTER_TEMPLATE_B64")).decode("utf-8")
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
        max_search_queries_per_run=data.get("max_search_queries_per_run", 30),
        max_canonical_resolutions_per_run=data.get(
            "max_canonical_resolutions_per_run", 80
        ),
        max_learned_ats_boards_per_run=_parse_max_learned_ats_boards_per_run(data),
        engineering_title_keywords=list(
            data.get("engineering_title_keywords", DEFAULT_ENGINEERING_TITLE_KEYWORDS)
        ),
        engineering_title_phrases=list(
            data.get("engineering_title_phrases", DEFAULT_ENGINEERING_TITLE_PHRASES)
        ),
        blocked_profession_title_phrases=list(
            data.get(
                "blocked_profession_title_phrases",
                DEFAULT_BLOCKED_PROFESSION_TITLE_PHRASES,
            )
        ),
        markets=_parse_markets(data.get("markets", [])),
    )

    return Settings(
        gemini_api_key=gemini_api_key,
        candidate_profile=candidate_profile,
        cover_letter_template=cover_letter_template,
        timezone=data.get("timezone", "Europe/Berlin"),
        scheduled_hour=data.get("scheduled_hour", 9),
        policy=policy,
        gemini_quota=GeminiQuotaSettings(
            rpm=_require_positive_int_env("GEMINI_FREE_RPM"),
            tpm=_require_positive_int_env("GEMINI_FREE_TPM"),
            rpd=_require_positive_int_env("GEMINI_FREE_RPD"),
        ),
        dry_run=dry_run,
        telegram_bot_token=telegram_bot_token,
        telegram_chat_id=telegram_chat_id,
        gemini_model=os.environ.get("GEMINI_MODEL", "gemini-3.6-flash"),
        db_path=os.environ.get("JOB_HUNTER_DB_PATH", "var/job_hunter.sqlite3"),
    )


def load_webhook_settings() -> WebhookSettings:
    return WebhookSettings(
        telegram_bot_token=_require_env("TELEGRAM_BOT_TOKEN"),
        telegram_webhook_secret=_require_env("TELEGRAM_WEBHOOK_SECRET"),
        github_repository=_require_env("GITHUB_REPOSITORY"),
        github_state_token=_require_env("GITHUB_STATE_TOKEN"),
        github_dispatch_token=_require_env("GITHUB_DISPATCH_TOKEN"),
        github_state_artifact_name=os.environ.get(
            "GITHUB_STATE_ARTIFACT_NAME", "job-hunter-state"
        ),
        github_state_cache_dir=os.environ.get(
            "GITHUB_STATE_CACHE_DIR", "/tmp/job-hunter-state"
        ),
    )


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise ValueError(f"Required environment variable {name!r} is not set")
    return val


def _require_positive_int_env(name: str) -> int:
    raw = _require_env(name)
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _parse_max_learned_ats_boards_per_run(data: dict) -> int:
    value = data.get("max_learned_ats_boards_per_run", 75)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("max_learned_ats_boards_per_run must be a positive integer")
    return value


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
