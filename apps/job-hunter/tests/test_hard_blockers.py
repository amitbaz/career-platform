"""Deciding hard blockers from stored facets, without a model call (#127).

The comparison is per-user by construction: the facts come from the posting's
shared facets, the numbers from this user's search profile. Every test here
either proves a blocker the model no longer has to find, or proves the module
fails open on a fact the posting never established.
"""

from __future__ import annotations

import pytest

from job_hunter.hard_blockers import (
    BlockingThresholds,
    blocked_evaluation,
    hard_blockers_from_facets,
)
from job_hunter.models import Compensation, Job, JobFacets, SearchPolicy
from tests.market_fixtures import make_market


def _policy(**overrides) -> SearchPolicy:
    defaults = dict(
        target_titles=["senior product engineer"],
        positive_keywords=["react"],
        blocked_title_keywords=["junior"],
        salary_floor_eur=90000,
        thresholds={"package": 75, "possible": 65},
    )
    defaults.update(overrides)
    return SearchPolicy(**defaults)


def _job(**overrides) -> Job:
    defaults = dict(
        source="ashby",
        title="Senior Product Engineer",
        company="Acme",
        location="Remote",
        remote=True,
        description="React TypeScript remote role",
    )
    defaults.update(overrides)
    return Job(**defaults)


def _facets(**overrides) -> JobFacets:
    defaults = dict(
        seniority="senior",
        remote_policy="unknown",
        relocation_policy="unknown",
        hiring_regions=[],
        stack=["react"],
        compensation=Compensation(),
        requirements=[],
    )
    defaults.update(overrides)
    return JobFacets(**defaults)


def _annual(maximum: int, *, currency: str = "EUR", minimum: int | None = None) -> Compensation:
    return Compensation(
        disclosed=True,
        currency=currency,
        minimum=minimum,
        maximum=maximum,
        period="year",
    )


def _thresholds(**overrides) -> BlockingThresholds:
    defaults = dict(
        currency="EUR",
        salary_floor=90000,
        remote_required=True,
        relocation_allowed=False,
    )
    defaults.update(overrides)
    return BlockingThresholds(**defaults)


# --- Compensation ------------------------------------------------------------------


def test_a_disclosed_maximum_below_the_floor_blocks():
    blockers = hard_blockers_from_facets(
        _facets(compensation=_annual(70000)), _thresholds(salary_floor=90000)
    )

    assert len(blockers) == 1
    assert "70000" in blockers[0] and "90000" in blockers[0]


def test_a_disclosed_maximum_at_the_floor_does_not_block():
    # The prompt's rule is "below the floor", not "at or below": a role paying
    # exactly the floor is one the user said they would take.
    assert hard_blockers_from_facets(
        _facets(compensation=_annual(90000)), _thresholds(salary_floor=90000)
    ) == []


def test_undisclosed_compensation_does_not_block():
    assert hard_blockers_from_facets(_facets(), _thresholds()) == []


def test_a_disclosed_minimum_with_no_maximum_does_not_block():
    # The top of the range is unknown, and an unknown top is not evidence that
    # the role pays under the floor.
    compensation = Compensation(
        disclosed=True, currency="EUR", minimum=60000, maximum=None, period="year"
    )

    assert hard_blockers_from_facets(_facets(compensation=compensation), _thresholds()) == []


def test_a_maximum_in_another_currency_does_not_block():
    # Converting needs a rate the engine has no source for. The model still
    # reads the posting and can block it.
    assert hard_blockers_from_facets(
        _facets(compensation=_annual(50000, currency="USD")), _thresholds(currency="EUR")
    ) == []


@pytest.mark.parametrize("period", ["month", "hour", "day", ""])
def test_a_maximum_that_is_not_annual_does_not_block(period):
    # 5000 a month is far below a 90000 floor read as an annual figure, and
    # x12 is wrong in every market with a 13th-month convention.
    compensation = Compensation(
        disclosed=True, currency="EUR", minimum=None, maximum=5000, period=period
    )

    assert hard_blockers_from_facets(_facets(compensation=compensation), _thresholds()) == []


# --- Remote and relocation ---------------------------------------------------------


@pytest.mark.parametrize("remote_policy", ["onsite", "hybrid"])
def test_a_role_that_is_not_remote_blocks_when_remote_is_required(remote_policy):
    blockers = hard_blockers_from_facets(
        _facets(remote_policy=remote_policy), _thresholds(remote_required=True)
    )

    assert len(blockers) == 1
    assert remote_policy in blockers[0]


@pytest.mark.parametrize("remote_policy", ["onsite", "hybrid"])
def test_a_role_that_is_not_remote_does_not_block_when_the_market_allows_it(remote_policy):
    assert hard_blockers_from_facets(
        _facets(remote_policy=remote_policy), _thresholds(remote_required=False)
    ) == []


