"""One-off: read config/search.yml and write it as this user's search profile.

Run once, by hand, after Task 5 lands and before config/search.yml is deleted:

    apps/job-hunter/.venv/bin/python apps/job-hunter/scripts/migrate_search_yml_to_profile.py

Requires the same environment variables as `job_hunter run` (SUPABASE_URL,
SUPABASE_PUBLISHABLE_KEY, SUPABASE_SIGNING_KEY_B64, JOB_HUNTER_USER_ID).
Not part of the app; safe to delete after it has been run successfully once.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from job_hunter.config import load_supabase_settings  # noqa: E402
from job_hunter.http import HttpClient  # noqa: E402
from job_hunter.postgres_store import PostgresJobStore  # noqa: E402
from job_hunter.search_profile import SearchProfile, SearchProfileMarket  # noqa: E402
from job_hunter.supabase_auth import AccessTokenMinter  # noqa: E402
from job_hunter.supabase_client import SupabaseClient  # noqa: E402

# Hardcoded lists this script carries over from ranking.py, since those
# module constants are removed by Task 4. Matches the values ranking.py had
# before this migration.
_SPECIALIST_BOARD_HOSTS = [
    "wellfound.com", "jobs.techaviv.com", "devjobs.co.il", "workvisajobs.co.uk",
    "nodeflair.com", "sg.jobstreet.com", "mycareersfuture.gov.sg", "builtin.com",
    "startup.jobs", "ycombinator.com",
]
_FRONTEND_SIGNALS = [
    "react", "next.js", "nextjs", "frontend", "front-end", "typescript", "design system",
]
_BACKEND_HEAVY_SIGNALS = [
    "distributed systems", "kubernetes", "golang", "java",
    "event-driven architecture", "backend architecture", "high-throughput", "message queues",
]

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "search.yml"


def _build_market(entry: dict) -> SearchProfileMarket:
    salary = entry.get("salary", {})
    return SearchProfileMarket(
        market_id=entry["id"],
        query_share=float(entry.get("query_share", 0.0)),
        locations=entry.get("locations", []),
        allowed_languages=entry.get("allowed_languages", []),
        currency=salary.get("currency", ""),
        gross_base_floor=salary.get("gross_base_floor", 0),
        location_floors=salary.get("location_floors", {}),
        remote_policy=entry.get("remote_policy", "allowed"),
        relocation_policy=entry.get("relocation_policy", "allowed"),
        sponsorship_policy=entry.get("sponsorship_policy", "not_required"),
        direct_sources=entry.get("direct_sources", []),
        discovery_domains=entry.get("discovery_domains", entry.get("source_domains", [])),
        query_templates=entry.get("query_templates", []),
        role_families=entry.get("role_families", []),
        enabled=entry.get("enabled", True),
    )


def main() -> int:
    with open(_CONFIG_PATH) as f:
        data = yaml.safe_load(f)

    profile = SearchProfile(
        timezone=data.get("timezone", "Europe/Berlin"),
        scheduled_hour=data.get("scheduled_hour", 9),
        max_jobs_per_run=data.get("max_jobs_per_run", 35),
        source_minimum_per_run=data.get("source_minimum_per_run", 0),
        source_max_share=data.get("source_max_share", 0.5),
        thresholds=data.get("thresholds", {}),
        salary_floor_eur=data.get("salary_floor_eur", 90000),
        target_titles=data.get("target_titles", []),
        positive_keywords=data.get("positive_keywords", []),
        blocked_title_keywords=data.get("blocked_title_keywords", []),
        role_families=data.get("role_families", []),
        search_query_templates=data.get("search_query_templates", []),
        search_domains=data.get("search_domains", []),
        specialist_search_domains=data.get("specialist_search_domains", []),
        specialist_query_templates=data.get("specialist_query_templates", []),
        search_queries=data.get("search_queries", []),
        yc_job_pages=data.get("yc_job_pages", []),
        engineering_title_keywords=data.get("engineering_title_keywords", []),
        engineering_title_phrases=data.get("engineering_title_phrases", []),
        blocked_profession_title_phrases=data.get("blocked_profession_title_phrases", []),
        specialist_board_hosts=_SPECIALIST_BOARD_HOSTS,
        frontend_signals=_FRONTEND_SIGNALS,
        backend_heavy_signals=_BACKEND_HEAVY_SIGNALS,
        max_search_queries_per_run=data.get("max_search_queries_per_run", 30),
        max_canonical_resolutions_per_run=data.get("max_canonical_resolutions_per_run", 80),
        max_learned_ats_boards_per_run=data.get("max_learned_ats_boards_per_run", 75),
        learned_ats_denylist=data.get("learned_ats_denylist") or [],
        learned_ats_allowlist=data.get("learned_ats_allowlist") or [],
        manual_company_watch=data.get("manual_company_watch") or [],
        ats=data.get("ats", {}),
        markets=[_build_market(entry) for entry in data.get("markets", [])],
    )

    settings = load_supabase_settings()
    http = HttpClient()
    client = SupabaseClient(
        http, settings, AccessTokenMinter(settings.user_id, settings.signing_key_jwk)
    )
    store = PostgresJobStore(client)
    profile_id = store.save_search_profile(profile)
    print(f"wrote search profile {profile_id} for user {settings.user_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
