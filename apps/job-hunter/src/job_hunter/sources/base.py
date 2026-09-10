from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Protocol

import requests
from bs4 import BeautifulSoup

from job_hunter.models import Job

logger = logging.getLogger(__name__)


class JobSource(Protocol):
    # How a source instance is keyed in the discovery cost statistics
    # (`DiscoveryStats.elapsed_by_source` / `requests_by_source`). Adapters
    # configured once per board make it a property that includes the board,
    # so `lever:acme` and `lever:globex` stay apart. A source without one is
    # still measured, under its class name -- see `discovery.source_cost_label`.
    source_label: str

    # The durable name this source is keyed by in `job_hunter_sources`,
    # in `job_hunter_source_crawls` and in its own pg_cron entry. It
    # defaults to `source_label` because the two have always been the
    # same string; it is separate because `source_label` is documented
    # as a metrics label and a registry key must not drift with it.
    source_key: str

    def discover(self) -> Iterator[Job]:
        """Yield jobs as they are found, not as one fully built list.

        Incremental production is what lets a caller stop a source part
        way through -- between feed pages, ATS boards, watched companies
        or search queries -- and keep everything it produced up to that
        point. A source that materialises its whole harvest first cannot
        be bounded from outside without discarding the harvest.

        `discovery._iter_source_jobs` is the caller that stops one, when a
        source overruns its wall-clock budget. It cuts between the units
        yielded here and never inside one, so the size of a unit is the
        granularity at which a source can be bounded at all: a source whose
        unit is one indivisible fetch is bounded by the request timeout
        instead. Every implementation is a generator function, which the
        protocol cannot express but `test_sources_incremental.py` enforces.

        Because the work now happens while the caller iterates rather than
        inside this call, a caller measuring what a source costs has to
        bracket the whole drain, not this call -- see
        `discovery._iter_source_jobs`.
        """
        ...


def source_key_for(source) -> str:
    """Return `source`'s registry key, falling back to its metrics label.

    `source_key` is declared on the Protocol but is not required of an
    adapter: every existing one predates it, and none of them needs
    changing for the key to be correct.

    A source with neither falls back to its class name, matching
    `discovery.source_cost_label` -- a test double or a source added without
    a label is keyed and measured rather than crashing the crawl over its
    own bookkeeping.
    """
    return (
        getattr(source, "source_key", None)
        or getattr(source, "source_label", None)
        or type(source).__name__
    )


def strip_html(text: str) -> str:
    if not text:
        return text
    soup = BeautifulSoup(text, "html.parser")
    return " ".join(soup.get_text(separator=" ").split())


def is_stale_board_error(exc: Exception) -> bool:
    """Return whether `exc` looks like a permanent 404 for a stale ATS board.

    A 404 from Lever/Greenhouse/Ashby means the board identifier no longer
    exists (renamed or removed company). That is expected registry noise,
    not a bug worth a full traceback.
    """
    return isinstance(exc, requests.HTTPError) and getattr(
        exc.response, "status_code", None
    ) == 404
