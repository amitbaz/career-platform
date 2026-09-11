"""Tests for the one matching operation over stored facets (#187).

Two things are asserted here that a pure-SQL pgtap suite cannot reach:
`matching.match_jobs` calling the model only for unblocked, already-read rows
and stopping at `limit` (this file), and the SQL ranking/hard-blocker port
agreeing with the pure-Python originals it replaces -- `ranking
.profile_priority_score` and `hard_blockers.hard_blockers_from_facets` -- on
a shared fixture (the equivalence tests below), which is what AC7 asks for:
"given the same corpus and profile, the operation returns the same jobs the
current run would have selected."
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from job_hunter.hard_blockers import BlockingThresholds, hard_blockers_from_facets
from job_hunter.matching import match_jobs
from job_hunter.models import (
    CandidateContext,
    CandidatePreferences,
    Compensation,
    Evaluation,
    Job,
    MarketPolicy,
    SalaryPolicy,
    SearchPolicy,
)
from job_hunter.ranking import profile_priority_score
from tests.facet_fixtures import make_facets


class FakeAI:
    """The minimal double `evaluate_job` needs: a model name and one call shape."""

    def __init__(self, evaluation_payload=None):
        self.model = "gemini-test"
        self.calls = 0

        self.evaluation_payload = evaluation_payload

    def generate_text(
        self,
        prompt,
        *,
        call_class,
        purpose=None,
        thinking_level=None,
        max_output_tokens=None,
        json_mode=False,
        json_schema=None,
        max_attempts=1,
        read_timeout=None,
    ):
        self.calls += 1
        payload = self.evaluation_payload or {
            "scores": {
                "role_seniority": 20,
                "technical": 20,
                "product_architecture": 15,
                "career_direction": 8,
                "location_language": 8,
                "company_environment": 4,
            },
            "total_score": 75,
            "hard_blockers": [],
            "strengths": [],
            "gaps": [],
            "salary_note": "",
            "location_note": "",
            "decision": "possible_match",
            "rationale": "ok",
            "requirements": {
                "must_have": [
                    {"requirement": "React", "depth": "experience", "candidate_support": "supported"}
                ],
                "preferred": [
                    {"requirement": "GraphQL", "depth": "familiarity", "candidate_support": "supported"}
                ],
            },
        }
        return json.dumps(payload)


def _candidate_context(**overrides) -> CandidateContext:
    defaults = dict(
        preferences=CandidatePreferences(
            preferred_roles=["Senior Backend Engineer"],
            preferred_seniority=["senior"],
            must_have_signals=["kubernetes"],
            nice_to_have_signals=["postgres"],
            preferred_locations=["germany"],
            avoid_signals=[],
            summary="Backend engineer.",
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
        evaluation_summary="Solid backend candidate.",
        source="test",
    )
    defaults.update(overrides)
    return CandidateContext(**defaults)


def _policy(**overrides) -> SearchPolicy:
    defaults = dict(
        target_titles=[],
        positive_keywords=[],
        blocked_title_keywords=[],
        salary_floor_eur=90000,
        thresholds={},
    )
    defaults.update(overrides)
    return SearchPolicy(**defaults)


def _insert_search_profile(supabase_client, user_id, **overrides):
    row = dict(
        user_id=user_id,
        timezone="Europe/Berlin",
        scheduled_hour=9,
        max_jobs_per_run=35,
        source_minimum_per_run=0,
        source_max_share=0.5,
        salary_floor_eur=90000,
        max_search_queries_per_run=30,
        max_canonical_resolutions_per_run=80,
        max_learned_ats_boards_per_run=75,
    )
    row.update(overrides)
    supabase_client.insert("job_hunter_search_profiles", [row])


def _posting_row(**overrides):
    row = dict(
        source="greenhouse",
        source_job_id=None,
        remote=True,
        description_hash="",
        content_confidence="official_ats",
        first_seen_at="2026-01-01T00:00:00Z",
        last_seen_at="2026-01-01T00:00:00Z",
    )
    row.update(overrides)
    # Postings persist across test runs (the shared tables are never
    # cleaned -- see conftest.py), so a literal fingerprint would collide
    # with a leftover row the next time this file runs against the same
    # stack. A random suffix keeps every posting unique to this process.
    row["fingerprint"] = f"{row['fingerprint']}-{uuid.uuid4().hex[:8]}"
    return row


def _evaluation(job_id, **overrides):
    defaults = dict(
        job_id=job_id,
        total_score=75,
        scores={},
        decision="possible_match",
        hard_blockers=[],
        strengths=[],
        gaps=[],
        salary_note="",
        location_note="",
        rationale="",
        model="gemini-test",
    )
    defaults.update(overrides)
    return Evaluation(**defaults)


def _insert_membership(supabase_client, user_id, posting_id, **overrides) -> str:
    row = dict(
        user_id=user_id,
        posting_id=posting_id,
        first_seen_at="2026-01-01T00:00:00Z",
        last_seen_at="2026-01-01T00:00:00Z",
    )
    row.update(overrides)
    (inserted,) = supabase_client.insert("job_hunter_jobs", [row])
    return inserted["id"]


def test_match_jobs_skips_a_facetless_row_and_never_calls_the_model(
    store, supabase_client, seed_postings
):
    user_id = supabase_client.user_id
    _insert_search_profile(supabase_client, user_id)

    (posting_id,) = seed_postings(
        [
            _posting_row(
                fingerprint="unread",
                url="https://boards.greenhouse.io/acme/unread",
                canonical_url="https://boards.greenhouse.io/acme/unread",
                company="Acme",
                title="Senior Backend Engineer",
                location="Berlin",
                description="kubernetes postgres",
            )
        ]
    )
    _insert_membership(supabase_client, user_id, posting_id)

    ai = FakeAI()
    results = match_jobs(store, ai, _policy(), _candidate_context(), limit=5, new_posting_limit=0).matched

    assert results == []
    assert ai.calls == 0


def test_match_jobs_scores_a_clean_facets_row(store, supabase_client, seed_postings):
    user_id = supabase_client.user_id
    _insert_search_profile(supabase_client, user_id)

    (posting_id,) = seed_postings(
        [
            _posting_row(
                fingerprint="clean",
                url="https://boards.greenhouse.io/acme/clean",
                canonical_url="https://boards.greenhouse.io/acme/clean",
                company="Acme",
                title="Senior Backend Engineer",
                location="Berlin",
                description="kubernetes postgres",
                description_hash="h-clean",
            )
        ]
    )
    job_id = _insert_membership(supabase_client, user_id, posting_id)
    store.save_job_facets(job_id, make_facets(compensation=Compensation()))

    ai = FakeAI()
    results = match_jobs(store, ai, _policy(), _candidate_context(), limit=5, new_posting_limit=0).matched

    assert ai.calls == 1
    assert len(results) == 1
    assert results[0].scored is True
    assert results[0].fresh is True
    assert results[0].evaluation.model == ai.model


def test_match_jobs_blocks_on_facets_at_zero_cost(store, supabase_client, seed_postings):
    user_id = supabase_client.user_id
    _insert_search_profile(supabase_client, user_id, salary_floor_eur=90000)

    (posting_id,) = seed_postings(
        [
            _posting_row(
                fingerprint="blocked",
                url="https://boards.greenhouse.io/acme/blocked",
                canonical_url="https://boards.greenhouse.io/acme/blocked",
                company="Acme",
                title="Senior Backend Engineer",
                location="Berlin",
                description="kubernetes postgres",
                description_hash="h-blocked",
            )
        ]
    )
    job_id = _insert_membership(supabase_client, user_id, posting_id)
    store.save_job_facets(
        job_id,
        make_facets(
            compensation=Compensation(
                disclosed=True, currency="EUR", maximum=50000, period="year"
            )
        ),
    )

    ai = FakeAI()
    results = match_jobs(store, ai, _policy(), _candidate_context(), limit=5, new_posting_limit=0).matched

    assert ai.calls == 0
    assert len(results) == 1
    assert results[0].scored is False
    assert results[0].fresh is True
    assert results[0].evaluation.decision == "blocked"
    assert results[0].evaluation.hard_blockers


def test_match_jobs_isolates_one_rows_scoring_failure_from_the_rest(
    store, supabase_client, seed_postings
):
    """#145's guarantee, carried into the operation: one bad row costs only

    that row its turn, and is reported back rather than crashing the call."""
    user_id = supabase_client.user_id
    _insert_search_profile(supabase_client, user_id)

    posting_ids = seed_postings(
        [
            _posting_row(
                fingerprint=f"failing-{i}",
                url=f"https://boards.greenhouse.io/acme/failing-{i}",
                canonical_url=f"https://boards.greenhouse.io/acme/failing-{i}",
                company="Acme",
                title="Senior Backend Engineer",
                location="Berlin",
                description="kubernetes postgres",
                description_hash=f"h-failing-{i}",
            )
            for i in range(2)
        ]
    )
    for posting_id in posting_ids:
        job_id = _insert_membership(supabase_client, user_id, posting_id)
        store.save_job_facets(job_id, make_facets(compensation=Compensation()))

    class FlakyAI(FakeAI):
        def generate_text(self, *args, **kwargs):
            if self.calls == 0:
                self.calls += 1
                raise RuntimeError("provider blew up")
            return super().generate_text(*args, **kwargs)

    ai = FlakyAI()
    result = match_jobs(store, ai, _policy(), _candidate_context(), limit=5, new_posting_limit=0)

    assert len(result.matched) == 1
    assert result.matched[0].fresh is True
    assert len(result.failed_job_ids) == 1


def test_match_jobs_stops_and_returns_what_it_has_on_quota_exhaustion(
    store, supabase_client, seed_postings
):
    """A user-key exhaustion must not lose rows already decided in this call.

    Every later row would fail identically (same key, no other claimant), so
    the operation stops rather than raising past work away (#188)."""
    from job_hunter.ai import AIBudgetExceeded

    user_id = supabase_client.user_id
    _insert_search_profile(supabase_client, user_id)

    posting_ids = seed_postings(
        [
            _posting_row(
                fingerprint=f"quota-{i}",
                url=f"https://boards.greenhouse.io/acme/quota-{i}",
                canonical_url=f"https://boards.greenhouse.io/acme/quota-{i}",
                company="Acme",
                title="Senior Backend Engineer",
                location="Berlin",
                description="kubernetes postgres",
                description_hash=f"h-quota-{i}",
            )
            for i in range(3)
        ]
    )
    job_ids = []
    for posting_id in posting_ids:
        job_id = _insert_membership(supabase_client, user_id, posting_id)
        store.save_job_facets(job_id, make_facets(compensation=Compensation()))
        job_ids.append(job_id)

    class ExhaustingAI(FakeAI):
        def generate_text(self, *args, **kwargs):
            if self.calls == 1:
                self.calls += 1
                raise AIBudgetExceeded("user key exhausted")
            return super().generate_text(*args, **kwargs)

    ai = ExhaustingAI()
    results = match_jobs(store, ai, _policy(), _candidate_context(), limit=5, new_posting_limit=0).matched

    assert ai.calls == 2
    assert len(results) == 1
    assert results[0].fresh is True
    assert results[0].scored is True


def test_match_jobs_skips_a_delivered_job_and_never_calls_the_model(
    store, supabase_client, seed_postings
):
    """AC: previously-delivered jobs do not reappear, at zero cost (#188)."""
    user_id = supabase_client.user_id
    _insert_search_profile(supabase_client, user_id)

    (posting_id,) = seed_postings(
        [
            _posting_row(
                fingerprint="delivered",
                url="https://boards.greenhouse.io/acme/delivered",
                canonical_url="https://boards.greenhouse.io/acme/delivered",
                company="Acme",
                title="Senior Backend Engineer",
                location="Berlin",
                description="kubernetes postgres",
                description_hash="h-delivered",
            )
        ]
    )
    job_id = _insert_membership(supabase_client, user_id, posting_id)
    store.save_job_facets(job_id, make_facets(compensation=Compensation()))
    store.save_evaluation(job_id, _evaluation(job_id))
    store.mark_delivered(job_id, "telegram_message")

    ai = FakeAI()
    results = match_jobs(store, ai, _policy(), _candidate_context(), limit=5, new_posting_limit=0).matched

    assert results == []
    assert ai.calls == 0


def test_match_jobs_reuses_an_evaluated_undelivered_job_without_calling_the_model(
    store, supabase_client, seed_postings
):
    """AC: delivery failures do not affect what was matched (#188) -- an

    evaluated-but-undelivered job is handed back as-is, not rescored."""
    user_id = supabase_client.user_id
    _insert_search_profile(supabase_client, user_id)

    (posting_id,) = seed_postings(
        [
            _posting_row(
                fingerprint="undelivered",
                url="https://boards.greenhouse.io/acme/undelivered",
                canonical_url="https://boards.greenhouse.io/acme/undelivered",
                company="Acme",
                title="Senior Backend Engineer",
                location="Berlin",
                description="kubernetes postgres",
                description_hash="h-undelivered",
            )
        ]
    )
    job_id = _insert_membership(supabase_client, user_id, posting_id)
    store.save_job_facets(job_id, make_facets(compensation=Compensation()))
    store.save_evaluation(job_id, _evaluation(job_id, total_score=91))

    ai = FakeAI()
    results = match_jobs(store, ai, _policy(), _candidate_context(), limit=5, new_posting_limit=0).matched

    assert ai.calls == 0
    assert len(results) == 1
    assert results[0].job_id == job_id
    assert results[0].fresh is False
    assert results[0].scored is True  # the reused evaluation is the model's own answer
    assert results[0].evaluation.total_score == 91


def test_match_jobs_does_not_reuse_an_evaluation_whose_posting_has_since_changed(
    store, supabase_client
):
    """A stored evaluation is only a cached model answer if the posting it

    answered still reads the way it did when scored. `store.upsert_job` with
    a changed description invalidates the facets row's
    `description_hash_at_extraction` the same way it would for a fresh
    extraction (`test_job_facets_store.py`); `match_jobs` must treat a job in
    that state as `skipped_without_facets`, never as a row to reuse verbatim
    -- reusing it would serve a score computed against text the posting no
    longer carries, forever, since nothing after this call would ever
    re-derive it."""
    user_id = supabase_client.user_id
    _insert_search_profile(supabase_client, user_id)

    job = Job(
        source="greenhouse",
        title="Senior Backend Engineer",
        company="Acme",
        location="Berlin",
        url="https://boards.greenhouse.io/acme/stale-facets",
        description="kubernetes postgres",
    )
    job_id, _, _ = store.upsert_job(job)
    store.save_job_facets(job_id, make_facets(compensation=Compensation()))
    store.save_evaluation(job_id, _evaluation(job_id, total_score=91))

    changed = Job(
        source="greenhouse",
        title="Senior Backend Engineer",
        company="Acme",
        location="Berlin",
        url="https://boards.greenhouse.io/acme/stale-facets",
        description="a materially different description",
    )
    same_id, _, description_changed = store.upsert_job(changed)
    assert same_id == job_id
    assert description_changed is True

    ai = FakeAI()
    result = match_jobs(store, ai, _policy(), _candidate_context(), limit=5, new_posting_limit=0)

    assert ai.calls == 0  # nothing trustworthy to score against either
    assert result.matched == []
    assert result.skipped_without_facets_job_ids == [job_id]


def test_match_jobs_stops_at_limit(store, supabase_client, seed_postings):
    user_id = supabase_client.user_id
    _insert_search_profile(supabase_client, user_id)

    posting_ids = seed_postings(
        [
            _posting_row(
                fingerprint=f"limit-{i}",
                url=f"https://boards.greenhouse.io/acme/limit-{i}",
                canonical_url=f"https://boards.greenhouse.io/acme/limit-{i}",
                company="Acme",
                title="Senior Backend Engineer",
                location="Berlin",
                description="kubernetes postgres",
                description_hash=f"h-limit-{i}",
            )
            for i in range(3)
        ]
    )
    for posting_id in posting_ids:
        job_id = _insert_membership(supabase_client, user_id, posting_id)
        store.save_job_facets(job_id, make_facets(compensation=Compensation()))

    ai = FakeAI()
    results = match_jobs(store, ai, _policy(), _candidate_context(), limit=2, new_posting_limit=0).matched

    assert ai.calls == 2
    assert len(results) == 2
    assert all(r.scored for r in results)


def test_match_jobs_folds_a_variant_group_to_one_scored_representative(
    store, supabase_client, seed_postings
):
    """Issue #61: two location variants of one position fold into one match,
    and subjective scoring runs once, not twice, carrying both open
    locations."""
    user_id = supabase_client.user_id
    _insert_search_profile(supabase_client, user_id)

    posting_ids = seed_postings(
        [
            _posting_row(
                fingerprint="variant-berlin",
                url="https://boards.greenhouse.io/acme/variant-berlin",
                canonical_url="https://boards.greenhouse.io/acme/variant-berlin",
                company="Acme",
                title="Senior Backend Engineer",
                location="Berlin",
                description="kubernetes postgres",
                description_hash="h-variant-berlin",
            ),
            _posting_row(
                fingerprint="variant-paris",
                url="https://boards.greenhouse.io/acme/variant-paris",
                canonical_url="https://boards.greenhouse.io/acme/variant-paris",
                company="Acme",
                title="Senior Backend Engineer",
                location="Paris",
                description="kubernetes postgres",
                description_hash="h-variant-paris",
            ),
        ]
    )
    # What job_hunter_assign_variant_groups would have written at ingestion --
    # set directly here so this test is about match_jobs's fold, not about
    # the grouping walk (covered in supabase/tests/pgtap/job_hunter_variant_groups.sql).
    group_id = posting_ids[0]
    with store.platform_ingestion.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "update public.job_hunter_postings set variant_group_id = %s "
                "where id = any(%s)",
                (group_id, posting_ids),
            )

    job_ids = [
        _insert_membership(supabase_client, user_id, posting_id)
        for posting_id in posting_ids
    ]
    for job_id in job_ids:
        store.save_job_facets(job_id, make_facets(compensation=Compensation()))

    ai = FakeAI()
    results = match_jobs(store, ai, _policy(), _candidate_context(), limit=5, new_posting_limit=0).matched

    assert ai.calls == 1
    assert len(results) == 1
    assert sorted(results[0].locations) == ["Berlin", "Paris"]


