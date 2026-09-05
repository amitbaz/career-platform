import dataclasses
import json
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import job_hunter.pipeline
from job_hunter.gemini_usage import (
    GeminiBudgetExceeded,
    GeminiQuotaPaused,
    GeminiTemporaryCapacity,
)
from job_hunter.gmail_models import ExtractedJob
from job_hunter.models import (
    CandidateContext,
    CandidatePreferences,
    CompanyWatchSeed,
    DigestItem,
    Evaluation,
    GeminiQuotaSettings,
    GeminiUsageSummary,
    Job,
    Material,
    RunSummary,
    SearchPolicy,
    Settings,
)
from job_hunter.pipeline import run_pipeline, should_run_scheduled
from job_hunter.sources import GmailStagedSource, LearnedAtsSource
from job_hunter.sources.company_watch import CompanyWatchSource
from job_hunter.store import JobStore
from job_hunter.telegram import build_digest, build_gemini_pause_warning, select_deliverable_items
from job_hunter.watchlist import promote_company as persist_promoted_company
from tests.market_fixtures import make_market_policy


class FakeGemini:
    def __init__(self, *, preference_payload=None, evaluation_payload=None):
        self.model = "gemini-test"
        self.preference_calls = 0
        self.eval_calls = 0
        self.cover_letter_calls = 0
        self.preference_payload = preference_payload
        self.evaluation_payload = evaluation_payload

    def generate_text(
        self,
        prompt,
        *,
        purpose=None,
        thinking_level=None,
        max_output_tokens=None,
        json_mode=False,
        json_schema=None,
        max_attempts=1,
    ):
        if purpose == "candidate_context":
            self.preference_calls += 1
            payload = self.preference_payload or {
                "preferences": {
                    "preferred_roles": ["Senior Product Engineer"],
                    "preferred_seniority": ["senior"],
                    "must_have_signals": ["React"],
                    "nice_to_have_signals": ["TypeScript"],
                    "preferred_locations": ["Germany"],
                    "avoid_signals": ["manager"],
                    "summary": "Remote product-oriented frontend engineer.",
                },
                "technical_skills": [],
                "architecture_evidence": [],
                "leadership_ownership": [],
                "agentic_ai_evidence": [],
                "product_domain_evidence": [],
                "location_language_facts": [],
                "career_direction": [],
                "company_environment": [],
                "career_evidence": [],
                "evaluation_summary": "Remote product-oriented frontend engineer.",
            }
            return json.dumps(payload)
        if json_mode:
            self.eval_calls += 1
            payload = self.evaluation_payload or {
                "scores": {
                    "role_seniority": 28,
                    "technical": 22,
                    "product_architecture": 18,
                    "career_direction": 8,
                    "location_language": 9,
                    "company_environment": 5,
                },
                "total_score": 90,
                "hard_blockers": [],
                "strengths": ["React expertise"],
                "gaps": [],
                "salary_note": "Not disclosed",
                "location_note": "Remote EU friendly",
                "decision": "high_priority",
                "rationale": "Strong fit",
                "requirements": {
                    "must_have": [
                        {"requirement": "React", "depth": "experience", "candidate_support": "supported"}
                    ],
                    "preferred": [],
                },
            }
            return json.dumps(payload)
        self.cover_letter_calls += 1
        return "Dear Hiring Team,\n\nI would love to join Acme as Senior Product Engineer.\n\nBest,\nAmit"


class FakeTelegram:
    def __init__(self):
        self.messages = []
        self.documents = []

    def send_message(self, text):
        self.messages.append(text)
        return "msg-1"

    def send_document(self, path, caption):
        self.documents.append((path, caption))
        return "doc-1"


class FlakyTelegram:
    """Simulates transient Telegram failures: the Nth calls fail, then succeed."""

    def __init__(self, fail_message_times=0, fail_document_times=0):
        self.messages = []
        self.documents = []
        self._fail_message_times = fail_message_times
        self._fail_document_times = fail_document_times

    def send_message(self, text):
        if self._fail_message_times > 0:
            self._fail_message_times -= 1
            return None
        self.messages.append(text)
        return f"msg-{len(self.messages)}"

    def send_document(self, path, caption):
        if self._fail_document_times > 0:
            self._fail_document_times -= 1
            return None
        self.documents.append((path, caption))
        return f"doc-{len(self.documents)}"


class FailOnSecondMessageTelegram:
    def __init__(self):
        self.attempts = []
        self.messages = []

    def send_message(self, text):
        self.attempts.append(text)
        if len(self.attempts) == 2:
            return None
        self.messages.append(text)
        return f"msg-{len(self.messages)}"

    def send_document(self, path, caption):
        raise AssertionError("review-only fixture must not send documents")


class OrderedFakeTelegram(FakeTelegram):
    def __init__(self):
        super().__init__()
        self.calls = []

    def send_message(self, text):
        self.calls.append(("message", text))
        return super().send_message(text)

    def send_document(self, path, caption):
        self.calls.append(("document", caption))
        return super().send_document(path, caption)


class OrderedNavigatorTelegram:
    """Records every send in order, including the interactive navigator card."""

    def __init__(self):
        self.events = []
        self.messages = []
        self.documents = []

    def send_message(self, text):
        self.events.append(("message", text))
        self.messages.append(text)
        return f"msg-{len(self.messages)}"

    def send_document(self, path, caption):
        self.events.append(("document", caption))
        self.documents.append((path, caption))
        return f"doc-{len(self.documents)}"

    def send_job_card(self, text, keyboard):
        self.events.append(("card", text))
        return "card-1"


class FakeUsageTracker:
    """Stands in for `GeminiUsageTracker`: returns a fixed summary, once per call."""

    def __init__(self, summary):
        self.summary = summary
        self.snapshot_calls = 0

    def snapshot(self, now, run_id=None):
        self.snapshot_calls += 1
        return self.summary


def _usage_summary(**overrides):
    defaults = dict(
        requests_today=21,
        rpd_percent=34.0,
        rpm_peak_percent=20.0,
        tpm_peak_percent=17.0,
        # cached_tokens_today is a subset of input_tokens_today, so
        # total_tokens_today is input+output+thinking (142k), not +cached too.
        input_tokens_today=102_000,
        output_tokens_today=30_000,
        thinking_tokens_today=10_000,
        cached_tokens_today=2_000,
        total_tokens_today=142_000,
        purpose_counts={"job_evaluation": 21},
        internal_budget_exhausted=False,
        provider_paused=False,
    )
    defaults.update(overrides)
    return GeminiUsageSummary(**defaults)


class FakeSource:
    def __init__(self, jobs):
        self._jobs = jobs

    def discover(self):
        return self._jobs


class BrokenSource:
    def discover(self):
        raise RuntimeError("source is down")


def _job(**overrides):
    defaults = dict(
        source="ashby",
        source_job_id="job-1",
        title="Senior Product Engineer",
        company="Acme",
        location="Remote",
        remote=True,
        description="React TypeScript remote role",
        content_confidence="official_ats",
    )
    defaults.update(overrides)
    return Job(**defaults)


def _candidate_context(**overrides):
    defaults = dict(
        preferences=CandidatePreferences(
            preferred_roles=["Senior Product Engineer"],
            preferred_seniority=["senior"],
            must_have_signals=["React"],
            nice_to_have_signals=["TypeScript"],
            preferred_locations=["Germany"],
            avoid_signals=["manager"],
            summary="Remote product-oriented frontend engineer.",
        ),
        technical_skills=[],
        architecture_evidence=[],
        leadership_ownership=[],
        agentic_ai_evidence=[],
        product_domain_evidence=[],
        location_language_facts=[],
        career_direction=[],
        company_environment=[],
        career_evidence=[],
        evaluation_summary="Remote product-oriented frontend engineer.",
    )
    defaults.update(overrides)
    return CandidateContext(**defaults)


class RaisingGemini(FakeGemini):
    """Raises a Gemini quota exception for one purpose, after `allow` successful

    calls for that purpose; otherwise behaves exactly like FakeGemini.
    """

    def __init__(self, *, raise_on_purpose, exception, allow=0, **kwargs):
        super().__init__(**kwargs)
        self._raise_on_purpose = raise_on_purpose
        self._exception = exception
        self._allow = allow
        self._purpose_calls = 0

    def generate_text(self, prompt, **kwargs):
        if kwargs.get("purpose") == self._raise_on_purpose:
            if self._purpose_calls >= self._allow:
                raise self._exception
            self._purpose_calls += 1
        return super().generate_text(prompt, **kwargs)


def _budget_exceeded():
    return GeminiBudgetExceeded("Gemini gemini-test budget exceeded for purpose 'job_evaluation'")


