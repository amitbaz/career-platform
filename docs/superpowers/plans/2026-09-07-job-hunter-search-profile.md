# Job Hunter Search Profile Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move Job Hunter's search configuration out of `config/search.yml` and hardcoded Python defaults into a validated, per-user Postgres record, so two users with different occupations/markets get different discovery and ranking behavior.

**Architecture:** Two new Postgres tables (`job_hunter_search_profiles`, `job_hunter_search_profile_markets`) hold what `config/search.yml` used to hold, plus three lists that today live as hardcoded constants in `ranking.py`. A Pydantic model validates a profile before it is written. On read, `config.py` fetches the user's row and market rows and reshapes them into the exact same `dict` shape `config.py`'s existing YAML-derived parsing functions (`_parse_markets`, `_parse_manual_company_watch`, `_parse_learned_ats_denylist`, etc.) already consume, so that validation logic is reused rather than duplicated. `ranking.py` stops reading its three lists from module constants and reads them from the loaded `SearchPolicy` instead.

**Tech Stack:** Python 3.12, Pydantic v2 (new dependency), Supabase/PostgREST via the existing `SupabaseClient`, pytest.

**Spec:** `docs/superpowers/specs/2026-09-07-job-hunter-search-profile-design.md`

## Global Constraints

- One row per user in `job_hunter_search_profiles` (`unique (user_id)`), overwritten in place on edit — no profile history.
- Markets are a separate child table (`job_hunter_search_profile_markets`), FK to the profile.
- Both tables follow the existing `job_hunter_*` RLS convention: `user_id uuid not null references auth.users(id) on delete cascade`, four `_own` policies with the wrapped `(select auth.uid()) = user_id` form.
- Both `_SPECIALIST_BOARD_HOSTS` and `_FRONTEND_SIGNALS`/`_BACKEND_HEAVY_SIGNALS` in `ranking.py` move into the profile.
- No new CLI plumbing for `user_id` — it is already resolved via `load_supabase_settings().user_id` before any config is loaded.
- `config/search.yml` and the `--config` CLI flag are deleted once the loader no longer needs them.

---

### Task 1: Migration — `job_hunter_search_profiles` and `job_hunter_search_profile_markets`

**Files:**
- Create: `supabase/migrations/202609070003_job_hunter_search_profile.sql`
- Test: `supabase/tests/pgtap/job_hunter_search_profile_isolation.sql`

**Interfaces:**
- Produces: two tables other tasks read/write through `SupabaseClient.select`/`upsert`.

- [ ] **Step 1: Write the migration**

```sql
-- 202609070003_job_hunter_search_profile.sql
-- Per-user search configuration (issue #71). One profile row per user;
-- markets are a child table so each market stays independently RLS-checkable.

create table public.job_hunter_search_profiles (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null unique references auth.users(id) on delete cascade,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),

  timezone text not null,
  scheduled_hour smallint not null,
  max_jobs_per_run integer not null,
  source_minimum_per_run integer not null,
  source_max_share double precision not null,
  thresholds jsonb not null default '{}',
  salary_floor_eur integer not null,

  target_titles text[] not null default '{}',
  positive_keywords text[] not null default '{}',
  blocked_title_keywords text[] not null default '{}',
  role_families text[] not null default '{}',
  search_query_templates text[] not null default '{}',
  search_domains text[] not null default '{}',
  specialist_search_domains text[] not null default '{}',
  specialist_query_templates text[] not null default '{}',
  search_queries text[] not null default '{}',
  yc_job_pages text[] not null default '{}',

  engineering_title_keywords text[] not null default '{}',
  engineering_title_phrases text[] not null default '{}',
  blocked_profession_title_phrases text[] not null default '{}',
  specialist_board_hosts text[] not null default '{}',
  frontend_signals text[] not null default '{}',
  backend_heavy_signals text[] not null default '{}',

  max_search_queries_per_run integer not null,
  max_canonical_resolutions_per_run integer not null,
  max_learned_ats_boards_per_run integer not null,
  learned_ats_denylist text[] not null default '{}',
  learned_ats_allowlist text[] not null default '{}',

  manual_company_watch jsonb not null default '[]',
  ats jsonb not null default '{}'
);

alter table public.job_hunter_search_profiles enable row level security;

create policy select_own on public.job_hunter_search_profiles
  for select to authenticated using ((select auth.uid()) = user_id);
create policy insert_own on public.job_hunter_search_profiles
  for insert to authenticated with check ((select auth.uid()) = user_id);
create policy update_own on public.job_hunter_search_profiles
  for update to authenticated using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);
create policy delete_own on public.job_hunter_search_profiles
  for delete to authenticated using ((select auth.uid()) = user_id);


create table public.job_hunter_search_profile_markets (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  profile_id uuid not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),

  market_id text not null,
  query_share double precision not null,
  locations text[] not null default '{}',
  allowed_languages text[] not null default '{}',
  currency text not null,
  gross_base_floor integer not null,
  location_floors jsonb not null default '{}',
  remote_policy text not null,
  relocation_policy text not null,
  sponsorship_policy text not null,
  direct_sources text[] not null default '{}',
  discovery_domains text[] not null default '{}',
  query_templates text[] not null default '{}',
  role_families text[] not null default '{}',
  enabled boolean not null default true,

  unique (profile_id, market_id),
  unique (id, user_id),
  foreign key (profile_id, user_id)
    references public.job_hunter_search_profiles (id, user_id) on delete cascade,

  constraint job_hunter_search_profile_markets_remote_policy_check
    check (remote_policy in ('preferred', 'required', 'allowed')),
  constraint job_hunter_search_profile_markets_relocation_policy_check
    check (relocation_policy in ('none', 'selective', 'allowed')),
  constraint job_hunter_search_profile_markets_sponsorship_policy_check
    check (sponsorship_policy in ('not_required', 'required'))
);

alter table public.job_hunter_search_profile_markets enable row level security;

create policy select_own on public.job_hunter_search_profile_markets
  for select to authenticated using ((select auth.uid()) = user_id);
create policy insert_own on public.job_hunter_search_profile_markets
  for insert to authenticated with check ((select auth.uid()) = user_id);
create policy update_own on public.job_hunter_search_profile_markets
  for update to authenticated using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);
create policy delete_own on public.job_hunter_search_profile_markets
  for delete to authenticated using ((select auth.uid()) = user_id);

create index job_hunter_search_profile_markets_profile_id_idx
  on public.job_hunter_search_profile_markets (profile_id);
```

- [ ] **Step 2: Apply the migration locally**

Run: `supabase db reset`
Expected: migration applies cleanly with no errors.

- [ ] **Step 3: Write the pgTAP isolation test**