# Matching without a prior membership row (#243) ----------------------------------


def test_match_jobs_considers_a_posting_with_no_prior_membership(
    store, supabase_client, seed_postings
):
    """AC: an open posting cannot become invisible solely for lack of a
    job_hunter_jobs row -- the ticket's literal acceptance criterion."""
    user_id = supabase_client.user_id
    _insert_search_profile(supabase_client, user_id)

    (posting_id,) = seed_postings(
        [
            _posting_row(
                fingerprint="never-discovered",
                url="https://boards.greenhouse.io/acme/never-discovered",
                canonical_url="https://boards.greenhouse.io/acme/never-discovered",
                company="Acme",
                title="Senior Backend Engineer",
                location="Berlin",
                description="kubernetes postgres",
                description_hash="h-never-discovered",
                # Recent, not _posting_row's default fixed date: found via
                # store.match_jobs's freshest-first bounded candidate scan
                # (#243), not by ranking the whole shared corpus (which a
                # long-lived local stack, or an earlier test in this same
                # session, may already hold many other postings in).
                last_seen_at=datetime.now(timezone.utc).isoformat(),
            )
        ]
    )
    # No _insert_membership call: this posting was never crawled for this
    # user. Facets are posting-keyed (#175), so they can be written directly
    # without a job_id at all -- `_write_posting_facets` is what
    # `save_job_facets` itself resolves down to once it has one.
    store._write_posting_facets(posting_id, make_facets(compensation=Compensation()))

    ai = FakeAI()
    # Not asserting an exact ai.calls/len(results) here (unlike the
    # membership-required tests above): with the default new_posting_limit,
    # this call also ranks whatever else is genuinely open in the shared
    # corpus (#243's own point), and a long-lived local stack -- or simply
    # every earlier test in this same session, since job_hunter_postings is
    # never cleaned between tests -- may hold real postings that score
    # too. What this test owns is this one posting's own outcome.
    results = match_jobs(store, ai, _policy(), _candidate_context(), limit=5).matched
    matches = [r for r in results if r.posting_id == posting_id]

    assert len(matches) == 1
    assert matches[0].fresh is True

    (membership,) = supabase_client.select(
        "job_hunter_jobs",
        params={"select": "id", "posting_id": f"eq.{posting_id}", "user_id": f"eq.{user_id}"},
    )
    assert membership["id"] == matches[0].job_id