def _quota_paused():
    return GeminiQuotaPaused(
        "Gemini gemini-test is paused until 2026-09-03T00:00:00+00:00 (daily_quota)",
        paused_until="2026-09-03T00:00:00+00:00",
        reason="daily_quota",
    )


def _evaluation_payload(scores, decision):
    return {
        "scores": scores,
        "total_score": sum(scores.values()),
        "hard_blockers": [],
        "strengths": ["React expertise"],
        "gaps": [],
        "salary_note": "Not disclosed",
        "location_note": "Remote EU friendly",
        "decision": decision,
        "rationale": "Strong fit",
        "requirements": {
            "must_have": [
                {"requirement": "React", "depth": "experience", "candidate_support": "supported"}
            ],
            "preferred": [],
        },
    }


def _jobs_for_source(source: str, count: int, *, title="Senior Product Engineer", description="React TypeScript remote role"):
    return [
        _job(
            source=source,
            source_job_id=f"{source}-{index}",
            company=f"{source.title()} {index:03d}",
            title=title,
            description=description,
        )
        for index in range(count)
    ]


def _record_review_event(store, *, message_id, occurred_at, company="Acme", role_title="Frontend Engineer"):
    store.record_gmail_message(
        message_id=message_id,
        thread_id=f"thread-{message_id}",
        sender="recruiter@example.com",
        subject=f"Interview update for {company}",
        occurred_at=occurred_at,
        classification="REVIEW_NEEDED",
        confidence=0.4,
        rationale="ambiguous scheduling language",
    )
    return store.save_application_event(
        job_id=None,
        event_type="REVIEW_NEEDED",
        occurred_at=occurred_at,
        source_message_id=message_id,
        source_thread_id=f"thread-{message_id}",
        confidence=0.4,
        company=company,
        role_title=role_title,
        rationale="ambiguous scheduling language",
    )


@pytest.fixture
def policy():
    return SearchPolicy(
        target_titles=["senior product engineer"],
        positive_keywords=["react"],
        blocked_title_keywords=["junior"],
        salary_floor_eur=90000,
        thresholds={"package": 75, "possible": 65},
        max_jobs_per_run=100,
    )


@pytest.fixture
def settings(tmp_path, policy):
    return Settings(
        gemini_api_key="key",
        candidate_profile="profile",
        cover_letter_template="template",
        timezone="Europe/Berlin",
        scheduled_hour=9,
        policy=policy,
        gemini_quota=GeminiQuotaSettings(rpm=10, tpm=250000, rpd=500),
        dry_run=False,
        telegram_bot_token="token",
        telegram_chat_id="chat",
        db_path=str(tmp_path / "state.sqlite3"),
    )


def test_pipeline_delivers_strong_match_and_dedupes_within_run(settings):
    strong_job = _job()
    duplicate_job = _job()
    source = FakeSource([strong_job, duplicate_job])
    store = JobStore(settings.db_path)
    gemini = FakeGemini()
    telegram = FakeTelegram()

    summary = run_pipeline(settings, sources=[source], store=store, gemini=gemini, telegram=telegram)

    assert summary.ready_to_apply == 1
    assert len(telegram.documents) == 0
    assert store.count_jobs() == 1
    job_id, _, _ = store.upsert_job(strong_job)
    assert store.has_delivery(job_id)
    assert gemini.eval_calls == 1

    # Second run rediscovers the same, unchanged job: no re-evaluation.
    source2 = FakeSource([strong_job])
    run_pipeline(settings, sources=[source2], store=store, gemini=gemini, telegram=telegram)
    assert gemini.eval_calls == 1


def test_pipeline_promotes_package_match_only_after_evaluation_is_persisted(
    settings, monkeypatch, caplog
):
    store = JobStore(settings.db_path)
    gemini = FakeGemini(
        evaluation_payload=_evaluation_payload(
            {
                "role_seniority": 25,
                "technical": 20,
                "product_architecture": 15,
                "career_direction": 7,
                "location_language": 8,
                "company_environment": 5,
            },
            "package_match",
        )
    )
    promotion_calls = []

    def assert_persisted_then_promote(
        passed_store, *, job_id, job, evaluation, package_threshold
    ):
        assert passed_store.get_evaluation(job_id) is not None
        promotion_calls.append((job_id, package_threshold))
        return persist_promoted_company(
            passed_store,
            job_id=job_id,
            job=job,
            evaluation=evaluation,
            package_threshold=package_threshold,
        )

    monkeypatch.setattr(
        "job_hunter.pipeline.promote_company",
        assert_persisted_then_promote,
        raising=False,
    )

    settings.candidate_profile = "PRIVATE_CV_TEXT"
    job = _job(description="PRIVATE_GMAIL_BODY React TypeScript")
    store.upsert_company_watch(
        company_name="Healthy Watch",
        careers_url="https://healthy.test/careers",
        ats_provider=None,
        ats_identifier=None,
        discovered_from_job_id=None,
        promotion_source="manual",
        confidence=1.0,
    )
    failing_watch_id = store.upsert_company_watch(
        company_name="Failing Watch",
        careers_url="https://failing.test/careers",
        ats_provider=None,
        ats_identifier=None,
        discovered_from_job_id=None,
        promotion_source="manual",
        confidence=1.0,
    )
    now = datetime.now(timezone.utc)
    store.record_watch_failure(failing_watch_id, now)
    store.record_watch_failure(failing_watch_id, now)

    class WatchResponse:
        text = "<html></html>"

        def raise_for_status(self):
            return None

    class WatchHttp:
        def get(self, url, **kwargs):
            if url == "https://healthy.test/careers":
                return WatchResponse()
            if url == "https://failing.test/careers":
                raise RuntimeError("watch unavailable")
            raise AssertionError(f"unexpected request for {url}")

    with caplog.at_level(logging.INFO):
        summary = run_pipeline(
            settings,
            sources=[FakeSource([job])],
            store=store,
            gemini=gemini,
            telegram=FakeTelegram(),
            http=WatchHttp(),
        )

    row = store.get_company_watch("Acme")
    assert row is not None
    assert row["promotion_source"] == "automatic"
    assert promotion_calls == [(1, 75)]
    assert summary.ready_to_apply == 1
    assert store.get_company_watch("Healthy Watch")["consecutive_failures"] == 0
    failing_watch = store.get_company_watch("Failing Watch")
    assert failing_watch["consecutive_failures"] == 3
    assert failing_watch["paused_until"] is not None
    assert "companies_promoted=1" in caplog.text
    assert "watch_checks=2" in caplog.text
    assert "watch_paused=1" in caplog.text
    assert "PRIVATE_CV_TEXT" not in caplog.text
    assert "PRIVATE_GMAIL_BODY" not in caplog.text


def test_pipeline_logs_when_match_score_is_capped(settings, caplog):
    job = _job()
    store = JobStore(settings.db_path)
    gemini = FakeGemini(
        evaluation_payload={
            "scores": {
                "role_seniority": 25,
                "technical": 20,
                "product_architecture": 15,
                "career_direction": 7,
                "location_language": 8,
                "company_environment": 5,
            },
            "total_score": 80,
            "hard_blockers": [],
            "strengths": ["React expertise"],
            "gaps": [],
            "salary_note": "Not disclosed",
            "location_note": "Remote EU friendly",
            "decision": "package_match",
            "rationale": "Strong fit but missing a must-have",
            "requirements": {
                "must_have": [
                    {
                        "requirement": "5+ years distributed systems",
                        "depth": "deep_expert",
                        "candidate_support": "unsupported",
                    }
                ],
                "preferred": [],
            },
        }
    )

    with caplog.at_level(logging.INFO):
        run_pipeline(
            settings,
            sources=[FakeSource([job])],
            store=store,
            gemini=gemini,
            telegram=FakeTelegram(),
        )

    assert "capped match score job_id=1 raw=80 effective=64 decision=skip" in caplog.text


def test_pipeline_aggregates_untrusted_gmail_source_labels_in_logs(settings, caplog):
    settings.dry_run = True
    store = JobStore(settings.db_path)
    platform = "MODEL_PLATFORM\nPRIVATE_PLATFORM_SECRET"
    store.stage_inbound_job(
        "message-1",
        "candidate-1",
        ExtractedJob(
            source_platform=platform,
            company="Acme",
            title="Senior Product Engineer",
            location="Remote",
            remote=True,
            description="React TypeScript remote role",
        ),
    )

    with caplog.at_level(logging.INFO):
        summary = run_pipeline(
            settings,
            sources=[],
            store=store,
            gemini=FakeGemini(),
            telegram=FakeTelegram(),
        )

    # The staged job's real description is never fetched in this test's
    # no-network environment, so its content_confidence stays partial_unknown
    # and the deterministic gating caps it at possible_match rather than
    # ready_to_apply (Task 7).
    assert summary.possible_matches == 1
    assert "gmail=1" in caplog.text
    assert platform not in caplog.text
    assert "PRIVATE_PLATFORM_SECRET" not in caplog.text


