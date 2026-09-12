"""Run #249's backfill (job_hunter_backfill_ats_triple_dupes) in batches.

The migration that defines the function (20260911100000) also calls it once,
unbatched, inside the migration's own transaction -- fine for the corpus #249
measured, but every row a batch touches stays locked until its transaction
commits, and a migration file cannot commit partway through. This script is
the chunked alternative: each batch runs in its own transaction (`connection()`
commits on a clean exit, exactly like every other privileged write ingestion
makes -- see `engine/pg.py`), so already-processed rows are released
before the next batch starts rather than all being held until the very end.

Safe to run more than once, and safe to stop and resume: the function is
idempotent (a later call sees only what an earlier one left undone), so a
run interrupted between batches has committed everything up to that point and
picking it back up just continues.

Run once, by hand, against the hosted project:

    SUPABASE_DB_URL=... python -m scripts.backfill_ats_triple_dupes

Rehearse against the local stack first (`supabase start`, `pnpm db:reset`)
with SUPABASE_DB_URL pointed at it, exactly as `migrate_sqlite_to_postgres.py`
recommends for the same reason.
"""

from __future__ import annotations

import argparse
import logging
import time

from engine.config import load_ingestion_dsn
from engine.pg import IngestionDatabase

logger = logging.getLogger(__name__)

_DEFAULT_BATCH_SIZE = 200
_DEFAULT_PAUSE_SECONDS = 0.5


def run_backfill(
    ingestion: IngestionDatabase,
    *,
    batch_size: int,
    pause_seconds: float,
) -> tuple[int, int, int]:
    """Call the backfill repeatedly until a batch does nothing, in one
    committing transaction per batch.

    Returns the running totals (groups processed, pairs merged, fingerprints
    rewritten) across every batch. `pause_seconds` between batches is
    deliberately not zero: this shares the database with live crawls, and a
    short pause is what keeps a long backfill from monopolising the pooler
    the way a tight loop of back-to-back transactions would.
    """
    total_groups = 0
    total_pairs = 0
    total_rewritten = 0
    batch_number = 0
    while True:
        batch_number += 1
        with ingestion.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "select groups_processed, merged_pairs, fingerprints_rewritten "
                    "from public.job_hunter_backfill_ats_triple_dupes(%s)",
                    (batch_size,),
                )
                groups, pairs, rewritten = cursor.fetchone()
        total_groups += groups
        total_pairs += pairs
        total_rewritten += rewritten
        logger.info(
            "batch %d: merged %d pair(s) across %d group(s), rewrote %d fingerprint(s)",
            batch_number, pairs, groups, rewritten,
        )
        if groups == 0 and rewritten == 0:
            break
        time.sleep(pause_seconds)
    return total_groups, total_pairs, total_rewritten


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="backfill_ats_triple_dupes",
        description=(
            "Run #249's ATS-triple posting backfill in separate, "
            "committing transactions rather than one unbounded migration transaction."
        ),
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=_DEFAULT_BATCH_SIZE,
        help=f"groups/fingerprints per transaction (default: {_DEFAULT_BATCH_SIZE})",
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

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    dsn = load_ingestion_dsn()
    if dsn is None:
        parser.error(
            "SUPABASE_DB_URL is not set. This backfill runs on the privileged "
            "ingestion connection, the same one job_hunter_postings writes go "
            "through since #179."
        )

    if not args.yes:
        print(f"About to run #249's backfill against the database SUPABASE_DB_URL names.")
        print(f"  batch size {args.batch_size}, pause {args.pause_seconds}s between batches")
        if input("Type 'backfill' to proceed: ").strip() != "backfill":
            print("Aborted.")
            return 1

    ingestion = IngestionDatabase(dsn)
    try:
        groups, pairs, rewritten = run_backfill(
            ingestion, batch_size=args.batch_size, pause_seconds=args.pause_seconds
        )
    finally:
        ingestion.close()

    print(
        f"\nDone: merged {pairs} pair(s) across {groups} group(s), "
        f"rewrote {rewritten} fingerprint(s)."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by hand
    raise SystemExit(main())