def test_match_jobs_never_creates_membership_for_an_unresolved_posting(
    store, supabase_client, seed_postings
):
    """AC8: membership is an output of matching acting on a row, never
    permission to be considered -- an unresolved row (no facets yet) is
    never acted on, so it earns no row."""
    user_id = supabase_client.user_id
    _insert_search_profile(supabase_client, user_id)

    (posting_id,) = seed_postings(
        [
            _posting_row(
                fingerprint="never-discovered-unresolved",
                url="https://boards.greenhouse.io/acme/never-discovered-unresolved",
                canonical_url="https://boards.greenhouse.io/acme/never-discovered-unresolved",
                company="Acme",
                title="Senior Backend Engineer",
                location="Berlin",
                description="kubernetes postgres",
            )
        ]
    )

    ai = FakeAI()
    result = match_jobs(store, ai, _policy(), _candidate_context(), limit=5, new_posting_limit=0)

    assert ai.calls == 0
    assert result.matched == []
    # Not in skipped_without_facets_posting_ids: that field only ever names
    # a row store.match_jobs actually returned, and a never-discovered,
    # unresolved posting is never one of them (AC10's bound would be
    # meaningless otherwise) -- store.match_state_counts is where this
    # posting's state is visible instead.
    assert posting_id not in result.skipped_without_facets_posting_ids
    counts = store.match_state_counts(
        preferred_roles=[], preferred_seniority=[], must_have_signals=[],
        nice_to_have_signals=[], preferred_locations=[], avoid_signals=[],
    )
    unresolved_reasons = {row["reason"] for row in counts if row["state"] == "unresolved"}
    assert "no_facets" in unresolved_reasons

    membership_rows = supabase_client.select(
        "job_hunter_jobs",
        params={"select": "id", "posting_id": f"eq.{posting_id}", "user_id": f"eq.{user_id}"},
    )
    assert membership_rows == []


