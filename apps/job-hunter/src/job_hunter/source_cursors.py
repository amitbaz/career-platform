"""Where each source resumes from, across crawls.

One row per source key, holding the HTTP cache validators that source's
board last answered with and the URL they belong to. Reading it is what makes
a crawl conditional; writing it back is what makes the cursor advance.

Both halves are best-effort and never raise into a crawl. A cursor is an
optimisation: losing one costs a full fetch next time, which is exactly what
happened on every crawl before this existed. Letting that failure abort a
crawl that has already succeeded would trade a cheap loss for an expensive
one.
"""

from __future__ import annotations

import logging

from job_hunter.http import Validators

logger = logging.getLogger(__name__)


class SourceCursorStore:
    """Reads and writes `job_hunter_source_cursors` over a privileged lease."""

    def __init__(self, database) -> None:
        self._database = database

    def read(self, source_key: str) -> tuple[str, Validators]:
        """Return `(url, validators)` for `source_key`, empty if it has none."""
        try:
            with self._database.connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "select url, etag, last_modified "
                        "from public.job_hunter_source_cursors "
                        "where source_key = %s",
                        (source_key,),
                    )
                    row = cursor.fetchone()
        except Exception:
            logger.warning(
                "could not read the crawl cursor for %s; crawling in full",
                source_key,
                exc_info=True,
            )
            return "", Validators()
        if row is None:
            return "", Validators()
        return row[0] or "", Validators(etag=row[1] or "", last_modified=row[2] or "")

    def write(self, source_key: str, url: str, validators: Validators) -> None:
        """Store the validators `url` last answered with.

        A response carrying neither validator is stored as empty rather than
        skipped, so a board that stops sending an ETag stops being asked
        conditionally instead of being asked with a validator it no longer
        honours.
        """
        if not url:
            return
        try:
            with self._database.connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "insert into public.job_hunter_source_cursors "
                        "  (source_key, url, etag, last_modified, updated_at) "
                        "values (%s, %s, %s, %s, now()) "
                        "on conflict (source_key) do update set "
                        "  url = excluded.url, "
                        "  etag = excluded.etag, "
                        "  last_modified = excluded.last_modified, "
                        "  updated_at = excluded.updated_at",
                        (source_key, url, validators.etag, validators.last_modified),
                    )
        except Exception:
            logger.warning(
                "could not store the crawl cursor for %s; the next crawl will "
                "fetch in full",
                source_key,
                exc_info=True,
            )


#: How a discovery source outcome maps onto a crawl row's `outcome`. The two
#: vocabularies differ because they answer different questions: discovery
#: reports how *reading* the source ended, a crawl row reports what the source
#: *yielded*. A source cut off by its time budget yielded what it yielded, so
#: it records as a fetch rather than as a failure -- it is not broken, and
#: banding it down for being large would visit it less and cut it off sooner.
_CRAWL_OUTCOMES = {
    "completed": "fetched",
    "cut_off": "fetched",
    "not_modified": "not_modified",
    "failed": "failed",
}


def record_run_crawls(
    database, stats, keys_by_label: dict[str, str], *, novelty_measured: bool = True
) -> int:
    """Write one `job_hunter_source_crawls` row per source this run read.

    The inline pipeline is the only thing that crawls today -- nothing drains
    the `crawl_source` queue yet -- so without this the yield table stays
    empty and `job_hunter_reschedule_sources` bands every source on no
    evidence, which demotes all of them to the slowest cadence. That is worse
    than not scheduling at all, so the writer and the scheduler have to land
    together.

    Best-effort, like the cursors: telemetry must not be able to fail a run
    that already delivered. Returns the number of rows written.
    """
    # A run whose staged batch fell back measured no novelty at all, and
    # `new_to_corpus` would be zero for every source -- which the scheduler
    # reads as "nothing was new", not as "nothing was counted". Six such runs
    # would walk the whole portfolio to the weekly band because of a queue
    # hiccup. Record nothing rather than record a zero that means something
    # else.
    if not novelty_measured:
        logger.info(
            "skipping this run's crawl rows: novelty was not measured, and a "
            "zero would be read as an absence of new postings"
        )
        return 0

    rows = []
    for label, outcome in stats.source_outcomes.items():
        rows.append(
            (
                keys_by_label.get(label, label),
                _CRAWL_OUTCOMES.get(outcome, "failed"),
                stats.raw_by_label.get(label, 0),
                stats.new_to_corpus_by_label.get(label, 0),
                stats.requests_by_source.get(label, 0),
                int(stats.elapsed_by_source.get(label, 0.0) * 1000),
            )
        )
    if not rows:
        return 0
    try:
        with database.connection() as connection:
            with connection.cursor() as cursor:
                # Register the crawl targets alongside their rows. The
                # scheduler loops over `job_hunter_crawl_targets`, and the
                # only other writer is the crawl stage -- which has no
                # consumer. Without this the yield table fills up while the
                # targets table stays empty, so `job_hunter_reschedule_sources`
                # iterates nothing and installs no cron entry at all.
                cursor.executemany(
                    "insert into public.job_hunter_crawl_targets (crawl_key) "
                    "values (%s) on conflict (crawl_key) do nothing",
                    [(row[0],) for row in rows],
                )
                cursor.executemany(
                    # `changed` is deliberately left at its default of zero.
                    # It counts listings that survived the description-hash
                    # short-circuit, and the inline pipeline does not run one
                    # -- only the crawl stage does. Reporting `fetched` there
                    # instead would make the cadence read every source as
                    # maximally productive, since the band sums
                    # `new_to_corpus + changed`, and peg the whole portfolio
                    # to the fastest band. An absent measurement is recorded
                    # as absent.
                    "insert into public.job_hunter_source_crawls "
                    "  (source_key, outcome, fetched, new_to_corpus, "
                    "   requests, elapsed_ms) "
                    "values (%s, %s, %s, %s, %s, %s)",
                    rows,
                )
    except Exception:
        logger.warning(
            "could not record this run's per-source crawl rows; the crawl "
            "cadence will band on older evidence",
            exc_info=True,
        )
        return 0
    return len(rows)
