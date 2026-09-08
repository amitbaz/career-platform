"""The AI ledger is per user, and one user's spend never governs another's.

`public.job_hunter_ai_usage` is keyed by `user_id` with own-rows RLS, and the
tracker reads its budget back through that policy. This proves the two halves
line up: a second user's calls are invisible to the first user's quota, and a
pause tripped for one user does not pause the other.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from job_hunter.ai.usage import AIQuotaPaused, AIUsageTracker
from job_hunter.models import AIQuotaSettings

MODEL = "gemini-test-per-user"
PROVIDER = "gemini"
NOW = datetime(2026, 9, 1, 20, 0, 0, tzinfo=timezone.utc)  # 13:00 PDT


def _tracker(store) -> AIUsageTracker:
    return AIUsageTracker(
        store, AIQuotaSettings(rpm=10, tpm=1000, rpd=100), MODEL, provider=PROVIDER
    )


@pytest.fixture
def other_store(other_supabase_client):
    from job_hunter.postgres_store import PostgresJobStore

    return PostgresJobStore(other_supabase_client)


def test_one_users_usage_does_not_count_against_anothers_budget(store, other_store):
    tracker = _tracker(store)
    other_tracker = _tracker(other_store)

    # Distinct instants: the ledger's natural key is
    # (user_id, run_id, model, purpose, occurred_at), so identical rows at the
    # same instant would collapse into one upsert.
    for offset in range(3):
        tracker.record_success(
            "job_evaluation", "prompt", NOW - timedelta(seconds=offset)
        )

    assert tracker.snapshot(NOW).requests_today == 3
    assert other_tracker.snapshot(NOW).requests_today == 0


def test_one_users_provider_pause_does_not_pause_another_user(store, other_store):
    tracker = _tracker(store)
    other_tracker = _tracker(other_store)

    tracker.record_429("job_evaluation", "prompt", NOW, kind="daily_quota")

    with pytest.raises(AIQuotaPaused):
        tracker.preflight("job_evaluation", "prompt", NOW)

    # The other user is unaffected: no exception, and nothing paused.
    other_tracker.preflight("job_evaluation", "prompt", NOW)
    assert other_tracker.snapshot(NOW).provider_paused is False
