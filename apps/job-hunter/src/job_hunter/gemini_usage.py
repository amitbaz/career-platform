"""Enforce Gemini free-tier budgets before any HTTP call reaches Google.

`GeminiUsageTracker` is the sole gatekeeper between the pipeline and the
Gemini API: `preflight` decides whether an attempt is allowed against our own
80%-of-provider ceilings (with a reserve for `job_evaluation`) and against any
persisted provider-quota pause, and the `record_*` methods log what actually
happened so future preflight checks and `snapshot` stay accurate.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Literal

from zoneinfo import ZoneInfo

from job_hunter.models import GeminiQuotaSettings, GeminiUsageSummary

if TYPE_CHECKING:
    import sqlite3

    from job_hunter.store import JobStore

GeminiPurpose = Literal[
    "gmail_semantic", "candidate_context", "job_evaluation", "cover_letter"
]
GEMINI_PURPOSES: tuple[GeminiPurpose, ...] = (
    "gmail_semantic",
    "candidate_context",
    "job_evaluation",
    "cover_letter",
)
_CORE_PURPOSE: GeminiPurpose = "job_evaluation"

GeminiPauseKind = Literal["daily_quota", "rate_limit", "unknown"]

_PACIFIC = ZoneInfo("America/Los_Angeles")
_ROLLING_WINDOW = timedelta(seconds=60)
_ROLLING_SAFETY_SECONDS = 0.05


class GeminiBudgetExceeded(RuntimeError):
    """Our own internal daily ceiling or core reserve refused this call."""


class GeminiTemporaryCapacity(GeminiBudgetExceeded):
    """Rolling RPM/TPM capacity is temporarily full but will free shortly."""

    def __init__(self, message: str, *, retry_after_seconds: float) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class GeminiQuotaPaused(RuntimeError):
    """A persisted circuit-breaker pause from a real provider 429 is active."""

    def __init__(self, message: str, *, paused_until: str, reason: str) -> None:
        super().__init__(message)
        self.paused_until = paused_until
        self.reason = reason


def estimate_input_tokens(prompt: str) -> int:
    """A conservative, cheap stand-in for `usageMetadata` before a call is made."""
    return max(1, math.ceil(len(prompt) / 3))


def _normalize_utc(now: datetime) -> datetime:
    """Return an aware datetime as UTC or reject an ambiguous naive input."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(timezone.utc)


def _pacific_day_bounds(now: datetime) -> tuple[datetime, datetime]:
    """Return the [start, end) UTC bounds of the Pacific calendar day containing `now`."""
    local = now.astimezone(_PACIFIC)
    start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def _row_input_tokens(row: sqlite3.Row) -> int:
    """Exact prompt tokens where Google reported them, else the pre-call estimate."""
    if row["prompt_tokens"] is not None:
        return row["prompt_tokens"]
    return row["estimated_input_tokens"]


def _row_total_tokens(row: sqlite3.Row) -> int:
    """Google's own `totalTokenCount` where reported, else a reconstructed estimate.

    `totalTokenCount` already equals `promptTokenCount + candidatesTokenCount +
    thoughtsTokenCount` with no separate term for cached tokens (they are a
    subset of `promptTokenCount`), so this is never `input + output + thinking
    + cached`. A row with no `usageMetadata` at all has no `total_tokens`
    either; its reconstruction below intentionally mirrors that same formula
    (input estimate + output + thinking, no cached) rather than inventing one.
    """
    if row["total_tokens"] is not None:
        return row["total_tokens"]
    return _row_input_tokens(row) + (row["output_tokens"] or 0) + (row["thinking_tokens"] or 0)


def _peak_rolling(rows: list[sqlite3.Row], window: timedelta) -> tuple[int, int]:
    """Peak (request count, input tokens) over any `window`-wide span in `rows`.

    `rows` must be ordered by `occurred_at`. Each row is used as the trailing
    edge of a candidate window; the maximum count/sum over a sliding window is
    always achieved with an edge at an actual event, so this covers every
    possible window without inspecting arbitrary instants.
    """
    times = [datetime.fromisoformat(row["occurred_at"]) for row in rows]
    tokens = [_row_input_tokens(row) for row in rows]
    peak_requests = 0
    peak_tokens = 0
    running_tokens = 0
    start = 0
    for end in range(len(rows)):
        while times[end] - times[start] > window:
            running_tokens -= tokens[start]
            start += 1
        running_tokens += tokens[end]
        peak_requests = max(peak_requests, end - start + 1)
        peak_tokens = max(peak_tokens, running_tokens)
    return peak_requests, peak_tokens


