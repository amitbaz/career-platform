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
        read_timeout=None,
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


class _NetworkFreeHttp:
    """An `HttpClient` stand-in that answers every request with "nothing found".

    Used only to keep `test_run_pipeline_builds_brave_backed_source_when_configured`
    from making real requests to Ashby/Greenhouse/DuckDuckGo/etc. That test's
    assertion is about what `build_sources` constructs -- a `TargetedSearchSource`
    -- not about what any of those sources actually discover, and
    `discovery.collect_candidates` already treats a source that raises during
    `discover()` as "no jobs from that source" (`except Exception: ... continue`),
    so failing every real call here is equivalent to those sources finding
    nothing, without depending on live external services or the real job data
    that made `test_run_pipeline_builds_brave_backed_source_when_configured`
    hit a `job_hunter_upsert_job` conflict when this test exercised the
    Postgres store instead of the SQLite one it used to.
    """

    def get_json(self, url, **kwargs):
        raise RuntimeError("network disabled in this test")

    def get(self, url, **kwargs):
        raise RuntimeError("network disabled in this test")

    def post(self, url, **kwargs):
        raise RuntimeError("network disabled in this test")


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


def _decision_for(store, company: str) -> str | None:
    """The decision the run stored for `company`'s job, by company name.

    The pipeline assigns job ids itself, so a test that wants to assert a
    tier has to find the row back through the one field it controls.
    """
    for row in store.list_jobs_for_matching():
        if row["company"] == company:
            evaluation = store.get_evaluation(row["id"])
            return evaluation.decision if evaluation else None
    return None


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


def _digest_offer_lines(digest_text: str) -> list[str]:
    """The job lines of a Telegram digest, ignoring its section headers."""
    return [line for line in digest_text.splitlines() if line.startswith("- ")]


def _digest_offer_count(digest_text: str) -> int:
    return len(_digest_offer_lines(digest_text))


def _digest_companies(digest_text: str) -> set[str]:
    # `- {score} | {company} - {title} | {url}`, per telegram._digest_line.
    return {line.split(" | ")[1].split(" - ")[0] for line in _digest_offer_lines(digest_text)}


class AlternatingDecisionGemini(FakeGemini):
    """Scores every other job below the `possible` threshold, so it is skipped.

    Lets a test tell a cap on delivered offers apart from a cap on evaluations:
    the two counts diverge only when some evaluations produce no offer.
    """

    _STRONG = {
        "role_seniority": 28,
        "technical": 22,
        "product_architecture": 18,
        "career_direction": 8,
        "location_language": 9,
        "company_environment": 5,
    }
    _WEAK = {
        "role_seniority": 10,
        "technical": 8,
        "product_architecture": 6,
        "career_direction": 2,
        "location_language": 2,
        "company_environment": 2,
    }

    def generate_text(self, prompt, **kwargs):
        if kwargs.get("purpose") == "candidate_context" or not kwargs.get("json_mode"):
            return super().generate_text(prompt, **kwargs)
        self.eval_calls += 1
        scores = self._STRONG if self.eval_calls % 2 == 1 else self._WEAK
        return json.dumps(_evaluation_payload(scores, "high_priority"))


class ScoreSequenceGemini(FakeGemini):
    """Returns known component scores for successive job evaluations."""

    def __init__(self, scores_by_evaluation):
        super().__init__()
        self._scores_by_evaluation = scores_by_evaluation

    def generate_text(self, prompt, **kwargs):
        if kwargs.get("purpose") == "candidate_context" or not kwargs.get("json_mode"):
            return super().generate_text(prompt, **kwargs)
        scores = self._scores_by_evaluation[self.eval_calls]
        self.eval_calls += 1
        return json.dumps(_evaluation_payload(scores, "high_priority"))


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
def settings(policy):
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
    )


def test_canonical_search_sites_keeps_original_filter_order():
    # _CANONICAL_SEARCH_SITES is generated from SUPPORTED_ATS_HOSTS and goes
    # verbatim into a public search query, where reordering OR terms can move
    # result ranking. Pin the three filters the hand-written string used, in
    # their original order, so a table reshuffle cannot silently change which
    # candidate a targeted canonical search returns.
    sites = job_hunter.pipeline._CANONICAL_SEARCH_SITES
    assert sites.startswith(
        "site:jobs.ashbyhq.com OR site:jobs.lever.co OR site:boards.greenhouse.io"
    )
    assert sites.endswith(" OR careers")


def test_pipeline_delivers_strong_match_and_dedupes_within_run(store, settings):
    strong_job = _job()
    duplicate_job = _job()
    source = FakeSource([strong_job, duplicate_job])
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
    store,
    settings, monkeypatch, caplog
):
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
    assert len(promotion_calls) == 1
    promoted_job_id, promoted_threshold = promotion_calls[0]
    assert promoted_threshold == 75
    assert promoted_job_id == store.upsert_job(job)[0]
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


def test_pipeline_logs_how_many_discovered_jobs_are_new(store, settings, caplog):
    with caplog.at_level(logging.INFO):
        run_pipeline(
            settings,
            sources=[FakeSource([_job()])],
            store=store,
            gemini=FakeGemini(),
            telegram=FakeTelegram(),
        )

    assert "raw=1 unique=1 newly_discovered=1" in caplog.text

    caplog.clear()
    with caplog.at_level(logging.INFO):
        run_pipeline(
            settings,
            sources=[FakeSource([_job()])],
            store=store,
            gemini=FakeGemini(),
            telegram=FakeTelegram(),
        )

    # A run that discovers nothing new reports zero rather than omitting it.
    assert "raw=1 unique=1 newly_discovered=0" in caplog.text


def test_pipeline_logs_when_match_score_is_capped(store, settings, caplog):
    job = _job()
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

    job_id, _, _ = store.upsert_job(job)
    assert (
        f"capped match score job_id={job_id} raw=80 effective=64 decision=skip"
        in caplog.text
    )


