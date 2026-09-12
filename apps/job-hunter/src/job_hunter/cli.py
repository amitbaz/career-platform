from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from job_hunter.config import (
    load_gmail_settings,
    load_ingestion_dsn,
    load_platform_ai_settings,
    load_settings,
    load_supabase_settings,
)
from job_hunter.ai.gemini import PROVIDER, build_gemini_provider
from job_hunter.ai.usage import AIUsageTracker, format_ai_usage_log
from job_hunter.circuit_breaker import CircuitBreaker
from job_hunter.cover_letter import cover_letter_output_dir, generate_cover_letter_on_demand
from job_hunter.gmail_auth import GoogleOAuthTokenProvider
from job_hunter.gmail_client import GmailClient
from job_hunter.gmail_sync import GmailSyncService
from job_hunter.http import HttpClient
from job_hunter.pg import IngestionDatabase
from job_hunter.postgres_store import DryRunStore, PostgresJobStore
from job_hunter.supabase_auth import AccessTokenMinter
from job_hunter.supabase_client import SupabaseClient
from job_hunter.telegram import TelegramClient
from job_hunter.worker_runs import WorkerRun, report_worker_health

logger = logging.getLogger(__name__)

#: Consecutive canonical-resolution search failures before a drain stops
#: paying for more of them this process.
_SEARCH_FAILURE_THRESHOLD = 5


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

    recheck_parser = subparsers.add_parser(
        "recheck-freshness",
        help="Re-check due postings: close the ones that are gone, refresh the changed",
    )
    recheck_parser.add_argument(
        "--limit",
        type=int,
        default=2000,
        help="Most re-checks to drain in this run",
    )

    crawl_parser = subparsers.add_parser(
        "crawl-source",
        help="Drain due per-source crawls: discover, drop unchanged, persist raw postings",
    )
    crawl_parser.add_argument(
        "--limit",
        type=int,
        default=200,
        help="Most crawls to drain in this run",
    )

    extract_parser = subparsers.add_parser(
        "extract-facets",
        help="Drain due objective extraction: read a posting's own words, once, for everyone",
    )
    extract_parser.add_argument(
        "--limit",
        type=int,
        default=200,
        help="Most extractions to drain in this run",
    )

    recover_parser = subparsers.add_parser(
        "recover-posting",
        help="Drain due posting recovery: try to turn a thin description into a trustworthy one",
    )
    recover_parser.add_argument(
        "--limit",
        type=int,
        default=500,
        help="Most recovery attempts to drain in this run",
    )

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
        if args.command == "recheck-freshness":
            return _recheck_freshness(args)
        if args.command == "crawl-source":
            return _crawl_source(args)
        if args.command == "extract-facets":
            return _extract_facets(args)
        if args.command == "recover-posting":
            return _recover_posting(args)
        raise AssertionError(f"unhandled command: {args.command}")
    except Exception:
        logger.exception("job hunter run failed")
        return 1


def _log_ai_usage(tracker, account: str) -> None:
    """Log one ledger's day-to-date AI spend, and never fail the caller for it.

    #189 retired the single process that printed every ledger together, so
    each command that spends AI budget reports its own account: `platform`
    for the shared key extraction runs against (#128), `user` for the key the
    person running the command owns. Reporting what the work cost must never
    cost the work its own success -- a drain that read every posting it
    claimed, or a cover letter that was delivered, must not turn red because
    the ledger read came back empty.
    """
    try:
        logger.info(
            format_ai_usage_log(tracker.snapshot(datetime.now(timezone.utc)), account)
        )
    except Exception:
        logger.exception("could not read %s AI usage for this invocation", account)


def _recorded_drain(database, worker: str, stale_after_seconds: int, drain):
    """Run `drain(run)` as one recorded worker run; return `(result, healthy)`.

    Every ingestion worker invocation that can reach the database gets a
    `job_hunter_worker_runs` row, including one that finds its queue empty
    (#258). So everything that can fail once the connection exists -- a
    missing key, a client or profile that will not load -- belongs inside
    `drain`, where it becomes a failed run rather than an absent one. An
    invocation with no SUPABASE_DB_URL cannot be recorded; health reports its
    worker missing instead. `drain` receives the run
    so it can pass `run.heartbeat` as its `on_batch` and `run.id` to rows it
    writes itself. An exception finishes the run as an error and propagates.
    A killed process finishes nothing, and health reports the run once its
    heartbeat is stale.

    `healthy` is the whole fleet's health read after this run finished, so a
    worker that stopped being invoked is reported by the ones still running.
    """
    run = WorkerRun(database, worker, stale_after_seconds=stale_after_seconds)
    run.start()
    try:
        result = drain(run)
    except Exception as error:
        run.fail(error)
        raise
    run.finish(result)
    return result, report_worker_health(database)


