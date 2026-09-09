from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone

from job_hunter.config import load_gmail_settings, load_settings, load_supabase_settings
from job_hunter.ai.gemini import PROVIDER, build_gemini_provider
from job_hunter.ai.usage import AIUsageTracker, PlatformUsageLedger
from job_hunter.gmail_auth import GoogleOAuthTokenProvider
from job_hunter.gmail_client import GmailClient
from job_hunter.gmail_sync import GmailSyncService
from job_hunter.http import HttpClient
from job_hunter.pipeline import (
    cover_letter_output_dir,
    generate_cover_letter_on_demand,
    run_pipeline,
    should_run_scheduled,
)
from job_hunter.postgres_store import DryRunStore, PostgresJobStore
from job_hunter.supabase_auth import AccessTokenMinter
from job_hunter.supabase_client import SupabaseClient
from job_hunter.telegram import TelegramClient

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _build_client(http: HttpClient) -> SupabaseClient:
    settings = load_supabase_settings()
    return SupabaseClient(
        http, settings, AccessTokenMinter(settings.user_id, settings.signing_key_jwk)
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="job_hunter")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Discover, evaluate, and deliver jobs")
    run_parser.add_argument("--scheduled", action="store_true", help="Only proceed at the configured scheduled hour")

    sync_parser = subparsers.add_parser("sync-gmail", help="Read Gmail job signals into shared state")
    sync_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Classify/extract without persisting Gmail-derived state",
    )
    sync_parser.add_argument(
        "--force-backfill",
        action="store_true",
        help="Repeat the 120-day backfill idempotently",
    )

    gen_parser = subparsers.add_parser(
        "generate-cover-letter", help="Generate (or resend) a cover letter for one job on demand"
    )
    gen_parser.add_argument("--job-id", type=str, required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "sync-gmail":
            return _sync_gmail(args)
        if args.command == "generate-cover-letter":
            return _generate_cover_letter(args)
        return _run(args)
    except Exception:
        logger.exception("job hunter run failed")
        return 1


def _run(args: argparse.Namespace) -> int:
    http = HttpClient()
    store = PostgresJobStore(_build_client(http))
    settings = load_settings(store)

    if args.scheduled:
        now = datetime.now(timezone.utc)
        if not should_run_scheduled(now, settings.timezone, settings.scheduled_hour):
            logger.info(
                "Scheduled run skipped: current time is not the configured %s:00 %s slot "
                "(this is the DST duplicate cron trigger).",
                settings.scheduled_hour,
                settings.timezone,
            )
            return 0

    cover_letter_output_dir(settings).mkdir(parents=True, exist_ok=True)

    tracker = AIUsageTracker(
        store, settings.ai_quota, settings.ai_model, provider=PROVIDER
    )
    # The platform key funds shared objective extraction and is metered in its
    # own global ledger (#128). Both are built here or neither is: a key with
    # no ledger would spend an allowance nobody is watching, and a ledger with
    # no key would meter calls that never happen. Where the deployment has no
    # platform key, extraction is simply off for the run -- the user's key is
    # never offered in its place.
    platform_key = None
    platform_tracker = None
    if settings.platform_ai_api_key and settings.platform_ai_quota is not None:
        platform_key = settings.platform_ai_api_key
        platform_tracker = AIUsageTracker(
            PlatformUsageLedger(store),
            settings.platform_ai_quota,
            settings.ai_model,
            provider=PROVIDER,
        )
    else:
        # Loud, because the consequence is larger than "no enrichment". Since
        # #126 a job cannot be scored before its posting has been read, so
        # without a platform key every job that has no stored facets yet --
        # which is every newly discovered job -- goes unscored. A deployment
        # that never sets PLATFORM_GEMINI_API_KEY delivers only what earlier
        # runs already enriched, and then nothing. It is still not a reason to
        # reach for the user's key.
        logger.warning(
            "no platform AI key is configured (PLATFORM_GEMINI_API_KEY): no posting "
            "will be read this run, so no job without stored facets can be scored. "
            "The user's own key is not used in its place."
        )
    ai = build_gemini_provider(
        settings.ai_api_key,
        settings.ai_model,
        http,
        tracker=tracker,
        platform_api_key=platform_key,
        platform_tracker=platform_tracker,
    )

    summary = run_pipeline(
        settings,
        store=store,
        ai=ai,
        usage=tracker,
        platform_usage=platform_tracker,
        http=http,
    )
    logger.info(
        "Run complete: ready_to_apply=%d possible_matches=%d skipped=%d errors=%d "
        "blocked_by_facets=%d facets_extracted=%d facets_reused=%d facets_failed=%d",
        summary.ready_to_apply,
        summary.possible_matches,
        summary.skipped,
        summary.errors,
        # Jobs the stored facets disqualified without a scoring call (#127):
        # the saving this run made against the user's own provider quota.
        summary.blocked_by_facets,
        # Facet extraction is shared, best-effort work: it is reported here so
        # a run whose enrichment is quietly failing is visible, but it never
        # decides the exit code -- a run that delivered its digest succeeded.
        summary.facet_extraction_attempted - summary.facet_extraction_failed,
        # Postings this run scored against without reading them: facets an
        # earlier run stored, whoever paid for it (#175).
        summary.facets_reused,
        summary.facet_extraction_failed,
    )
    if summary.evaluation_attempted and summary.evaluated == 0:
        # Isolated source/job failures stay non-fatal (see summary.errors above),
        # but if every fresh evaluation this run failed to produce a decision,
        # the core pipeline is unusable -- fail the run so GitHub Actions goes
        # red instead of quietly reporting success on a run that produced
        # nothing.
        logger.error(
            "core evaluation catastrophically unsuccessful: %d attempted, 0 evaluated "
            "(errors=%d)",
            summary.evaluation_attempted,
            summary.errors,
        )
        return 1
    return 0


def _generate_cover_letter(args: argparse.Namespace) -> int:
    http = HttpClient()
    store = PostgresJobStore(_build_client(http))
    settings = load_settings(store)
    cover_letter_output_dir(settings).mkdir(parents=True, exist_ok=True)

    tracker = AIUsageTracker(
        store, settings.ai_quota, settings.ai_model, provider=PROVIDER
    )
    ai = build_gemini_provider(
        settings.ai_api_key, settings.ai_model, http, tracker=tracker
    )
    telegram = TelegramClient(settings.telegram_bot_token, settings.telegram_chat_id, http)

    delivered = generate_cover_letter_on_demand(
        settings, args.job_id, store=store, ai=ai, telegram=telegram
    )
    logger.info("on-demand cover letter for job_id=%s: delivered=%s", args.job_id, delivered)
    return 0 if delivered else 1


def _sync_gmail(args: argparse.Namespace) -> int:
    http = HttpClient()
    real_store = PostgresJobStore(_build_client(http))
    settings = load_gmail_settings(real_store)
    if args.dry_run:
        # An AIUsageTracker WRITES usage/pause rows, and `store` below is
        # a DryRunStore precisely so --dry-run can never persist anything
        # live. A dry run still makes real provider calls (see
        # GmailSyncService.process_message), so the guardrails must still be
        # active for it -- just against a store that discards every write,
        # so the "never persists" guarantee for --dry-run holds regardless
        # of how much quota history a real (non-dry-run) process has
        # already written today.
        store = DryRunStore(real_store)
        tracker_store = DryRunStore(real_store)
    else:
        store = real_store
        tracker_store = store

    gmail = GmailClient(http, GoogleOAuthTokenProvider(settings))
    tracker = AIUsageTracker(
        tracker_store, settings.ai_quota, settings.ai_model, provider=PROVIDER
    )
    ai = build_gemini_provider(
        settings.ai_api_key, settings.ai_model, http, tracker=tracker
    )
    service = GmailSyncService(gmail=gmail, ai=ai, store=store)
    summary = service.sync(
        datetime.now(timezone.utc),
        dry_run=args.dry_run,
        force_backfill=args.force_backfill,
    )
    logger.info(
        "Gmail sync complete: fetched=%d processed=%d job_alerts=%d "
        "application_events=%d review_needed=%d irrelevant=%d errors=%d",
        summary.fetched,
        summary.processed,
        summary.job_alerts,
        summary.application_events,
        summary.review_needed,
        summary.irrelevant,
        summary.errors,
    )
    if summary.errors:
        logger.warning(
            "Gmail sync completed with %d per-message errors; the cursor was retained and "
            "those messages will retry on the next sync.",
            summary.errors,
        )
    return 0
