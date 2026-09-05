from job_hunter.models import CandidatePreferences, Job, SearchPolicy
from job_hunter.ranking import (
    market_priority_bonus,
    priority_score,
    profile_priority_score,
    rank_jobs,
    select_diverse_candidates,
    source_quality,
)
from tests.market_fixtures import make_market_policy


def make_policy() -> SearchPolicy:
    return SearchPolicy(
        target_titles=["senior product engineer", "staff product engineer", "senior frontend engineer"],
        positive_keywords=["react", "typescript", "next.js", "product ownership", "design system"],
        blocked_title_keywords=["junior"],
        salary_floor_eur=90000,
        thresholds={"package": 75, "possible": 65},
    )


def make_preferences() -> CandidatePreferences:
    return CandidatePreferences(
        preferred_roles=["staff product engineer", "senior frontend engineer"],
        preferred_seniority=["senior", "staff"],
        must_have_signals=["react", "typescript"],
        nice_to_have_signals=["design system", "mentorship"],
        preferred_locations=["europe", "germany"],
        avoid_signals=["on-site", "manager"],
        summary="Senior frontend/product engineer focused on remote Europe roles.",
    )


def test_product_engineer_outranks_generic_react_role():
    policy = make_policy()
    strong = Job(
        source="ashby",
        title="Staff Product Engineer",
        company="A",
        location="Remote Europe",
        remote=True,
        description="React TypeScript product ownership architecture end-to-end ownership",
    )
    generic = Job(
        source="remotive",
        title="React Developer",
        company="B",
        location="Worldwide",
        remote=True,
        description="React React React React React",
    )
    assert priority_score(strong, policy) > priority_score(generic, policy)


def test_explicit_europe_remote_outranks_unknown_location():
    policy = make_policy()
    europe = Job(source="remotive", title="Senior Frontend Engineer", location="Remote Europe", remote=True, description="React TypeScript")
    unknown = Job(source="remotive", title="Senior Frontend Engineer", location="", remote=True, description="React TypeScript")
    assert priority_score(europe, policy) > priority_score(unknown, policy)


def test_ats_url_gets_higher_source_quality_than_general_web_result():
    ats = Job(source="duckduckgo", title="Senior Product Engineer", url="https://jobs.ashbyhq.com/acme/123")
    web = Job(source="duckduckgo", title="Senior Product Engineer", url="https://example.com/jobs/123")
    assert source_quality(ats) > source_quality(web)


def test_keyword_repetition_is_capped():
    policy = make_policy()
    normal = Job(source="remotive", title="React Developer", description="React TypeScript product ownership")
    spammy = Job(source="remotive", title="React Developer", description="React " * 100)
    assert priority_score(spammy, policy) < priority_score(normal, policy) + 20


def test_rank_jobs_is_stable_for_equal_scores():
    policy = make_policy()
    jobs = [
        (2, Job(source="remotive", title="Senior Frontend Engineer", company="Beta", description="React TypeScript")),
        (1, Job(source="remotive", title="Senior Frontend Engineer", company="Acme", description="React TypeScript")),
    ]
    ranked = rank_jobs(jobs, policy)
    assert [job_id for job_id, _job, _score in ranked] == [1, 2]


def test_profile_priority_score_prefers_role_fit_over_generic_keyword_spam():
    policy = make_policy()
    preferences = make_preferences()
    preferred = Job(
        source="ashby",
        title="Staff Product Engineer",
        company="Acme",
        location="Remote Europe",
        remote=True,
        description="React TypeScript design system mentorship architecture",
    )
    generic = Job(
        source="remotive",
        title="Frontend Developer",
        company="Beta",
        location="Remote Worldwide",
        remote=True,
        description="React " * 30,
    )

    assert profile_priority_score(preferred, preferences, policy) > profile_priority_score(generic, preferences, policy)


def test_profile_priority_score_deducts_avoid_signals():
    policy = make_policy()
    preferences = make_preferences()
    safe = Job(
        source="ashby",
        title="Senior Frontend Engineer",
        company="Acme",
        location="Remote Germany",
        remote=True,
        description="React TypeScript design system",
    )
    risky = Job(
        source="ashby",
        title="Senior Frontend Manager",
        company="Beta",
        location="Berlin on-site",
        remote=False,
        description="React TypeScript design system on-site manager",
    )

    assert profile_priority_score(safe, preferences, policy) > profile_priority_score(risky, preferences, policy)


def test_profile_priority_score_counts_unique_signals_once():
    policy = make_policy()
    preferences = make_preferences()
    unique = Job(
        source="ashby",
        title="Senior Frontend Engineer",
        company="Acme",
        location="Remote Europe",
        remote=True,
        description="React TypeScript design system mentorship",
    )
    repeated = Job(
        source="ashby",
        title="Senior Frontend Engineer",
        company="Beta",
        location="Remote Europe",
        remote=True,
        description="React React React React TypeScript",
    )

    assert profile_priority_score(unique, preferences, policy) > profile_priority_score(repeated, preferences, policy)


def test_select_diverse_candidates_does_not_reserve_slots_for_weak_sources():
    ranked = [
        (101, Job(source="ashby", title="Role"), 95),
        (102, Job(source="ashby", title="Role"), 94),
        (201, Job(source="remotive", title="Role"), 70),
        (301, Job(source="hackernews", title="Role"), 40),
    ]

    selected = select_diverse_candidates(
        ranked,
        limit=3,
        minimum_per_source=0,
        max_share=0.75,
    )

    assert [job_id for job_id, _job, _score in selected] == [101, 102, 201]


