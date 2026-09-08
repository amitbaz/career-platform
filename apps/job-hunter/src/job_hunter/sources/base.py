from __future__ import annotations

import logging
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

    def discover(self) -> list[Job]: ...


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
