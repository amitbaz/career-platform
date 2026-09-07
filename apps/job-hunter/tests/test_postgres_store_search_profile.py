from job_hunter.postgres_store import PostgresJobStore
from job_hunter.search_profile import SearchProfile, SearchProfileMarket


class FakeSupabaseClient:
    def __init__(self):
        self.user_id = "u1"
        self.rows = {"job_hunter_search_profiles": [], "job_hunter_search_profile_markets": []}
        self._next_id = 1

    def _new_id(self) -> str:
        value = f"id-{self._next_id}"
        self._next_id += 1
        return value

    def select(self, table, *, params=None):
        params = params or {}
        rows = self.rows[table]
        if "user_id" in params:
            rows = [r for r in rows if r["user_id"] == params["user_id"].removeprefix("eq.")]
        if "profile_id" in params:
            rows = [r for r in rows if r["profile_id"] == params["profile_id"].removeprefix("eq.")]
        return rows

    def upsert(self, table, rows, *, on_conflict):
        written = []
        for row in rows:
            row = dict(row)
            row.setdefault("id", self._new_id())
            existing = [
                r for r in self.rows[table]
                if all(r.get(k) == row.get(k) for k in on_conflict.split(","))
            ]
            if existing:
                existing[0].update(row)
                written.append(existing[0])
            else:
                self.rows[table].append(row)
                written.append(row)
        return written

    def delete(self, table, *, params):
        key = params["profile_id"].removeprefix("eq.")
        self.rows[table] = [r for r in self.rows[table] if r.get("profile_id") != key]


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