def _health_exit_code(worker: str, healthy: bool) -> int:
    if healthy:
        return 0
    # Non-zero on purpose, even though this worker's own drain was fine: a
    # failed Render cron run is visible, and a log line is not (AGENTS.md
    # rule 5). The ingestion_health lines above name the worker at fault.
    logger.error(
        "%s finished its own work, but not every ingestion worker is healthy; "
        "see the ingestion_health lines above",
        worker,
    )
    return 1


def _recheck_freshness(args: argparse.Namespace) -> int:
    """Drain the recheck_freshness queue (#186).

    User-free end to end: it needs ingestion's direct connection and nothing
    else -- no user id, no search profile, no provider key -- because whether
    an advertisement still exists is the same answer for everyone. Without
    that connection it cannot write a posting at all, so it fails rather than
    reporting a quiet day.
    """
    from job_hunter.recheck_freshness_stage import (
        FAILED,
        VISIBILITY_TIMEOUT_SECONDS,
        drain_recheck_freshness,
    )

    dsn = load_ingestion_dsn()
    if dsn is None:
        logger.error(
            "recheck-freshness needs SUPABASE_DB_URL: re-checks write postings, "
            "which only ingestion's direct connection may do"
        )
        return 1
    database = IngestionDatabase(dsn)
    try:
        drain, healthy = _recorded_drain(
            database,
            "recheck_freshness",
            VISIBILITY_TIMEOUT_SECONDS,
            lambda run: drain_recheck_freshness(
                database, HttpClient(), limit=args.limit, on_batch=run.heartbeat
            ),
        )
    finally:
        database.close()
    logger.info("recheck_freshness complete: %s", drain.summary())
    if drain.claimed and drain.completed == 0 and drain.outcomes[FAILED]:
        # Isolated failures are the queue's to retry. Every single check
        # failing is a worker that cannot reach anything -- make it red.
        logger.error(
            "recheck_freshness completed no check: %d claimed, %d failed",
            drain.claimed,
            drain.outcomes[FAILED],
        )
        return 1
    return _health_exit_code("recheck_freshness", healthy)


def _recover_posting(args: argparse.Namespace) -> int:
    """Drain the recover_posting queue (#259).

    User-free end to end, exactly like recheck-freshness: whether a thin
    description can be upgraded to a trustworthy one is the same answer for
    everyone. Without the direct connection it cannot write a posting at
    all, so it fails rather than reporting a quiet day.
    """
    from job_hunter.recover_posting_stage import (
        FAILED,
        VISIBILITY_TIMEOUT_SECONDS,
        drain_recover_posting,
    )

    dsn = load_ingestion_dsn()
    if dsn is None:
        logger.error(
            "recover-posting needs SUPABASE_DB_URL: a recovery attempt writes "
            "postings, which only ingestion's direct connection may do"
        )
        return 1
    database = IngestionDatabase(dsn)
    try:
        drain, healthy = _recorded_drain(
            database,
            "recover_posting",
            VISIBILITY_TIMEOUT_SECONDS,
            lambda run: drain_recover_posting(
                database, HttpClient(), limit=args.limit, on_batch=run.heartbeat
            ),
        )
    finally:
        database.close()
    logger.info("recover_posting complete: %s", drain.summary())
    if drain.claimed and drain.completed == 0 and drain.outcomes[FAILED]:
        logger.error(
            "recover_posting completed no attempt: %d claimed, %d failed",
            drain.claimed,
            drain.outcomes[FAILED],
        )
        return 1
    return _health_exit_code("recover_posting", healthy)


