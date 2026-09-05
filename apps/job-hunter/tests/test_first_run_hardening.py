import importlib
from datetime import datetime, timedelta, timezone

import pytest

from job_hunter.candidate_context import get_candidate_context
from job_hunter.gemini_usage import GeminiUsageTracker
from job_hunter.models import GeminiQuotaSettings, SearchPolicy
from job_hunter.store import JobStore


def _policy() -> SearchPolicy:
    return SearchPolicy(
        target_titles=["senior frontend engineer"],
        positive_keywords=["react"],
        blocked_title_keywords=[],
        salary_floor_eur=90000,
        thresholds={"package": 75, "possible": 65},
    )


def test_search_backend_module_supports_fallback_after_primary_failure():
    search_backend = importlib.import_module("job_hunter.search_backend")

    class Primary:
        name = "primary"

        def search(self, query):
            raise RuntimeError("primary unavailable")

    class Secondary:
        name = "secondary"

        def search(self, query):
            return search_backend.SearchResponse(
                hits=[search_backend.SearchHit(title="Senior Frontend Engineer", url="https://example.test/job")],
                backend=self.name,
            )

    backend = search_backend.FallbackSearchBackend(Primary(), Secondary())
    response = backend.search('"senior frontend engineer" London')

    assert response.backend == "secondary"
    assert response.hits[0].url == "https://example.test/job"


class _InvalidContextGemini:
    model = "gemini-test"

    def generate_text(self, *args, **kwargs):
        return "not-json"


def test_candidate_context_fallback_exposes_source_and_sanitized_error(caplog):
    profile = "VERY_PRIVATE_PROFILE_MARKER"
    context = get_candidate_context(profile, _policy(), _InvalidContextGemini(), JobStore(":memory:"))

    assert context.source == "fallback_error"
    assert context.load_error == "ValueError"
    assert "candidate context extraction failed" in caplog.text.lower()
    assert profile not in caplog.text
    assert "not-json" not in caplog.text


def test_rolling_rpm_pressure_is_temporary_capacity_not_daily_exhaustion():
    gemini_usage = importlib.import_module("job_hunter.gemini_usage")
    temporary_capacity = getattr(gemini_usage, "GeminiTemporaryCapacity", None)
    assert temporary_capacity is not None

    store = JobStore(":memory:")
    quota = GeminiQuotaSettings(rpm=15, tpm=250000, rpd=500)
    tracker = GeminiUsageTracker(store, quota, "gemini-test", run_id="run-1")
    now = datetime(2026, 9, 2, 20, 0, tzinfo=timezone.utc)

    for index in range(12):
        store.record_gemini_usage(
            occurred_at=(now - timedelta(seconds=30 - index)).isoformat(),
            run_id="previous",
            model="gemini-test",
            purpose="job_evaluation",
            status="success",
            estimated_input_tokens=100,
            prompt_tokens=100,
            output_tokens=10,
            thinking_tokens=0,
            total_tokens=110,
        )

    with pytest.raises(temporary_capacity) as exc_info:
        tracker.preflight("job_evaluation", "small prompt", now)

    assert 29 <= exc_info.value.retry_after_seconds <= 31
    rows = store.gemini_usage_rows(
        (now - timedelta(minutes=1)).isoformat(),
        (now + timedelta(minutes=1)).isoformat(),
        model="gemini-test",
    )
    assert all(row["status"] != "blocked_budget" for row in rows)