def test_pipeline_counts_only_meaningful_company_watch_promotions(settings, caplog):
    settings.dry_run = True
    store = JobStore(settings.db_path)
    watch_seeds = (
        ("Repeat", "automatic"),
        ("Manual", "manual"),
        ("Upgrade", "automatic"),
    )
    for company_name, promotion_source in watch_seeds:
        store.upsert_company_watch(
            company_name=company_name,
            careers_url="",
            ats_provider=None,
            ats_identifier=None,
            discovered_from_job_id=None,
            promotion_source=promotion_source,
            confidence=1.0,
        )
    jobs = [
        _job(company="Repeat", source_job_id="repeat"),
        _job(company="Manual", source_job_id="manual"),
        _job(company="New", source_job_id="new"),
        _job(
            company="Upgrade",
            source_job_id="upgrade",
            canonical_url="https://upgrade.test/careers",
        ),
    ]

    with caplog.at_level(logging.INFO):
        summary = run_pipeline(
            settings,
            sources=[FakeSource(jobs)],
            store=store,
            gemini=FakeGemini(),
            telegram=FakeTelegram(),
        )

    assert summary.ready_to_apply == 4
    assert store.get_company_watch("Repeat")["promotion_source"] == "automatic"
    assert store.get_company_watch("Manual")["promotion_source"] == "manual"
    assert store.get_company_watch("Upgrade")["careers_url"] == "https://upgrade.test/careers"
    assert "companies_promoted=2" in caplog.text


def test_pipeline_counts_a_failed_expired_watch_retry_as_a_new_pause(settings, caplog):
    settings.dry_run = True
    store = JobStore(settings.db_path)
    watch_id = store.upsert_company_watch(
        company_name="Retry Watch",
        careers_url="https://retry.test/careers",
        ats_provider=None,
        ats_identifier=None,
        discovered_from_job_id=None,
        promotion_source="manual",
        confidence=1.0,
    )
    past = datetime.now(timezone.utc) - timedelta(days=2)
    for _ in range(3):
        store.record_watch_failure(watch_id, past)
    previous_pause = store.get_company_watch("Retry Watch")["paused_until"]

    class FailingWatchHttp:
        def get(self, url, **kwargs):
            raise RuntimeError("retry unavailable")

    with caplog.at_level(logging.INFO):
        run_pipeline(
            settings,
            sources=[],
            store=store,
            gemini=FakeGemini(),
            telegram=FakeTelegram(),
            http=FailingWatchHttp(),
        )

    watch = store.get_company_watch("Retry Watch")
    assert watch["consecutive_failures"] == 4
    assert watch["paused_until"] != previous_pause
    assert "watch_checks=1" in caplog.text
    assert "watch_paused=1" in caplog.text


def test_pipeline_does_not_promote_possible_match(settings, monkeypatch):
    store = JobStore(settings.db_path)
    gemini = FakeGemini(
        evaluation_payload=_evaluation_payload(
            {
                "role_seniority": 20,
                "technical": 18,
                "product_architecture": 14,
                "career_direction": 6,
                "location_language": 7,
                "company_environment": 5,
            },
            "possible_match",
        )
    )
    promotion_calls = []

    def record_promotion_attempt(
        passed_store, *, job_id, job, evaluation, package_threshold
    ):
        promotion_calls.append((job_id, package_threshold))
        return persist_promoted_company(
            passed_store,
            job_id=job_id,
            job=job,
            evaluation=evaluation,
            package_threshold=package_threshold,
        )

    monkeypatch.setattr(
        "job_hunter.pipeline.promote_company",
        record_promotion_attempt,
        raising=False,
    )

    summary = run_pipeline(
        settings,
        sources=[FakeSource([_job()])],
        store=store,
        gemini=gemini,
        telegram=FakeTelegram(),
    )

    assert store.get_company_watch("Acme") is None
    assert promotion_calls == [(1, 75)]
    assert summary.possible_matches == 1


def test_pipeline_passes_configured_package_threshold_to_promotion(
    settings, monkeypatch
):
    settings.policy.thresholds["package"] = 95
    store = JobStore(settings.db_path)
    promotion_calls = []

    def record_threshold(
        passed_store, *, job_id, job, evaluation, package_threshold
    ):
        promotion_calls.append(package_threshold)
        return persist_promoted_company(
            passed_store,
            job_id=job_id,
            job=job,
            evaluation=evaluation,
            package_threshold=package_threshold,
        )

    monkeypatch.setattr(
        "job_hunter.pipeline.promote_company",
        record_threshold,
        raising=False,
    )

    summary = run_pipeline(
        settings,
        sources=[FakeSource([_job()])],
        store=store,
        gemini=FakeGemini(),
        telegram=FakeTelegram(),
    )

    assert summary.ready_to_apply == 1
    assert promotion_calls == [95]
    assert store.get_company_watch("Acme") is None


def test_pipeline_isolates_company_watch_source_failure(
    settings, monkeypatch
):
    store = JobStore(settings.db_path)
    gemini = FakeGemini()
    attempts = []

    def raise_watch_failure(self):
        attempts.append(self)
        raise RuntimeError("watch down")

    monkeypatch.setattr(CompanyWatchSource, "discover", raise_watch_failure)

    summary = run_pipeline(
        settings,
        sources=[FakeSource([_job()])],
        store=store,
        gemini=gemini,
        telegram=FakeTelegram(),
    )

    assert gemini.eval_calls == 1
    assert len(attempts) == 1
    assert summary.ready_to_apply == 1
    assert summary.errors == 0


def test_pipeline_syncs_structured_manual_watch_seeds(settings):
    settings.policy.manual_company_watch = [
        CompanyWatchSeed(company_name="Manual Co")
    ]
    store = JobStore(settings.db_path)

    run_pipeline(
        settings,
        sources=[],
        store=store,
        gemini=FakeGemini(),
        telegram=FakeTelegram(),
    )

    row = store.get_company_watch("Manual Co")
    assert row is not None
    assert row["promotion_source"] == "manual"


def test_pipeline_injects_resolver_for_direct_ats_canonical_metadata(settings):
    job = _job(
        source="remotive",
        url="https://jobs.lever.co/acme/job-1",
    )
    store = JobStore(settings.db_path)

    run_pipeline(
        settings,
        sources=[FakeSource([job])],
        store=store,
        gemini=FakeGemini(),
        telegram=FakeTelegram(),
        http=ExplodingHttp(),
    )

    persisted = store._conn.execute(
        "SELECT canonical_url, ats_provider, ats_board, ats_job_id "
        "FROM jobs WHERE id = 1"
    ).fetchone()
    assert persisted is not None
    assert persisted["canonical_url"] == "https://jobs.lever.co/acme/job-1"
    assert persisted["ats_provider"] == "lever"
    assert persisted["ats_board"] == "acme"
    assert persisted["ats_job_id"] == "job-1"


def test_pipeline_uses_one_targeted_duckduckgo_query_for_canonical_resolution(
    settings
):
    class Response:
        def __init__(self, *, url, text):
            self.url = url
            self.text = text

        def raise_for_status(self):
            return None

    class TargetedSearchHttp:
        def __init__(self):
            self.calls = []

        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            if url == "https://aggregator.test/jobs/1":
                return Response(url=url, text="<html></html>")
            if url == "https://duckduckgo.com/html/":
                return Response(
                    url=url,
                    text=(
                        '<a class="result__a" '
                        'href="https://jobs.ashbyhq.com/acme/ats-1">'
                        "Senior Product Engineer</a>"
                    ),
                )
            raise AssertionError(f"unexpected GET {url}")

    http = TargetedSearchHttp()
    store = JobStore(settings.db_path)

    run_pipeline(
        settings,
        sources=[
            FakeSource(
                [
                    _job(
                        source="aggregator",
                        url="https://aggregator.test/jobs/1",
                    )
                ]
            )
        ],
        store=store,
        gemini=FakeGemini(),
        telegram=FakeTelegram(),
        http=http,
    )

    persisted = store._conn.execute(
        "SELECT canonical_url FROM jobs WHERE id = 1"
    ).fetchone()
    assert persisted is not None
    assert persisted["canonical_url"] == "https://jobs.ashbyhq.com/acme/ats-1"
    search_calls = [
        kwargs["params"]["q"]
        for url, kwargs in http.calls
        if url == "https://duckduckgo.com/html/"
    ]
    assert len(search_calls) == 1
    assert '"Acme"' in search_calls[0]
    assert '"Senior Product Engineer"' in search_calls[0]
    assert "site:jobs.ashbyhq.com" in search_calls[0]


