from __future__ import annotations

import argparse
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from job_hunter.config import load_gmail_settings, load_settings
from job_hunter.gemini import GeminiClient
from job_hunter.gemini_usage import GeminiUsageTracker
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
from job_hunter.store import JobStore
from job_hunter.telegram import TelegramClient

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="job_hunter")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Discover, evaluate, and deliver jobs")
    run_parser.add_argument("--scheduled", action="store_true", help="Only proceed at the configured scheduled hour")
    run_parser.add_argument("--config", default="config/search.yml", help="Path to search.yml")

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
    gen_parser.add_argument("--job-id", type=int, required=True)
    gen_parser.add_argument("--config", default="config/search.yml", help="Path to search.yml")

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
    settings = load_settings(Path(args.config))

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

    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    cover_letter_output_dir(settings).mkdir(parents=True, exist_ok=True)

    store = JobStore(settings.db_path)
    http = HttpClient()
    tracker = GeminiUsageTracker(
        store, settings.gemini_quota, settings.gemini_model, run_id=os.getenv("GEMINI_RUN_ID")
    )
    gemini = GeminiClient(settings.gemini_api_key, settings.gemini_model, http, tracker=tracker)

    summary = run_pipeline(settings, store=store, gemini=gemini, http=http)
    logger.info(
        "Run complete: ready_to_apply=%d possible_matches=%d skipped=%d errors=%d",
        summary.ready_to_apply,
        summary.possible_matches,
        summary.skipped,
        summary.errors,
    )
    return 0


def _generate_cover_letter(args: argparse.Namespace) -> int:
    settings = load_settings(Path(args.config))
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    cover_letter_output_dir(settings).mkdir(parents=True, exist_ok=True)

    store = JobStore(settings.db_path)
    http = HttpClient()
    tracker = GeminiUsageTracker(
        store, settings.gemini_quota, settings.gemini_model, run_id=os.getenv("GEMINI_RUN_ID")
    )
    gemini = GeminiClient(settings.gemini_api_key, settings.gemini_model, http, tracker=tracker)
    telegram = TelegramClient(settings.telegram_bot_token, settings.telegram_chat_id, http)

    delivered = generate_cover_letter_on_demand(
        settings, args.job_id, store=store, gemini=gemini, telegram=telegram
    )
    logger.info("on-demand cover letter for job_id=%s: delivered=%s", args.job_id, delivered)
    return 0 if delivered else 1


def _sync_gmail(args: argparse.Namespace) -> int:
    settings = load_gmail_settings()
    db_path = Path(settings.db_path)
    if args.dry_run:
        store = (
            JobStore(db_path, read_only=True)
            if db_path.exists()
            else JobStore(":memory:")
        )
        # A GeminiUsageTracker WRITES usage/pause rows, and `store` above is
        # opened read-only against the real db precisely so --dry-run can
        # never persist anything there. A dry run still makes real Gemini
        # calls (see GmailSyncService.process_message), so the guardrails
        # must still be active for it -- just against a dedicated, ephemeral
        # in-memory ledger rather than the real one, so the "never persists"
        # guarantee for --dry-run holds regardless of how much quota history
        # a real (non-dry-run) process has already written today.
        tracker_store = JobStore(":memory:")
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        store = JobStore(db_path)
        tracker_store = store

    http = HttpClient()
    gmail = GmailClient(http, GoogleOAuthTokenProvider(settings))
    tracker = GeminiUsageTracker(
        tracker_store, settings.gemini_quota, settings.gemini_model, run_id=os.getenv("GEMINI_RUN_ID")
    )
    gemini = GeminiClient(settings.gemini_api_key, settings.gemini_model, http, tracker=tracker)
    service = GmailSyncService(gmail=gmail, gemini=gemini, store=store)
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