```sql
-- supabase/tests/pgtap/job_hunter_search_profile_isolation.sql
begin;
select plan(14);

select tests.create_supabase_user('user_a');
select tests.create_supabase_user('user_b');

select tests.authenticate_as('user_a');

insert into public.job_hunter_search_profiles
  (user_id, timezone, scheduled_hour, max_jobs_per_run, source_minimum_per_run,
   source_max_share, salary_floor_eur, max_search_queries_per_run,
   max_canonical_resolutions_per_run, max_learned_ats_boards_per_run)
values
  (tests.get_supabase_uid('user_a'), 'Europe/Berlin', 9, 35, 0, 0.5, 90000, 30, 80, 75)
returning id as profile_id \gset

select ok(
  (select count(*) from public.job_hunter_search_profiles) = 1,
  'user_a can insert their own profile'
);

select tests.authenticate_as('user_b');

select is(
  (select count(*)::int from public.job_hunter_search_profiles),
  0,
  'user_b cannot see user_a''s profile'
);

select throws_ok(
  $$ insert into public.job_hunter_search_profiles
       (user_id, timezone, scheduled_hour, max_jobs_per_run, source_minimum_per_run,
        source_max_share, salary_floor_eur, max_search_queries_per_run,
        max_canonical_resolutions_per_run, max_learned_ats_boards_per_run)
     values
       (tests.get_supabase_uid('user_a'), 'Europe/Berlin', 9, 35, 0, 0.5, 90000, 30, 80, 75) $$,
  '42501',
  null,
  'user_b cannot insert a profile owned by user_a'
);

select tests.authenticate_as('user_a');

insert into public.job_hunter_search_profile_markets
  (user_id, profile_id, market_id, query_share, currency, gross_base_floor,
   remote_policy, relocation_policy, sponsorship_policy)
values
  (tests.get_supabase_uid('user_a'), :'profile_id', 'germany_eu', 0.5, 'EUR', 90000,
   'preferred', 'selective', 'not_required')
returning id as market_id_pk \gset

select ok(
  (select count(*) from public.job_hunter_search_profile_markets) = 1,
  'user_a can insert a market row for their own profile'
);

select tests.authenticate_as('user_b');

select is(
  (select count(*)::int from public.job_hunter_search_profile_markets),
  0,
  'user_b cannot see user_a''s market row'
);

select throws_ok(
  format(
    $$ insert into public.job_hunter_search_profile_markets
         (user_id, profile_id, market_id, query_share, currency, gross_base_floor,
          remote_policy, relocation_policy, sponsorship_policy)
       values
         (tests.get_supabase_uid('user_b'), %L, 'israel_remote', 0.5, 'ILS', 420000,
          'required', 'none', 'not_required') $$,
    :'profile_id'
  ),
  '23503',
  null,
  'user_b cannot attach a market row to user_a''s profile (foreign key, not just RLS)'
);

select tests.authenticate_as(null);

select throws_ok(
  $$ select * from public.job_hunter_search_profiles $$,
  '42501',
  null,
  'anon cannot read search profiles'
);

select throws_ok(
  $$ select * from public.job_hunter_search_profile_markets $$,
  '42501',
  null,
  'anon cannot read search profile markets'
);

select tests.authenticate_as('user_a');

select is(
  (select count(*)::int from public.job_hunter_search_profiles),
  1,
  'user_a''s profile is still there and readable'
);

select bag_eq(
  $$ select policyname from pg_policies where tablename = 'job_hunter_search_profiles' $$,
  ARRAY['select_own', 'insert_own', 'update_own', 'delete_own'],
  'job_hunter_search_profiles has exactly the four expected RLS policies'
);

select bag_eq(
  $$ select policyname from pg_policies where tablename = 'job_hunter_search_profile_markets' $$,
  ARRAY['select_own', 'insert_own', 'update_own', 'delete_own'],
  'job_hunter_search_profile_markets has exactly the four expected RLS policies'
);

select ok(
  (select relrowsecurity from pg_class where relname = 'job_hunter_search_profiles'),
  'RLS is enabled on job_hunter_search_profiles'
);

select ok(
  (select relrowsecurity from pg_class where relname = 'job_hunter_search_profile_markets'),
  'RLS is enabled on job_hunter_search_profile_markets'
);

select * from finish();
rollback;
```

- [ ] **Step 4: Run the isolation test**

Run: `supabase test db supabase/tests/pgtap/job_hunter_search_profile_isolation.sql`
Expected: PASS, plan 14/14.

- [ ] **Step 5: Commit**

```bash
git add supabase/migrations/202609070003_job_hunter_search_profile.sql supabase/tests/pgtap/job_hunter_search_profile_isolation.sql
git commit -m "feat(db): add job_hunter_search_profiles and job_hunter_search_profile_markets"
```

---

### Task 2: `SearchProfile` Pydantic model

**Files:**
- Create: `apps/job-hunter/src/job_hunter/search_profile.py`
- Test: `apps/job-hunter/tests/test_search_profile.py`
- Modify: `apps/job-hunter/pyproject.toml`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `SearchProfileMarket(BaseModel)`, `SearchProfile(BaseModel)`, `SearchProfile.to_profile_row() -> dict`, `SearchProfile.to_market_rows(profile_id: str) -> list[dict]` — used by Task 3's store methods and Task 7's migration script.

- [ ] **Step 1: Add the Pydantic dependency**

In `apps/job-hunter/pyproject.toml`, add to `dependencies`:

```toml
dependencies = [
    "requests>=2.31",
    "beautifulsoup4>=4.12",
    "PyYAML>=6.0",
    "reportlab>=4.1",
    "google-auth>=2.40",
    "google-auth-oauthlib>=1.2",
    "PyJWT[crypto]>=2.8",
    "pydantic>=2.7",
]
```

- [ ] **Step 2: Write the failing test**

```python
# apps/job-hunter/tests/test_search_profile.py
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
```

- [ ] **Step 3: Run test to verify it fails**

Run: `apps/job-hunter/.venv/bin/pytest apps/job-hunter/tests/test_search_profile.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'job_hunter.search_profile'`

- [ ] **Step 4: Write the implementation**

