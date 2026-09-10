"""Retrying a provider call that hits rolling capacity (#188).

Extracted from `pipeline.py`'s `_waiting_out_capacity` so the scoring call
inside `matching.match_jobs` gets the same unbounded-wait behavior the daily
run always gave it, rather than a second, ad hoc version of the same
tradeoff. Nothing about the wait itself changed in the move.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, TypeVar

from job_hunter.ai.port import AITemporaryCapacity

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


def wait_out_capacity(
    call: Callable[[], _T],
    *,
    doing: str,
    job_id: str,
    max_waits: int | None = None,
) -> _T:
    """Run `call`, waiting out the provider's rolling window on `AITemporaryCapacity`.

    A caller reading a posting or scoring a job waits rather than giving up
    its turn: the work in front of it has no later chance today. The
    adapter's own preflight pacing has already slept out one window and
    re-checked before raising, so each pass here is a second, deliberate
    wait.

    `max_waits` bounds patience where the capacity being waited on is not
    this caller's alone to clear (the platform key's rolling window, shared
    with every other user's run); `None` waits as long as it takes and
    re-raises nothing. The last `AITemporaryCapacity` propagates when the
    bound is spent, for the caller to turn into whatever giving up means to
    it. Scoring against the user's own key passes `None`: the only other
    claimant of that key is this same run.
    """
    waits = 0
    while True:
        try:
            return call()
        except AITemporaryCapacity as exc:
            if max_waits is not None and waits >= max_waits:
                logger.info(
                    "AI temporary capacity still full after %s waits; giving up on "
                    "%s for job_id=%s",
                    waits,
                    doing,
                    job_id,
                )
                raise
            waits += 1
            logger.info(
                "AI temporary capacity reached; waiting %.2fs before %s for job_id=%s",
                exc.retry_after_seconds,
                doing,
                job_id,
            )
            time.sleep(exc.retry_after_seconds)