def _crawl_source(args: argparse.Namespace) -> int:
    """Drain the crawl_source queue (#184, #189).

    Not as user-free as `recheck-freshness`: `sources.build_source` needs
    `settings.policy` (which markets/ATS boards/keywords to crawl, the Brave
    key) to build the one source a message names, and that policy still
    lives on the per-user search profile row -- so this needs the full
    per-user `Settings`, not just ingestion's connection. Accepted for a
    single-user deployment; splitting search policy out of the per-user
    profile is separate, real work this ticket does not do.
    """
    from job_hunter.crawl_source import VISIBILITY_TIMEOUT_SECONDS, drain_crawl_source
    from job_hunter.sources import build_brave_budget, build_source

    dsn = load_ingestion_dsn()
    if dsn is None:
        logger.error(
            "crawl-source needs SUPABASE_DB_URL: a crawl writes postings, "
            "which only ingestion's direct connection may do"
        )
        return 1

    http = HttpClient()
    ingestion = IngestionDatabase(dsn)

    def _drain(run: WorkerRun):
        # Inside the recorded run, so a Supabase client or search profile that
        # cannot be loaded is a failed run rather than an absent one.
        store = PostgresJobStore(_build_client(http), ingestion)
        settings = load_settings(store)
        search_breaker = CircuitBreaker(_SEARCH_FAILURE_THRESHOLD)
        brave_budget = build_brave_budget(settings, store.client)
        query_date = datetime.now(ZoneInfo(settings.timezone)).date()

        def _build_one_source(crawl_key: str):
            return build_source(
                settings,
                http,
                crawl_key,
                store=store,
                search_breaker=search_breaker,
                query_date=query_date,
                brave_budget=brave_budget,
                supabase_client=store.client,
            )

        return drain_crawl_source(
            ingestion,
            http,
            build_source=_build_one_source,
            persist=store.merge_posting_batch,
            limit=args.limit,
            on_batch=run.heartbeat,
            worker_run_id=run.id,
        )

    try:
        drain, healthy = _recorded_drain(
            ingestion, "crawl_source", VISIBILITY_TIMEOUT_SECONDS, _drain
        )
    finally:
        # The pool is the only thing a store holds (`PostgresJobStore.close`
        # closes exactly this), and it outlives the store so health can be read.
        ingestion.close()
    logger.info("crawl_source complete: %s", drain.summary())
    if drain.claimed and drain.claimed == drain.outcomes.get("failed", 0):
        # Every single crawl failing is a process that cannot reach
        # anything -- make it red. An isolated source failure is the
        # queue's own retry/backoff to handle, same rule recheck-freshness
        # applies.
        logger.error(
            "crawl_source completed no crawl: %d claimed, %d failed",
            drain.claimed,
            drain.outcomes["failed"],
        )
        return 1
    return _health_exit_code("crawl_source", healthy)


def _extract_facets(args: argparse.Namespace) -> int:
    """Drain the extract_facets queue (#185, #189).

    Spends only the platform key, never a user's -- `load_platform_ai_settings`
    reads it straight from the environment, with no search profile, CV, or
    cover letter required. A working Supabase client is still needed: the
    platform ledger (`job_hunter_platform_ai_quota_state`,
    `PlatformUsageLedger`) is read and written over PostgREST as an
    authenticated user even though the rows themselves carry no user id.
    """
    from job_hunter.ai.gemini import PROVIDER, build_gemini_provider
    from job_hunter.ai.usage import AIUsageTracker, PlatformUsageLedger
    from job_hunter.extract_facets_stage import (
        VISIBILITY_TIMEOUT_SECONDS,
        drain_extract_facets,
    )

    dsn = load_ingestion_dsn()
    if dsn is None:
        logger.error(
            "extract-facets needs SUPABASE_DB_URL: extraction writes shared "
            "facets, which only ingestion's direct connection may do"
        )
        return 1

    http = HttpClient()
    ingestion = IngestionDatabase(dsn)

    def _drain(run: WorkerRun):
        # Inside the recorded run: a missing platform key is a failed
        # invocation with its reason on the row, not an absent one.
        platform_settings = load_platform_ai_settings()
        if platform_settings is None:
            raise RuntimeError(
                "extract-facets needs PLATFORM_GEMINI_API_KEY: extraction spends "
                "only the platform key, never a user's"
            )
        platform_key, platform_quota, ai_model = platform_settings
        store = PostgresJobStore(_build_client(http), ingestion)
        platform_tracker = AIUsageTracker(
            PlatformUsageLedger(store), platform_quota, ai_model, provider=PROVIDER
        )
        ai = build_gemini_provider(
            "",
            ai_model,
            http,
            platform_api_key=platform_key,
            platform_tracker=platform_tracker,
        )
        result = drain_extract_facets(
            ingestion, ai, limit=args.limit, on_batch=run.heartbeat
        )
        # The engine's per-day AI cost report, now that #189 retired the one
        # process that used to print every ledger together: each stage that
        # spends AI budget logs its own account, here the shared platform key
        # extraction runs against (#128).
        _log_ai_usage(platform_tracker, "platform")
        return result

    try:
        drain, healthy = _recorded_drain(
            ingestion, "extract_facets", VISIBILITY_TIMEOUT_SECONDS, _drain
        )
    finally:
        ingestion.close()
    logger.info("extract_facets complete: %s", drain.summary())
    if drain.claimed and drain.claimed == drain.outcomes.get("failed", 0):
        logger.error(
            "extract_facets completed no extraction: %d claimed, %d failed",
            drain.claimed,
            drain.outcomes["failed"],
        )
        return 1
    return _health_exit_code("extract_facets", healthy)


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
    # Spends the user's own key, so it reports the user ledger -- the other
    # half of the report `extract-facets` prints for the platform ledger.
    # Without it, #189 would have left the user's key with no cost report at
    # all now that the run that printed one is gone.
    _log_ai_usage(tracker, "user")
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
    # Also the user's own key. `tracker_store` is a DryRunStore under
    # --dry-run, so this reads the discarded dry-run ledger rather than the
    # live one -- which is the same store every guardrail in this command
    # already consults, and keeps --dry-run's "persists nothing" promise.
    _log_ai_usage(tracker, "user")
    return 0