def test_pipeline_rejects_targeted_ats_result_for_wrong_company(settings):
    class Response:
        def __init__(self, *, url, text):
            self.url = url
            self.text = text

        def raise_for_status(self):
            return None

    class WrongCompanySearchHttp:
        def get(self, url, **kwargs):
            if url == "https://aggregator.test/jobs/1":
                return Response(url=url, text="<html></html>")
            if url == "https://duckduckgo.com/html/":
                return Response(
                    url=url,
                    text=(
                        '<a class="result__a" '
                        'href="https://jobs.ashbyhq.com/wrong-company/123">'
                        "Senior Product Engineer</a>"
                    ),
                )
            raise AssertionError(f"unexpected GET {url}")

    store = JobStore(settings.db_path)

    run_pipeline(
        settings,
        sources=[
            FakeSource(
                [
                    _job(
                        source="aggregator",
                        url="https://aggregator.test/jobs/1",
                    )
                ]
            )
        ],
        store=store,
        gemini=FakeGemini(),
        telegram=FakeTelegram(),
        http=WrongCompanySearchHttp(),
    )

    persisted = store._conn.execute(
        "SELECT url, canonical_url, ats_provider, ats_board, ats_job_id "
        "FROM jobs WHERE id = 1"
    ).fetchone()
    assert persisted is not None
    assert persisted["url"] == "https://aggregator.test/jobs/1"
    assert persisted["canonical_url"] == "https://aggregator.test/jobs/1"
    assert persisted["ats_provider"] is None
    assert persisted["ats_board"] is None
    assert persisted["ats_job_id"] is None


def test_pipeline_counts_promotion_failure_but_continues_delivery(
    settings, monkeypatch
):
    store = JobStore(settings.db_path)
    telegram = FakeTelegram()

    def raise_promotion_failure(*args, **kwargs):
        raise RuntimeError("watch persistence down")

    monkeypatch.setattr(
        "job_hunter.pipeline.promote_company",
        raise_promotion_failure,
        raising=False,
    )

    summary = run_pipeline(
        settings,
        sources=[FakeSource([_job()])],
        store=store,
        gemini=FakeGemini(),
        telegram=telegram,
    )

    assert summary.errors == 1
    assert summary.ready_to_apply == 1
    assert len(telegram.messages) == 1
    assert len(telegram.documents) == 0


def test_pipeline_isolates_broken_source(settings):
    good_job = _job(source_job_id="job-2", company="Beta")
    store = JobStore(settings.db_path)
    gemini = FakeGemini()
    telegram = FakeTelegram()

    summary = run_pipeline(
        settings,
        sources=[BrokenSource(), FakeSource([good_job])],
        store=store,
        gemini=gemini,
        telegram=telegram,
    )

    assert summary.ready_to_apply == 1
    assert store.count_jobs() == 1


def test_pipeline_dry_run_persists_but_does_not_deliver(settings, policy):
    dry_settings = Settings(
        gemini_api_key=settings.gemini_api_key,
        candidate_profile=settings.candidate_profile,
        cover_letter_template=settings.cover_letter_template,
        timezone=settings.timezone,
        scheduled_hour=settings.scheduled_hour,
        policy=policy,
        gemini_quota=settings.gemini_quota,
        dry_run=True,
        db_path=settings.db_path,
    )
    job = _job()
    store = JobStore(dry_settings.db_path)
    gemini = FakeGemini()
    telegram = FakeTelegram()

    summary = run_pipeline(dry_settings, sources=[FakeSource([job])], store=store, gemini=gemini, telegram=telegram)

    assert summary.ready_to_apply == 1
    assert len(telegram.messages) == 0
    assert len(telegram.documents) == 0
    job_id, _, _ = store.upsert_job(job)
    assert store.has_delivery(job_id) is False


def test_pipeline_delivers_all_pending_gmail_reviews_in_one_message(settings):
    store = JobStore(settings.db_path)
    first_event_id = _record_review_event(
        store,
        message_id="review-2",
        occurred_at="2026-08-31T11:00:00+00:00",
        company="Beta",
    )
    second_event_id = _record_review_event(
        store,
        message_id="review-1",
        occurred_at="2026-08-31T10:00:00+00:00",
    )
    telegram = FakeTelegram()

    run_pipeline(settings, sources=[], store=store, gemini=FakeGemini(), telegram=telegram)

    assert telegram.messages == [
        "Gmail activity I couldn't link\n\n"
        "Acme — Frontend Engineer\n"
        "This looks job-related, but I couldn't classify or link it confidently.\n"
        "Open email: https://mail.google.com/mail/u/0/#all/thread-review-1\n\n"
        "Beta — Frontend Engineer\n"
        "This looks job-related, but I couldn't classify or link it confidently.\n"
        "Open email: https://mail.google.com/mail/u/0/#all/thread-review-2"
    ]
    assert store.pending_review_events() == []
    delivered = store._conn.execute(
        "SELECT event_id, telegram_message_id FROM review_deliveries ORDER BY event_id"
    ).fetchall()
    assert [(row["event_id"], row["telegram_message_id"]) for row in delivered] == [
        (first_event_id, "msg-1"),
        (second_event_id, "msg-1"),
    ]


def test_pipeline_retries_gmail_reviews_after_a_failed_telegram_send(settings):
    store = JobStore(settings.db_path)
    event_id = _record_review_event(
        store,
        message_id="review-1",
        occurred_at="2026-08-31T10:00:00+00:00",
    )
    telegram = FlakyTelegram(fail_message_times=1)

    run_pipeline(settings, sources=[], store=store, gemini=FakeGemini(), telegram=telegram)

    assert [row["id"] for row in store.pending_review_events()] == [event_id]
    assert telegram.messages == []

    run_pipeline(settings, sources=[], store=store, gemini=FakeGemini(), telegram=telegram)

    assert store.pending_review_events() == []
    assert telegram.messages == [
        "Gmail activity I couldn't link\n\n"
        "Acme — Frontend Engineer\n"
        "This looks job-related, but I couldn't classify or link it confidently.\n"
        "Open email: https://mail.google.com/mail/u/0/#all/thread-review-1"
    ]


def test_pipeline_marks_each_review_chunk_before_retrying_partial_failure(settings):
    store = JobStore(settings.db_path)
    first_event_id = _record_review_event(
        store,
        message_id="review-1",
        occurred_at="2026-08-31T10:00:00+00:00",
        company="A" * 2000,
    )
    second_event_id = _record_review_event(
        store,
        message_id="review-2",
        occurred_at="2026-08-31T11:00:00+00:00",
        company="B" * 2000,
    )
    telegram = FailOnSecondMessageTelegram()

    run_pipeline(settings, sources=[], store=store, gemini=FakeGemini(), telegram=telegram)

    assert len(telegram.attempts) == 2
    assert all(len(message) <= 3900 for message in telegram.attempts)
    assert [row["id"] for row in store.pending_review_events()] == [second_event_id]
    delivered = store._conn.execute(
        "SELECT event_id FROM review_deliveries ORDER BY event_id"
    ).fetchall()
    assert [row["event_id"] for row in delivered] == [first_event_id]

    run_pipeline(settings, sources=[], store=store, gemini=FakeGemini(), telegram=telegram)

    assert len(telegram.attempts) == 3
    assert "A" * 2000 not in telegram.attempts[2]
    assert "B" * 2000 in telegram.attempts[2]
    assert store.pending_review_events() == []


def test_pipeline_sends_gmail_reviews_after_normal_job_delivery_without_scoring_them(settings):
    store = JobStore(settings.db_path)
    _record_review_event(
        store,
        message_id="review-1",
        occurred_at="2026-08-31T10:00:00+00:00",
        company="Review Co",
        role_title="Review Role",
    )
    telegram = OrderedFakeTelegram()

    run_pipeline(
        settings,
        sources=[FakeSource([_job()])],
        store=store,
        gemini=FakeGemini(),
        telegram=telegram,
    )

    assert [kind for kind, _content in telegram.calls] == ["message", "message"]
    assert telegram.messages[0].startswith("Ready to apply\n- 90 | Acme - Senior Product Engineer")
    assert "Gmail activity I couldn't link" not in telegram.messages[0]
    assert telegram.messages[1] == (
        "Gmail activity I couldn't link\n\n"
        "Review Co — Review Role\n"
        "This looks job-related, but I couldn't classify or link it confidently.\n"
        "Open email: https://mail.google.com/mail/u/0/#all/thread-review-1"
    )