def test_an_unknown_remote_policy_does_not_block():
    assert hard_blockers_from_facets(
        _facets(remote_policy="unknown"), _thresholds(remote_required=True)
    ) == []


def test_a_role_requiring_relocation_blocks_when_relocation_is_not_allowed():
    blockers = hard_blockers_from_facets(
        _facets(relocation_policy="required"), _thresholds(relocation_allowed=False)
    )

    assert len(blockers) == 1
    assert "relocation" in blockers[0]


def test_a_role_requiring_relocation_does_not_block_when_the_market_allows_it():
    assert hard_blockers_from_facets(
        _facets(relocation_policy="required"), _thresholds(relocation_allowed=True)
    ) == []


@pytest.mark.parametrize("relocation_policy", ["offered", "not_offered", "unknown"])
def test_relocation_that_is_not_required_does_not_block(relocation_policy):
    # "offered" says the employer will help someone who moves, not that the
    # role demands it.
    assert hard_blockers_from_facets(
        _facets(relocation_policy=relocation_policy), _thresholds(relocation_allowed=False)
    ) == []


def test_every_blocker_the_facets_establish_is_reported():
    blockers = hard_blockers_from_facets(
        _facets(
            remote_policy="onsite",
            relocation_policy="required",
            compensation=_annual(50000),
        ),
        _thresholds(),
    )

    assert len(blockers) == 3


# --- The per-user half of the comparison -------------------------------------------


def test_thresholds_without_markets_come_from_the_search_profile():
    thresholds = BlockingThresholds.for_job(_job(), _policy(salary_floor_eur=120000), None)

    assert thresholds == BlockingThresholds(
        currency="EUR", salary_floor=120000, remote_required=True, relocation_allowed=False
    )


def test_the_same_facts_block_for_one_user_and_not_another():
    # The whole point of doing this per user: the facts are shared, the floor
    # is not, so the result may never be cached across users.
    facets = _facets(compensation=_annual(100000))

    strict = BlockingThresholds.for_job(_job(), _policy(salary_floor_eur=120000), None)
    lenient = BlockingThresholds.for_job(_job(), _policy(salary_floor_eur=90000), None)

    assert hard_blockers_from_facets(facets, strict) != []
    assert hard_blockers_from_facets(facets, lenient) == []


def test_a_market_supplies_its_own_currency_floor_and_work_mode_rules():
    market = make_market(
        "israel_remote",
        0.25,
        currency="ILS",
        floor=420000,
        remote_policy="required",
        relocation_policy="none",
    )

    thresholds = BlockingThresholds.for_job(_job(), _policy(), market)

    assert thresholds == BlockingThresholds(
        currency="ILS", salary_floor=420000, remote_required=True, relocation_allowed=False
    )


def test_a_permissive_market_requires_neither_remote_nor_staying_put():
    market = make_market("germany_eu", 0.35, remote_policy="preferred", relocation_policy="selective")

    thresholds = BlockingThresholds.for_job(_job(), _policy(), market)

    assert thresholds.remote_required is False
    assert thresholds.relocation_allowed is True


def test_a_market_city_floor_applies_to_a_job_in_that_city():
    # Reuses `salary_floor_for_job`, so a location-specific floor is the one
    # compared against rather than the market's headline number.
    market = make_market(
        "us_nyc_sf",
        0.10,
        locations=["New York", "San Francisco"],
        currency="USD",
        floor=180000,
        location_floors={"San Francisco": 200000},
    )

    thresholds = BlockingThresholds.for_job(_job(location="San Francisco"), _policy(), market)

    assert thresholds.salary_floor == 200000


# --- The record a block produces ---------------------------------------------------


def test_a_blocked_evaluation_records_the_same_outcome_as_a_model_block():
    job = _job(market_id="germany_eu", content_confidence="official_ats")

    evaluation = blocked_evaluation(job, ["role is onsite, not remote"])

    assert evaluation.decision == "blocked"
    assert evaluation.hard_blockers == ["role is onsite, not remote"]
    assert evaluation.total_score == 0
    assert evaluation.raw_model_score == 0
    assert set(evaluation.scores) == {
        "role_seniority",
        "technical",
        "product_architecture",
        "career_direction",
        "location_language",
        "company_environment",
    }
    assert all(value == 0 for value in evaluation.scores.values())
    assert evaluation.market_id == "germany_eu"
    assert evaluation.content_confidence == "official_ats"
    # No model produced this evaluation, and claiming one did would make the
    # per-model quality numbers lie.
    assert evaluation.model == ""