def _retry_after_for_rolling_capacity(
    rows: list[sqlite3.Row],
    *,
    now: datetime,
    rpm_ceiling: int,
    tpm_ceiling: int,
    proposed_input_tokens: int,
) -> float:
    """Return the earliest safe retry delay for current rolling RPM/TPM pressure."""
    release_times: list[datetime] = []

    if len(rows) + 1 > rpm_ceiling:
        rows_to_expire = len(rows) + 1 - rpm_ceiling
        limiting_row = rows[rows_to_expire - 1]
        release_times.append(
            datetime.fromisoformat(limiting_row["occurred_at"])
            + _ROLLING_WINDOW
            + timedelta(seconds=_ROLLING_SAFETY_SECONDS)
        )

    rolling_tokens = sum(_row_input_tokens(row) for row in rows)
    if rolling_tokens + proposed_input_tokens > tpm_ceiling:
        remaining_tokens = rolling_tokens
        for row in rows:
            remaining_tokens -= _row_input_tokens(row)
            if remaining_tokens + proposed_input_tokens <= tpm_ceiling:
                release_times.append(
                    datetime.fromisoformat(row["occurred_at"])
                    + _ROLLING_WINDOW
                    + timedelta(seconds=_ROLLING_SAFETY_SECONDS)
                )
                break

    if not release_times:
        return _ROLLING_SAFETY_SECONDS
    return max(
        _ROLLING_SAFETY_SECONDS,
        max((release - now).total_seconds() for release in release_times),
    )