def test_pipeline_evaluates_staged_gmail_job_through_normal_discovery(settings):
    store = JobStore(settings.db_path)
    store.stage_inbound_job(
        "message-1",
        "linkedin:job-1",
        ExtractedJob(
            source_platform="linkedin",
            source_job_id="job-1",
            url="https://linkedin.example/jobs/1",
            company="Acme",
            title="Senior Product Engineer",
            location="Remote",
            remote=True,
            description="React TypeScript remote role",
        ),
    )
    gemini = FakeGemini()
    telegram = FakeTelegram()

    summary = run_pipeline(
        settings,
        sources=[],
        store=store,
        gemini=gemini,
        telegram=telegram,
    )

    # The staged job's real description is never fetched in this test's
    # no-network environment, so its content_confidence stays partial_unknown
    # and the deterministic gating caps it at possible_match rather than
    # ready_to_apply (Task 7).
    assert summary.possible_matches == 1
    assert gemini.eval_calls == 1
    assert store.count_jobs() == 1
    job = store.get_job(1)
    assert job is not None
    assert job.source == "gmail:linkedin"
    assert job.source_job_id == "linkedin:job-1"


def test_pipeline_keeps_richer_public_job_and_filters_staged_gmail_duplicate(settings):
    store = JobStore(settings.db_path)
    store.stage_inbound_job(
        "message-1",
        "linkedin:job-1",
        ExtractedJob(
            source_platform="linkedin",
            source_job_id="job-1",
            url="https://jobs.acme.example/roles/1?utm_source=linkedin",
            company="Acme",
            title="Senior Product Engineer",
        ),
    )
    public_job = _job(
        source="ashby",
        source_job_id="public-1",
        url="https://jobs.acme.example/roles/1",
        description="React TypeScript remote role with complete public details",
    )
    gemini = FakeGemini()
    telegram = FakeTelegram()

    run_pipeline(
        settings,
        sources=[FakeSource([public_job])],
        store=store,
        gemini=gemini,
        telegram=telegram,
    )

    persisted_job = store.get_job(1)
    assert persisted_job is not None
    assert persisted_job.source == "ashby"
    assert GmailStagedSource(store).discover() == []

    run_pipeline(
        settings,
        sources=[],
        store=store,
        gemini=gemini,
        telegram=telegram,
    )

    assert gemini.eval_calls == 1
    assert store.count_jobs() == 1


def test_pipeline_prefilters_non_matching_jobs(settings):
    irrelevant_job = _job(
        source_job_id="job-3",
        title="Junior QA Tester",
        description="manual testing",
    )
    store = JobStore(settings.db_path)
    gemini = FakeGemini()
    telegram = FakeTelegram()

    summary = run_pipeline(settings, sources=[FakeSource([irrelevant_job])], store=store, gemini=gemini, telegram=telegram)

    assert summary.ready_to_apply == 0
    assert summary.skipped == 1
    assert gemini.eval_calls == 0


class ExplodingHttp:
    def get(self, url, **kwargs):
        raise AssertionError(f"unexpected enrichment fetch for {url!r}")

    def post(self, url, **kwargs):
        raise AssertionError(f"unexpected post to {url!r}")


def test_pipeline_does_not_reenrich_job_with_existing_description(settings):
    job = _job(url="https://acme.example/jobs/1")
    store = JobStore(settings.db_path)
    gemini = FakeGemini()
    telegram = FakeTelegram()

    summary = run_pipeline(
        settings,
        sources=[FakeSource([job])],
        store=store,
        gemini=gemini,
        telegram=telegram,
        http=ExplodingHttp(),
    )

    assert summary.ready_to_apply == 1


def test_pipeline_retries_failed_telegram_delivery_on_next_run(settings):
    job = _job()
    store = JobStore(settings.db_path)
    gemini = FakeGemini()
    telegram = FlakyTelegram(fail_message_times=1)

    # Run 1: evaluation succeeds, message Telegram send fails.
    run_pipeline(settings, sources=[FakeSource([job])], store=store, gemini=gemini, telegram=telegram)

    job_id, _, _ = store.upsert_job(job)
    assert store.has_delivery(job_id, "telegram_message") is False
    assert len(telegram.messages) == 0

    # Run 2: same job rediscovered, Telegram now works -> retry succeeds.
    run_pipeline(settings, sources=[FakeSource([job])], store=store, gemini=gemini, telegram=telegram)

    assert len(telegram.messages) == 1
    assert store.has_delivery(job_id, "telegram_message") is True


def test_pipeline_retry_does_not_call_gemini_again(settings):
    job = _job()
    store = JobStore(settings.db_path)
    gemini = FakeGemini()
    telegram = FlakyTelegram(fail_message_times=1)

    run_pipeline(settings, sources=[FakeSource([job])], store=store, gemini=gemini, telegram=telegram)
    assert gemini.eval_calls == 1
    assert gemini.cover_letter_calls == 0

    run_pipeline(settings, sources=[FakeSource([job])], store=store, gemini=gemini, telegram=telegram)

    # Retry must reuse the persisted evaluation, not call Gemini again.
    assert gemini.eval_calls == 1
    assert gemini.cover_letter_calls == 0


def test_pipeline_no_duplicate_sends_after_successful_delivery(settings):
    job = _job()
    store = JobStore(settings.db_path)
    gemini = FakeGemini()
    telegram = FakeTelegram()

    run_pipeline(settings, sources=[FakeSource([job])], store=store, gemini=gemini, telegram=telegram)
    assert len(telegram.messages) == 1
    assert len(telegram.documents) == 0

    # Job rediscovered on a later run after delivery already succeeded.
    run_pipeline(settings, sources=[FakeSource([job])], store=store, gemini=gemini, telegram=telegram)

    assert len(telegram.messages) == 1
    assert len(telegram.documents) == 0
    assert gemini.eval_calls == 1
    assert gemini.cover_letter_calls == 0


def test_pipeline_loads_candidate_context_once_without_logging_profile(settings, monkeypatch, caplog):
    settings.candidate_profile = "SENSITIVE_PROFILE_TEXT"
    job = _job()
    store = JobStore(settings.db_path)
    gemini = FakeGemini()
    telegram = FakeTelegram()
    loaded = []

    def fake_get_context(profile, policy, passed_gemini, passed_store):
        loaded.append((profile, policy, passed_gemini, passed_store))
        return _candidate_context()

    monkeypatch.setattr("job_hunter.pipeline.get_candidate_context", fake_get_context)

    with caplog.at_level(logging.INFO):
        run_pipeline(settings, sources=[FakeSource([job])], store=store, gemini=gemini, telegram=telegram)

    # Exactly one load: no redundant second (candidate_context vs.
    # preferences) call, unlike the pre-Task-8 pipeline.
    assert loaded == [(settings.candidate_profile, settings.policy, gemini, store)]
    assert "profile extraction: source=" in caplog.text
    assert settings.candidate_profile not in caplog.text
    assert job.description not in caplog.text
    assert gemini.eval_calls == 1


def test_pipeline_passes_loaded_preferences_into_discovery(settings, monkeypatch):
    job = _job()
    store = JobStore(settings.db_path)
    gemini = FakeGemini()
    telegram = FakeTelegram()
    captured = {}

    real_collect_candidates = job_hunter.pipeline.collect_candidates

    def capturing_collect_candidates(*args, **kwargs):
        captured["preferences"] = kwargs.get("preferences")
        return real_collect_candidates(*args, **kwargs)

    monkeypatch.setattr(
        "job_hunter.pipeline.collect_candidates", capturing_collect_candidates
    )

    run_pipeline(settings, sources=[FakeSource([job])], store=store, gemini=gemini, telegram=telegram)

    assert captured["preferences"] is not None
    assert captured["preferences"] == _candidate_context().preferences


def test_pipeline_defers_evaluation_when_budget_exceeded(settings):
    job = _job()
    store = JobStore(settings.db_path)
    gemini = RaisingGemini(raise_on_purpose="job_evaluation", exception=_budget_exceeded())
    telegram = FakeTelegram()

    summary = run_pipeline(settings, sources=[FakeSource([job])], store=store, gemini=gemini, telegram=telegram)

    job_id, _, _ = store.upsert_job(job)
    assert store.get_evaluation(job_id) is None
    assert [row["job_id"] for row in store.list_pending_ai_work("job_evaluation")] == [job_id]
    assert summary.errors == 0
    assert summary.skipped == 0
    assert summary.possible_matches == 0
    assert summary.ready_to_apply == 0
    assert telegram.messages == []