def test_match_jobs_reports_state_counts_on_a_short_result(
    store, supabase_client, seed_postings
):
    """AC9: an empty or short result still names how many postings were
    ineligible/unresolved and why."""
    user_id = supabase_client.user_id
    _insert_search_profile(supabase_client, user_id, salary_floor_eur=90000)

    # Read before this test's own postings exist, and compare by delta
    # rather than an exact total (#243 review fix's test): state_counts is
    # corpus-wide, and a shared local stack already holds many postings that
    # are ineligible for a fresh, marketless user's own floor -- a delta
    # is what stays true regardless of how much of that pre-exists.
    _preferences = _candidate_context().preferences
    _state_counts_kwargs = dict(
        preferred_roles=_preferences.preferred_roles,
        preferred_seniority=_preferences.preferred_seniority,
        must_have_signals=_preferences.must_have_signals,
        nice_to_have_signals=_preferences.nice_to_have_signals,
        preferred_locations=_preferences.preferred_locations,
        avoid_signals=_preferences.avoid_signals,
    )
    def _ineligible_total(rows) -> int:
        # Mirrors matching.match_jobs's own aggregation: reason is None is
        # the state's one-row-per-posting total; a reason-not-null row is a
        # breakdown entry and must never be summed into it.
        return sum(row["count"] for row in rows if row["state"] == "ineligible" and row["reason"] is None)

    before_ineligible = _ineligible_total(store.match_state_counts(**_state_counts_kwargs))

    (blocked_posting_id, unresolved_posting_id) = seed_postings(
        [
            _posting_row(
                fingerprint="counts-ineligible",
                url="https://boards.greenhouse.io/acme/counts-ineligible",
                canonical_url="https://boards.greenhouse.io/acme/counts-ineligible",
                company="Acme",
                title="Senior Backend Engineer",
                location="Berlin",
                description="kubernetes postgres",
                description_hash="h-counts-ineligible",
            ),
            _posting_row(
                fingerprint="counts-unresolved",
                url="https://boards.greenhouse.io/acme/counts-unresolved",
                canonical_url="https://boards.greenhouse.io/acme/counts-unresolved",
                company="Acme",
                title="Senior Backend Engineer",
                location="Berlin",
                description="kubernetes postgres",
            ),
        ]
    )
    blocked_job_id = _insert_membership(supabase_client, user_id, blocked_posting_id)
    store.save_job_facets(
        blocked_job_id,
        # Two blockers at once (salary and relocation -- this user has no
        # configured market, so the fallback treats relocation as not
        # allowed): job_hunter_hard_blockers returns an array precisely
        # because several can fire together, and this posting must still
        # count once toward "ineligible", not twice (#243 review fix).
        make_facets(
            compensation=Compensation(disclosed=True, currency="EUR", maximum=50000, period="year"),
            relocation_policy="required",
        ),
    )

    ai = FakeAI()
    result = match_jobs(store, ai, _policy(salary_floor_eur=90000), _candidate_context(), limit=5, new_posting_limit=0)

    # Exactly +1 over the pre-existing corpus baseline, not +2: blocked_
    # posting_id carries two hard-blocker reasons (salary and relocation),
    # and must still count once (#243 review fix -- the SQL used to sum one
    # row per reason, so a two-reason posting used to add 2 here).
    assert result.state_counts.get("ineligible", 0) - before_ineligible == 1
    # Both of this posting's own reasons are visible in the (corpus-wide)
    # breakdown -- not asserting the breakdown's total size, which reflects
    # every ineligible posting on the stack, not just this test's own.
    ineligible_reasons = result.state_reasons.get("ineligible", {})
    assert "disclosed compensation maximum EUR 50000 is below the EUR 90000 floor" in ineligible_reasons
    assert "posting requires relocation" in ineligible_reasons
    assert result.state_counts.get("unresolved", 0) >= 1
    # unresolved_posting_id is never-discovered and has no facets, so
    # store.match_jobs never returns it at all (AC10's bound) -- it is
    # visible only through the aggregate below, not the job/posting id
    # lists, which stay scoped to rows that were actually returned.
    assert unresolved_posting_id not in result.skipped_without_facets_posting_ids
    assert "no_facets" in result.state_reasons.get("unresolved", {})


