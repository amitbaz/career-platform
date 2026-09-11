"""Engine Lab card selection and measurement ledger (#257).

These are pure unit tests against fakes -- they exercise the invariants the
acceptance criteria describe (impression before render, independent
judgements, cohort concealment, no missing cohort silently dropped) without
needing the live Supabase stack or a real GoTrue email round trip. Schema
and identity-boundary enforcement (not-null columns, RLS, the owner
bootstrap/invite/claim functions, one-judgement-per-impression) is proved
separately in `supabase/tests/pgtap/job_hunter_engine_lab.sql`, which this
suite does not duplicate.
"""

from __future__ import annotations

import pytest

from job_hunter import engine_lab
from job_hunter.models import CandidateContext, CandidatePreferences, Evaluation, Job

_PROFILE_ROW = {
    "match_score_floor": 80,
    "salary_floor_eur": 90000,
    "thresholds": {},
    "blocked_title_keywords": [],
}


def _candidate_context() -> CandidateContext:
    preferences = CandidatePreferences(
        preferred_roles=["engineer"],
        preferred_seniority=["senior"],
        must_have_signals=[],
        nice_to_have_signals=[],
        preferred_locations=[],
        avoid_signals=[],
        summary="summary",
    )
    return CandidateContext(
        preferences=preferences,
        technical_skills=[],
        architecture_evidence=[],
        leadership_ownership=[],
        agentic_ai_evidence=[],
        product_domain_evidence=[],
        location_language_facts=[],
        career_direction=[],
        company_environment=[],
        career_evidence=[],
        evaluation_summary="summary",
    )


def _dataclass_to_dict(context: CandidateContext) -> dict:
    from dataclasses import asdict

    return asdict(context)


class FakeCacheEntry:
    def __init__(self, context: dict) -> None:
        self.context = context