def test_pipeline_defers_evaluation_when_quota_paused(settings):
    job = _job()
    store = JobStore(settings.db_path)
    gemini = RaisingGemini(raise_on_purpose="job_evaluation", exception=_quota_paused())
    telegram = FakeTelegram()

    summary = run_pipeline(settings, sources=[FakeSource([job])], store=store, gemini=gemini, telegram=telegram)

    job_id, _, _ = store.upsert_job(job)
    assert store.get_evaluation(job_id) is None
    assert [row["job_id"] for row in store.list_pending_ai_work("job_evaluation")] == [job_id]
    assert summary.errors == 0
    assert summary.skipped == 0

def test_pipeline_waits_and_retries_when_gemini_capacity_is_temporary(
    settings, monkeypatch
):
    job = _job()
    store = JobStore(settings.db_path)
    telegram = FakeTelegram()

    class TemporarilyLimitedGemini(FakeGemini):
        def __init__(self):
            super().__init__()
            self.temporary_limit_raised = False

        def generate_text(self, prompt, **kwargs):
            if (
                kwargs.get("purpose") == "job_evaluation"
                and not self.temporary_limit_raised
            ):
                self.temporary_limit_raised = True
                raise GeminiTemporaryCapacity(
                    "temporary Gemini RPM capacity reached",
                    retry_after_seconds=2.5,
                )

            return super().generate_text(prompt, **kwargs)

    gemini = TemporarilyLimitedGemini()
    sleeps = []

    monkeypatch.setattr(
        job_hunter.pipeline.time,
        "sleep",
        lambda seconds: sleeps.append(seconds),
    )

    summary = run_pipeline(
        settings,
        sources=[FakeSource([job])],
        store=store,
        gemini=gemini,
        telegram=telegram,
    )

    job_id, _, _ = store.upsert_job(job)

    assert sleeps == [2.5]
    assert store.get_evaluation(job_id) is not None
    assert store.list_pending_ai_work("job_evaluation") == []
    assert summary.ready_to_apply == 1
    assert gemini.eval_calls == 1


def test_pipeline_defers_remaining_candidates_after_first_quota_exception(settings):
    jobs = _jobs_for_source("ashby", 3)
    store = JobStore(settings.db_path)
    # Only the first job_evaluation call succeeds; every later one is blocked.
    gemini = RaisingGemini(raise_on_purpose="job_evaluation", exception=_budget_exceeded(), allow=1)
    telegram = FakeTelegram()

    run_pipeline(settings, sources=[FakeSource(jobs)], store=store, gemini=gemini, telegram=telegram)

    job_ids = [store.upsert_job(job)[0] for job in jobs]
    evaluated = [job_id for job_id in job_ids if store.get_evaluation(job_id) is not None]
    pending = {row["job_id"] for row in store.list_pending_ai_work("job_evaluation")}

    assert len(evaluated) == 1
    assert pending == set(job_ids) - set(evaluated)
    # The blocked jobs were deferred directly, without a second wasted Gemini attempt.
    assert gemini.eval_calls == 1


def test_pipeline_retries_pending_evaluation_before_new_candidates(settings):
    store = JobStore(settings.db_path)
    old_job = _job(source_job_id="deferred-job", company="Deferred Co")
    old_job_id, _, _ = store.upsert_job(old_job)
    store.enqueue_ai_work("job_evaluation", old_job_id)

    new_job = _job(source_job_id="fresh-job", company="Fresh Co")
    # Only one job_evaluation call is allowed this run.
    gemini = RaisingGemini(raise_on_purpose="job_evaluation", exception=_budget_exceeded(), allow=1)
    telegram = FakeTelegram()

    run_pipeline(settings, sources=[FakeSource([new_job])], store=store, gemini=gemini, telegram=telegram)

    new_job_id, _, _ = store.upsert_job(new_job)

    # The older, already-pending job wins the single available call...
    assert store.get_evaluation(old_job_id) is not None
    # ...and the fresh candidate is deferred instead of evaluated, taking the
    # older job's place in the pending queue.
    assert store.get_evaluation(new_job_id) is None
    assert [row["job_id"] for row in store.list_pending_ai_work("job_evaluation")] == [new_job_id]
    assert gemini.eval_calls == 1


def test_pipeline_retries_pending_evaluation_and_delivers_it(settings):
    store = JobStore(settings.db_path)
    job = _job()
    job_id, _, _ = store.upsert_job(job)
    store.enqueue_ai_work("job_evaluation", job_id)

    gemini = FakeGemini()
    telegram = FakeTelegram()

    summary = run_pipeline(settings, sources=[FakeSource([])], store=store, gemini=gemini, telegram=telegram)

    assert store.get_evaluation(job_id) is not None
    assert store.list_pending_ai_work("job_evaluation") == []
    assert summary.ready_to_apply == 1
    assert len(telegram.messages) == 1
    assert len(telegram.documents) == 0


def test_pipeline_ignores_stale_pending_evaluation_for_already_delivered_job(settings):
    """A crash between save_evaluation and complete_ai_work can leave an

    already-evaluated-and-delivered job's `job_evaluation` row stuck pending.
    A later run must not re-spend Gemini or re-deliver duplicates for it --
    it should just clear the stale row.
    """
    job = _job()
    store = JobStore(settings.db_path)
    gemini = FakeGemini()
    telegram = FakeTelegram()

    # Run 1: fully evaluate and deliver the job normally.
    run_pipeline(settings, sources=[FakeSource([job])], store=store, gemini=gemini, telegram=telegram)
    job_id, _, _ = store.upsert_job(job)
    assert store.get_evaluation(job_id) is not None
    assert store.has_delivery(job_id, "telegram_message") is True
    assert store.has_delivery(job_id, "telegram_document") is False
    assert len(telegram.messages) == 1
    assert len(telegram.documents) == 0
    assert gemini.eval_calls == 1

    # Simulate the crash window: the queue row survives even though the job
    # was already fully evaluated and delivered.
    store.enqueue_ai_work("job_evaluation", job_id)

    # Run 2: no new candidates, only the stale pending row to process.
    run_pipeline(settings, sources=[FakeSource([])], store=store, gemini=gemini, telegram=telegram)

    assert store.list_pending_ai_work("job_evaluation") == []
    # Zero wasted Gemini evaluation calls...
    assert gemini.eval_calls == 1
    # ...and zero duplicate deliveries.
    assert len(telegram.messages) == 1
    assert len(telegram.documents) == 0


def _evaluation(job_id, *, decision="high_priority", total_score=90):
    return Evaluation(
        job_id=job_id,
        total_score=total_score,
        scores={},
        decision=decision,
        hard_blockers=[],
        strengths=["React expertise"],
        gaps=[],
        salary_note="Not disclosed",
        location_note="Remote EU friendly",
        rationale="Strong fit",
        model="gemini-test",
    )


def test_generate_cover_letter_on_demand_calls_gemini_when_no_material(settings):
    job = _job()
    store = JobStore(settings.db_path)
    job_id, _, _ = store.upsert_job(job)
    store.save_evaluation(job_id, _evaluation(job_id))
    gemini = FakeGemini()
    telegram = FakeTelegram()

    delivered = job_hunter.pipeline.generate_cover_letter_on_demand(
        settings, job_id, store=store, gemini=gemini, telegram=telegram
    )

    assert delivered is True
    assert gemini.cover_letter_calls == 1
    assert len(telegram.documents) == 1
    assert store.get_material(job_id) is not None
    assert store.has_delivery(job_id, "telegram_document")


def test_generate_cover_letter_on_demand_resends_without_regenerating(settings):
    job = _job()
    store = JobStore(settings.db_path)
    job_id, _, _ = store.upsert_job(job)
    store.save_evaluation(job_id, _evaluation(job_id))
    store.save_material(job_id, Material(job_id=job_id, cover_letter_text="Existing letter text"))
    gemini = FakeGemini()
    telegram = FakeTelegram()

    delivered = job_hunter.pipeline.generate_cover_letter_on_demand(
        settings, job_id, store=store, gemini=gemini, telegram=telegram
    )

    assert delivered is True
    assert gemini.cover_letter_calls == 0
    assert len(telegram.documents) == 1


def test_generate_cover_letter_on_demand_missing_job_returns_false(settings):
    store = JobStore(settings.db_path)
    gemini = FakeGemini()
    telegram = FakeTelegram()

    delivered = job_hunter.pipeline.generate_cover_letter_on_demand(
        settings, 999, store=store, gemini=gemini, telegram=telegram
    )

    assert delivered is False
    assert len(telegram.documents) == 0


