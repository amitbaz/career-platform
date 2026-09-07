import pytest
from pydantic import ValidationError

from job_hunter.search_profile import SearchProfile, SearchProfileMarket


def _market(**overrides) -> SearchProfileMarket:
    base = dict(
        market_id="germany_eu",
        query_share=0.5,
        locations=["Berlin", "Germany"],
        allowed_languages=["English"],
        currency="EUR",
        gross_base_floor=90000,
        location_floors={},
        remote_policy="preferred",
        relocation_policy="selective",
        sponsorship_policy="not_required",
        direct_sources=[],
        discovery_domains=["wellfound.com"],
        query_templates=['"{role}" React'],
        role_families=[],
        enabled=True,
    )
    base.update(overrides)
    return SearchProfileMarket(**base)


def _profile(**overrides) -> SearchProfile:
    base = dict(
        timezone="Europe/Berlin",
        scheduled_hour=9,
        max_jobs_per_run=35,
        source_minimum_per_run=0,
        source_max_share=0.5,
        thresholds={"package": 75, "possible": 65},
        salary_floor_eur=90000,
        target_titles=["senior product engineer"],
        positive_keywords=["react"],
        blocked_title_keywords=["junior"],
        role_families=[],
        search_query_templates=[],
        search_domains=[],
        specialist_search_domains=[],
        specialist_query_templates=[],
        search_queries=[],
        yc_job_pages=[],
        engineering_title_keywords=["engineer"],
        engineering_title_phrases=["technical lead"],
        blocked_profession_title_phrases=["product manager"],
        specialist_board_hosts=["devjobs.co.il"],
        frontend_signals=["react"],
        backend_heavy_signals=["kubernetes"],
        max_search_queries_per_run=30,
        max_canonical_resolutions_per_run=80,
        max_learned_ats_boards_per_run=75,
        learned_ats_denylist=[],
        learned_ats_allowlist=[],
        manual_company_watch=[],
        ats={},
        markets=[_market()],
    )
    base.update(overrides)
    return SearchProfile(**base)


def test_valid_profile_constructs():
    profile = _profile()
    assert profile.markets[0].market_id == "germany_eu"


def test_query_share_must_be_between_0_and_1():
    with pytest.raises(ValidationError):
        _profile(markets=[_market(query_share=1.5)])


def test_remote_policy_must_be_known_value():
    with pytest.raises(ValidationError):
        _profile(markets=[_market(remote_policy="sometimes")])


def test_to_profile_row_excludes_markets():
    row = _profile().to_profile_row()
    assert "markets" not in row
    assert row["timezone"] == "Europe/Berlin"
    assert row["salary_floor_eur"] == 90000


def test_to_market_rows_carries_profile_id():
    rows = _profile().to_market_rows("11111111-1111-1111-1111-111111111111")
    assert len(rows) == 1
    assert rows[0]["profile_id"] == "11111111-1111-1111-1111-111111111111"
    assert rows[0]["market_id"] == "germany_eu"


def test_delivery_policy_fields_default_when_not_given():
    profile = _profile()
    assert profile.daily_offer_limit == 10
    assert profile.match_score_floor == 80


@pytest.mark.parametrize("limit", [5, 10, 20])
def test_daily_offer_limit_accepts_allowed_values(limit):
    assert _profile(daily_offer_limit=limit).daily_offer_limit == limit


@pytest.mark.parametrize("limit", [0, 1, 7, 15, 21, 100, -5])
def test_daily_offer_limit_rejects_values_outside_the_allowed_set(limit):
    with pytest.raises(ValidationError):
        _profile(daily_offer_limit=limit)


@pytest.mark.parametrize("floor", [50, 65, 80, 95])
def test_match_score_floor_accepts_values_inside_the_band(floor):
    assert _profile(match_score_floor=floor).match_score_floor == floor


@pytest.mark.parametrize("floor", [0, 49, 96, 100, 101, -1])
def test_match_score_floor_rejects_values_outside_the_band(floor):
    """0 would disable the floor silently; 100 would silence the digest."""
    with pytest.raises(ValidationError):
        _profile(match_score_floor=floor)


@pytest.mark.parametrize("field, value", [("daily_offer_limit", 7), ("match_score_floor", 100)])
def test_delivery_policy_rejects_a_bad_value_assigned_after_construction(field, value):
    """A loaded profile is edited in place, so assignment must validate too."""
    profile = _profile()
    with pytest.raises(ValidationError):
        setattr(profile, field, value)


@pytest.mark.parametrize("field, value", [("daily_offer_limit", 7), ("match_score_floor", 100)])
def test_delivery_policy_rejects_a_bad_value_assigned_after_construction(field, value):
    """A loaded profile is edited in place, so assignment must validate too."""
    profile = _profile()
    with pytest.raises(ValidationError):
        setattr(profile, field, value)


def test_to_profile_row_carries_delivery_policy():
    row = _profile(daily_offer_limit=20, match_score_floor=70).to_profile_row()
    assert row["daily_offer_limit"] == 20
    assert row["match_score_floor"] == 70