def test_select_diverse_candidates_respects_max_share_when_filling():
    ranked = [
        (101, Job(source="ashby", title="Role", company="A1"), 99),
        (102, Job(source="ashby", title="Role", company="A2"), 98),
        (103, Job(source="ashby", title="Role", company="A3"), 97),
        (201, Job(source="remotive", title="Role", company="B1"), 96),
        (202, Job(source="remotive", title="Role", company="B2"), 95),
        (301, Job(source="hackernews", title="Role", company="C1"), 94),
    ]

    selected = select_diverse_candidates(ranked, limit=4, minimum_per_source=0, max_share=0.5)

    assert [job_id for job_id, _job, _score in selected] == [101, 102, 201, 202]


def test_select_diverse_candidates_preserves_rank_order_for_ties():
    ranked = [
        (2, Job(source="ashby", title="Role", company="Beta"), 90),
        (1, Job(source="ashby", title="Role", company="Acme"), 90),
        (3, Job(source="remotive", title="Role", company="Gamma"), 80),
    ]

    selected = select_diverse_candidates(ranked, limit=3, minimum_per_source=0, max_share=1.0)

    assert [job_id for job_id, _job, _score in selected] == [2, 1, 3]


def test_select_diverse_candidates_fills_budget_when_share_cap_blocks_remaining_slots():
    ranked = [
        *[(100 + index, Job(source="ashby", title="Role", company=f"A{index:02d}"), 100 - index) for index in range(20)],
        *[(200 + index, Job(source="remotive", title="Role", company=f"B{index:02d}"), 80 - index) for index in range(20)],
    ]

    selected = select_diverse_candidates(ranked, limit=35, minimum_per_source=0, max_share=0.5)

    assert len(selected) == 35
    counts = {}
    for _job_id, job, _score in selected:
        counts[job.source] = counts.get(job.source, 0) + 1
    assert counts == {"ashby": 18, "remotive": 17}


def test_frontend_heavy_full_stack_beats_backend_heavy_full_stack():
    policy = make_market_policy()
    preferences = make_preferences()
    frontend_heavy = Job(source="wellfound", title="Full-Stack Engineer", market_id="germany_eu", description="React Next.js TypeScript frontend, Node.js REST APIs and PostgreSQL")
    backend_heavy = Job(source="wellfound", title="Full-Stack Engineer", market_id="germany_eu", description="Go Java Kubernetes distributed systems event-driven backend architecture")
    assert profile_priority_score(frontend_heavy, preferences, policy) > profile_priority_score(backend_heavy, preferences, policy)


def test_london_hybrid_gets_nonzero_location_fit():
    policy = make_market_policy()
    job = Job(source="x", title="Senior Frontend Engineer", location="London - Hybrid", remote=False, market_id="london", description="React TypeScript")
    assert profile_priority_score(job, make_preferences(), policy) > 0


def test_small_quality_edge_survives_max_market_bonus_swing():
    # market_priority_bonus ranges from len(policy.markets) (index 0, germany_eu
    # = 6) down to 1 (index 5, secondary_eu_relocation): the largest possible
    # bonus delta between two configured markets in this policy is 6 - 1 = 5.
    # This test pits a *lower*-bonus candidate (secondary_eu_relocation, bonus
    # 1) with only a small, deliberate quality edge on the non-bonus axes
    # against a *higher*-bonus candidate (germany_eu, bonus 6) that is
    # otherwise slightly stronger on location fit. The quality edge here is 8
    # points (extra signal-coverage matches) against germany's 2-point location
    # advantage, netting a 6-point non-bonus edge for the weaker-bonus
    # candidate -- just enough to clear the 5-point max bonus swing. If
    # market_priority_bonus were miscalibrated to be larger than a handful of
    # points, this assertion would flip and fail, proving the bonus stays a
    # modest nudge rather than an absolute partition.
    policy = make_market_policy()
    preferences = make_preferences()

    higher_bonus_lower_quality = Job(
        source="ashby",
        title="Senior Frontend Engineer",
        company="A",
        location="Berlin",
        remote=False,
        market_id="germany_eu",  # bonus 6 (index 0)
        description="React TypeScript backend services",  # must-have signals only
    )
    lower_bonus_higher_quality = Job(
        source="ashby",
        title="Senior Frontend Engineer",
        company="B",
        location="Amsterdam",
        remote=False,
        market_id="secondary_eu_relocation",  # bonus 1 (index 5)
        description="React TypeScript design system mentorship growth",  # must-have + both nice-to-have signals
    )

    assert market_priority_bonus(higher_bonus_lower_quality, policy) - market_priority_bonus(lower_bonus_higher_quality, policy) == 5
    assert profile_priority_score(lower_bonus_higher_quality, preferences, policy) > profile_priority_score(higher_bonus_lower_quality, preferences, policy)


def test_market_priority_bonus_rewards_higher_priority_market():
    policy = make_market_policy()

    germany_job = Job(source="ashby", title="x", market_id="germany_eu")
    london_job = Job(source="ashby", title="x", market_id="london")
    no_market_job = Job(source="ashby", title="x")
    unknown_market_job = Job(source="ashby", title="x", market_id="nowhere")

    assert market_priority_bonus(germany_job, policy) > market_priority_bonus(london_job, policy) > 0
    assert market_priority_bonus(no_market_job, policy) == 0
    assert market_priority_bonus(unknown_market_job, policy) == 0


def test_specialist_board_url_source_quality_between_ats_and_generic_web():
    ats = Job(source="ashby", title="x", url="https://jobs.ashbyhq.com/acme/1")
    specialist = Job(source="duckduckgo", title="x", url="https://wellfound.com/jobs/123")
    generic = Job(source="duckduckgo", title="x", url="https://example.com/jobs/123")
    assert source_quality(ats) > source_quality(specialist) > source_quality(generic)
