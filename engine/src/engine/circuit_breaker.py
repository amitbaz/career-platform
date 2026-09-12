"""Stop calling a failing dependency once it is clearly unavailable."""

from __future__ import annotations


class CircuitBreaker:
    """Trip after N consecutive failures so a dead host stops costing retries.

    Scoped to a single run: state is held in memory and never resets on a
    timer, because a run that has already seen a host refuse N calls in a row
    gains nothing from paying the full retry ladder on every remaining call.
    """

    def __init__(self, failure_threshold: int) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        self._failure_threshold = failure_threshold
        self._consecutive_failures = 0
        self._open_notice_claimed = False

    @property
    def is_open(self) -> bool:
        return self._consecutive_failures >= self._failure_threshold

    def claim_open_notice(self) -> bool:
        """Return true once per open period so shared callers log it only once."""
        if not self.is_open or self._open_notice_claimed:
            return False
        self._open_notice_claimed = True
        return True

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._open_notice_claimed = False

    def record_failure(self) -> None:
        self._consecutive_failures += 1
