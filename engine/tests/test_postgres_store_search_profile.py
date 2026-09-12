from engine.postgres_store import PostgresJobStore
from engine.search_profile import SearchProfile, SearchProfileMarket
from tests.fake_supabase_client import FakeSupabaseClient


def _profile() -> SearchProfile:
    return SearchProfile(
        timezone="Europe/Berlin",
        scheduled_hour=9,
        max_jobs_per_run=35,
        source_minimum_per_run=0,
        source_max_share=0.5,
        thresholds={},
        salary_floor_eur=90000,
        max_search_queries_per_run=30,
        max_canonical_resolutions_per_run=80,
        max_learned_ats_boards_per_run=75,
        markets=[
            SearchProfileMarket(
                market_id="germany_eu",
                query_share=0.5,
                currency="EUR",
                gross_base_floor=90000,
                remote_policy="preferred",
                relocation_policy="selective",
                sponsorship_policy="not_required",
            )
        ],
    )


def test_save_then_get_search_profile_round_trips():
    client = FakeSupabaseClient()
    store = PostgresJobStore(client)

    profile_id = store.save_search_profile(_profile())

    result = store.get_search_profile()
    assert result is not None
    profile_row, market_rows = result
    assert profile_row["id"] == profile_id
    assert profile_row["timezone"] == "Europe/Berlin"
    assert len(market_rows) == 1
    assert market_rows[0]["market_id"] == "germany_eu"


def test_get_search_profile_returns_none_when_absent():
    client = FakeSupabaseClient()
    store = PostgresJobStore(client)
    assert store.get_search_profile() is None


def _market(market_id: str, **overrides) -> SearchProfileMarket:
    defaults = dict(
        market_id=market_id,
        query_share=0.5,
        currency="EUR",
        gross_base_floor=90000,
        remote_policy="preferred",
        relocation_policy="selective",
        sponsorship_policy="not_required",
    )
    defaults.update(overrides)
    return SearchProfileMarket(**defaults)


def test_save_search_profile_preserves_declared_market_order_across_resave():
    """Re-saving a profile (e.g. an edit) must not scramble market order.

    save_search_profile deletes and re-inserts every market row in one
    batch, so a real Postgres/PostgREST insert would give every row in
    that batch an identical created_at -- without an explicit position
    column, get_search_profile's order would then be an unpredictable
    permutation (tiebreak on a random id) rather than the declared order.
    Saving the same three markets twice, in the same order, is the exact
    scenario that would previously have been at risk.
    """
    client = FakeSupabaseClient()
    store = PostgresJobStore(client)
    declared_order = ["germany_eu", "israel_remote", "us_nyc_sf"]
    profile = _profile()
    profile.markets = [_market(market_id) for market_id in declared_order]

    store.save_search_profile(profile)
    store.save_search_profile(profile)

    _, market_rows = store.get_search_profile()
    assert [row["market_id"] for row in market_rows] == declared_order


def test_save_search_profile_replaces_markets():
    client = FakeSupabaseClient()
    store = PostgresJobStore(client)
    store.save_search_profile(_profile())

    second = _profile()
    second.markets = [
        SearchProfileMarket(
            market_id="israel_remote",
            query_share=0.5,
            currency="ILS",
            gross_base_floor=420000,
            remote_policy="required",
            relocation_policy="none",
            sponsorship_policy="not_required",
        )
    ]
    store.save_search_profile(second)

    _, market_rows = store.get_search_profile()
    assert [row["market_id"] for row in market_rows] == ["israel_remote"]


def test_delivery_policy_round_trips():
    client = FakeSupabaseClient()
    store = PostgresJobStore(client)
    profile = _profile()
    profile.daily_offer_limit = 20
    profile.match_score_floor = 70

    store.save_search_profile(profile)

    profile_row, _ = store.get_search_profile()
    assert profile_row["daily_offer_limit"] == 20
    assert profile_row["match_score_floor"] == 70


def test_delivery_policy_defaults_round_trip():
    client = FakeSupabaseClient()
    store = PostgresJobStore(client)

    store.save_search_profile(_profile())

    profile_row, _ = store.get_search_profile()
    assert profile_row["daily_offer_limit"] == 10
    assert profile_row["match_score_floor"] == 80