# Equivalence with the Python originals (AC7) -------------------------------------
#
# `ranking.profile_priority_score` and `hard_blockers.hard_blockers_from_facets`
# are what today's pipeline selects with. Each case below builds the same
# candidate/policy/job/facets fixture in memory (for the Python originals)
# and in Postgres (for the SQL port under `store.match_jobs`), and asserts
# the two agree -- which is what makes "the operation returns the same jobs
# the current run would have selected" a checked property rather than an
# assumption.


def _equivalence_preferences() -> CandidatePreferences:
    return CandidatePreferences(
        preferred_roles=["Staff Product Engineer", "Senior Backend Engineer"],
        preferred_seniority=["senior", "staff"],
        must_have_signals=["kubernetes", "postgres"],
        nice_to_have_signals=["terraform"],
        preferred_locations=["germany"],
        avoid_signals=["on-site"],
        summary="Backend engineer.",
    )


def test_match_jobs_agrees_with_ranking_profile_priority_score(
    store, supabase_client, seed_postings
):
    user_id = supabase_client.user_id
    _insert_search_profile(supabase_client, user_id, salary_floor_eur=90000)

    (posting_id,) = seed_postings(
        [
            _posting_row(
                fingerprint="equiv-score",
                url="https://boards.greenhouse.io/acme/equiv-score",
                canonical_url="https://boards.greenhouse.io/acme/equiv-score",
                company="Acme",
                title="Senior Backend Engineer",
                location="Berlin, Germany",
                description="we run kubernetes and postgres in production",
                description_hash="h-equiv-score",
            )
        ]
    )
    _insert_membership(supabase_client, user_id, posting_id)

    preferences = _equivalence_preferences()
    (sql_row,) = store.match_jobs(
        preferred_roles=preferences.preferred_roles,
        preferred_seniority=preferences.preferred_seniority,
        must_have_signals=preferences.must_have_signals,
        nice_to_have_signals=preferences.nice_to_have_signals,
        preferred_locations=preferences.preferred_locations,
        avoid_signals=preferences.avoid_signals,
        # #243: this user has no membership rows at all -- everything in
        # the SQL's "never-discovered" branch would be real corpus noise on
        # a long-lived stack, and this test wants exactly the one fixture
        # posting it just seeded.
        limit=0,
    )

    python_job = Job(
        source="greenhouse",
        title="Senior Backend Engineer",
        company="Acme",
        location="Berlin, Germany",
        url="https://boards.greenhouse.io/acme/equiv-score",
        remote=True,
        description="we run kubernetes and postgres in production",
    )
    python_score = profile_priority_score(python_job, preferences, _policy())

    assert sql_row["score"] == python_score