class GeminiUsageTracker:
    """Preflight budget checks and usage recording for one Gemini model."""

    def __init__(
        self,
        store: JobStore,
        quota: GeminiQuotaSettings,
        model: str,
        *,
        run_id: str | None = None,
    ) -> None:
        self._store = store
        self._quota = quota
        self._model = model
        self._run_id = run_id

    def preflight(self, purpose: GeminiPurpose, prompt: str, now: datetime) -> None:
        """Raise before any HTTP call if this attempt would exceed a budget.

        Persisted provider pauses and daily/internal reserve exhaustion remain
        hard blockers. Rolling RPM/TPM pressure is temporary: callers receive
        `GeminiTemporaryCapacity` with the earliest safe retry delay and no
        blocked-budget ledger row is written merely for waiting.
        """
        if purpose not in GEMINI_PURPOSES:
            raise ValueError(f"unknown Gemini purpose: {purpose!r}")
        now = _normalize_utc(now)

        pause = self._store.get_gemini_pause(self._model)
        if pause is not None and pause["paused_until"] is not None:
            paused_until = datetime.fromisoformat(pause["paused_until"])
            if paused_until > now:
                raise GeminiQuotaPaused(
                    f"Gemini {self._model} is paused until "
                    f"{pause['paused_until']} ({pause['reason']})",
                    paused_until=pause["paused_until"],
                    reason=pause["reason"],
                )

        quota = self._quota
        rpd_ceiling = math.floor(quota.rpd * quota.ceiling_ratio)
        rpm_ceiling = math.floor(quota.rpm * quota.ceiling_ratio)
        tpm_ceiling = math.floor(quota.tpm * quota.ceiling_ratio)
        core_reserve = math.floor(rpd_ceiling * quota.core_reserve_ratio)
        non_core_daily_limit = rpd_ceiling - core_reserve
        daily_limit = rpd_ceiling if purpose == _CORE_PURPOSE else non_core_daily_limit

        day_start, day_end = _pacific_day_bounds(now)
        day_rows = self._provider_rows(day_start, day_end)
        if len(day_rows) + 1 > daily_limit:
            self._record_blocked(purpose, prompt, now)
            raise GeminiBudgetExceeded(
                f"Gemini {self._model} daily budget exceeded for purpose {purpose!r}"
            )

        proposed_input_tokens = estimate_input_tokens(prompt)
        if proposed_input_tokens > tpm_ceiling:
            self._record_blocked(purpose, prompt, now)
            raise GeminiBudgetExceeded(
                f"Gemini {self._model} prompt exceeds internal TPM ceiling for purpose {purpose!r}"
            )

        minute_start = now - _ROLLING_WINDOW
        minute_rows = self._provider_rows(minute_start, now)
        rolling_requests = len(minute_rows)
        rolling_input_tokens = sum(_row_input_tokens(row) for row in minute_rows)

        if (
            rolling_requests + 1 > rpm_ceiling
            or rolling_input_tokens + proposed_input_tokens > tpm_ceiling
        ):
            retry_after = _retry_after_for_rolling_capacity(
                minute_rows,
                now=now,
                rpm_ceiling=rpm_ceiling,
                tpm_ceiling=tpm_ceiling,
                proposed_input_tokens=proposed_input_tokens,
            )
            raise GeminiTemporaryCapacity(
                f"Gemini {self._model} rolling capacity temporarily full for purpose {purpose!r}",
                retry_after_seconds=retry_after,
            )

    def record_success(
        self,
        purpose: GeminiPurpose,
        prompt: str,
        now: datetime,
        *,
        prompt_tokens: int | None = None,
        output_tokens: int | None = None,
        thinking_tokens: int | None = None,
        cached_tokens: int | None = None,
        total_tokens: int | None = None,
    ) -> None:
        """Log a successful attempt with exact `usageMetadata` where available."""
        now = _normalize_utc(now)
        self._store.record_gemini_usage(
            occurred_at=now.isoformat(),
            run_id=self._run_id,
            model=self._model,
            purpose=purpose,
            status="success",
            estimated_input_tokens=estimate_input_tokens(prompt),
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
            thinking_tokens=thinking_tokens,
            cached_tokens=cached_tokens,
            total_tokens=total_tokens,
        )

    def record_error(
        self,
        purpose: GeminiPurpose,
        prompt: str,
        now: datetime,
        *,
        http_status: int | None = None,
        error_code: str | None = None,
    ) -> None:
        """Log an attempt that reached Google but failed for a non-429 reason."""
        now = _normalize_utc(now)
        self._store.record_gemini_usage(
            occurred_at=now.isoformat(),
            run_id=self._run_id,
            model=self._model,
            purpose=purpose,
            status="error",
            estimated_input_tokens=estimate_input_tokens(prompt),
            http_status=http_status,
            error_code=error_code,
        )

    def record_429(
        self,
        purpose: GeminiPurpose,
        prompt: str,
        now: datetime,
        *,
        kind: GeminiPauseKind,
        error_code: str | None = None,
    ) -> tuple[str, str]:
        """Log a 429 and trip the persisted pause matching what Google reported.

        `kind` is the caller's classification of the 429 body: `daily_quota`
        pauses until the next Pacific-day reset, `rate_limit` pauses for
        `rate_pause_seconds`, and `unknown` pauses the same conservative
        `rate_pause_seconds` rather than assuming the shorter or longer case.

        Returns the exact `(paused_until_iso, reason)` pair just persisted, so
        a caller can raise `GeminiQuotaPaused` directly from these values.
        Do not re-derive the pause by calling `preflight` again afterward: that
        re-runs the daily budget check, which counts the `quota_429` row this
        method just wrote and can trip `GeminiBudgetExceeded` instead — the
        wrong exception type for a call that indisputably reached Google.
        """
        now = _normalize_utc(now)
        if kind == "daily_quota":
            _, paused_until = _pacific_day_bounds(now)
        else:
            paused_until = now + timedelta(seconds=self._quota.rate_pause_seconds)

        paused_until_iso = paused_until.isoformat()
        self._store.set_gemini_pause(self._model, paused_until_iso, kind)
        self._store.record_gemini_usage(
            occurred_at=now.isoformat(),
            run_id=self._run_id,
            model=self._model,
            purpose=purpose,
            status="quota_429",
            estimated_input_tokens=estimate_input_tokens(prompt),
            http_status=429,
            error_code=error_code,
        )
        return paused_until_iso, kind

    def snapshot(
        self, now: datetime, run_id: str | None = None
    ) -> GeminiUsageSummary:
        """Return today's (Pacific) usage against provider limits."""
        now = _normalize_utc(now)
        quota = self._quota
        day_start, day_end = _pacific_day_bounds(now)
        rows = self._store.gemini_usage_rows(
            day_start.isoformat(), day_end.isoformat(), model=self._model, run_id=run_id
        )
        provider_rows = [row for row in rows if row["status"] != "blocked_budget"]

        requests_today = len(provider_rows)
        peak_requests, peak_tokens = _peak_rolling(provider_rows, _ROLLING_WINDOW)

        purpose_counts: dict[str, int] = {}
        input_tokens = output_tokens = thinking_tokens = cached_tokens = total_tokens = 0
        for row in provider_rows:
            purpose_counts[row["purpose"]] = purpose_counts.get(row["purpose"], 0) + 1
            input_tokens += _row_input_tokens(row)
            output_tokens += row["output_tokens"] or 0
            thinking_tokens += row["thinking_tokens"] or 0
            cached_tokens += row["cached_tokens"] or 0
            total_tokens += _row_total_tokens(row)

        rpd_ceiling = math.floor(quota.rpd * quota.ceiling_ratio)
        core_reserve = math.floor(rpd_ceiling * quota.core_reserve_ratio)
        non_core_daily_limit = rpd_ceiling - core_reserve

        pause = self._store.get_gemini_pause(self._model)
        provider_paused = (
            pause is not None
            and pause["paused_until"] is not None
            and datetime.fromisoformat(pause["paused_until"]) > now
        )

        return GeminiUsageSummary(
            requests_today=requests_today,
            rpd_percent=requests_today / quota.rpd * 100,
            rpm_peak_percent=peak_requests / quota.rpm * 100,
            tpm_peak_percent=peak_tokens / quota.tpm * 100,
            input_tokens_today=input_tokens,
            output_tokens_today=output_tokens,
            thinking_tokens_today=thinking_tokens,
            cached_tokens_today=cached_tokens,
            total_tokens_today=total_tokens,
            purpose_counts=purpose_counts,
            internal_budget_exhausted=requests_today >= non_core_daily_limit,
            provider_paused=provider_paused,
        )

    def _record_blocked(self, purpose: GeminiPurpose, prompt: str, now: datetime) -> None:
        self._store.record_gemini_usage(
            occurred_at=now.isoformat(),
            run_id=self._run_id,
            model=self._model,
            purpose=purpose,
            status="blocked_budget",
            estimated_input_tokens=estimate_input_tokens(prompt),
        )

    def _provider_rows(self, start: datetime, end: datetime) -> list[sqlite3.Row]:
        rows = self._store.gemini_usage_rows(
            start.isoformat(), end.isoformat(), model=self._model
        )
        return [row for row in rows if row["status"] != "blocked_budget"]
