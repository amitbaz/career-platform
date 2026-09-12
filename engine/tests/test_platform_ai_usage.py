"""The platform key's ledger is global, and never the user's (#128).

Shared objective extraction is funded by one platform-owned key, so what that
key has spent is one number for the whole deployment rather than one per user.
Two properties follow, and both are proved here against the real schema
because both are enforced by it:

* the two ledgers do not see each other, so neither allowance can be spent
  down by the other's calls, and each is reportable on its own;
* every runner reads the same platform ledger, whichever user it acts for --
  the per-user ledger's own guarantee, deliberately inverted.

Each test uses a model id of its own, because these rows carry no user_id and
so are not cleaned up between runs the way `job_hunter_ai_usage` rows are.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from engine.ai.usage import AIQuotaPaused, AIUsageTracker, PlatformUsageLedger
from engine.models import AIQuotaSettings

PROVIDER = "gemini"
NOW = datetime(2026, 9, 1, 20, 0, 0, tzinfo=timezone.utc)  # 13:00 PDT


@pytest.fixture
def model() -> str:
    return f"gemini-platform-{uuid.uuid4()}"


def _user_tracker(store, model: str) -> AIUsageTracker:
    return AIUsageTracker(
        store, AIQuotaSettings(rpm=10, tpm=1000, rpd=100), model, provider=PROVIDER
    )


def _platform_tracker(store, model: str) -> AIUsageTracker:
    return AIUsageTracker(
        PlatformUsageLedger(store),
        AIQuotaSettings(rpm=10, tpm=1000, rpd=100, core_reserve_ratio=0.0),
        model,
        provider=PROVIDER,
    )


def test_platform_spend_is_not_counted_against_the_users_budget(store, model):
    user = _user_tracker(store, model)
    platform = _platform_tracker(store, model)

    for offset in range(3):
        platform.record_success("job_facets", "prompt", NOW - timedelta(seconds=offset))

    assert platform.snapshot(NOW).requests_today == 3
    assert user.snapshot(NOW).requests_today == 0


def test_user_spend_is_not_counted_against_the_platform_allowance(store, model):
    user = _user_tracker(store, model)
    platform = _platform_tracker(store, model)

    for offset in range(2):
        user.record_success("job_evaluation", "prompt", NOW - timedelta(seconds=offset))

    assert user.snapshot(NOW).requests_today == 2
    assert platform.snapshot(NOW).requests_today == 0


def test_both_ledgers_are_reportable_side_by_side(store, model):
    user = _user_tracker(store, model)
    platform = _platform_tracker(store, model)

    user.record_success("job_evaluation", "prompt", NOW)
    platform.record_success("job_facets", "prompt", NOW)

    assert user.snapshot(NOW).purpose_counts == {"job_evaluation": 1}
    assert platform.snapshot(NOW).purpose_counts == {"job_facets": 1}


def test_an_exhausted_platform_key_does_not_pause_the_users_own(store, model):
    """The failure this separation exists to prevent.

    A shared 429 tripped by extraction would, on one shared pause row, stop the
    user's scoring for the rest of the day -- charging them, in delivered
    digests, for the platform running out.
    """
    user = _user_tracker(store, model)
    platform = _platform_tracker(store, model)

    platform.record_429("job_facets", "prompt", NOW, kind="daily_quota")

    with pytest.raises(AIQuotaPaused):
        platform.preflight("job_facets", "prompt", NOW)

    user.preflight("job_evaluation", "prompt", NOW)
    assert user.snapshot(NOW).provider_paused is False


def test_every_runner_reads_the_same_platform_ledger(store, other_store, model):
    """Not per-user, on purpose: one key, one allowance, one day's total.

    `test_ai_usage_per_user` asserts the exact opposite for the per-user
    ledger. Both are correct, which is why they are two tables.
    """
    mine = _platform_tracker(store, model)
    theirs = _platform_tracker(other_store, model)

    mine.record_success("job_facets", "prompt", NOW)

    assert theirs.snapshot(NOW).requests_today == 1

    theirs.record_429("job_facets", "prompt", NOW + timedelta(seconds=1), kind="daily_quota")
    with pytest.raises(AIQuotaPaused):
        mine.preflight("job_facets", "prompt", NOW + timedelta(seconds=2))