def test_match_jobs_agrees_with_hard_blockers_from_facets_on_salary(
    store, supabase_client, seed_postings
):
    user_id = supabase_client.user_id
    _insert_search_profile(supabase_client, user_id, salary_floor_eur=90000)

    (posting_id,) = seed_postings(
        [
            _posting_row(
                fingerprint="equiv-blocked",
                url="https://boards.greenhouse.io/acme/equiv-blocked",
                canonical_url="https://boards.greenhouse.io/acme/equiv-blocked",
                company="Acme",
                title="Senior Backend Engineer",
                location="Berlin",
                description="kubernetes postgres",
                description_hash="h-equiv-blocked",
            )
        ]
    )
    job_id = _insert_membership(supabase_client, user_id, posting_id)
    facets = make_facets(
        compensation=Compensation(disclosed=True, currency="EUR", maximum=50000, period="year")
    )
    store.save_job_facets(job_id, facets)

    (sql_row,) = store.match_jobs(
        preferred_roles=[], preferred_seniority=[], must_have_signals=[],
        nice_to_have_signals=[], preferred_locations=[], avoid_signals=[],
        # #243: see the equivalence test above -- no membership-free noise.
        limit=0,
    )

    python_job = Job(
        source="greenhouse", title="Senior Backend Engineer", company="Acme",
        location="Berlin", remote=True, description="kubernetes postgres",
    )
    python_blockers = hard_blockers_from_facets(
        facets, BlockingThresholds.for_job(python_job, _policy(salary_floor_eur=90000), None)
    )

    # Content, not just count: the two must agree on the blocker's own
    # wording (currency, amount, floor), not merely on how many there are.
    assert (sql_row["hard_blockers"] or []) == python_blockers


