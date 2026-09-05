from __future__ import annotations

import math
import sqlite3
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from job_hunter.models import SearchQuery


_CREATE_SEARCH_API_USAGE = """
CREATE TABLE IF NOT EXISTS search_api_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    occurred_at TEXT NOT NULL
)
"""

_CREATE_SEARCH_API_USAGE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_search_api_usage_provider_time
ON search_api_usage(provider, occurred_at)
"""


class SearchUsageLedger:
    """Tiny SQLite ledger for metered external search API requests."""

    def __init__(self, db_path: Path | str) -> None:
        self._path = str(db_path)
        with sqlite3.connect(self._path) as conn:
            conn.execute(_CREATE_SEARCH_API_USAGE)
            conn.execute(_CREATE_SEARCH_API_USAGE_INDEX)

    def record(self, *, provider: str, occurred_at: datetime) -> None:
        occurred_at = _normalize_utc(occurred_at)
        with sqlite3.connect(self._path) as conn:
            conn.execute(
                "INSERT INTO search_api_usage (provider, occurred_at) VALUES (?, ?)",
                (provider, occurred_at.isoformat()),
            )

    def count(self, *, provider: str, start_at: datetime, end_at: datetime) -> int:
        start_at = _normalize_utc(start_at)
        end_at = _normalize_utc(end_at)
        with sqlite3.connect(self._path) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*)
                FROM search_api_usage
                WHERE provider = ? AND occurred_at >= ? AND occurred_at < ?
                """,
                (provider, start_at.isoformat(), end_at.isoformat()),
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def try_record(
        self,
        *,
        provider: str,
        occurred_at: datetime,
        monthly_limit: int,
        daily_limit: int,
    ) -> bool:
        """Atomically reserve one request without exceeding persisted limits."""
        if monthly_limit <= 0 or daily_limit <= 0:
            return False

        occurred_at = _normalize_utc(occurred_at)
        month_start = occurred_at.replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        next_month = _next_month_start(occurred_at)
        day_start = occurred_at.replace(hour=0, minute=0, second=0, microsecond=0)
        next_day = day_start + timedelta(days=1)

        with sqlite3.connect(self._path, timeout=30) as conn:
            # Serialize the read-check-insert sequence so overlapping manual runs
            # cannot both observe the same final slot and exceed the hard cap.
            conn.execute("BEGIN IMMEDIATE")
            used_month_row = conn.execute(
                """
                SELECT COUNT(*)
                FROM search_api_usage
                WHERE provider = ? AND occurred_at >= ? AND occurred_at < ?
                """,
                (provider, month_start.isoformat(), next_month.isoformat()),
            ).fetchone()
            used_day_row = conn.execute(
                """
                SELECT COUNT(*)
                FROM search_api_usage
                WHERE provider = ? AND occurred_at >= ? AND occurred_at < ?
                """,
                (provider, day_start.isoformat(), next_day.isoformat()),
            ).fetchone()
            used_month = int(used_month_row[0]) if used_month_row is not None else 0
            used_day = int(used_day_row[0]) if used_day_row is not None else 0
            if used_month >= monthly_limit or used_day >= daily_limit:
                conn.rollback()
                return False

            conn.execute(
                "INSERT INTO search_api_usage (provider, occurred_at) VALUES (?, ?)",
                (provider, occurred_at.isoformat()),
            )
            conn.commit()
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
        """Reserve one Brave attempt before HTTP; false means make no request."""
        now = _normalize_utc(self._now())
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