def test_pipeline_aggregates_untrusted_gmail_source_labels_in_logs(store, settings, caplog):
    settings.dry_run = True
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


def test_pipeline_counts_only_meaningful_company_watch_promotions(store, settings, caplog):
    settings.dry_run = True
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


def test_pipeline_counts_a_failed_expired_watch_retry_as_a_new_pause(store, settings, caplog):
    settings.dry_run = True
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


def test_pipeline_does_not_promote_possible_match(store, settings, monkeypatch):
    settings.policy.match_score_floor = 70
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
    assert len(promotion_calls) == 1
    promoted_job_id, promoted_threshold = promotion_calls[0]
    assert promoted_threshold == 75
    assert promoted_job_id == store.upsert_job(_job())[0]
    assert summary.possible_matches == 1


def test_pipeline_passes_configured_package_threshold_to_promotion(
    store,
    settings, monkeypatch
):
    settings.policy.thresholds["package"] = 95
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
    store,
    settings, monkeypatch
):
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


def test_pipeline_syncs_structured_manual_watch_seeds(store, settings):
    settings.policy.manual_company_watch = [
        CompanyWatchSeed(company_name="Manual Co")
    ]

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


def test_pipeline_injects_resolver_for_direct_ats_canonical_metadata(store, settings):
    job = _job(
        source="remotive",
        url="https://jobs.lever.co/acme/job-1",
    )

    run_pipeline(
        settings,
        sources=[FakeSource([job])],
        store=store,
        gemini=FakeGemini(),
        telegram=FakeTelegram(),
        http=ExplodingHttp(),
    )

    persisted = store.client.select(
        "job_hunter_jobs",
        params={"select": "canonical_url,ats_provider,ats_board,ats_job_id"},
    )[0]
    assert persisted is not None
    assert persisted["canonical_url"] == "https://jobs.lever.co/acme/job-1"
    assert persisted["ats_provider"] == "lever"
    assert persisted["ats_board"] == "acme"
    assert persisted["ats_job_id"] == "job-1"


