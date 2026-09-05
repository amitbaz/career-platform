import pytest
from dataclasses import replace

from job_hunter.market_policy import attribute_market, market_by_id, salary_floor_for_job
from job_hunter.models import Job
from tests.market_fixtures import make_market_policy


def test_london_hybrid_maps_to_london():
    policy = make_market_policy()
    job = Job(source="x", title="Senior Frontend Engineer", location="London, UK - Hybrid", remote=False)
    assert attribute_market(job, policy.markets) == "london"


def test_remote_germany_beats_london_query_hint():
    policy = make_market_policy()
    job = Job(source="x", title="Senior Frontend Engineer", location="Remote Germany", remote=True, market_hint="london")
    assert attribute_market(job, policy.markets) == "germany_eu"


def test_city_specific_salary_floors():
    policy = make_market_policy()
    us = market_by_id(policy, "us_nyc_sf")
    secondary = market_by_id(policy, "secondary_eu_relocation")
    assert salary_floor_for_job(Job(source="x", title="x", location="New York City"), us) == 180000
    assert salary_floor_for_job(Job(source="x", title="x", location="San Francisco Bay Area"), us) == 200000
    assert salary_floor_for_job(Job(source="x", title="x", location="Amsterdam"), secondary) == 90000
    assert salary_floor_for_job(Job(source="x", title="x", location="Paris"), secondary) == 80000
    assert salary_floor_for_job(Job(source="x", title="x", location="Barcelona"), secondary) == 70000


def test_israeli_remote_role_maps_to_israel_remote_market():
    policy = make_market_policy()
    job = Job(
        source="x",
        title="Senior Frontend Engineer",
        location="Remote",
        remote=True,
        description="This fully remote position requires you to be based in Israel or Tel Aviv.",
    )
    assert attribute_market(job, policy.markets) == "israel_remote"


def test_singapore_onsite_role_maps_to_singapore():
    policy = make_market_policy()
    job = Job(source="x", title="Senior Frontend Engineer", location="Singapore", remote=False)
    assert attribute_market(job, policy.markets) == "singapore"


def test_paris_role_maps_to_secondary_eu_relocation():
    policy = make_market_policy()
    job = Job(source="x", title="Senior Frontend Engineer", location="Paris, France", remote=False)
    assert attribute_market(job, policy.markets) == "secondary_eu_relocation"


def test_ambiguous_remote_europe_resolves_to_the_market_that_declares_it():
    policy = make_market_policy()
    job = Job(source="x", title="Senior Frontend Engineer", location="Remote (Europe)", remote=True)
    assert attribute_market(job, policy.markets) == "germany_eu"


def test_sponsorship_language_without_remote_scope_ties_to_named_market():
    policy = make_market_policy()
    job = Job(
        source="x",
        title="Senior Frontend Engineer",
        location="Onsite",
        remote=False,
        description="We are hiring for our Singapore office and can sponsor a visa for the right candidate.",
    )
    assert attribute_market(job, policy.markets) == "singapore"


def test_market_hint_used_only_when_no_stronger_evidence_exists():
    policy = make_market_policy()
    job = Job(source="x", title="Senior Frontend Engineer", location="", market_hint="israel_remote")
    assert attribute_market(job, policy.markets) == "israel_remote"


def test_no_evidence_falls_back_to_first_enabled_market_in_configured_order():
    policy = make_market_policy()
    job = Job(source="x", title="Senior Frontend Engineer", location="")
    assert attribute_market(job, policy.markets) == "germany_eu"


@pytest.mark.parametrize(
    ("location", "description"),
    [
        ("Bangalore, India (Onsite)", "React and TypeScript."),
        ("Austin, TX", "Onsite 5 days a week. React and TypeScript."),
    ],
)
def test_explicitly_non_remote_job_with_no_market_evidence_is_unattributed(location, description):
    """A non-remote job in a place no market names is compatible with no
    market; forcing it into the first enabled one would drop the non-remote
    hard blocker entirely."""
    policy = make_market_policy()
    job = Job(
        source="x",
        title="Senior Frontend Engineer",
        location=location,
        remote=False,
        description=description,
    )
    assert attribute_market(job, policy.markets) is None


def test_non_remote_job_still_uses_its_market_hint_when_no_location_evidence():
    """Only *zero* evidence triggers the unattributed path; a query hint is
    still evidence."""
    policy = make_market_policy()
    job = Job(
        source="x",
        title="Senior Frontend Engineer",
        location="Bangalore, India",
        remote=False,
        market_hint="singapore",
    )
    assert attribute_market(job, policy.markets) == "singapore"


def test_remote_unknown_job_with_no_evidence_still_falls_back():
    """Market uncertainty alone must not drop a job."""
    policy = make_market_policy()
    job = Job(source="x", title="Senior Frontend Engineer", location="Bangalore, India")
    assert attribute_market(job, policy.markets) == "germany_eu"


def test_fallback_skips_disabled_markets():
    policy = make_market_policy()
    markets = [
        replace(market, enabled=False) if market.id == "germany_eu" else market
        for market in policy.markets
    ]
    job = Job(source="x", title="Senior Frontend Engineer", location="")
    assert attribute_market(job, markets) == "israel_remote"


def test_market_by_id_returns_none_for_unknown_id():
    policy = make_market_policy()
    assert market_by_id(policy, "does-not-exist") is None


def test_salary_floor_falls_back_to_gross_base_floor_for_unlisted_city():
    policy = make_market_policy()
    secondary = market_by_id(policy, "secondary_eu_relocation")
    assert salary_floor_for_job(Job(source="x", title="x", location="Lisbon"), secondary) == 70000
