import logging

from job_hunter.discovery import collect_candidates
from job_hunter.models import (
    CandidateContext,
    CandidatePreferences,
    Evaluation,
    GeminiQuotaSettings,
    Job,
    SearchQuery,
    Settings,
)
from job_hunter.pipeline import run_pipeline
from job_hunter.search_backend import SearchHit, SearchResponse
from job_hunter.sources.targeted_search import TargetedSearchSource
from job_hunter.store import JobStore
from tests.market_fixtures import make_market_policy


class _Backend:
    name = "fake"

    def search(self, query):
        return SearchResponse(
            hits=[SearchHit("Senior Frontend Engineer", "https://example.test/job")],
            backend=self.name,
        )


class _Source:
    def __init__(self, jobs):
        self.jobs = jobs

    def discover(self):
        return self.jobs


class _NoHttp:
    def get(self, *args, **kwargs):
        raise AssertionError("unexpected HTTP request")


class _Gemini:
    model = "gemini-test"
    _tracker = None


class _FailedNavigator:
    def send_job_card(self, text, keyboard):
        return None

    def send_message(self, text):
        return "message"

    def send_document(self, path, caption):
        raise AssertionError("possible match must not generate a PDF")


def _context():
    return CandidateContext(
        preferences=CandidatePreferences(
            preferred_roles=["Senior Frontend Engineer"],
            preferred_seniority=["senior"],
            must_have_signals=["React"],
            nice_to_have_signals=[],
            preferred_locations=["London"],
            avoid_signals=[],
            summary="Frontend engineer.",
        ),
        technical_skills=["React"],
        architecture_evidence=[],
        leadership_ownership=[],
        agentic_ai_evidence=[],
        product_domain_evidence=[],
        location_language_facts=[],
        career_direction=[],
        company_environment=[],
        career_evidence=[],
        evaluation_summary="Frontend engineer.",
        source="cache",
    )


def test_targeted_search_stats_report_attempts_successes_and_results_after_discovery():
    from job_hunter.pipeline import _aggregate_targeted_search_stats

    source = TargetedSearchSource(
        _Backend(),
        [SearchQuery('"senior frontend engineer" London', market_id="london")],
    )
    source.discover()

    planned, attempted, succeeded, results = _aggregate_targeted_search_stats([source])

    assert planned == {"london": 1}
    assert attempted == {"london": 1}
    assert succeeded == {"london": 1}
    assert results == {"london": 1}


def test_discovery_reports_market_reattribution_from_query_hint_to_real_location():
    policy = make_market_policy()
    store = JobStore(":memory:")
    job = Job(
        source="search:fake",
        title="Senior Frontend Engineer",
        company="Acme",
        location="London",
        description="React TypeScript",
        remote=True,
        market_hint="germany_eu",
    )

    result = collect_candidates([_Source([job])], store, _NoHttp(), policy)

    assert result.stats.raw_by_market["germany_eu"] == 1
    assert result.stats.unique_by_market["london"] == 1
    assert result.stats.reattributed_by_market["london"] == 1


def test_market_delivered_metric_counts_only_successful_telegram_delivery(
    tmp_path, monkeypatch, caplog
):
    policy = make_market_policy()
    policy.max_jobs_per_run = 1
    settings = Settings(
        gemini_api_key="key",
        candidate_profile="profile",
        cover_letter_template="template",
        timezone="Europe/Berlin",
        scheduled_hour=9,
        policy=policy,
        gemini_quota=GeminiQuotaSettings(rpm=15, tpm=250000, rpd=500),
        dry_run=False,
        telegram_bot_token="token",
        telegram_chat_id="chat",
        db_path=str(tmp_path / "state.sqlite3"),
    )
    job = Job(
        source="manual",
        source_job_id="london-1",
        title="Senior Frontend Engineer",
        company="Acme",
        location="London",
        description="React TypeScript",
        remote=True,
    )

    monkeypatch.setattr("job_hunter.pipeline.get_candidate_context", lambda *a, **k: _context())
    monkeypatch.setattr(
        "job_hunter.pipeline.evaluate_job",
        lambda current_job, context, current_policy, gemini: Evaluation(
            job_id=0,
            total_score=70,
            scores={},
            decision="possible_match",
            hard_blockers=[],
            strengths=[],
            gaps=[],
            salary_note="Not disclosed",
            location_note="Sponsorship unknown",
            rationale="Possible fit",
            model="gemini-test",
            market_id=current_job.market_id or "",
        ),
    )

    with caplog.at_level(logging.INFO):
        run_pipeline(
            settings,
            sources=[_Source([job])],
            store=JobStore(settings.db_path),
            gemini=_Gemini(),
            telegram=_FailedNavigator(),
            http=_NoHttp(),
        )

    london_lines = [record.message for record in caplog.records if "market=london " in record.message]
    assert len(london_lines) == 1
    assert "selected=1" in london_lines[0]
    assert "delivered=0" in london_lines[0]