def test_pipeline_uses_one_targeted_duckduckgo_query_for_canonical_resolution(
    store,
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

    rows = store.client.select(
        "job_hunter_jobs", params={"select": "canonical_url"}
    )
    assert len(rows) == 1
    persisted = rows[0]
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


def test_pipeline_rejects_targeted_ats_result_for_wrong_company(store, settings):
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

    persisted = store.client.select(
        "job_hunter_jobs",
        params={"select": "url,canonical_url,ats_provider,ats_board,ats_job_id"},
    )[0]
    assert persisted is not None
    assert persisted["url"] == "https://aggregator.test/jobs/1"
    assert persisted["canonical_url"] == "https://aggregator.test/jobs/1"
    assert persisted["ats_provider"] is None
    assert persisted["ats_board"] is None
    assert persisted["ats_job_id"] is None


def test_pipeline_counts_promotion_failure_but_continues_delivery(
    store,
    settings, monkeypatch
):
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
    # Promotion failure happens after a decision is already reached, so it
    # must not look like a core-evaluation failure.
    assert summary.evaluation_attempted == 1
    assert summary.evaluated == 1


def test_pipeline_marks_evaluation_attempted_but_not_evaluated_on_failure(
    store,
    settings, monkeypatch
):

    def raise_evaluation_failure(*args, **kwargs):
        raise RuntimeError("gemini evaluation exploded")

    monkeypatch.setattr(
        "job_hunter.pipeline.evaluate_job",
        raise_evaluation_failure,
        raising=False,
    )

    summary = run_pipeline(
        settings,
        sources=[FakeSource([_job()])],
        store=store,
        gemini=FakeGemini(),
        telegram=FakeTelegram(),
    )

    assert summary.errors == 1
    assert summary.evaluation_attempted == 1
    assert summary.evaluated == 0


def test_pipeline_isolates_broken_source(store, settings):
    good_job = _job(source_job_id="job-2", company="Beta")
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


def test_pipeline_dry_run_persists_but_does_not_deliver(store, settings, policy):
    dry_settings = Settings(
        gemini_api_key=settings.gemini_api_key,
        candidate_profile=settings.candidate_profile,
        cover_letter_template=settings.cover_letter_template,
        timezone=settings.timezone,
        scheduled_hour=settings.scheduled_hour,
        policy=policy,
        gemini_quota=settings.gemini_quota,
        dry_run=True,
    )
    job = _job()
    gemini = FakeGemini()
    telegram = FakeTelegram()

    summary = run_pipeline(dry_settings, sources=[FakeSource([job])], store=store, gemini=gemini, telegram=telegram)

    assert summary.ready_to_apply == 1
    assert len(telegram.messages) == 0
    assert len(telegram.documents) == 0
    job_id, _, _ = store.upsert_job(job)
    assert store.has_delivery(job_id) is False


def test_pipeline_delivers_all_pending_gmail_reviews_in_one_message(store, settings):
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
    delivered = store.client.select(
        "job_hunter_review_deliveries",
        params={"select": "event_id,telegram_message_id"},
    )
    assert {row["event_id"]: row["telegram_message_id"] for row in delivered} == {
        first_event_id: "msg-1",
        second_event_id: "msg-1",
    }


def test_pipeline_retries_gmail_reviews_after_a_failed_telegram_send(store, settings):
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


def test_pipeline_marks_each_review_chunk_before_retrying_partial_failure(store, settings):
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
    delivered = store.client.select(
        "job_hunter_review_deliveries", params={"select": "event_id"}
    )
    assert {row["event_id"] for row in delivered} == {first_event_id}

    run_pipeline(settings, sources=[], store=store, gemini=FakeGemini(), telegram=telegram)

    assert len(telegram.attempts) == 3
    assert "A" * 2000 not in telegram.attempts[2]
    assert "B" * 2000 in telegram.attempts[2]
    assert store.pending_review_events() == []


def test_pipeline_sends_gmail_reviews_after_normal_job_delivery_without_scoring_them(store, settings):
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


def test_pipeline_evaluates_staged_gmail_job_through_normal_discovery(store, settings):
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
    rows = store.client.select("job_hunter_jobs", params={"select": "id"})
    assert len(rows) == 1
    only_job_id = rows[0]["id"]
    job = store.get_job(only_job_id)
    assert job is not None
    assert job.source == "gmail:linkedin"
    assert job.source_job_id == "linkedin:job-1"


def test_pipeline_keeps_richer_public_job_and_filters_staged_gmail_duplicate(store, settings):
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

    rows = store.client.select("job_hunter_jobs", params={"select": "id"})
    assert len(rows) == 1
    only_job_id = rows[0]["id"]
    persisted_job = store.get_job(only_job_id)
    assert persisted_job is not None
    assert persisted_job.source == "ashby"
    assert list(GmailStagedSource(store).discover()) == []

    run_pipeline(
        settings,
        sources=[],
        store=store,
        gemini=gemini,
        telegram=telegram,
    )

    assert gemini.eval_calls == 1
    assert store.count_jobs() == 1


def test_pipeline_prefilters_non_matching_jobs(store, settings):
    irrelevant_job = _job(
        source_job_id="job-3",
        title="Junior QA Tester",
        description="manual testing",
    )
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


def test_pipeline_does_not_reenrich_job_with_existing_description(store, settings):
    job = _job(url="https://acme.example/jobs/1")
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


def test_pipeline_retries_failed_telegram_delivery_on_next_run(store, settings):
    job = _job()
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


def test_pipeline_retry_does_not_call_gemini_again(store, settings):
    job = _job()
    gemini = FakeGemini()
    telegram = FlakyTelegram(fail_message_times=1)

    run_pipeline(settings, sources=[FakeSource([job])], store=store, gemini=gemini, telegram=telegram)
    assert gemini.eval_calls == 1
    assert gemini.cover_letter_calls == 0

    run_pipeline(settings, sources=[FakeSource([job])], store=store, gemini=gemini, telegram=telegram)

    # Retry must reuse the persisted evaluation, not call Gemini again.
    assert gemini.eval_calls == 1
    assert gemini.cover_letter_calls == 0


def test_pipeline_no_duplicate_sends_after_successful_delivery(store, settings):
    job = _job()
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


def test_pipeline_loads_candidate_context_once_without_logging_profile(store, settings, monkeypatch, caplog):
    settings.candidate_profile = "SENSITIVE_PROFILE_TEXT"
    job = _job()
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


def test_pipeline_passes_loaded_preferences_into_discovery(store, settings, monkeypatch):
    job = _job()
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


def test_pipeline_defers_evaluation_when_budget_exceeded(store, settings):
    job = _job()
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


def test_pipeline_defers_evaluation_when_quota_paused(store, settings):
    job = _job()
    gemini = RaisingGemini(raise_on_purpose="job_evaluation", exception=_quota_paused())
    telegram = FakeTelegram()

    summary = run_pipeline(settings, sources=[FakeSource([job])], store=store, gemini=gemini, telegram=telegram)

    job_id, _, _ = store.upsert_job(job)
    assert store.get_evaluation(job_id) is None
    assert [row["job_id"] for row in store.list_pending_ai_work("job_evaluation")] == [job_id]
    assert summary.errors == 0
    assert summary.skipped == 0

def test_pipeline_waits_and_retries_when_gemini_capacity_is_temporary(
    store,
    settings, monkeypatch
):
    job = _job()
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


def test_pipeline_defers_remaining_candidates_after_first_quota_exception(store, settings):
    jobs = _jobs_for_source("ashby", 3)
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


def test_pipeline_retries_pending_evaluation_before_new_candidates(store, settings):
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


def test_pipeline_delivered_card_warns_when_availability_check_fails(store, settings):
    class TimingOutHttp:
        def get(self, url, **kwargs):
            raise RuntimeError("timeout")

    job = _job(description="", url="https://example.test/jobs/1")
    gemini = FakeGemini()
    telegram = OrderedNavigatorTelegram()

    run_pipeline(
        settings,
        sources=[FakeSource([job])],
        store=store,
        gemini=gemini,
        telegram=telegram,
        http=TimingOutHttp(),
    )

    cards = [text for kind, text in telegram.events if kind == "card"]
    assert len(cards) == 1
    assert "⚠️ Availability not verified - check the posting before applying" in cards[0]


def test_pipeline_delivered_card_has_no_warning_for_a_normal_posting(store, settings):
    job = _job()
    gemini = FakeGemini()
    telegram = OrderedNavigatorTelegram()

    run_pipeline(settings, sources=[FakeSource([job])], store=store, gemini=gemini, telegram=telegram)

    cards = [text for kind, text in telegram.events if kind == "card"]
    assert len(cards) == 1
    assert "⚠️" not in cards[0]


def test_pipeline_retries_pending_evaluation_and_delivers_it(store, settings):
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


def test_pipeline_ignores_stale_pending_evaluation_for_already_delivered_job(store, settings):
    """A crash between save_evaluation and complete_ai_work can leave an

    already-evaluated-and-delivered job's `job_evaluation` row stuck pending.
    A later run must not re-spend Gemini or re-deliver duplicates for it --
    it should just clear the stale row.
    """
    job = _job()
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


def _record_application_history(store, job_id):
    """Give the intended merge survivor history stronger than a card delivery."""
    store.save_application_event(
        job_id=job_id,
        event_type="INTERVIEW",
        occurred_at="2026-09-08T10:00:00+00:00",
        source_message_id=f"application-{job_id}",
        source_thread_id=None,
        confidence=1.0,
        company="Acme Survivor",
        role_title="Senior Product Engineer",
        rationale="interview invitation",
    )


def test_generate_cover_letter_on_demand_calls_gemini_when_no_material(store, settings):
    job = _job()
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


def test_generate_cover_letter_on_demand_resends_without_regenerating(store, settings):
    job = _job()
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


def test_generate_cover_letter_on_demand_missing_job_returns_false(store, settings):
    gemini = FakeGemini()
    telegram = FakeTelegram()

    delivered = job_hunter.pipeline.generate_cover_letter_on_demand(
        settings,
        "00000000-0000-0000-0000-000000000999",
        store=store,
        gemini=gemini,
        telegram=telegram,
    )

    assert delivered is False
    assert len(telegram.documents) == 0


def test_generate_cover_letter_on_demand_follows_a_job_merged_since_delivery(
    store, settings
):
    """A card outlives the job it names; the button must still produce a letter.

    Cards sit in Telegram for days and every discovery run merges duplicates
    away, so the id a card carries can name a row that no longer exists by the
    time it is tapped (#146). The redirect written by the merge says which job
    replaced it, and the letter belongs to that one.
    """
    duplicate_id, _, _ = store.upsert_job(
        _job(source_job_id="duplicate", company="Acme Duplicate")
    )
    survivor_id, _, _ = store.upsert_job(
        _job(source_job_id="survivor", company="Acme Survivor")
    )
    store.save_evaluation(survivor_id, _evaluation(survivor_id))
    _record_application_history(store, survivor_id)
    store.mark_delivered(duplicate_id, "telegram_message", "card-message")
    assert store.merge_jobs(survivor_id, duplicate_id) == survivor_id
    gemini = FakeGemini()
    telegram = FakeTelegram()

    delivered = job_hunter.pipeline.generate_cover_letter_on_demand(
        settings, duplicate_id, store=store, gemini=gemini, telegram=telegram
    )

    assert delivered is True
    assert gemini.cover_letter_calls == 1
    assert len(telegram.documents) == 1
    # The letter and its delivery belong to the surviving job, not the dead id.
    assert store.get_material(survivor_id) is not None
    assert store.has_delivery(survivor_id, "telegram_message")
    assert store.has_delivery(survivor_id, "telegram_document")


def test_generate_cover_letter_on_demand_resends_a_merged_job_letter_for_free(
    store, settings
):
    """The free-resend path still applies once the merge has been followed."""
    duplicate_id, _, _ = store.upsert_job(
        _job(source_job_id="duplicate", company="Acme Duplicate")
    )
    survivor_id, _, _ = store.upsert_job(
        _job(source_job_id="survivor", company="Acme Survivor")
    )
    store.save_evaluation(survivor_id, _evaluation(survivor_id))
    store.save_material(
        survivor_id,
        Material(job_id=survivor_id, cover_letter_text="Existing letter text"),
    )
    _record_application_history(store, survivor_id)
    store.mark_delivered(duplicate_id, "telegram_message", "card-message")
    assert store.merge_jobs(survivor_id, duplicate_id) == survivor_id
    gemini = FakeGemini()
    telegram = FakeTelegram()

    delivered = job_hunter.pipeline.generate_cover_letter_on_demand(
        settings, duplicate_id, store=store, gemini=gemini, telegram=telegram
    )

    assert delivered is True
    assert (gemini.preference_calls, gemini.eval_calls, gemini.cover_letter_calls) == (
        0,
        0,
        0,
    )
    assert len(telegram.documents) == 1


def test_generate_cover_letter_on_demand_tells_the_user_when_the_job_is_gone(store, settings):
    """An id that is neither live nor merged gets a reply, not silence.

    A button that does nothing is indistinguishable from a broken bot, so the
    dead-id branch says so the way the quota and failure branches already do.
    """
    gemini = FakeGemini()
    telegram = FakeTelegram()

    delivered = job_hunter.pipeline.generate_cover_letter_on_demand(
        settings,
        "00000000-0000-0000-0000-000000000999",
        store=store,
        gemini=gemini,
        telegram=telegram,
    )

    assert delivered is False
    assert (gemini.preference_calls, gemini.eval_calls, gemini.cover_letter_calls) == (
        0,
        0,
        0,
    )
    assert telegram.documents == []
    assert len(telegram.messages) == 1
    assert "no longer available" in telegram.messages[0].lower()


def test_generate_cover_letter_on_demand_notifies_on_quota_block(store, settings):
    job = _job()
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


def test_pipeline_defers_all_evaluations_when_context_load_is_quota_blocked(store, settings):
    jobs = _jobs_for_source("ashby", 2)
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


def test_pipeline_evaluates_all_eligible_jobs_when_under_budget(store, settings):
    # About the shortlist budget, not the daily offer limit: raise the offer
    # limit above the pool so the run is bounded only by max_jobs_per_run.
    settings.policy.daily_offer_limit = 20
    jobs = _jobs_for_source("ashby", 18)
    gemini = FakeGemini()
    telegram = FakeTelegram()

    summary = run_pipeline(settings, sources=[FakeSource(jobs)], store=store, gemini=gemini, telegram=telegram)

    assert summary.ready_to_apply == 18
    assert gemini.eval_calls == 18


def test_pipeline_caps_evaluations_at_diverse_shortlist_budget(store, settings, caplog):
    # As above: the assertion is about max_jobs_per_run, so the offer limit is
    # lifted out of the way rather than left at its product default of 10.
    settings.policy.daily_offer_limit = 200
    ashby_jobs = _jobs_for_source("ashby", 160)
    remotive_jobs = _jobs_for_source("remotive", 40)
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


@pytest.mark.parametrize("limit", [5, 10, 20])
def test_pipeline_delivers_at_most_the_daily_offer_limit(store, settings, limit):
    settings.policy.daily_offer_limit = limit
    jobs = _jobs_for_source("ashby", limit + 8)
    gemini = FakeGemini()
    telegram = FakeTelegram()

    summary = run_pipeline(
        settings, sources=[FakeSource(jobs)], store=store, gemini=gemini, telegram=telegram
    )

    assert summary.ready_to_apply == limit
    assert _digest_offer_count(telegram.messages[0]) == limit
    # The cap is the run's budget, not a filter at the end: evaluation stops
    # once the user has their offers, so the Gemini spend falls with it.
    assert gemini.eval_calls == limit


def test_pipeline_delivers_the_highest_ranked_offers_when_the_cap_bites(store, settings):
    settings.policy.daily_offer_limit = 5
    # Both tiers pass the prefilter; the weak tier lacks the nice-to-have
    # signal, so profile-aware ranking puts it below the strong tier.
    strong = [
        _job(
            source_job_id=f"strong-{index}",
            company=f"Strong {index:02d}",
            description="React TypeScript remote role",
        )
        for index in range(5)
    ]
    weak = [
        _job(
            source_job_id=f"weak-{index}",
            company=f"Weak {index:02d}",
            description="React remote role",
        )
        for index in range(10)
    ]
    gemini = FakeGemini()
    telegram = FakeTelegram()

    run_pipeline(
        settings,
        sources=[FakeSource(strong + weak)],
        store=store,
        gemini=gemini,
        telegram=telegram,
    )

    assert _digest_companies(telegram.messages[0]) == {job.company for job in strong}


def test_pipeline_delivers_what_it_found_when_the_pool_is_smaller_than_the_cap(store, settings):
    settings.policy.daily_offer_limit = 10
    jobs = _jobs_for_source("ashby", 3)
    gemini = FakeGemini()
    telegram = FakeTelegram()

    summary = run_pipeline(
        settings, sources=[FakeSource(jobs)], store=store, gemini=gemini, telegram=telegram
    )

    assert summary.ready_to_apply == 3
    assert summary.errors == 0
    assert _digest_offer_count(telegram.messages[0]) == 3


def test_pipeline_spends_the_cap_on_offers_rather_than_evaluations(store, settings):
    """A run that keeps rejecting candidates keeps going until it has the cap.

    The cap counts what reaches the user, so a `skip` costs an evaluation but
    no delivery budget -- otherwise a day of poor candidates would deliver
    almost nothing while still reporting the cap as met.
    """
    settings.policy.daily_offer_limit = 5
    jobs = _jobs_for_source("ashby", 20)
    gemini = AlternatingDecisionGemini()
    telegram = FakeTelegram()

    summary = run_pipeline(
        settings, sources=[FakeSource(jobs)], store=store, gemini=gemini, telegram=telegram
    )

    # Every other candidate scores about 30 -- below the `possible` rung,
    # so a decision-ladder skip rather than a job the floor took away.
    # Reaching five offers costs nine evaluations rather than five.
    assert summary.ready_to_apply == 5
    assert summary.skipped == 4
    assert summary.withheld_by_score_floor == 0
    assert gemini.eval_calls == 9


def test_pipeline_does_not_spend_the_cap_on_offers_below_the_match_score_floor(store, settings):
    """An offer scoring under the profile floor costs no delivery budget.

    A profile may put its `possible` rung below the match-score floor. Counting
    a withheld possible_match would end the run early and leave the digest
    short of the cap.
    """
    settings.policy.thresholds = {"package": 75, "possible": 30}
    settings.policy.match_score_floor = 61
    settings.policy.daily_offer_limit = 5
    jobs = _jobs_for_source("ashby", 20)
    gemini = AlternatingDecisionGemini()
    telegram = FakeTelegram()

    run_pipeline(
        settings, sources=[FakeSource(jobs)], store=store, gemini=gemini, telegram=telegram
    )

    assert _digest_offer_count(telegram.messages[0]) == 5


def test_pipeline_withholds_offers_below_the_match_score_floor(store, settings):
    settings.policy.match_score_floor = 80
    settings.policy.daily_offer_limit = 20
    jobs = _jobs_for_source("ashby", 3)
    gemini = ScoreSequenceGemini(
        [
            {
                "role_seniority": 28,
                "technical": 22,
                "product_architecture": 18,
                "career_direction": 8,
                "location_language": 5,
                "company_environment": 0,
            },
            {
                "role_seniority": 28,
                "technical": 22,
                "product_architecture": 18,
                "career_direction": 8,
                "location_language": 3,
                "company_environment": 0,
            },
            {
                "role_seniority": 28,
                "technical": 22,
                "product_architecture": 18,
                "career_direction": 2,
                "location_language": 0,
                "company_environment": 0,
            },
        ]
    )
    telegram = FakeTelegram()

    summary = run_pipeline(
        settings, sources=[FakeSource(jobs)], store=store, gemini=gemini, telegram=telegram
    )

    assert _digest_companies(telegram.messages[0]) == {jobs[0].company}
    assert summary.ready_to_apply == 1
    assert summary.possible_matches == 0
    assert summary.withheld_by_score_floor == 2
    # The spec's named case: the third job scores 70, which clears the
    # `possible` rung of 65 and so would have been delivered as a possible
    # match before the floor existed. Asserting the tier, not just the
    # count, keeps the case alive if a threshold moves.
    assert _decision_for(store, jobs[2].company) == "possible_match"
    # A withheld offer is not a decision-ladder skip. Nothing here scored
    # below `possible`, so `skipped` must stay empty.
    assert summary.skipped == 0


def test_pipeline_sends_nothing_when_no_offer_clears_the_match_score_floor(store, settings):
    settings.policy.match_score_floor = 80
    jobs = _jobs_for_source("ashby", 1)
    gemini = ScoreSequenceGemini(
        [
            {
                "role_seniority": 28,
                "technical": 22,
                "product_architecture": 18,
                "career_direction": 8,
                "location_language": 3,
                "company_environment": 0,
            }
        ]
    )
    telegram = FakeTelegram()

    summary = run_pipeline(
        settings, sources=[FakeSource(jobs)], store=store, gemini=gemini, telegram=telegram
    )

    assert telegram.messages == []
    assert summary.withheld_by_score_floor == 1


def test_pipeline_delivers_an_offer_at_a_lower_configured_match_score_floor(store, settings):
    settings.policy.thresholds["possible"] = 50
    settings.policy.match_score_floor = 50
    jobs = _jobs_for_source("ashby", 1)
    gemini = ScoreSequenceGemini(
        [
            {
                "role_seniority": 28,
                "technical": 22,
                "product_architecture": 0,
                "career_direction": 0,
                "location_language": 0,
                "company_environment": 0,
            }
        ]
    )
    telegram = FakeTelegram()

    summary = run_pipeline(
        settings, sources=[FakeSource(jobs)], store=store, gemini=gemini, telegram=telegram
    )

    assert _digest_companies(telegram.messages[0]) == {jobs[0].company}
    assert summary.possible_matches == 1
    assert summary.withheld_by_score_floor == 0


def test_pipeline_fills_the_daily_cap_after_withholding_lower_scored_jobs(store, settings):
    settings.policy.match_score_floor = 80
    settings.policy.daily_offer_limit = 5
    jobs = _jobs_for_source("ashby", 8)
    below_floor = {
        "role_seniority": 28,
        "technical": 22,
        "product_architecture": 18,
        "career_direction": 8,
        "location_language": 3,
        "company_environment": 0,
    }
    above_floor = {
        "role_seniority": 28,
        "technical": 22,
        "product_architecture": 18,
        "career_direction": 8,
        "location_language": 9,
        "company_environment": 5,
    }
    gemini = ScoreSequenceGemini([below_floor] * 3 + [above_floor] * 5)
    telegram = FakeTelegram()

    summary = run_pipeline(
        settings, sources=[FakeSource(jobs)], store=store, gemini=gemini, telegram=telegram
    )

    assert _digest_offer_count(telegram.messages[0]) == 5
    assert summary.ready_to_apply == 5
    assert summary.withheld_by_score_floor == 3
    assert gemini.eval_calls == 8


def test_pipeline_does_not_retry_delivery_below_the_current_match_score_floor(store, settings):
    settings.policy.match_score_floor = 80
    job = _job()
    job_id, _, _ = store.upsert_job(job)
    store.save_evaluation(
        job_id,
        _evaluation(job_id, decision="package_match", total_score=79),
    )
    telegram = FakeTelegram()

    run_pipeline(
        settings,
        sources=[FakeSource([])],
        store=store,
        gemini=FakeGemini(),
        telegram=telegram,
    )

    assert telegram.messages == []


def test_pipeline_logs_the_match_score_floor_and_withheld_count(store, settings, caplog):
    settings.policy.match_score_floor = 80
    jobs = _jobs_for_source("ashby", 1)
    gemini = ScoreSequenceGemini(
        [
            {
                "role_seniority": 28,
                "technical": 22,
                "product_architecture": 18,
                "career_direction": 8,
                "location_language": 3,
                "company_environment": 0,
            }
        ]
    )

    with caplog.at_level(logging.INFO):
        run_pipeline(
            settings,
            sources=[FakeSource(jobs)],
            store=store,
            gemini=gemini,
            telegram=FakeTelegram(),
        )

    assert "match_score_floor=80 withheld_by_score_floor=1" in caplog.text


def test_pipeline_leaves_candidates_beyond_the_cap_for_the_next_run(store, settings):
    settings.policy.daily_offer_limit = 5
    jobs = _jobs_for_source("ashby", 12)
    gemini = FakeGemini()
    telegram = FakeTelegram()

    run_pipeline(
        settings, sources=[FakeSource(jobs)], store=store, gemini=gemini, telegram=telegram
    )

    first_run_companies = _digest_companies(telegram.messages[0])
    # Cap-deferred candidates are not queued work: they are simply unevaluated
    # and get ranked again tomorrow, like anything else discovery finds.
    assert store.list_pending_ai_work("job_evaluation") == []

    summary = run_pipeline(
        settings, sources=[FakeSource(jobs)], store=store, gemini=gemini, telegram=telegram
    )

    second_run_companies = _digest_companies(telegram.messages[1])
    assert summary.ready_to_apply == 5
    assert gemini.eval_calls == 10
    assert not (first_run_companies & second_run_companies)


def test_pipeline_logs_the_offer_cap_and_what_it_deferred(store, settings, caplog):
    settings.policy.daily_offer_limit = 5
    jobs = _jobs_for_source("ashby", 9)
    gemini = FakeGemini()
    telegram = FakeTelegram()

    with caplog.at_level(logging.INFO):
        run_pipeline(
            settings, sources=[FakeSource(jobs)], store=store, gemini=gemini, telegram=telegram
        )

    # Deferred by the cap is kept apart from deferred by the ranking budget:
    # they answer different questions about a short digest.
    assert (
        "evaluation_capacity selected=9 evaluated=5 deferred_by_budget=0 "
        "quota_deferred=0 daily_offer_limit=5 delivered_offers=5 "
        "deferred_by_offer_cap=4"
    ) in caplog.text


def test_pipeline_logs_profile_fallback_without_private_content(store, settings, caplog):
    settings.candidate_profile = "PRIVATE_RESUME_TEXT"
    job = _job()
    gemini = FakeGemini(preference_payload="{not-json")
    telegram = FakeTelegram()

    with caplog.at_level(logging.INFO):
        run_pipeline(settings, sources=[FakeSource([job])], store=store, gemini=gemini, telegram=telegram)

    assert "profile extraction: source=fallback" in caplog.text
    assert "eligible sources: ashby=1" in caplog.text
    assert "selected sources: ashby=1" in caplog.text
    assert settings.candidate_profile not in caplog.text
    assert job.description not in caplog.text


def test_pipeline_logs_per_market_metrics_and_bounds_fresh_gemini_calls(store, settings, caplog):
    market_policy = make_market_policy()
    market_policy.max_jobs_per_run = 5
    settings.policy = market_policy
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


def test_pipeline_logs_source_quality_and_ats_registry_metrics(store, settings, caplog):
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
        assert _targeted_canonical_candidates(http, job, breaker, None) == []

    assert http.calls == 1


def test_pipeline_sends_no_message_events_when_navigator_supported(store, settings):
    job = _job()
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


def test_pipeline_surfaces_evaluation_location_note_in_navigator_card(store, settings):
    job = _job()
    gemini = FakeGemini()
    telegram = OrderedNavigatorTelegram()

    run_pipeline(settings, sources=[FakeSource([job])], store=store, gemini=gemini, telegram=telegram)

    card_events = [payload for kind, payload in telegram.events if kind == "card"]
    assert card_events
    assert "Note: Remote EU friendly" in card_events[-1]


def test_pipeline_sends_gemini_pause_warning_as_last_message(store, settings):
    job = _job()
    summary = _usage_summary(provider_paused=True)
    gemini = FakeGemini()
    gemini._tracker = FakeUsageTracker(summary)
    telegram = FakeTelegram()

    run_pipeline(settings, sources=[FakeSource([job])], store=store, gemini=gemini, telegram=telegram)

    expected_warning = build_gemini_pause_warning(summary)
    assert expected_warning is not None
    assert telegram.messages[-1] == expected_warning


def test_pipeline_sends_no_warning_when_usage_is_healthy(store, settings):
    job = _job()
    summary = _usage_summary()
    gemini = FakeGemini()
    gemini._tracker = FakeUsageTracker(summary)
    telegram = FakeTelegram()

    run_pipeline(settings, sources=[FakeSource([job])], store=store, gemini=gemini, telegram=telegram)

    assert build_gemini_pause_warning(summary) is None
    # Only the digest message was sent — no warning, no usage status.
    assert len(telegram.messages) == 1


def test_pipeline_sends_exactly_one_warning_despite_many_locally_blocked_calls(store, settings):
    """Many candidates deferred by quota this run must still yield one warning."""
    jobs = _jobs_for_source("ashby", 5)
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


def test_pipeline_logs_structured_gemini_usage_line(store, settings, caplog):
    job = _job()
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


def test_pipeline_log_total_does_not_double_count_cached_tokens(store, settings, caplog):
    """Regression: cachedContentTokenCount is a subset of promptTokenCount.

    One real Gemini call: promptTokenCount=1000 (400 cached),
    candidatesTokenCount=200, thoughtsTokenCount=50 -> Google's real total is
    1250. The structured log's input+output+thinking (1000+200+50=1250) must
    match `total_tokens_today` exactly -- a formula that also added the
    cached portion would overcount by 32%.
    """
    job = _job()
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


def test_pipeline_logs_gemini_usage_even_in_dry_run(store, settings, caplog):
    dry_settings = dataclasses.replace(settings, dry_run=True)
    job = _job()
    summary = _usage_summary()
    gemini = FakeGemini()
    gemini._tracker = FakeUsageTracker(summary)

    with caplog.at_level(logging.INFO):
        run_pipeline(dry_settings, sources=[FakeSource([job])], store=store, gemini=gemini)

    assert "gemini_usage run_calls=21" in caplog.text
    assert gemini._tracker.snapshot_calls == 1


def test_pipeline_without_gemini_tracker_sends_no_usage_status(store, settings):
    """Legacy/test gemini fakes without a tracker must not break or send usage."""
    job = _job()
    gemini = FakeGemini()  # no `_tracker` attribute at all
    telegram = FakeTelegram()

    run_pipeline(settings, sources=[FakeSource([job])], store=store, gemini=gemini, telegram=telegram)

    # Only the digest message was sent -- no usage status, no crash.
    assert len(telegram.messages) == 1


def test_run_pipeline_forwards_store_to_build_sources_when_sources_not_given(
    store,
    settings, monkeypatch
):
    import job_hunter.pipeline as pipeline_module

    gemini = FakeGemini()
    telegram = FakeTelegram()
    captured = {}

    def fake_build_sources(
        passed_settings,
        http,
        *,
        store=None,
        search_breaker=None,
        query_date=None,
        brave_budget=None,
        supabase_client=None,
    ):
        captured["store"] = store
        return []

    monkeypatch.setattr(pipeline_module, "build_sources", fake_build_sources)

    run_pipeline(settings, store=store, gemini=gemini, telegram=telegram)

    assert captured["store"] is store


def test_run_pipeline_builds_brave_backed_source_when_configured(
    store, tmp_path, monkeypatch
):
    """`run_pipeline` itself -- not `build_sources` called directly with a
    client -- must produce a Brave-backed source when Brave is configured.

    `test_sources.py::test_build_sources_uses_only_brave_for_metered_market_discovery`
    already proves `build_sources` does the right thing given a client; it
    would keep passing even if `run_pipeline` never threaded one through.
    This test wraps the real `build_sources` to observe what `run_pipeline`
    actually calls it with. It exercises the real Postgres-backed `store`
    fixture (not a SQLite `JobStore`), because `run_pipeline` derives its
    Supabase client from `store.client` when `store` is a
    `PostgresJobStore` -- see issue #70 task 14a.

    Uses `make_market_policy()` (not the module's plain `policy`/`settings`
    fixtures) because `build_sources` only ever builds a `TargetedSearchSource`
    when `generate_search_queries` has something to give it, which needs a
    configured market with query templates -- the module's default `policy`
    fixture has neither.
    """
    import job_hunter.pipeline as pipeline_module
    from job_hunter.sources import build_sources as real_build_sources

    settings = Settings(
        gemini_api_key="key",
        candidate_profile="profile",
        cover_letter_template="template",
        timezone="Europe/Berlin",
        scheduled_hour=9,
        policy=make_market_policy(),
        gemini_quota=GeminiQuotaSettings(rpm=10, tpm=250000, rpd=500),
        brave_search_api_key="brave-key",
        dry_run=False,
        telegram_bot_token="token",
        telegram_chat_id="chat",
        output_dir=str(tmp_path),
    )
    gemini = FakeGemini()
    telegram = FakeTelegram()
    captured = {}

    def capturing_build_sources(*args, **kwargs):
        result = real_build_sources(*args, **kwargs)
        captured["kinds"] = [type(s).__name__ for s in result]
        return result

    monkeypatch.setattr(pipeline_module, "build_sources", capturing_build_sources)

    run_pipeline(
        settings,
        store=store,
        gemini=gemini,
        telegram=telegram,
        http=_NetworkFreeHttp(),
    )

    assert "TargetedSearchSource" in captured.get("kinds", [])


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


def test_pipeline_records_evaluation_against_survivor_when_job_merged_mid_run(
    store, settings, monkeypatch
):
    """Issue #145: a merge after selection must not strand the evaluation.

    Discovery merges duplicates while it is still building the shortlist, so a
    job selected early can be deleted by a later canonical resolution in the
    same run. The pipeline is left holding the duplicate's id, and writing the
    evaluation against it used to raise a 23503 foreign key violation that
    aborted the entire run.
    """
    discovered = _job()
    survivor_job = _job(source_job_id="job-survivor", title="Staff Product Engineer")
    merge = {}
    real_evaluate = job_hunter.pipeline.evaluate_job

    def evaluate_then_merge(job, *args, **kwargs):
        evaluation = real_evaluate(job, *args, **kwargs)
        if not merge:
            duplicate_id, _, _ = store.upsert_job(job)
            survivor_id, _, _ = store.upsert_job(survivor_job)
            # An evaluation is history, and history decides the survivor:
            # this pins which of the two rows merge_jobs keeps, so the job
            # the pipeline is holding is always the one that disappears.
            store.save_evaluation(survivor_id, _evaluation(survivor_id, total_score=55))
            merge["duplicate"] = duplicate_id
            merge["survivor"] = store.merge_jobs(survivor_id, duplicate_id)
        return evaluation

    monkeypatch.setattr(job_hunter.pipeline, "evaluate_job", evaluate_then_merge)
    telegram = FakeTelegram()

    summary = run_pipeline(
        settings,
        sources=[FakeSource([discovered])],
        store=store,
        gemini=FakeGemini(),
        telegram=telegram,
    )

    assert merge["survivor"] != merge["duplicate"]
    assert summary.errors == 0
    assert summary.ready_to_apply == 1
    # The fresh evaluation landed on the surviving row, replacing the score
    # that was only there to decide the merge.
    recorded = store.get_evaluation(merge["survivor"])
    assert recorded is not None
    assert recorded.total_score == 90
    # ...and the offer reached the user rather than being silently dropped.
    assert len(telegram.messages) == 1
    assert store.has_delivery(merge["survivor"], "telegram_message")
    # Exactly once: the survivor's id has to join the run's working set, or
    # the pending-delivery sweep queues the same job into the digest again.
    assert telegram.messages[0].count("Staff Product Engineer") == 1
    # The card describes the surviving row, not the one the merge discarded.
    assert "Senior Product Engineer" not in telegram.messages[0]


def test_pipeline_contains_a_store_write_failure_for_one_job(store, settings, monkeypatch):
    """Issue #145: one job's store failure must not end the run.

    The per-job guard only covered the Gemini call, so a failing write escaped
    the evaluation loop and killed the process, discarding every remaining
    candidate and the digest with them.
    """
    doomed = _job(source_job_id="job-doomed", company="Doomed GmbH")
    healthy = _job(source_job_id="job-healthy", company="Healthy GmbH")
    real_save_evaluation = store.save_evaluation

    def fail_for_the_doomed_job(job_id, evaluation):
        job = store.get_job(job_id)
        if job is not None and job.company == "Doomed GmbH":
            raise RuntimeError("store write exploded")
        return real_save_evaluation(job_id, evaluation)

    monkeypatch.setattr(store, "save_evaluation", fail_for_the_doomed_job)
    telegram = FakeTelegram()

    summary = run_pipeline(
        settings,
        sources=[FakeSource([doomed, healthy])],
        store=store,
        gemini=FakeGemini(),
        telegram=telegram,
    )

    assert summary.errors == 1
    # The rest of the shortlist was still evaluated...
    assert summary.ready_to_apply == 1
    healthy_id, _, _ = store.upsert_job(healthy)
    assert store.get_evaluation(healthy_id) is not None
    # ...and the digest still went out.
    assert len(telegram.messages) == 1
    assert "Healthy GmbH" in telegram.messages[0]
    assert "Doomed GmbH" not in telegram.messages[0]


def test_pipeline_does_not_offer_a_job_merged_into_an_already_delivered_one(
    store, settings, monkeypatch
):
    """Issue #145: following a merge must not re-offer a job the user has seen.

    `merge_jobs` moves the duplicate's deliveries onto the survivor, so the
    already-delivered check at the top of the evaluation -- made against the
    duplicate's id -- sees nothing. Redirecting the write without re-checking
    would send the same job a second time under a different id.
    """
    discovered = _job()
    delivered_job = _job(source_job_id="job-delivered", title="Staff Product Engineer")
    delivered_id, _, _ = store.upsert_job(delivered_job)
    store.save_evaluation(delivered_id, _evaluation(delivered_id))
    store.mark_delivered(delivered_id, "telegram_message", "msg-yesterday")

    merge = {}
    real_evaluate = job_hunter.pipeline.evaluate_job

    def evaluate_then_merge(job, *args, **kwargs):
        evaluation = real_evaluate(job, *args, **kwargs)
        if not merge:
            duplicate_id, _, _ = store.upsert_job(job)
            merge["survivor"] = store.merge_jobs(delivered_id, duplicate_id)
        return evaluation

    monkeypatch.setattr(job_hunter.pipeline, "evaluate_job", evaluate_then_merge)
    telegram = FakeTelegram()

    summary = run_pipeline(
        settings,
        sources=[FakeSource([discovered])],
        store=store,
        gemini=FakeGemini(),
        telegram=telegram,
    )

    assert merge["survivor"] == delivered_id
    assert summary.errors == 0
    assert telegram.messages == []