def test_match_jobs_agrees_with_hard_blockers_from_facets_under_a_market(
    store, supabase_client, seed_postings
):
    user_id = supabase_client.user_id
    _insert_search_profile(supabase_client, user_id, salary_floor_eur=90000)
    supabase_client.insert(
        "job_hunter_search_profile_markets",
        [
            {
                "user_id": user_id,
                "profile_id": supabase_client.select(
                    "job_hunter_search_profiles", params={"select": "id", "limit": "1"}
                )[0]["id"],
                "market_id": "eu",
                "query_share": 1.0,
                "locations": ["Berlin"],
                "currency": "EUR",
                "gross_base_floor": 80000,
                "remote_policy": "required",
                "relocation_policy": "none",
                "sponsorship_policy": "not_required",
                "position": 0,
            }
        ],
    )

    (posting_id,) = seed_postings(
        [
            _posting_row(
                fingerprint="equiv-market-blocked",
                url="https://boards.greenhouse.io/acme/equiv-market-blocked",
                canonical_url="https://boards.greenhouse.io/acme/equiv-market-blocked",
                company="Acme",
                title="Senior Backend Engineer",
                location="Berlin",
                description="kubernetes postgres",
                description_hash="h-equiv-market-blocked",
                remote=False,
            )
        ]
    )
    job_id = _insert_membership(supabase_client, user_id, posting_id, market_id="eu")
    facets = make_facets(remote_policy="onsite")
    store.save_job_facets(job_id, facets)

    (sql_row,) = store.match_jobs(
        preferred_roles=[], preferred_seniority=[], must_have_signals=[],
        nice_to_have_signals=[], preferred_locations=[], avoid_signals=[],
        # #243: see the equivalence test above -- no membership-free noise.
        limit=0,
    )

    market = MarketPolicy(
        id="eu",
        query_share=1.0,
        locations=["Berlin"],
        allowed_languages=[],
        salary=SalaryPolicy(currency="EUR", gross_base_floor=80000),
        remote_policy="required",
        relocation_policy="none",
        sponsorship_policy="not_required",
    )
    python_job = Job(
        source="greenhouse", title="Senior Backend Engineer", company="Acme",
        location="Berlin", remote=False, description="kubernetes postgres", market_id="eu",
    )
    python_blockers = hard_blockers_from_facets(
        facets, BlockingThresholds.for_job(python_job, _policy(salary_floor_eur=90000), market)
    )

    # Content, not just count: see the salary-only equivalence test above.
    assert (sql_row["hard_blockers"] or []) == python_blockers
