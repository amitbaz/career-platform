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
from job_hunter.ai.usage import AIUsageTracker, PlatformUsageLedger
from job_hunter.circuit_breaker import CircuitBreaker
from job_hunter.gmail_auth import GoogleOAuthTokenProvider
from job_hunter.gmail_client import GmailClient
from job_hunter.gmail_sync import GmailSyncService
from job_hunter.http import HttpClient
from job_hunter.pg import IngestionDatabase
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
from job_hunter.worker_runs import WorkerRun, report_worker_health

logger = logging.getLogger(__name__)

#: Consecutive canonical-resolution search failures before a drain stops
#: paying for more of them this process. Same threshold `pipeline.py` uses.
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
        return _run(args)
    except Exception:
        logger.exception("job hunter run failed")
        return 1


def _build_ingestion_database() -> IngestionDatabase | None:
    """Ingestion's direct Postgres connection, where one is configured.

    Only the daily pipeline builds one. The webhook and the on-demand cover
    letter do no ingestion, and a privileged connection they would never use
    is a privileged connection nobody is watching.
    """
    dsn = load_ingestion_dsn()
    if dsn is None:
        logger.warning(
            "no SUPABASE_DB_URL is configured: this run cannot write shared rows "
            "(postings, their facets, company facts, ATS board health), so it will "
            "skip discovery and enrichment entirely and deliver from the postings "
            "that already exist. The corpus will not change until it is configured."
        )
        return None
    try:
        return IngestionDatabase(dsn)
    except Exception:
        # Since #179 this is no longer "the fast path is unavailable, take the
        # slow one": the shared corpus is writable only over this connection,
        # so a run without it delivers from what it already has and adds
        # nothing. Still not a reason to refuse to start -- a degraded day is
        # better than an outage -- but it is a warning, not a note.
        logger.exception(
            "SUPABASE_DB_URL is set but ingestion could not open a direct "
            "Postgres connection; this run will skip discovery and enrichment and "
            "deliver from the postings that already exist"
        )
        return None


def _run(args: argparse.Namespace) -> int:
    http = HttpClient()
    store = PostgresJobStore(_build_client(http), _build_ingestion_database())
    try:
        return _run_with(args, http, store)
    finally:
        # The pool holds real server connections. Closing it is the store's
        # job, and doing it here means a run that raises releases them too.
        store.close()


def _run_with(
    args: argparse.Namespace, http: HttpClient, store: PostgresJobStore
) -> int:
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
        "blocked_by_facets=%d postings_written=%d facets_extracted=%d "
        "facets_reused=%d facets_failed=%d",
        summary.ready_to_apply,
        summary.possible_matches,
        summary.skipped,
        summary.errors,
        # Jobs the stored facets disqualified without a scoring call (#127):
        # the saving this run made against the user's own provider quota.
        summary.blocked_by_facets,
        # What this run added to the shared corpus (#179). Reported next to
        # the facet counters because the two zeroes together are the
        # signature of a run with no direct Postgres connection: it scored
        # and delivered from postings that already existed and could add
        # nothing. Without this number that run reads exactly like a quiet
        # week, which is the failure mode rule 5 in AGENTS.md is about.
        summary.postings_written,
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
        return drain_extract_facets(
            ingestion, ai, limit=args.limit, on_batch=run.heartbeat
        )

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
