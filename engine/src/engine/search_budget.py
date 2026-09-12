"""Metered external search API budget, backed by `job_hunter_platform_search_usage`.

The reservation is atomic, and it has to be here rather than in this file. The
SQLite original serialized its read-check-insert inside `BEGIN IMMEDIATE`, so
two overlapping callers could never both observe the same under-the-cap slot
and both insert. PostgREST offers nothing equivalent from the client side --
there is no session-scoped transaction to hold open across a `select` and a
later `upsert` -- so `try_record` below delegates the whole check-and-insert
to `job_hunter_reserve_search_request`, which runs it in one transaction
behind a per-provider advisory lock.

That guarantee used to come from somewhere else. Until #189 retired
`job-hunter-daily.yml`, the GitHub Actions `concurrency: group:
job-hunter-state` guard serialized every writer that touched this ledger, and
a plain read-then-write here was safe because there was only ever one writer.
`crawl-source` (render.yaml) is the only caller left, and Render's cron
scheduling does not promise one invocation finishes before the next starts: a
drain that outlives its fifteen-minute slot, or a local `cli.py` run while the
cron fires, puts two callers on this path at once. The lock is what makes that
harmless.

Pacing is a separate, deliberately softer thing. `BraveRequestBudget` computes
the day's target share (`_brave_daily_limit`) from unlocked reads before
calling `try_record`, so two racing callers can pass slightly different
`daily_limit` values into the same reservation. That only moves a query
between days; the monthly cap, which is the one drawn against the API key, is
counted inside the lock and is exact.
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Callable

from engine.models import SearchQuery
from engine.store_mapping import to_iso
from engine.supabase_client import SupabaseClient

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

        The count and the insert happen inside
        `job_hunter_reserve_search_request`, one transaction behind a
        per-provider advisory lock, so two overlapping callers cannot both
        claim the last slot under the cap. Doing it here instead would need
        a transaction held open across two PostgREST requests, which does
        not exist -- see the module docstring.

        Safe to retry: the reservation is keyed by `(provider, occurred_at)`,
        so a repeated call for the same instant converges on the row it
        already wrote rather than spending a second slot.
        """
        if monthly_limit <= 0 or daily_limit <= 0:
            return False

        occurred_at = _normalize_utc(occurred_at)
        granted = self._client.rpc(
            "job_hunter_reserve_search_request",
            {
                "p_provider": provider,
                "p_occurred_at": to_iso(occurred_at),
                "p_monthly_limit": monthly_limit,
                "p_daily_limit": daily_limit,
            },
        )
        return bool(granted and granted[0])


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