def test_generate_cover_letter_on_demand_notifies_on_quota_block(settings):
    job = _job()
    store = JobStore(settings.db_path)
    job_id, _, _ = store.upsert_job(job)
    store.save_evaluation(job_id, _evaluation(job_id))
    gemini = RaisingGemini(raise_on_purpose="cover_letter", exception=_budget_exceeded())
    telegram = FakeTelegram()

    delivered = job_hunter.pipeline.generate_cover_letter_on_demand(
        settings, job_id, store=store, gemini=gemini, telegram=telegram
    )

    assert delivered is False
    assert len(telegram.documents) == 0
    assert len(telegram.messages) == 1
    assert "quota" in telegram.messages[0].lower()


def test_pipeline_defers_all_evaluations_when_context_load_is_quota_blocked(settings):
    jobs = _jobs_for_source("ashby", 2)
    store = JobStore(settings.db_path)
    gemini = RaisingGemini(raise_on_purpose="candidate_context", exception=_budget_exceeded())
    telegram = FakeTelegram()

    summary = run_pipeline(settings, sources=[FakeSource(jobs)], store=store, gemini=gemini, telegram=telegram)

    job_ids = [store.upsert_job(job)[0] for job in jobs]
    pending = {row["job_id"] for row in store.list_pending_ai_work("job_evaluation")}

    assert pending == set(job_ids)
    assert all(store.get_evaluation(job_id) is None for job_id in job_ids)
    assert summary.errors == 0
    assert summary.skipped == 0
    # No evaluation was even attempted; ranking degraded gracefully instead.
    assert gemini.eval_calls == 0


def test_pipeline_evaluates_all_eligible_jobs_when_under_budget(settings):
    jobs = _jobs_for_source("ashby", 18)
    store = JobStore(settings.db_path)
    gemini = FakeGemini()
    telegram = FakeTelegram()

    summary = run_pipeline(settings, sources=[FakeSource(jobs)], store=store, gemini=gemini, telegram=telegram)

    assert summary.ready_to_apply == 18
    assert gemini.eval_calls == 18


def test_pipeline_caps_evaluations_at_diverse_shortlist_budget(settings, caplog):
    ashby_jobs = _jobs_for_source("ashby", 160)
    remotive_jobs = _jobs_for_source("remotive", 40)
    store = JobStore(settings.db_path)
    gemini = FakeGemini()
    telegram = FakeTelegram()

    with caplog.at_level(logging.INFO):
        summary = run_pipeline(
            settings,
            sources=[FakeSource(ashby_jobs), FakeSource(remotive_jobs)],
            store=store,
            gemini=gemini,
            telegram=telegram,
        )

    assert summary.ready_to_apply == 100
    assert gemini.eval_calls == 100
    assert "deferred_by_budget=100" in caplog.text
    assert "canonical_network_attempts=" in caplog.text
    assert "eligible sources: ashby=160 remotive=40" in caplog.text
    assert "selected sources: ashby=60 remotive=40" in caplog.text
    assert (
        "evaluation_capacity selected=100 evaluated=100 "
        "deferred_by_budget=100 quota_deferred=0"
    ) in caplog.text

def test_pipeline_logs_profile_fallback_without_private_content(settings, caplog):
    settings.candidate_profile = "PRIVATE_RESUME_TEXT"
    job = _job()
    store = JobStore(settings.db_path)
    gemini = FakeGemini(preference_payload="{not-json")
    telegram = FakeTelegram()

    with caplog.at_level(logging.INFO):
        run_pipeline(settings, sources=[FakeSource([job])], store=store, gemini=gemini, telegram=telegram)

    assert "profile extraction: source=fallback" in caplog.text
    assert "eligible sources: ashby=1" in caplog.text
    assert "selected sources: ashby=1" in caplog.text
    assert settings.candidate_profile not in caplog.text
    assert job.description not in caplog.text


def test_pipeline_logs_per_market_metrics_and_bounds_fresh_gemini_calls(settings, caplog):
    market_policy = make_market_policy()
    market_policy.max_jobs_per_run = 5
    settings.policy = market_policy
    store = JobStore(settings.db_path)
    jobs = [
        _job(
            source_job_id=f"london-{index}",
            company=f"London Co {index}",
            location="London",
            remote=False,
            description="React TypeScript. Visa sponsorship available.",
        )
        for index in range(10)
    ]
    gemini = FakeGemini()
    telegram = FakeTelegram()

    with caplog.at_level(logging.INFO):
        run_pipeline(
            settings,
            sources=[FakeSource(jobs)],
            store=store,
            gemini=gemini,
            telegram=telegram,
        )

    # More eligible jobs than the configured budget: fresh Gemini spend stays
    # bounded by max_jobs_per_run rather than evaluating every eligible job.
    assert gemini.eval_calls == 5
    assert gemini.eval_calls <= market_policy.max_jobs_per_run

    assert (
        "market=london queries_planned=0 queries_attempted=0 queries_succeeded=0 "
        "raw=10 unique=10 rejected=0 eligible=10 selected=5 high_priority=5 "
        "package_match=0 possible_match=0 skip=0 blocked=0 delivered=5"
    ) in caplog.text
    # One line per configured market, even markets with no activity this run.
    assert "market=israel_remote" in caplog.text
    assert "market=singapore" in caplog.text


class RoutingAtsHttp:
    """Fake get_json client for LearnedAtsSource, routing by URL substring."""

    def __init__(self, responses):
        self.responses = responses

    def get_json(self, url, **kwargs):
        for marker, payload in self.responses.items():
            if marker in url:
                return payload
        raise RuntimeError(f"no fake response configured for {url}")


def test_pipeline_logs_source_quality_and_ats_registry_metrics(settings, caplog):
    store = JobStore(settings.db_path)
    store.upsert_ats_board(
        provider="ashby",
        board_identifier="acme-ashby",
        company_name="Acme",
        market_hint="",
    )
    devjobs_job = _job(source="devjobs", source_job_id="1", company="Acme")
    ats_http = RoutingAtsHttp(
        responses={
            "ashbyhq.com": {
                "jobs": [
                    {
                        "id": 1,
                        "title": "Senior Product Engineer",
                        "location": "Remote",
                        "jobUrl": "https://jobs.ashbyhq.com/acme-ashby/1",
                        "descriptionPlain": "React",
                        "isRemote": True,
                    },
                    {
                        "id": 2,
                        "title": "Senior Product Engineer",
                        "location": "Remote",
                        "jobUrl": "https://jobs.ashbyhq.com/acme-ashby/2",
                        "descriptionPlain": "React",
                        "isRemote": True,
                    },
                ]
            }
        }
    )
    learned_source = LearnedAtsSource(
        store,
        ats_http,
        limit=10,
        market_order=[],
        now=lambda: datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc),
    )
    gemini = FakeGemini()
    telegram = FakeTelegram()

    with caplog.at_level(logging.INFO):
        run_pipeline(
            settings,
            sources=[FakeSource([devjobs_job]), learned_source],
            store=store,
            gemini=gemini,
            telegram=telegram,
        )

    assert (
        "source_quality source=devjobs raw=1 unique=1 rejected=0 eligible=1 "
        "selected=1 high_priority=1 package_match=0 possible_match=0 skip=0 "
        "blocked=0 delivered=1"
    ) in caplog.text
    assert (
        "ats_registry total=1 discovered=0 scanned=1 successful=1 failed=0 jobs_raw=2"
    ) in caplog.text


def test_should_run_scheduled_matches_local_hour():
    now = datetime(2026, 8, 30, 7, 0, tzinfo=timezone.utc)  # 09:00 in Europe/Berlin (CEST, UTC+2)
    assert should_run_scheduled(now, "Europe/Berlin", 9) is True


def test_should_run_scheduled_rejects_other_hours():
    now = datetime(2026, 8, 30, 6, 0, tzinfo=timezone.utc)  # 08:00 in Europe/Berlin
    assert should_run_scheduled(now, "Europe/Berlin", 9) is False


def test_targeted_canonical_search_stops_after_shared_breaker_opens():
    """One breaker spans every per-job search, so a dead host is called once."""
    from job_hunter.circuit_breaker import CircuitBreaker
    from job_hunter.pipeline import _targeted_canonical_candidates

    class FailingHttp:
        def __init__(self):
            self.calls = 0

        def get(self, url, **kwargs):
            self.calls += 1
            raise RuntimeError("network down")

    http = FailingHttp()
    breaker = CircuitBreaker(failure_threshold=1)
    jobs = [
        Job(source="aggregator", title=f"Senior Product Engineer {i}", company="Acme")
        for i in range(4)
    ]

    for job in jobs:
        assert _targeted_canonical_candidates(http, job, breaker) == []

    assert http.calls == 1