class FakeStore:
    """Duck-types just the `PostgresJobStore` methods `engine_lab` calls."""

    def __init__(
        self,
        *,
        cv: str = "a real cv",
        profile=(_PROFILE_ROW, []),
        cached_context: FakeCacheEntry | None = "default",
        match_rows: list[dict] | None = None,
        jobs: dict[str, Job] | None = None,
        evaluations: dict[str, Evaluation] | None = None,
        posting_hashes: dict[str, str] | None = None,
    ) -> None:
        self._cv = cv
        self._profile = profile
        self._cached_context = (
            FakeCacheEntry(_dataclass_to_dict(_candidate_context()))
            if cached_context == "default"
            else cached_context
        )
        self._match_rows = match_rows or []
        self._jobs = jobs or {}
        self._evaluations = evaluations or {}
        self._posting_hashes = posting_hashes or {}

    def get_source_documents(self) -> dict[str, str]:
        return {"cv": self._cv, "cover_letter": ""}

    def get_search_profile(self):
        return self._profile

    def get_candidate_context(self, cache_key: str):
        return self._cached_context

    def match_jobs(self, **kwargs) -> list[dict]:
        return self._match_rows

    def get_job(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def get_evaluations_bulk(self, job_ids: list[str]) -> dict[str, Evaluation]:
        return {jid: self._evaluations[jid] for jid in job_ids if jid in self._evaluations}

    def get_posting_description_hash(self, posting_id: str) -> str | None:
        return self._posting_hashes.get(posting_id)


class FakeSupabaseClient:
    """Duck-types the `SupabaseClient` calls `engine_lab` makes."""

    def __init__(self, select_results: dict[str, list[dict]] | None = None) -> None:
        self.inserted: list[tuple[str, list[dict]]] = []
        self.rpc_calls: list[tuple[str, dict | None]] = []
        self._select_results = select_results or {}
        self._rpc_results: dict[str, list] = {}
        self._next_id = 0

    def set_rpc_result(self, function: str, result: list) -> None:
        self._rpc_results[function] = result

    def insert(self, table: str, rows: list[dict]) -> list[dict]:
        self.inserted.append((table, [dict(r) for r in rows]))
        self._next_id += 1
        row = dict(rows[0])
        row.setdefault("id", f"generated-{self._next_id}")
        return [row]

    def select(self, table: str, *, params: dict[str, str] | None = None) -> list[dict]:
        return self._select_results.get(table, [])

    def rpc(self, function: str, payload: dict | None = None, *, retry: bool = True) -> list:
        self.rpc_calls.append((function, payload))
        return self._rpc_results.get(function, [])


_ROW_INTENDED = {
    "job_id": "job-intended",
    "posting_id": "posting-intended",
    "score": 90,
    "hard_blockers": [],
    "has_facets": True,
    "locations": (),
}
_ROW_HARD_EXCLUDED = {
    "job_id": "job-excluded",
    "posting_id": "posting-excluded",
    "score": 10,
    "hard_blockers": ["salary below floor"],
    "has_facets": True,
    "locations": (),
}
_ROW_UNRESOLVED = {
    "job_id": "job-unresolved",
    "posting_id": "posting-unresolved",
    "score": 0,
    "hard_blockers": [],
    "has_facets": False,
    "locations": (),
}
_ROW_BELOW_THRESHOLD = {
    "job_id": "job-below",
    "posting_id": "posting-below",
    "score": 50,
    "hard_blockers": [],
    "has_facets": True,
    "locations": (),
}


# _bucket_candidates -----------------------------------------------------------


def test_bucket_candidates_classifies_each_row_into_exactly_one_cohort():
    rows = [_ROW_INTENDED, _ROW_HARD_EXCLUDED, _ROW_UNRESOLVED, _ROW_BELOW_THRESHOLD]
    buckets = engine_lab._bucket_candidates(rows, score_floor=80)

    assert buckets["intended"] == [_ROW_INTENDED]
    assert buckets["audit_hard_excluded"] == [_ROW_HARD_EXCLUDED]
    assert buckets["audit_unresolved"] == [_ROW_UNRESOLVED]
    assert buckets["audit_below_threshold"] == [_ROW_BELOW_THRESHOLD]


def test_bucket_candidates_hard_blocker_wins_over_missing_facets():
    row = {**_ROW_UNRESOLVED, "hard_blockers": ["not open to remote"]}
    buckets = engine_lab._bucket_candidates([row], score_floor=80)
    assert buckets["audit_hard_excluded"] == [row]
    assert buckets["audit_unresolved"] == []


# _choose_cohort -----------------------------------------------------------------


def test_choose_cohort_only_ever_returns_a_non_empty_bucket():
    buckets = {
        "intended": [],
        "audit_hard_excluded": [_ROW_HARD_EXCLUDED],
        "audit_unresolved": [],
        "audit_below_threshold": [],
    }
    for _ in range(20):
        assert engine_lab._choose_cohort(buckets) == "audit_hard_excluded"


def test_choose_cohort_raises_with_a_reason_when_every_bucket_is_empty():
    empty = {cohort: [] for cohort in engine_lab.COHORTS}
    with pytest.raises(engine_lab.EngineLabUnavailable, match="no eligible postings"):
        engine_lab._choose_cohort(empty)


# select_next_card: durable before render -----------------------------------------


def test_select_next_card_writes_the_impression_before_returning_the_card():
    store = FakeStore(
        match_rows=[_ROW_INTENDED],
        jobs={"job-intended": Job(source="test", title="Engineer", company="Acme", location="Remote", url="https://x/1")},
        evaluations={"job-intended": Evaluation(job_id="job-intended", total_score=90, scores={}, decision="offer", hard_blockers=[], strengths=[], gaps=[], salary_note="", location_note="", rationale="Strong fit.", model="gemini")},
        posting_hashes={"posting-intended": "hash-1"},
    )
    client = FakeSupabaseClient()

    card = engine_lab.select_next_card(
        store, client, reviewer_id="reviewer-1", already_shown_posting_ids=set()
    )

    assert len(client.inserted) == 1
    table, rows = client.inserted[0]
    assert table == "job_hunter_engine_lab_impressions"
    assert rows[0]["reviewer_id"] == "reviewer-1"
    assert rows[0]["posting_id"] == "posting-intended"
    assert card.impression_id == "generated-1"
    assert card.posting_title == "Engineer"
    assert card.why_line == "Strong fit."
    assert card.posting_version == "hash-1"


def test_select_next_card_never_returns_an_intended_cohort_without_a_score_check():
    store = FakeStore(match_rows=[_ROW_BELOW_THRESHOLD], jobs={
        "job-below": Job(source="test", title="Below", company="Acme", location="Remote", url="https://x/2")
    })
    client = FakeSupabaseClient()

    card = engine_lab.select_next_card(
        store, client, reviewer_id="reviewer-1", already_shown_posting_ids=set()
    )

    _, rows = client.inserted[0]
    assert rows[0]["cohort"] == "audit_below_threshold"
    assert "no grounded reasoning" in card.why_line


def test_select_next_card_excludes_postings_already_shown_today():
    store = FakeStore(match_rows=[_ROW_INTENDED])
    client = FakeSupabaseClient()

    with pytest.raises(engine_lab.EngineLabUnavailable, match="no eligible postings"):
        engine_lab.select_next_card(
            store,
            client,
            reviewer_id="reviewer-1",
            already_shown_posting_ids={"posting-intended"},
        )
    assert client.inserted == []


@pytest.mark.parametrize(
    "overrides,reason_fragment",
    [
        ({"cv": ""}, "no CV"),
        ({"profile": None}, "no search profile"),
        ({"cached_context": None}, "no candidate context"),
    ],
)
def test_select_next_card_reports_why_it_is_unavailable(overrides, reason_fragment):
    store = FakeStore(**overrides)
    client = FakeSupabaseClient()

    with pytest.raises(engine_lab.EngineLabUnavailable, match=reason_fragment):
        engine_lab.select_next_card(
            store, client, reviewer_id="reviewer-1", already_shown_posting_ids=set()
        )
    assert client.inserted == [], "an unavailable card must never write an impression"


# record_judgement ----------------------------------------------------------------


def test_record_judgement_rejects_an_invalid_why_line_verdict_before_writing():
    client = FakeSupabaseClient()
    with pytest.raises(ValueError, match="helpful.*flawed"):
        engine_lab.record_judgement(
            client,
            reviewer_id="reviewer-1",
            impression_id="impression-1",
            worth_applying=True,
            why_line_judgement="sort_of",
            problem_reason=None,
        )
    assert client.inserted == []


def test_record_judgement_stores_both_verdicts_as_independent_fields():
    client = FakeSupabaseClient()
    engine_lab.record_judgement(
        client,
        reviewer_id="reviewer-1",
        impression_id="impression-1",
        worth_applying=True,
        why_line_judgement="flawed",
        problem_reason="title mismatch",
    )
    _, rows = client.inserted[0]
    assert rows[0]["worth_applying"] is True
    assert rows[0]["why_line_judgement"] == "flawed"
    assert rows[0]["problem_reason"] == "title mismatch"


# daily_summary: a missing cohort is reported, not omitted -----------------------


def test_daily_summary_names_a_cohort_with_zero_impressions():
    import datetime as dt

    client = FakeSupabaseClient(
        select_results={
            "job_hunter_engine_lab_impressions": [
                {"id": "i1", "cohort": "intended"},
            ],
            "job_hunter_engine_lab_judgements": [
                {"impression_id": "i1", "worth_applying": True, "why_line_judgement": "helpful"},
            ],
        }
    )

    summaries = engine_lab.daily_summary(client, dt.date(2026, 9, 11))

    by_cohort = {row.cohort: row for row in summaries}
    assert set(by_cohort) == set(engine_lab.COHORTS)
    assert by_cohort["intended"].impressions == 1
    assert by_cohort["intended"].judged == 1
    assert by_cohort["intended"].worth_applying_rate == 1.0
    assert by_cohort["intended"].helpful_rate == 1.0
    for cohort in ("audit_hard_excluded", "audit_unresolved", "audit_below_threshold"):
        assert by_cohort[cohort].impressions == 0
        assert by_cohort[cohort].judged == 0
        assert by_cohort[cohort].worth_applying_rate is None
        assert by_cohort[cohort].helpful_rate is None


# Versions ------------------------------------------------------------------------


def test_configuration_version_changes_when_a_relevant_field_changes():
    base = engine_lab._configuration_version(_PROFILE_ROW)
    changed = engine_lab._configuration_version({**_PROFILE_ROW, "match_score_floor": 70})
    assert base != changed


def test_configuration_version_is_stable_for_identical_input():
    assert engine_lab._configuration_version(_PROFILE_ROW) == engine_lab._configuration_version(dict(_PROFILE_ROW))


# Collaborators: owner bootstrap gate, invite, claim (RPC call shape only) ---------
#
# The functions' actual authorisation logic (only the configured owner email
# can bootstrap, only the owner may invite, only a matching verified email
# can claim) lives in Postgres and is proved in the pgtap suite. What's
# tested here is that this module calls the right RPC with the right
# arguments, and refuses to call it at all when its own precondition fails.


def test_bootstrap_owner_if_matching_never_calls_the_rpc_for_a_non_owner_email():
    client = FakeSupabaseClient()
    engine_lab.bootstrap_owner_if_matching(
        client, user_id="user-1", verified_email="someone-else@example.com", owner_email="owner@example.com"
    )
    assert client.rpc_calls == []


def test_bootstrap_owner_if_matching_calls_the_rpc_for_the_owner_email_case_insensitively():
    client = FakeSupabaseClient()
    engine_lab.bootstrap_owner_if_matching(
        client, user_id="user-1", verified_email="Owner@Example.com", owner_email="owner@example.com"
    )
    assert client.rpc_calls == [
        ("job_hunter_engine_lab_bootstrap_owner", {"p_user_id": "user-1", "p_email": "Owner@Example.com"})
    ]


def test_claim_invite_returns_false_when_the_rpc_reports_nothing_claimed():
    client = FakeSupabaseClient()
    client.set_rpc_result("job_hunter_engine_lab_claim_invite", [False])
    assert engine_lab.claim_invite(client) is False


def test_claim_invite_returns_true_when_the_rpc_reports_a_claim():
    client = FakeSupabaseClient()
    client.set_rpc_result("job_hunter_engine_lab_claim_invite", [True])
    assert engine_lab.claim_invite(client) is True


def test_invite_collaborator_calls_the_invite_rpc_with_the_email():
    client = FakeSupabaseClient()
    engine_lab.invite_collaborator(client, "new-reviewer@example.com")
    assert client.rpc_calls == [("job_hunter_engine_lab_invite", {"p_email": "new-reviewer@example.com"})]


def test_get_own_collaborator_treats_a_revoked_row_as_absent():
    client = FakeSupabaseClient(
        select_results={
            "job_hunter_engine_lab_collaborators": [
                {"user_id": "u1", "email": "a@b.com", "is_owner": False, "revoked_at": "2026-01-01T00:00:00Z"}
            ]
        }
    )
    assert engine_lab.get_own_collaborator(client, "u1") is None


# Sessions ------------------------------------------------------------------------


def test_session_needs_refresh_is_true_within_the_margin():
    import time

    fresh = engine_lab.AuthSession(
        user_id="u1", email="a@b.com", access_token="t", refresh_token="r",
        expires_at=time.time() + 30,
    )
    stale = engine_lab.AuthSession(
        user_id="u1", email="a@b.com", access_token="t", refresh_token="r",
        expires_at=time.time() + 3600,
    )
    assert engine_lab.session_needs_refresh(fresh) is True
    assert engine_lab.session_needs_refresh(stale) is False
