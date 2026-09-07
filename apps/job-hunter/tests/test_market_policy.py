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


# --- explicit hiring scope (issue #16) ------------------------------------
#
# A listing variant's location label is strong evidence, but the posting's own
# statement of who it will hire is stronger. These cases pin both directions:
# explicit scope must be able to widen attribution past a narrow label, and it
# must stop an incidental region mention from widening it.

_LINEAR_DESCRIPTION = (
    "Linear is a fully remote company. This role is open to candidates based "
    "in the US and Europe, and can be performed from anywhere within those "
    "regions. React and TypeScript."
)

_US_ONLY_DESCRIPTION = (
    "This role is open to candidates based in the United States. Our "
    "engineering team collaborates with partners across Europe. React and "
    "TypeScript."
)


def test_explicit_europe_eligibility_beats_a_north_america_location_label():
    policy = make_market_policy()
    job = Job(
        source="x",
        title="Senior / Staff Product Engineer",
        location="North America",
        remote=True,
        description=_LINEAR_DESCRIPTION,
    )
    assert attribute_market(job, policy.markets) == "germany_eu"


def test_stated_hiring_region_does_not_rescue_an_explicitly_non_remote_job():
    """Scope says *where* an employer hires, never *whether* work is remote.

    The unattributed path exists so the legacy non-remote hard blocker still
    reaches a job no market's locations name. Letting a stated region alone
    attribute such a job would route an onsite role in an uncovered city into
    a market whose remote policy never checks work mode.
    """
    policy = make_market_policy()
    job = Job(
        source="x",
        title="Senior Product Engineer",
        location="Austin, TX",
        remote=False,
        description=(
            "Onsite role. Candidates must be located in the United States."
        ),
    )
    assert attribute_market(job, policy.markets) is None


def test_us_only_role_is_not_attributed_to_germany_eu():
    policy = make_market_policy()
    job = Job(
        source="x",
        title="Senior Product Engineer",
        location="Remote (US)",
        remote=True,
        description=_US_ONLY_DESCRIPTION,
    )
    assert attribute_market(job, policy.markets) == "us_nyc_sf"


def test_a_scope_no_market_covers_does_not_push_a_remote_job_out_of_the_system():
    """Unattributed is weaker filtering, not stronger, so it is not the answer.

    A job with no market falls through to the legacy global prefilter, which
    applies none of the market rules -- salary floor, language, sponsorship,
    employment type -- and only blocks an explicitly non-remote job. When a
    posting's stated regions exclude every configured market there is no good
    answer, and the least bad one is the ordinary evidence path.
    """
    policy = make_market_policy()
    markets = [market for market in policy.markets if market.id != "us_nyc_sf"]
    job = Job(
        source="x",
        title="Senior Product Engineer",
        location="Remote (US)",
        remote=True,
        description=_US_ONLY_DESCRIPTION,
    )
    assert attribute_market(job, markets) == "germany_eu"


def test_explicit_scope_outranks_a_contradicting_location_label():
    policy = make_market_policy()
    job = Job(
        source="x",
        title="Senior Product Engineer",
        location="New York, NY",
        remote=True,
        description=(
            "This role is open to candidates based in Europe only. React and "
            "TypeScript."
        ),
    )
    assert attribute_market(job, policy.markets) == "germany_eu"


def test_incidental_region_prose_does_not_widen_attribution():
    """Without eligibility language, a Europe mention stays weak evidence.

    The job still lands in germany_eu here -- via the existing remote-scope
    tier, not via hiring scope -- so this pins that the new tier did not
    quietly become the only thing attributing European roles.
    """
    policy = make_market_policy()
    job = Job(
        source="x",
        title="Senior Product Engineer",
        location="Remote",
        remote=True,
        description="Our engineering team spans Europe. React and TypeScript.",
    )
    assert attribute_market(job, policy.markets) == "germany_eu"


def test_global_posting_still_falls_back_rather_than_being_dropped():
    policy = make_market_policy()
    job = Job(
        source="x",
        title="Senior Product Engineer",
        location="Remote",
        remote=True,
        description="Work from anywhere in the world. React and TypeScript.",
    )
    assert attribute_market(job, policy.markets) == "germany_eu"


def test_scope_membership_does_not_flatten_evidence_between_in_scope_markets():
    """Naming a region wins the market the field, not the tie inside it.

    A posting open to all of Europe still belongs in the European market its
    location label names, so the hiring-scope tier has to add to the ordinary
    evidence rather than replace it.
    """
    policy = make_market_policy()
    job = Job(
        source="x",
        title="Senior Product Engineer",
        location="London, UK",
        remote=True,
        description=(
            "This role is open to candidates based in Europe. React and TypeScript."
        ),
    )
    assert attribute_market(job, policy.markets) == "london"


def test_colleague_location_prose_does_not_delete_the_located_market():
    """A false eligibility read must not drop the market the label names.

    Dropping happens before scoring, so a mis-read clause would not merely add
    noise -- it would remove the right answer from the field entirely.
    """
    policy = make_market_policy()
    job = Job(
        source="x",
        title="Senior Product Engineer",
        location="Tel Aviv, Israel",
        remote=True,
        description=(
            "Remote position. Our employees are located in the US and Germany. "
            "React and TypeScript."
        ),
    )
    assert attribute_market(job, policy.markets) == "israel_remote"