def test_pipeline_sends_no_message_events_when_navigator_supported(settings):
    job = _job()
    store = JobStore(settings.db_path)
    summary = _usage_summary()
    gemini = FakeGemini()
    gemini._tracker = FakeUsageTracker(summary)
    telegram = OrderedNavigatorTelegram()

    run_pipeline(settings, sources=[FakeSource([job])], store=store, gemini=gemini, telegram=telegram)

    kinds = [kind for kind, _payload in telegram.events]
    # With navigator support, the digest is delivered via the interactive
    # card, not a message -- and no Gemini usage status message is sent.
    assert kinds[-1] == "card"
    assert "message" not in kinds
    assert gemini._tracker.snapshot_calls == 1


def test_pipeline_surfaces_evaluation_location_note_in_navigator_card(settings):
    job = _job()
    store = JobStore(settings.db_path)
    gemini = FakeGemini()
    telegram = OrderedNavigatorTelegram()

    run_pipeline(settings, sources=[FakeSource([job])], store=store, gemini=gemini, telegram=telegram)

    card_events = [payload for kind, payload in telegram.events if kind == "card"]
    assert card_events
    assert "Note: Remote EU friendly" in card_events[-1]


def test_pipeline_sends_gemini_pause_warning_as_last_message(settings):
    job = _job()
    store = JobStore(settings.db_path)
    summary = _usage_summary(provider_paused=True)
    gemini = FakeGemini()
    gemini._tracker = FakeUsageTracker(summary)
    telegram = FakeTelegram()

    run_pipeline(settings, sources=[FakeSource([job])], store=store, gemini=gemini, telegram=telegram)

    expected_warning = build_gemini_pause_warning(summary)
    assert expected_warning is not None
    assert telegram.messages[-1] == expected_warning


def test_pipeline_sends_no_warning_when_usage_is_healthy(settings):
    job = _job()
    store = JobStore(settings.db_path)
    summary = _usage_summary()
    gemini = FakeGemini()
    gemini._tracker = FakeUsageTracker(summary)
    telegram = FakeTelegram()

    run_pipeline(settings, sources=[FakeSource([job])], store=store, gemini=gemini, telegram=telegram)

    assert build_gemini_pause_warning(summary) is None
    # Only the digest message was sent — no warning, no usage status.
    assert len(telegram.messages) == 1


def test_pipeline_sends_exactly_one_warning_despite_many_locally_blocked_calls(settings):
    """Many candidates deferred by quota this run must still yield one warning."""
    jobs = _jobs_for_source("ashby", 5)
    store = JobStore(settings.db_path)
    # Only the first job_evaluation call succeeds; the other four are blocked
    # without a second wasted Gemini attempt (Task 8's short-circuit) — but
    # the run-completion summary still reports the day as budget-exhausted.
    gemini = RaisingGemini(raise_on_purpose="job_evaluation", exception=_budget_exceeded(), allow=1)
    gemini._tracker = FakeUsageTracker(_usage_summary(internal_budget_exhausted=True))
    telegram = FakeTelegram()

    run_pipeline(settings, sources=[FakeSource(jobs)], store=store, gemini=gemini, telegram=telegram)

    job_ids = [store.upsert_job(job)[0] for job in jobs]
    pending = {row["job_id"] for row in store.list_pending_ai_work("job_evaluation")}
    assert len(pending) == 4  # confirms many calls were in fact locally blocked

    expected_warning = build_gemini_pause_warning(_usage_summary(internal_budget_exhausted=True))
    warning_occurrences = [msg for msg in telegram.messages if msg == expected_warning]
    assert len(warning_occurrences) == 1
    assert gemini._tracker.snapshot_calls == 1


def test_pipeline_logs_structured_gemini_usage_line(settings, caplog):
    job = _job()
    store = JobStore(settings.db_path)
    summary = _usage_summary(
        requests_today=21,
        rpd_percent=34.0,
        rpm_peak_percent=20.0,
        tpm_peak_percent=17.0,
        input_tokens_today=111,
        output_tokens_today=22,
        thinking_tokens_today=3,
        purpose_counts={
            "gmail_semantic": 5,
            "job_evaluation": 13,
            "cover_letter": 2,
            "candidate_context": 1,
        },
    )
    gemini = FakeGemini()
    gemini._tracker = FakeUsageTracker(summary)
    telegram = FakeTelegram()

    with caplog.at_level(logging.INFO):
        run_pipeline(settings, sources=[FakeSource([job])], store=store, gemini=gemini, telegram=telegram)

    assert "gemini_usage run_calls=21" in caplog.text
    assert "rpd_pct=34.0" in caplog.text
    assert "rpm_peak_pct=20.0" in caplog.text
    assert "tpm_peak_pct=17.0" in caplog.text
    assert "input=111" in caplog.text
    assert "output=22" in caplog.text
    assert "thinking=3" in caplog.text
    assert "gmail_semantic:5" in caplog.text
    assert "job_evaluation:13" in caplog.text
    assert "cover_letter:2" in caplog.text
    assert "candidate_context:1" in caplog.text


def test_pipeline_log_total_does_not_double_count_cached_tokens(settings, caplog):
    """Regression: cachedContentTokenCount is a subset of promptTokenCount.

    One real Gemini call: promptTokenCount=1000 (400 cached),
    candidatesTokenCount=200, thoughtsTokenCount=50 -> Google's real total is
    1250. The structured log's input+output+thinking (1000+200+50=1250) must
    match `total_tokens_today` exactly -- a formula that also added the
    cached portion would overcount by 32%.
    """
    job = _job()
    store = JobStore(settings.db_path)
    summary = _usage_summary(
        requests_today=1,
        input_tokens_today=1000,
        output_tokens_today=200,
        thinking_tokens_today=50,
        cached_tokens_today=400,
        total_tokens_today=1250,
    )
    gemini = FakeGemini()
    gemini._tracker = FakeUsageTracker(summary)
    telegram = FakeTelegram()

    with caplog.at_level(logging.INFO):
        run_pipeline(settings, sources=[FakeSource([job])], store=store, gemini=gemini, telegram=telegram)

    assert "input=1000" in caplog.text
    assert "output=200" in caplog.text
    assert "thinking=50" in caplog.text
    log_total = 1000 + 200 + 50  # what the structured log's fields sum to
    assert log_total == summary.total_tokens_today == 1250


def test_pipeline_logs_gemini_usage_even_in_dry_run(settings, caplog):
    dry_settings = dataclasses.replace(settings, dry_run=True)
    job = _job()
    store = JobStore(dry_settings.db_path)
    summary = _usage_summary()
    gemini = FakeGemini()
    gemini._tracker = FakeUsageTracker(summary)

    with caplog.at_level(logging.INFO):
        run_pipeline(dry_settings, sources=[FakeSource([job])], store=store, gemini=gemini)

    assert "gemini_usage run_calls=21" in caplog.text
    assert gemini._tracker.snapshot_calls == 1


def test_pipeline_without_gemini_tracker_sends_no_usage_status(settings):
    """Legacy/test gemini fakes without a tracker must not break or send usage."""
    job = _job()
    store = JobStore(settings.db_path)
    gemini = FakeGemini()  # no `_tracker` attribute at all
    telegram = FakeTelegram()

    run_pipeline(settings, sources=[FakeSource([job])], store=store, gemini=gemini, telegram=telegram)

    # Only the digest message was sent -- no usage status, no crash.
    assert len(telegram.messages) == 1


def test_run_pipeline_forwards_store_to_build_sources_when_sources_not_given(
    settings, monkeypatch
):
    import job_hunter.pipeline as pipeline_module

    store = JobStore(settings.db_path)
    gemini = FakeGemini()
    telegram = FakeTelegram()
    captured = {}

    def fake_build_sources(passed_settings, http, *, store=None, search_breaker=None, query_date=None):
        captured["store"] = store
        return []

    monkeypatch.setattr(pipeline_module, "build_sources", fake_build_sources)

    run_pipeline(settings, store=store, gemini=gemini, telegram=telegram)

    assert captured["store"] is store


def test_capped_job_is_excluded_from_delivery():
    capped = DigestItem(
        job_id=1,
        company="Forecast GmbH",
        title="Product Analytics Lead",
        score=64,
        decision="skip",
        url="https://example.test/jobs/1",
        hard_blockers=[],
    )
    plausible = DigestItem(
        job_id=2,
        company="Example GmbH",
        title="Senior Frontend Engineer",
        score=70,
        decision="possible_match",
        url="https://example.test/jobs/2",
        hard_blockers=[],
    )

    deliverable = select_deliverable_items([capped, plausible])

    assert [item.job_id for item in deliverable] == [2]
    assert "Forecast GmbH" not in build_digest([capped, plausible])