```python
# apps/job-hunter/src/job_hunter/search_profile.py
from __future__ import annotations

from pydantic import BaseModel, Field

_REMOTE_POLICIES = {"preferred", "required", "allowed"}
_RELOCATION_POLICIES = {"none", "selective", "allowed"}
_SPONSORSHIP_POLICIES = {"not_required", "required"}


class SearchProfileMarket(BaseModel):
    market_id: str = Field(min_length=1)
    query_share: float = Field(ge=0, le=1)
    locations: list[str] = Field(default_factory=list)
    allowed_languages: list[str] = Field(default_factory=list)
    currency: str = Field(min_length=1)
    gross_base_floor: int = Field(gt=0)
    location_floors: dict[str, int] = Field(default_factory=dict)
    remote_policy: str
    relocation_policy: str
    sponsorship_policy: str
    direct_sources: list[str] = Field(default_factory=list)
    discovery_domains: list[str] = Field(default_factory=list)
    query_templates: list[str] = Field(default_factory=list)
    role_families: list[str] = Field(default_factory=list)
    enabled: bool = True

    def model_post_init(self, __context) -> None:
        if self.remote_policy not in _REMOTE_POLICIES:
            raise ValueError(f"invalid remote_policy: {self.remote_policy!r}")
        if self.relocation_policy not in _RELOCATION_POLICIES:
            raise ValueError(f"invalid relocation_policy: {self.relocation_policy!r}")
        if self.sponsorship_policy not in _SPONSORSHIP_POLICIES:
            raise ValueError(f"invalid sponsorship_policy: {self.sponsorship_policy!r}")

    def to_market_row(self, profile_id: str, user_id: str) -> dict:
        return {
            "user_id": user_id,
            "profile_id": profile_id,
            "market_id": self.market_id,
            "query_share": self.query_share,
            "locations": self.locations,
            "allowed_languages": self.allowed_languages,
            "currency": self.currency,
            "gross_base_floor": self.gross_base_floor,
            "location_floors": self.location_floors,
            "remote_policy": self.remote_policy,
            "relocation_policy": self.relocation_policy,
            "sponsorship_policy": self.sponsorship_policy,
            "direct_sources": self.direct_sources,
            "discovery_domains": self.discovery_domains,
            "query_templates": self.query_templates,
            "role_families": self.role_families,
            "enabled": self.enabled,
        }


class SearchProfile(BaseModel):
    timezone: str = Field(min_length=1)
    scheduled_hour: int = Field(ge=0, le=23)
    max_jobs_per_run: int = Field(gt=0)
    source_minimum_per_run: int = Field(ge=0)
    source_max_share: float = Field(ge=0)
    thresholds: dict = Field(default_factory=dict)
    salary_floor_eur: int = Field(ge=0)

    target_titles: list[str] = Field(default_factory=list)
    positive_keywords: list[str] = Field(default_factory=list)
    blocked_title_keywords: list[str] = Field(default_factory=list)
    role_families: list[str] = Field(default_factory=list)
    search_query_templates: list[str] = Field(default_factory=list)
    search_domains: list[str] = Field(default_factory=list)
    specialist_search_domains: list[str] = Field(default_factory=list)
    specialist_query_templates: list[str] = Field(default_factory=list)
    search_queries: list[str] = Field(default_factory=list)
    yc_job_pages: list[str] = Field(default_factory=list)

    engineering_title_keywords: list[str] = Field(default_factory=list)
    engineering_title_phrases: list[str] = Field(default_factory=list)
    blocked_profession_title_phrases: list[str] = Field(default_factory=list)
    specialist_board_hosts: list[str] = Field(default_factory=list)
    frontend_signals: list[str] = Field(default_factory=list)
    backend_heavy_signals: list[str] = Field(default_factory=list)

    max_search_queries_per_run: int = Field(gt=0)
    max_canonical_resolutions_per_run: int = Field(gt=0)
    max_learned_ats_boards_per_run: int = Field(gt=0)
    learned_ats_denylist: list[str] = Field(default_factory=list)
    learned_ats_allowlist: list[str] = Field(default_factory=list)

    manual_company_watch: list[dict] = Field(default_factory=list)
    ats: dict = Field(default_factory=dict)

    markets: list[SearchProfileMarket] = Field(default_factory=list)

    def to_profile_row(self) -> dict:
        row = self.model_dump()
        row.pop("markets")
        return row

    def to_market_rows(self, profile_id: str, user_id: str = "") -> list[dict]:
        return [market.to_market_row(profile_id, user_id) for market in self.markets]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `apps/job-hunter/.venv/bin/pytest apps/job-hunter/tests/test_search_profile.py -v`
Expected: PASS (5 passed)

- [ ] **Step 6: Commit**

```bash
git add apps/job-hunter/pyproject.toml apps/job-hunter/src/job_hunter/search_profile.py apps/job-hunter/tests/test_search_profile.py
git commit -m "feat: add SearchProfile Pydantic model"
```

---

### Task 3: Store methods — `get_search_profile` and `save_search_profile`

**Files:**
- Modify: `apps/job-hunter/src/job_hunter/postgres_store.py`
- Test: `apps/job-hunter/tests/test_postgres_store_search_profile.py`

**Interfaces:**
- Consumes: `SearchProfile`, `SearchProfileMarket` from Task 2 (`job_hunter.search_profile`).
- Produces: `PostgresJobStore.get_search_profile() -> tuple[dict, list[dict]] | None` (profile row dict, list of market row dicts), `PostgresJobStore.save_search_profile(profile: SearchProfile) -> str` (returns profile id) — used by Task 4's loader and Task 7's migration script.

- [ ] **Step 1: Write the failing test**

```python
# apps/job-hunter/tests/test_postgres_store_search_profile.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `apps/job-hunter/.venv/bin/pytest apps/job-hunter/tests/test_postgres_store_search_profile.py -v`
Expected: FAIL with `AttributeError: 'PostgresJobStore' object has no attribute 'save_search_profile'`

- [ ] **Step 3: Write the implementation**

Add to `apps/job-hunter/src/job_hunter/postgres_store.py`, near the other table-scoped method groups (after the AI-accounting section, before Gmail sync). Add the import at the top alongside the existing `from job_hunter.models import (...)` block:

```python
from job_hunter.search_profile import SearchProfile
```

```python
    # ------------------------------------------------------------------
    # Search profile
    # ------------------------------------------------------------------

    def get_search_profile(self) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        """Return the caller's search profile row and its market rows, if any.

        RLS scopes both selects to the acting user; no explicit user_id
        filter is needed.
        """
        profiles = self._client.select("job_hunter_search_profiles", params={"limit": "1"})
        if not profiles:
            return None
        profile_row = profiles[0]
        market_rows = self._client.select(
            "job_hunter_search_profile_markets",
            params={"profile_id": f"eq.{profile_row['id']}", "order": "created_at.asc"},
        )
        return profile_row, market_rows

    def save_search_profile(self, profile: SearchProfile) -> str:
        """Upsert the caller's one search profile and replace its market rows.

        Markets have no natural per-row update semantics from the caller's
        point of view (the profile is edited as a whole) -- existing market
        rows for this profile are deleted and replaced, matching the "one
        active search profile" model rather than trying to diff old and new
        market lists.
        """
        profile_row = touch({**profile.to_profile_row(), "user_id": self._client.user_id})
        written = self._client.upsert(
            "job_hunter_search_profiles", [profile_row], on_conflict="user_id"
        )
        profile_id = written[0]["id"]

        self._client.delete(
            "job_hunter_search_profile_markets", params={"profile_id": f"eq.{profile_id}"}
        )
        market_rows = profile.to_market_rows(profile_id, self._client.user_id)
        if market_rows:
            self._client.upsert(
                "job_hunter_search_profile_markets",
                market_rows,
                on_conflict="profile_id,market_id",
            )
        return profile_id
```

- [ ] **Step 4: Run test to verify it passes**

