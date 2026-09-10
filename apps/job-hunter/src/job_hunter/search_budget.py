"""Metered external search API budget, backed by `job_hunter_platform_search_usage`.

**The one weakening this port accepts:** the SQLite original serialized its
read-check-insert reservation inside `BEGIN IMMEDIATE`, so two overlapping
callers could never both observe the same under-the-cap slot and both insert.
PostgREST has no equivalent -- there is no session-scoped transaction to hold
open across a `select` and a later `upsert`. `try_record` below is a plain
read-then-write: two concurrent callers can each read "under the cap" and
both write, overshooting the daily/monthly limit by the number of racing
callers. What protects this in practice is not application logic but
deployment shape: every workflow that can call `try_record`
(`job-hunter-daily.yml`, `job-hunter-generate-cover-letter.yml`) shares
`concurrency: group: job-hunter-state` with `cancel-in-progress: false`, so
at most one writer is ever *running* against a given user's rows at a time
-- a second run in that group, including a manually-triggered
`workflow_dispatch` of either workflow, queues behind the first rather than
overlapping it. The real escape hatch is anything outside GitHub Actions
entirely: a local `cli.py` invocation on the owner's machine runs with no
concurrency group at all and can call `try_record` while an Actions run is
in flight, reintroducing the race this docstring describes.
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Callable

from job_hunter.models import SearchQuery
from job_hunter.store_mapping import to_iso
from job_hunter.supabase_client import SupabaseClient

_TABLE = "job_hunter_platform_search_usage"


class SearchUsageLedger:
    """Metered ledger for external search API requests, one row per request.

    Translates `search_budget.py`'s original SQLite-backed ledger (deleted)
    onto `job_hunter_platform_search_usage` (issue #184). The quota belongs
    to the API key, not to a person: a shared crawl runs as the privileged
    role with no user identity, so the ledger carries no `user_id`. The
    table's `(provider, occurred_at)` unique constraint is what makes
    `record`'s upsert converge instead of duplicating a retried write.
    """

    def __init__(self, client: SupabaseClient) -> None:
        self._client = client

    def record(self, *, provider: str, occurred_at: datetime) -> None:
        occurred_at = _normalize_utc(occurred_at)
        self._client.upsert(
            _TABLE,
            [
                {
                    "provider": provider,
                    "occurred_at": to_iso(occurred_at),
                }
            ],
            on_conflict="provider,occurred_at",
        )

    def count(self, *, provider: str, start_at: datetime, end_at: datetime) -> int:
        start_at = _normalize_utc(start_at)
        end_at = _normalize_utc(end_at)
        rows = self._client.select(
            _TABLE,
            params={
                "provider": f"eq.{provider}",
                "and": f"(occurred_at.gte.{to_iso(start_at)},occurred_at.lt.{to_iso(end_at)})",
                "select": "id",
            },
        )
        return len(rows)

    def try_record(
        self,
        *,
        provider: str,
        occurred_at: datetime,
        monthly_limit: int,
        daily_limit: int,
    ) -> bool:
        """Reserve one request without exceeding persisted limits.

        Not atomic against a concurrent caller -- see the module docstring
        for what protects this in production. `count` reads the current
        usage, and if both caps still allow one more request, `record`
        writes it; a race between two callers can let both through.
        """
        if monthly_limit <= 0 or daily_limit <= 0:
            return False

        occurred_at = _normalize_utc(occurred_at)
        month_start = occurred_at.replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        next_month = _next_month_start(occurred_at)
        day_start = occurred_at.replace(hour=0, minute=0, second=0, microsecond=0)
        next_day = day_start + timedelta(days=1)

        used_month = self.count(provider=provider, start_at=month_start, end_at=next_month)
        used_day = self.count(provider=provider, start_at=day_start, end_at=next_day)
        if used_month >= monthly_limit or used_day >= daily_limit:
            return False

        self.record(provider=provider, occurred_at=occurred_at)
        return True


def _normalize_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc)


def _next_month_start(now: datetime) -> datetime:
    if now.month == 12:
        return now.replace(
            year=now.year + 1,
            month=1,
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
    return now.replace(
        month=now.month + 1,
        day=1,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )


def _brave_daily_limit(
    ledger: SearchUsageLedger,
    *,
    monthly_limit: int,
    now: datetime,
) -> int:
    """Return today's stable target based on capacity available at day start."""
    if monthly_limit <= 0:
        return 0

    now = _normalize_utc(now)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month = _next_month_start(now)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    next_day = day_start + timedelta(days=1)

    used_month = ledger.count(provider="brave", start_at=month_start, end_at=next_month)
    used_today = ledger.count(provider="brave", start_at=day_start, end_at=next_day)
    used_before_today = max(0, used_month - used_today)
    capacity_at_day_start = max(0, monthly_limit - used_before_today)
    days_remaining = max(1, (next_month.date() - now.date()).days)
    return math.ceil(capacity_at_day_start / days_remaining)


def brave_queries_available_today(
    ledger: SearchUsageLedger,
    *,
    monthly_limit: int,
    now: datetime,
) -> int:
    """Return today's remaining Brave allowance while respecting a monthly hard cap.

    Remaining monthly capacity is spread across the remaining calendar days.
    Because daily usage is persisted, manual reruns on the same day cannot spend
    another full daily allocation.
    """
    if monthly_limit <= 0:
        return 0

    now = _normalize_utc(now)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month = _next_month_start(now)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    next_day = day_start + timedelta(days=1)

    used_month = ledger.count(provider="brave", start_at=month_start, end_at=next_month)
    remaining_month = max(0, monthly_limit - used_month)
    if remaining_month == 0:
        return 0

    used_today = ledger.count(provider="brave", start_at=day_start, end_at=next_day)
    target_today = _brave_daily_limit(ledger, monthly_limit=monthly_limit, now=now)
    return max(0, min(remaining_month, target_today - used_today))


class BraveRequestBudget:
    """One persisted, paced Brave allowance shared by every production caller."""

    def __init__(
        self,
        ledger: SearchUsageLedger,
        *,
        monthly_limit: int,
        discovery_share: float = 0.8,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not 0 < discovery_share <= 1:
            raise ValueError("discovery_share must be in (0, 1]")
        self._ledger = ledger
        self.monthly_limit = monthly_limit
        self.discovery_share = discovery_share
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._last_occurred_at: datetime | None = None

    def available_today(self) -> int:
        return brave_queries_available_today(
            self._ledger,
            monthly_limit=self.monthly_limit,
            now=self._now(),
        )

    def discovery_allowance(self) -> int:
        """Give discovery priority while leaving a soft share for later canonical work."""
        available = self.available_today()
        if available <= 0:
            return 0
        return min(available, max(1, math.floor(available * self.discovery_share)))

    def reserve(self) -> bool:
        """Reserve one Brave attempt before HTTP; false means make no request.

        The `(provider, occurred_at)` unique key on
        `job_hunter_platform_search_usage` makes a retried write converge
        instead of double-counting -- but it does so by treating a repeated
        `occurred_at` as *the same* reservation. If this instance's clock
        ever returns a value it has already issued (or an earlier one), the
        upsert in `record` overwrites the prior row instead of adding a new
        one: the ledger stops growing, `count()` stops rising, and this cap
        stops applying. Guard against clock resolution/monotonicity by
        bumping into strictly-increasing territory ourselves.
        """
        now = _normalize_utc(self._now())
        if self._last_occurred_at is not None and now <= self._last_occurred_at:
            now = self._last_occurred_at + timedelta(microseconds=1)
        self._last_occurred_at = now
        daily_limit = _brave_daily_limit(
            self._ledger,
            monthly_limit=self.monthly_limit,
            now=now,
        )
        return self._ledger.try_record(
            provider="brave",
            occurred_at=now,
            monthly_limit=self.monthly_limit,
            daily_limit=daily_limit,
        )


def split_queries_for_brave(
    queries: list[SearchQuery],
    *,
    limit: int,
) -> tuple[list[SearchQuery], list[SearchQuery]]:
    """Select scarce Brave queries round-robin across markets; preserve fallback order."""
    if limit <= 0 or not queries:
        return [], list(queries)
    if limit >= len(queries):
        return list(queries), []

    market_order: list[str] = []
    grouped: dict[str, deque[tuple[int, SearchQuery]]] = defaultdict(deque)
    for index, query in enumerate(queries):
        market_key = query.market_id or "legacy"
        if market_key not in grouped:
            market_order.append(market_key)
        grouped[market_key].append((index, query))

    selected_indices: list[int] = []
    selected: list[SearchQuery] = []
    while len(selected) < limit:
        progressed = False
        for market_key in market_order:
            queue = grouped[market_key]
            if not queue:
                continue
            index, query = queue.popleft()
            selected_indices.append(index)
            selected.append(query)
            progressed = True
            if len(selected) >= limit:
                break
        if not progressed:
            break

    chosen = set(selected_indices)
    fallback = [query for index, query in enumerate(queries) if index not in chosen]
    return selected, fallback
