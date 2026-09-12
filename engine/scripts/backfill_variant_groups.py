"""Group the existing corpus into variant groups (#61) in committing batches.

The migration that defines job_hunter_assign_variant_groups (20260910160000)
used to backfill the whole corpus itself, inside the deploy's one statement.
On the live corpus that ran past the deploy's statement timeout and rolled the
migration back, so the backfill moved here: each batch is one call to
job_hunter_assign_variant_groups over the next ungrouped postings, in its own
transaction (`connection()` commits on a clean exit -- see `engine/pg.py`),
so no single statement or transaction has to cover the corpus.

job_hunter_backfill_variant_groups is not used: it loops to completion inside
one call, which is exactly the one-transaction shape this avoids.

Safe to run more than once, and safe to stop and resume: only postings whose
variant_group_id is still null are touched, so a later run continues where an
earlier one stopped and a finished run does nothing.

Run once, by hand, against the hosted project:

    SUPABASE_DB_URL=... python -m scripts.backfill_variant_groups

Rehearse against the local stack first (`supabase start`, `pnpm db:reset`)
with SUPABASE_DB_URL pointed at it.
"""

from __future__ import annotations

import argparse
import logging
import time

from engine.config import load_ingestion_dsn
from engine.pg import IngestionDatabase

logger = logging.getLogger(__name__)

_DEFAULT_BATCH_SIZE = 500
_DEFAULT_PAUSE_SECONDS = 0.5

# Same selection and order as job_hunter_backfill_variant_groups, one batch.
_BATCH_SQL = """
select count(*) filter (where r.group_id is not null),
       count(*) filter (where r.group_id is not null and not r.joined_existing),
       count(*)
  from public.job_hunter_assign_variant_groups(array(
         select p.id
           from public.job_hunter_postings p
          where p.variant_group_id is null
            and coalesce(p.ats_provider, '') <> ''
            and coalesce(p.ats_board, '') <> ''
          order by p.first_seen_at, p.id
          limit %s)) r
"""


def run_backfill(
    ingestion: IngestionDatabase,
    *,
    batch_size: int,
    pause_seconds: float,
) -> tuple[int, int]:
    """Group batches until one finds nothing left, one committing transaction
    per batch.

    Returns the running totals (postings grouped, groups formed). The pause
    between batches is deliberately not zero: this shares the database with
    live crawls.
    """
    total_grouped = 0
    total_formed = 0
    batch_number = 0
    while True:
        batch_number += 1
        with ingestion.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(_BATCH_SQL, (batch_size,))
                grouped, formed, seen = cursor.fetchone()
        total_grouped += grouped
        total_formed += formed
        logger.info(
            "batch %d: grouped %d posting(s), formed %d new group(s)",
            batch_number, grouped, formed,
        )
        if seen == 0:
            break
        time.sleep(pause_seconds)
    return total_grouped, total_formed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="backfill_variant_groups",
        description=(
            "Group the existing corpus into #61's variant groups in separate, "
            "committing transactions rather than one migration statement."
        ),
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=_DEFAULT_BATCH_SIZE,
        help=f"postings per transaction (default: {_DEFAULT_BATCH_SIZE})",
    )
    parser.add_argument(
        "--pause-seconds",
        type=float,
        default=_DEFAULT_PAUSE_SECONDS,
        help=f"delay between batches (default: {_DEFAULT_PAUSE_SECONDS})",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="skip the confirmation prompt (required when stdin is not a terminal)",
    )
    args = parser.parse_args(argv)
    if args.batch_size <= 0:
        parser.error("--batch-size must be positive")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    dsn = load_ingestion_dsn()
    if dsn is None:
        parser.error(
            "SUPABASE_DB_URL is not set. This backfill runs on the privileged "
            "ingestion connection, the same one job_hunter_postings writes go "
            "through since #179."
        )

    if not args.yes:
        print("About to run #61's variant-group backfill against the database SUPABASE_DB_URL names.")
        print(f"  batch size {args.batch_size}, pause {args.pause_seconds}s between batches")
        if input("Type 'backfill' to proceed: ").strip() != "backfill":
            print("Aborted.")
            return 1

    ingestion = IngestionDatabase(dsn)
    try:
        grouped, formed = run_backfill(
            ingestion, batch_size=args.batch_size, pause_seconds=args.pause_seconds
        )
    finally:
        ingestion.close()

    print(f"\nDone: grouped {grouped} posting(s) into {formed} new group(s).")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by hand
    raise SystemExit(main())