Run: `apps/job-hunter/.venv/bin/pytest apps/job-hunter/tests/test_postgres_store_search_profile.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add apps/job-hunter/src/job_hunter/postgres_store.py apps/job-hunter/tests/test_postgres_store_search_profile.py
git commit -m "feat: add get_search_profile/save_search_profile to PostgresJobStore"
```

---

### Task 4: `ranking.py` reads its three lists from `SearchPolicy`

**Files:**
- Modify: `apps/job-hunter/src/job_hunter/models.py`
- Modify: `apps/job-hunter/src/job_hunter/ranking.py`
- Modify: `apps/job-hunter/tests/market_fixtures.py`
- Test: `apps/job-hunter/tests/test_ranking.py` (existing file — add new tests, don't replace it)

**Interfaces:**
- Consumes: nothing new.
- Produces: `SearchPolicy.specialist_board_hosts: list[str]`, `SearchPolicy.frontend_signals: list[str]`, `SearchPolicy.backend_heavy_signals: list[str]` — consumed by Task 5's config loader.

- [ ] **Step 1: Write the failing test**

Add to `apps/job-hunter/tests/test_ranking.py` (create the file if it does not exist, following the existing test style in `test_sources.py`):

```python
from job_hunter.models import CandidatePreferences, Job
from job_hunter.ranking import source_quality, _backend_transition_penalty
from tests.market_fixtures import make_market_policy


def test_source_quality_uses_specialist_board_hosts_from_policy():
    policy = make_market_policy()
    policy.specialist_board_hosts = ["myboard.example.com"]
    job = Job(source="web", title="Engineer", url="https://myboard.example.com/jobs/1")
    assert source_quality(job, policy) == 8


def test_source_quality_ignores_host_not_in_policy():
    policy = make_market_policy()
    policy.specialist_board_hosts = ["myboard.example.com"]
    job = Job(source="web", title="Engineer", url="https://devjobs.co.il/jobs/1")
    assert source_quality(job, policy) == 3


def test_backend_transition_penalty_uses_signals_from_policy():
    policy = make_market_policy()
    policy.frontend_signals = []
    policy.backend_heavy_signals = ["kubernetes", "golang"]
    job = Job(
        source="web",
        title="Full Stack Engineer",
        description="kubernetes golang expert needed",
    )
    assert _backend_transition_penalty(job, policy) == 15
```

- [ ] **Step 2: Run test to verify it fails**

Run: `apps/job-hunter/.venv/bin/pytest apps/job-hunter/tests/test_ranking.py -v`
Expected: FAIL — `source_quality() takes 1 positional argument but 2 were given` (and similarly for `_backend_transition_penalty`)

- [ ] **Step 3: Add the three fields to `SearchPolicy`**

In `apps/job-hunter/src/job_hunter/models.py`, add to the `SearchPolicy` dataclass, after `blocked_profession_title_phrases`:

```python
    specialist_board_hosts: list[str] = field(default_factory=list)
    frontend_signals: list[str] = field(default_factory=list)
    backend_heavy_signals: list[str] = field(default_factory=list)
```

- [ ] **Step 4: Update `ranking.py` to take `policy` and drop the module constants**

In `apps/job-hunter/src/job_hunter/ranking.py`:

Remove the `_SPECIALIST_BOARD_HOSTS`, `_FRONTEND_SIGNALS`, and `_BACKEND_HEAVY_SIGNALS` tuple definitions (lines 15-29 and 209-228 in the current file).

Change `source_quality`:

```python
def source_quality(job: Job, policy: SearchPolicy) -> int:
    url = (job.url or "").lower()
    if any(host in url for host in _ATS_HOSTS):
        return 10
    if job.source in {"ashby", "lever", "greenhouse"}:
        return 10
    if any(host in url for host in policy.specialist_board_hosts):
        return 8
    if job.source in {"remoteok", "remotive", "weworkremotely", "arbeitnow"}:
        return 7
    if job.source == "hackernews":
        return 5
    return 3
```

Change `_backend_transition_penalty`:

```python
def _backend_transition_penalty(job: Job, policy: SearchPolicy) -> int:
    normalized_title = normalize_text(job.title or "")
    if not any(phrase in normalized_title for phrase in _FULL_STACK_TITLE_PHRASES):
        return 0

    haystack = normalize_text(" ".join([job.title or "", job.description or ""]))
    frontend_matches = sum(1 for signal in policy.frontend_signals if signal in haystack)
    backend_matches = sum(1 for signal in policy.backend_heavy_signals if signal in haystack)

    if backend_matches < 2:
        return 0
    if frontend_matches == 0:
        return 15
    return 6
```

Update every caller of `source_quality` and `_backend_transition_penalty` inside `ranking.py` to pass `policy` through:

```python
def _title_fit(title: str, policy: SearchPolicy) -> int:
    # unchanged body
    ...

def priority_score(job: Job, policy: SearchPolicy) -> int:
    total = (
        _title_fit(job.title, policy)
        + _strength_evidence(job.description, policy)
        + _career_direction_evidence(job.description)
        + _location_evidence(job)
        + source_quality(job, policy)
    )
    return max(0, min(100, total))


def profile_priority_score(job: Job, preferences: CandidatePreferences, policy: SearchPolicy) -> int:
    total = (
        _role_seniority_fit(job, preferences)
        + _signal_coverage(job, preferences)
        + _market_location_fit(job, preferences, policy)
        + source_quality(job, policy)
        + market_priority_bonus(job, policy)
        - _avoid_signal_penalty(job, preferences)
        - _backend_transition_penalty(job, policy)
    )
    return max(0, min(100, total))
```

- [ ] **Step 5: Update `market_fixtures.py`'s `make_market_policy` to seed the three new fields**

In `apps/job-hunter/tests/market_fixtures.py`, add to the `SearchPolicy(...)` call inside `make_market_policy`:

```python
        specialist_board_hosts=["devjobs.co.il", "wellfound.com"],
        frontend_signals=["react", "next.js", "nextjs", "frontend", "front-end", "typescript", "design system"],
        backend_heavy_signals=[
            "distributed systems", "kubernetes", "golang", "java",
            "event-driven architecture", "backend architecture",
            "high-throughput", "message queues",
        ],
```

- [ ] **Step 6: Run test to verify it passes**

Run: `apps/job-hunter/.venv/bin/pytest apps/job-hunter/tests/test_ranking.py -v`
Expected: PASS (3 passed)

- [ ] **Step 7: Run the full existing test suite to catch other `source_quality`/`_backend_transition_penalty` callers**

Run: `apps/job-hunter/.venv/bin/pytest apps/job-hunter/tests -v`
Expected: any pre-existing test calling `source_quality(job)` or `priority_score`/`profile_priority_score` without a policy fails with a `TypeError`; fix each call site to pass the `policy` fixture already in scope in that test (do not add new policy fixtures — every existing ranking test already builds a `SearchPolicy`).

- [ ] **Step 8: Commit**

```bash
git add apps/job-hunter/src/job_hunter/models.py apps/job-hunter/src/job_hunter/ranking.py apps/job-hunter/tests/market_fixtures.py apps/job-hunter/tests/test_ranking.py
git commit -m "refactor: move ranking.py's specialist-board and frontend/backend lists onto SearchPolicy"
```

---

### Task 5: `config.py` loads from Postgres instead of YAML

**Files:**
- Modify: `apps/job-hunter/src/job_hunter/config.py`
- Modify: `apps/job-hunter/src/job_hunter/cli.py`
- Test: `apps/job-hunter/tests/test_config.py` (existing file — add new tests)

**Interfaces:**
- Consumes: `PostgresJobStore.get_search_profile()` from Task 3.
- Produces: `load_settings(store: PostgresJobStore) -> Settings` — same return type as before, new first-argument type. Consumed by `cli.py`.

- [ ] **Step 1: Write the failing test**

Add to `apps/job-hunter/tests/test_config.py`:

```python
import base64

import pytest

from job_hunter.config import ProfileNotFoundError, load_settings
from job_hunter.postgres_store import PostgresJobStore
from job_hunter.search_profile import SearchProfile, SearchProfileMarket


class FakeSupabaseClient:
    def __init__(self):
        self.user_id = "u1"
        self.rows = {"job_hunter_search_profiles": [], "job_hunter_search_profile_markets": []}
        self._next_id = 1

    def _new_id(self):
        value = f"id-{self._next_id}"
        self._next_id += 1
        return value

    def select(self, table, *, params=None):
        params = params or {}
        rows = self.rows[table]
        if "profile_id" in params:
            rows = [r for r in rows if r["profile_id"] == params["profile_id"].removeprefix("eq.")]
        return rows[: int(params["limit"])] if "limit" in params else rows

    def upsert(self, table, rows, *, on_conflict):
        written = []
        for row in rows:
            row = dict(row)
            row.setdefault("id", self._new_id())
            self.rows[table].append(row)
            written.append(row)
        return written

    def delete(self, table, *, params):
        pass


@pytest.fixture(autouse=True)
def _required_env(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("CANDIDATE_PROFILE_B64", base64.b64encode(b"profile").decode())
    monkeypatch.setenv("COVER_LETTER_TEMPLATE_B64", base64.b64encode(b"template").decode())
    monkeypatch.setenv("JOB_HUNTER_DRY_RUN", "1")
    monkeypatch.setenv("GEMINI_FREE_RPM", "10")
    monkeypatch.setenv("GEMINI_FREE_TPM", "250000")
    monkeypatch.setenv("GEMINI_FREE_RPD", "500")


def _seeded_store() -> PostgresJobStore:
    store = PostgresJobStore(FakeSupabaseClient())
    profile = SearchProfile(
        timezone="Europe/Berlin",
        scheduled_hour=9,
        max_jobs_per_run=35,
        source_minimum_per_run=0,
        source_max_share=0.5,
        thresholds={"package": 75, "possible": 65},
        salary_floor_eur=90000,
        target_titles=["senior product engineer"],
        max_search_queries_per_run=30,
        max_canonical_resolutions_per_run=80,
        max_learned_ats_boards_per_run=75,
        markets=[
            SearchProfileMarket(
                market_id="germany_eu",
                query_share=1.0,
                currency="EUR",
                gross_base_floor=90000,
                remote_policy="preferred",
                relocation_policy="selective",
                sponsorship_policy="not_required",
                direct_sources=["devjobs"],
            )
        ],
    )
    store.save_search_profile(profile)
    return store


def test_load_settings_reads_from_the_search_profile():
    settings = load_settings(_seeded_store())
    assert settings.timezone == "Europe/Berlin"
    assert settings.policy.salary_floor_eur == 90000
    assert settings.policy.target_titles == ["senior product engineer"]
    assert len(settings.policy.markets) == 1
    assert settings.policy.markets[0].id == "germany_eu"
    assert settings.policy.markets[0].direct_sources == ["devjobs"]


def test_load_settings_raises_when_no_profile_exists():
    store = PostgresJobStore(FakeSupabaseClient())
    with pytest.raises(ProfileNotFoundError):
        load_settings(store)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `apps/job-hunter/.venv/bin/pytest apps/job-hunter/tests/test_config.py -v`
Expected: FAIL — `ImportError: cannot import name 'ProfileNotFoundError'`

- [ ] **Step 3: Rewrite `load_settings` in `config.py`**

Replace the `load_settings` function and its `Path`/`yaml` usage. Remove `import yaml` and the `from pathlib import Path` usage tied to config loading (keep `Path` import if still used elsewhere in the file — check before removing). Add:

```python
class ProfileNotFoundError(RuntimeError):
    """Raised when the acting user has no row in job_hunter_search_profiles."""


def load_settings(store: "PostgresJobStore") -> Settings:
    result = store.get_search_profile()
    if result is None:
        raise ProfileNotFoundError(
            "no job_hunter_search_profiles row for this user; run the "
            "one-off migration script or create a profile first"
        )
    profile_row, market_row_list = result
    data = _profile_row_to_legacy_dict(profile_row, market_row_list)

    gemini_api_key = _require_env("GEMINI_API_KEY")
    candidate_profile = base64.b64decode(_require_env("CANDIDATE_PROFILE_B64")).decode("utf-8")
    cover_letter_template = base64.b64decode(_require_env("COVER_LETTER_TEMPLATE_B64")).decode("utf-8")
    dry_run = os.environ.get("JOB_HUNTER_DRY_RUN", "").strip().lower() in ("1", "true", "yes")

    if not dry_run:
        telegram_bot_token = _require_env("TELEGRAM_BOT_TOKEN")
        telegram_chat_id = _require_env("TELEGRAM_CHAT_ID")
    else:
        telegram_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
        telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    policy = SearchPolicy(
        target_titles=data.get("target_titles", []),
        positive_keywords=data.get("positive_keywords", []),
        blocked_title_keywords=data.get("blocked_title_keywords", []),
        salary_floor_eur=data.get("salary_floor_eur", 90000),
        thresholds=data.get("thresholds", {}),
        max_jobs_per_run=data.get("max_jobs_per_run", 35),
        source_minimum_per_run=data.get("source_minimum_per_run", 0),
        source_max_share=data.get("source_max_share", 0.5),
        search_queries=data.get("search_queries", []),
        ats=data.get("ats", {}),
        role_families=data.get("role_families", []),
        search_query_templates=data.get("search_query_templates", []),
        search_domains=data.get("search_domains", []),
        specialist_search_domains=data.get("specialist_search_domains", []),
        specialist_query_templates=data.get("specialist_query_templates", []),
        yc_job_pages=data.get("yc_job_pages", []),
        manual_company_watch=_parse_manual_company_watch(
            data.get("manual_company_watch", [])
        ),
        max_search_queries_per_run=data.get("max_search_queries_per_run", 30),
        max_canonical_resolutions_per_run=data.get(
            "max_canonical_resolutions_per_run", 80
        ),
        max_learned_ats_boards_per_run=_parse_max_learned_ats_boards_per_run(data),
        learned_ats_denylist=_parse_learned_ats_denylist(data),
        learned_ats_allowlist=_parse_learned_ats_allowlist(data),
        engineering_title_keywords=list(
            data.get("engineering_title_keywords", DEFAULT_ENGINEERING_TITLE_KEYWORDS)
        ),
        engineering_title_phrases=list(
            data.get("engineering_title_phrases", DEFAULT_ENGINEERING_TITLE_PHRASES)
        ),
        blocked_profession_title_phrases=list(
            data.get(
                "blocked_profession_title_phrases",
                DEFAULT_BLOCKED_PROFESSION_TITLE_PHRASES,
            )
        ),
        specialist_board_hosts=list(data.get("specialist_board_hosts", [])),
        frontend_signals=list(data.get("frontend_signals", [])),
        backend_heavy_signals=list(data.get("backend_heavy_signals", [])),
        markets=_parse_markets(data.get("markets", [])),
    )

    return Settings(
        gemini_api_key=gemini_api_key,
        candidate_profile=candidate_profile,
        cover_letter_template=cover_letter_template,
        timezone=data.get("timezone", "Europe/Berlin"),
        scheduled_hour=data.get("scheduled_hour", 9),
        policy=policy,
        gemini_quota=GeminiQuotaSettings(
            rpm=_require_positive_int_env("GEMINI_FREE_RPM"),
            tpm=_require_positive_int_env("GEMINI_FREE_TPM"),
            rpd=_require_positive_int_env("GEMINI_FREE_RPD"),
        ),
        dry_run=dry_run,
        telegram_bot_token=telegram_bot_token,
        telegram_chat_id=telegram_chat_id,
        gemini_model=os.environ.get("GEMINI_MODEL", "gemini-3.6-flash"),
        output_dir=os.environ.get("JOB_HUNTER_OUTPUT_DIR", "var"),
    )


def _profile_row_to_legacy_dict(
    profile_row: dict, market_rows: list[dict]
) -> dict:
    """Reshape Postgres rows into the dict shape `_parse_*` already expects.

    Keeps every existing YAML-era parsing/validation function (`_parse_markets`,
    `_parse_manual_company_watch`, the ATS-list parsers) unchanged: they were
    written against `yaml.safe_load`'s output, and a Postgres row reshaped into
    the same shape is a drop-in replacement for it.
    """
    data = dict(profile_row)
    data["markets"] = [
        {
            "id": row["market_id"],
            "query_share": row["query_share"],
            "locations": row["locations"],
            "allowed_languages": row["allowed_languages"],
            "salary": {
                "currency": row["currency"],
                "gross_base_floor": row["gross_base_floor"],
                "location_floors": row["location_floors"],
            },
            "remote_policy": row["remote_policy"],
            "relocation_policy": row["relocation_policy"],
            "sponsorship_policy": row["sponsorship_policy"],
            "direct_sources": row["direct_sources"],
            "discovery_domains": row["discovery_domains"],
            "query_templates": row["query_templates"],
            "role_families": row["role_families"],
            "enabled": row["enabled"],
        }
        for row in market_rows
    ]
    return data
```

Remove the old `load_settings(config_path: Path)` signature and its `with open(config_path) as f: data = yaml.safe_load(f)` body — replaced by the version above. Remove the now-unused `import yaml` line if nothing else in the file uses it (check with `grep -n yaml apps/job-hunter/src/job_hunter/config.py` first).

- [ ] **Step 4: Update `cli.py` to build the store before loading settings and drop `--config`**

In `apps/job-hunter/src/job_hunter/cli.py`:

Remove `run_parser.add_argument("--config", default="config/search.yml", help="Path to search.yml")` and the equivalent line on `gen_parser`.

Change `_run` and `_generate_cover_letter`:

```python
def _run(args: argparse.Namespace) -> int:
    http = HttpClient()
    store = PostgresJobStore(_build_client(http))
    settings = load_settings(store)

    if args.scheduled:
        ...  # unchanged
```

(Move `http = HttpClient()` and `store = PostgresJobStore(_build_client(http))` up before `load_settings`, and delete the old `store = PostgresJobStore(_build_client(http))` line that used to appear after `load_settings`, along with `from pathlib import Path` usage for `args.config` — remove `Path` import if nothing else in `cli.py` uses it.)

```python
def _generate_cover_letter(args: argparse.Namespace) -> int:
    http = HttpClient()
    store = PostgresJobStore(_build_client(http))
    settings = load_settings(store)
    cover_letter_output_dir(settings).mkdir(parents=True, exist_ok=True)
    ...  # rest unchanged, remove the old duplicate `store = ...` line
```

- [ ] **Step 5: Run test to verify it passes**

Run: `apps/job-hunter/.venv/bin/pytest apps/job-hunter/tests/test_config.py -v`
Expected: PASS (2 passed)

- [ ] **Step 6: Run the full suite and fix remaining `load_settings(Path(...))` call sites**

Run: `apps/job-hunter/.venv/bin/pytest apps/job-hunter/tests -v`
Expected: `test_sources.py`'s `test_build_sources_from_real_config_includes_new_coverage_sources_and_no_ddg` fails (it calls `load_settings(_REPO_CONFIG_PATH)`) — this is fixed in Task 8, not here. Confirm no other call site outside that one test and `cli.py` references `load_settings` with a path.

- [ ] **Step 7: Commit**

```bash
git add apps/job-hunter/src/job_hunter/config.py apps/job-hunter/src/job_hunter/cli.py apps/job-hunter/tests/test_config.py
git commit -m "feat: load_settings reads the search profile from Postgres instead of YAML"
```

---

### Task 6: One-off migration script for the existing owner's data

**Files:**
- Create: `apps/job-hunter/scripts/migrate_search_yml_to_profile.py`

**Interfaces:**
- Consumes: `SearchProfile`, `SearchProfileMarket` (Task 2), `PostgresJobStore.save_search_profile` (Task 3), `load_supabase_settings`/`SupabaseClient`/`AccessTokenMinter` (existing, from `cli.py`'s `_build_client` pattern).
- Produces: nothing consumed by later tasks — this script is run once by hand, not imported by the app.

- [ ] **Step 1: Write the script**

```python
# apps/job-hunter/scripts/migrate_search_yml_to_profile.py
"""One-off: read config/search.yml and write it as this user's search profile.

Run once, by hand, after Task 5 lands and before config/search.yml is deleted:

    apps/job-hunter/.venv/bin/python apps/job-hunter/scripts/migrate_search_yml_to_profile.py

Requires the same environment variables as `job_hunter run` (SUPABASE_URL,
SUPABASE_PUBLISHABLE_KEY, SUPABASE_SIGNING_KEY_B64, JOB_HUNTER_USER_ID).
Not part of the app; safe to delete after it has been run successfully once.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from job_hunter.config import load_supabase_settings  # noqa: E402
from job_hunter.http import HttpClient  # noqa: E402
from job_hunter.postgres_store import PostgresJobStore  # noqa: E402
from job_hunter.search_profile import SearchProfile, SearchProfileMarket  # noqa: E402
from job_hunter.supabase_auth import AccessTokenMinter  # noqa: E402
from job_hunter.supabase_client import SupabaseClient  # noqa: E402

# Hardcoded lists this script carries over from ranking.py, since those
# module constants are removed by Task 4. Matches the values ranking.py had
# before this migration.
_SPECIALIST_BOARD_HOSTS = [
    "wellfound.com", "jobs.techaviv.com", "devjobs.co.il", "workvisajobs.co.uk",
    "nodeflair.com", "sg.jobstreet.com", "mycareersfuture.gov.sg", "builtin.com",
    "startup.jobs", "ycombinator.com",
]
_FRONTEND_SIGNALS = [
    "react", "next.js", "nextjs", "frontend", "front-end", "typescript", "design system",
]
_BACKEND_HEAVY_SIGNALS = [
    "distributed systems", "kubernetes", "golang", "java",
    "event-driven architecture", "backend architecture", "high-throughput", "message queues",
]

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "search.yml"


def _build_market(entry: dict) -> SearchProfileMarket:
    salary = entry.get("salary", {})
    return SearchProfileMarket(
        market_id=entry["id"],
        query_share=float(entry.get("query_share", 0.0)),
        locations=entry.get("locations", []),
        allowed_languages=entry.get("allowed_languages", []),
        currency=salary.get("currency", ""),
        gross_base_floor=salary.get("gross_base_floor", 0),
        location_floors=salary.get("location_floors", {}),
        remote_policy=entry.get("remote_policy", "allowed"),
        relocation_policy=entry.get("relocation_policy", "allowed"),
        sponsorship_policy=entry.get("sponsorship_policy", "not_required"),
        direct_sources=entry.get("direct_sources", []),
        discovery_domains=entry.get("discovery_domains", entry.get("source_domains", [])),
        query_templates=entry.get("query_templates", []),
        role_families=entry.get("role_families", []),
        enabled=entry.get("enabled", True),
    )


def main() -> int:
    with open(_CONFIG_PATH) as f:
        data = yaml.safe_load(f)

    profile = SearchProfile(
        timezone=data.get("timezone", "Europe/Berlin"),
        scheduled_hour=data.get("scheduled_hour", 9),
        max_jobs_per_run=data.get("max_jobs_per_run", 35),
        source_minimum_per_run=data.get("source_minimum_per_run", 0),
        source_max_share=data.get("source_max_share", 0.5),
        thresholds=data.get("thresholds", {}),
        salary_floor_eur=data.get("salary_floor_eur", 90000),
        target_titles=data.get("target_titles", []),
        positive_keywords=data.get("positive_keywords", []),
        blocked_title_keywords=data.get("blocked_title_keywords", []),
        role_families=data.get("role_families", []),
        search_query_templates=data.get("search_query_templates", []),
        search_domains=data.get("search_domains", []),
        specialist_search_domains=data.get("specialist_search_domains", []),
        specialist_query_templates=data.get("specialist_query_templates", []),
        search_queries=data.get("search_queries", []),
        yc_job_pages=data.get("yc_job_pages", []),
        engineering_title_keywords=data.get("engineering_title_keywords", []),
        engineering_title_phrases=data.get("engineering_title_phrases", []),
        blocked_profession_title_phrases=data.get("blocked_profession_title_phrases", []),
        specialist_board_hosts=_SPECIALIST_BOARD_HOSTS,
        frontend_signals=_FRONTEND_SIGNALS,
        backend_heavy_signals=_BACKEND_HEAVY_SIGNALS,
        max_search_queries_per_run=data.get("max_search_queries_per_run", 30),
        max_canonical_resolutions_per_run=data.get("max_canonical_resolutions_per_run", 80),
        max_learned_ats_boards_per_run=data.get("max_learned_ats_boards_per_run", 75),
        learned_ats_denylist=data.get("learned_ats_denylist") or [],
        learned_ats_allowlist=data.get("learned_ats_allowlist") or [],
        manual_company_watch=data.get("manual_company_watch") or [],
        ats=data.get("ats", {}),
        markets=[_build_market(entry) for entry in data.get("markets", [])],
    )

    settings = load_supabase_settings()
    http = HttpClient()
    client = SupabaseClient(
        http, settings, AccessTokenMinter(settings.user_id, settings.signing_key_jwk)
    )
    store = PostgresJobStore(client)
    profile_id = store.save_search_profile(profile)
    print(f"wrote search profile {profile_id} for user {settings.user_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Run it against the real config and Supabase project**

Run: `apps/job-hunter/.venv/bin/python apps/job-hunter/scripts/migrate_search_yml_to_profile.py`
Expected: prints `wrote search profile <uuid> for user <uuid>` with no exception.

- [ ] **Step 3: Verify the row manually**

Run: `apps/job-hunter/.venv/bin/python -c "
from job_hunter.config import load_supabase_settings
from job_hunter.http import HttpClient
from job_hunter.postgres_store import PostgresJobStore
from job_hunter.supabase_auth import AccessTokenMinter
from job_hunter.supabase_client import SupabaseClient
settings = load_supabase_settings()
client = SupabaseClient(HttpClient(), settings, AccessTokenMinter(settings.user_id, settings.signing_key_jwk))
profile_row, market_rows = PostgresJobStore(client).get_search_profile()
print(profile_row['timezone'], len(market_rows), 'markets')
"`
Expected: prints the timezone from `config/search.yml` and the correct market count.

- [ ] **Step 4: Commit**

```bash
git add apps/job-hunter/scripts/migrate_search_yml_to_profile.py
git commit -m "chore: add one-off script to migrate config/search.yml into a search profile row"
```

---

### Task 7: Delete `config/search.yml` and its remaining references

**Files:**
- Delete: `apps/job-hunter/config/search.yml`
- Modify: `apps/job-hunter/tests/test_sources.py`

**Interfaces:**
- Consumes: Task 5's `load_settings(store)`, Task 3's `save_search_profile`.
- Produces: nothing further consumed.

- [ ] **Step 1: Replace the real-config test to build a seeded fake store instead of reading the file**

In `apps/job-hunter/tests/test_sources.py`, remove `_REPO_CONFIG_PATH` and the `Path` import if nothing else in the file uses it. Add a fixture next to the other fixtures:

```python
from job_hunter.search_profile import SearchProfile, SearchProfileMarket


def _profile_matching_former_search_yml() -> SearchProfile:
    """Mirrors config/search.yml's shape, for the one test that used to read it."""
    return SearchProfile(
        timezone="Europe/Berlin",
        scheduled_hour=9,
        max_jobs_per_run=100,
        source_minimum_per_run=0,
        source_max_share=0.5,
        thresholds={"package": 75, "possible": 65},
        salary_floor_eur=90000,
        max_search_queries_per_run=30,
        max_canonical_resolutions_per_run=80,
        max_learned_ats_boards_per_run=75,
        markets=[
            SearchProfileMarket(
                market_id="germany_eu",
                query_share=0.35,
                locations=["Berlin", "Germany"],
                currency="EUR",
                gross_base_floor=90000,
                remote_policy="preferred",
                relocation_policy="selective",
                sponsorship_policy="not_required",
                direct_sources=["devjobs"],
            )
        ],
    )
```

Replace the test body:

```python
def test_build_sources_from_real_config_includes_new_coverage_sources_and_no_ddg(
    store,
    fake_http, monkeypatch
):
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv(
        "CANDIDATE_PROFILE_B64", base64.b64encode(b"profile").decode()
    )
    monkeypatch.setenv(
        "COVER_LETTER_TEMPLATE_B64", base64.b64encode(b"template").decode()
    )
    monkeypatch.setenv("JOB_HUNTER_DRY_RUN", "1")
    monkeypatch.setenv("GEMINI_FREE_RPM", "10")
    monkeypatch.setenv("GEMINI_FREE_TPM", "250000")
    monkeypatch.setenv("GEMINI_FREE_RPD", "500")

    profile_store = PostgresJobStore(FakeSupabaseClient())
    profile_store.save_search_profile(_profile_matching_former_search_yml())
    settings = load_settings(profile_store)

    sources = build_sources(settings, fake_http, store=store)

    kinds = [type(s).__name__ for s in sources]
    assert "RemotiveSource" in kinds
    assert "ArbeitnowSource" in kinds
    assert "JobicySource" in kinds
    assert "HimalayasSource" in kinds
    assert "DevJobsSource" in kinds
```

Add `PostgresJobStore` and the `FakeSupabaseClient` helper (reuse the exact class from `test_config.py` — move it to a shared `tests/fake_supabase_client.py` module and import it from both test files rather than duplicating it):

```python
# apps/job-hunter/tests/fake_supabase_client.py
class FakeSupabaseClient:
    def __init__(self):
        self.user_id = "u1"
        self.rows = {"job_hunter_search_profiles": [], "job_hunter_search_profile_markets": []}
        self._next_id = 1

    def _new_id(self):
        value = f"id-{self._next_id}"
        self._next_id += 1
        return value

    def select(self, table, *, params=None):
        params = params or {}
        rows = self.rows[table]
        if "profile_id" in params:
            rows = [r for r in rows if r["profile_id"] == params["profile_id"].removeprefix("eq.")]
        return rows[: int(params["limit"])] if "limit" in params else rows

    def upsert(self, table, rows, *, on_conflict):
        written = []
        for row in rows:
            row = dict(row)
            row.setdefault("id", self._new_id())
            self.rows[table].append(row)
            written.append(row)
        return written

    def delete(self, table, *, params):
        pass
```

Update `test_config.py` and `test_postgres_store_search_profile.py` (Tasks 3 and 5) to `from tests.fake_supabase_client import FakeSupabaseClient` instead of their inline copies, and delete those inline class definitions.

Add `from job_hunter.postgres_store import PostgresJobStore` and `from job_hunter.config import load_settings` and `from tests.fake_supabase_client import FakeSupabaseClient` to `test_sources.py`'s imports (`load_settings` is already imported there).

- [ ] **Step 2: Run the updated test**

Run: `apps/job-hunter/.venv/bin/pytest apps/job-hunter/tests/test_sources.py -v`
Expected: PASS

- [ ] **Step 3: Delete the YAML file**

```bash
git rm apps/job-hunter/config/search.yml
```

- [ ] **Step 4: Search for any remaining reference to `config/search.yml` or `config.py`'s old `Path` signature**

Run: `grep -rn "config/search.yml\|search\.yml" apps/job-hunter/src apps/job-hunter/tests apps/job-hunter/AGENTS.md apps/job-hunter/README.md 2>/dev/null`
Expected: no remaining references outside comments that describe history (e.g. `postgres_store.py`'s docstrings, `ranking.py` comments referencing where a value used to live) — update any comment that states the file as the *current* source of truth (e.g. `ranking.py:15-17`'s "config/search.yml markets[*].source_domains" comment, `aggregator_detection.py:10`, `sources/learned_ats.py:86`) to instead say "the user's search profile."

- [ ] **Step 5: Run the full test suite**

Run: `apps/job-hunter/.venv/bin/pytest apps/job-hunter/tests -v`
Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/job-hunter/tests/test_sources.py apps/job-hunter/tests/test_config.py apps/job-hunter/tests/test_postgres_store_search_profile.py apps/job-hunter/tests/fake_supabase_client.py apps/job-hunter/src/job_hunter/ranking.py apps/job-hunter/src/job_hunter/aggregator_detection.py apps/job-hunter/src/job_hunter/sources/learned_ats.py
git rm apps/job-hunter/config/search.yml
git commit -m "chore: delete config/search.yml, now that search profiles live in Postgres"
```

---

### Task 8: Full-suite verification and CI check

**Files:** none (verification only)

**Interfaces:** none

- [ ] **Step 1: Run the full Job Hunter suite**

Run: `apps/job-hunter/.venv/bin/pytest apps/job-hunter/tests -v`
Expected: all PASS, zero references to `config/search.yml` remaining anywhere in test output or fixtures.

- [ ] **Step 2: Run the full pgTAP suite**

Run: `supabase test db supabase/tests/pgtap`
Expected: all files PASS, including the new `job_hunter_search_profile_isolation.sql`.

- [ ] **Step 3: Manual two-user acceptance check**

Using the migration script's pattern (or a short ad-hoc script), seed a second Supabase user with a `SearchProfile` for a non-engineering occupation and a different market (e.g. `blocked_profession_title_phrases=[]`, `specialist_board_hosts=[]`, a market with `remote_policy="required"`, `sponsorship_policy="not_required"`, no `devjobs` in `direct_sources`). Run `build_sources` for both users' settings and confirm: the non-engineering user's source list has no `DevJobsSource`, and `rank_jobs` produces different scores for the same candidate job under the two policies (specifically: a job hosted on `devjobs.co.il` scores an 8 for the engineering user's policy and a 3 for the non-engineering user's, since their `specialist_board_hosts` differs).
Expected: source lists and scores differ as described, confirming the epic's acceptance criterion "two users with different occupations and markets produce appropriately different discovery and ranking behaviour."

- [ ] **Step 4: Commit (only if step 3 required code changes; otherwise skip)**

If step 3 surfaces no code changes, there is nothing to commit — this task is verification-only.

---

## Explicitly not done here

- No profile-editing UI or API endpoint.
- No versioning/history of past search profiles.
- No multi-user run orchestration (#76) or credential storage (#72).
- No changes to discovery, evaluation, Telegram, or Gmail beyond what `ranking.py` and `sources/__init__.py` already needed to keep working unchanged.
