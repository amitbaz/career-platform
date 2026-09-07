# Job Hunter per-user search profile design

Date: 2026-09-07
Issue: #71 (child of epic #34, private multi-user alpha)
Scope: search configuration data model, its migration into Postgres, and the
code paths that read software-engineering-specific hardcoded lists. No changes
to discovery, evaluation, Telegram, or credential handling.

## Problem

Job Hunter's search behavior is driven by one file, `apps/job-hunter/config/search.yml`,
parsed by `config.py` into a frozen `SearchPolicy`/`Settings` dataclass. Missing YAML
keys fall back to hardcoded Python defaults in `models.py` (engineering title
keywords/phrases, blocked-profession phrases). Two more software-engineering-specific
lists live directly in `ranking.py` outside that fallback path: `_SPECIALIST_BOARD_HOSTS`
(specialist SWE job-board domains, e.g. `devjobs.co.il`) and `_FRONTEND_SIGNALS`/
`_BACKEND_HEAVY_SIGNALS` (title-penalty signal words). `sources/devjobs.py` is gated
by whether the active market's `direct_sources` list names it, which today reads from
the YAML.

Because there is one file, there can only ever be one search configuration. The
multi-user alpha needs each invited user to have their own validated search profile,
stored per-user in Postgres like every other piece of Job Hunter state (#66, #69, #70
already put jobs, sources, evaluations, etc. there with per-user RLS and a per-user
Supabase client). No search-profile table exists yet; three prior design docs (#66,
#69, #70) each explicitly named this as out of scope and deferred it to #71.

## Goal

A run reads its entire search configuration from Postgres, scoped to the acting user
by the existing per-user Supabase client (`settings.user_id`, from #69). No file on
disk drives search behavior. Two users with different occupations and markets produce
different discovery and ranking behavior without code changes.

## Decisions

| Decision | Choice | Reason |
|---|---|---|
| Profile cardinality | One row per user, unique index on `user_id`, overwritten in place on edit | Matches the epic's "one active search profile per user" literally. No history table — the epic does not ask for edit history, and the store pattern (#70) does not carry versioning elsewhere. |
| Markets | Separate child table (`job_hunter_search_profile_markets`), FK to the profile, one row per market | A JSONB array on the profile row was the alternative; a relational child table keeps each market RLS-checkable on its own and matches the platform's composite-FK pattern from `job_hunter_job_sources` and other parent/child tables in the #66 schema. |
| Validation | Pydantic models for both tables, validated before every write | No validation library exists anywhere in Job Hunter today (config is dataclass + `dict.get()` defaults); this is the first record type that needs real field validation (enums, ranges, required fields), and Pydantic is the standard tool for it. Scoped to this new config path only, not a repo-wide swap. |
| `ranking.py` hardcoded lists | Both `_SPECIALIST_BOARD_HOSTS` and `_FRONTEND_SIGNALS`/`_BACKEND_HEAVY_SIGNALS` move into the profile | Both are software-engineering assumptions on a ranking path every user goes through; leaving either hardcoded still blocks the epic's non-tech acceptance persona from correct ranking behavior. |
| `devjobs.py` gating | Unchanged logic, new data source | `sources/__init__.py` already gates `devjobs` by checking `"devjobs" in market.direct_sources`. That check now reads `direct_sources` from `job_hunter_search_profile_markets` instead of YAML; no new gating code. |
| Existing owner's data | One-off migration script, not app code | Reads the current `config/search.yml`, inserts the corresponding profile + market rows, then the script is discarded. Not a reusable import path — there is no product requirement yet for users to import a file. |
| `config/search.yml` and `--config` CLI flag | Deleted | Nothing reads them once `load_settings` sources from Postgres. |

## Data model

Both tables follow the #66 convention: `user_id uuid not null references auth.users(id)
on delete cascade`, `created_at timestamptz not null default now()`, `updated_at`
maintained the same way existing tables do (application-set, no trigger), RLS with the
four `_own` policies (`select_own`, `insert_own`, `update_own`, `delete_own`), migration
file under `supabase/migrations/` following the existing `YYYYMMDDNNNN` naming.

### `job_hunter_search_profiles`

One row per user (`unique (user_id)`).

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid primary key default gen_random_uuid()` | |
| `user_id` | `uuid not null unique references auth.users(id) on delete cascade` | |
| `timezone` | `text not null` | |
| `scheduled_hour` | `smallint not null` | |
| `max_jobs_per_run` | `integer not null` | |
| `source_minimum_per_run` | `integer not null` | |
| `source_max_share` | `double precision not null` | |
| `thresholds` | `jsonb not null default '{}'` | package/possible thresholds, same shape as today's YAML section |
| `salary_floor_eur` | `double precision` | nullable, matches optional YAML key |
| `engineering_title_keywords` | `text[] not null default '{}'` | was `DEFAULT_ENGINEERING_TITLE_KEYWORDS` fallback |
| `engineering_title_phrases` | `text[] not null default '{}'` | was `DEFAULT_ENGINEERING_TITLE_PHRASES` fallback |
| `blocked_profession_title_phrases` | `text[] not null default '{}'` | was `DEFAULT_BLOCKED_PROFESSION_TITLE_PHRASES` fallback |
| `target_titles` | `text[] not null default '{}'` | |
| `positive_keywords` | `text[] not null default '{}'` | |
| `blocked_title_keywords` | `text[] not null default '{}'` | |
| `role_families` | `text[] not null default '{}'` | no prior hardcoded default existed; stays empty-array default |
| `specialist_board_hosts` | `text[] not null default '{}'` | was `ranking.py`'s `_SPECIALIST_BOARD_HOSTS` |
| `frontend_signals` | `text[] not null default '{}'` | was `_FRONTEND_SIGNALS` |
| `backend_heavy_signals` | `text[] not null default '{}'` | was `_BACKEND_HEAVY_SIGNALS` |
| `max_search_queries_per_run` | `integer not null` | |
| `max_learned_ats_boards_per_run` | `integer not null` | |
| `learned_ats_denylist` | `text[] not null default '{}'` | |
| `learned_ats_allowlist` | `text[] not null default '{}'` | |
| `yc_job_pages` | `jsonb not null default '[]'` | |
| `manual_company_watch` | `jsonb not null default '[]'` | |
| `search_queries` | `jsonb not null default '[]'` | |
| `ats` | `jsonb not null default '{}'` | |

### `job_hunter_search_profile_markets`

One row per market. Composite FK to the parent profile follows the #66 pattern:
`(profile_id, user_id)` referencing `unique (id, user_id)` on
`job_hunter_search_profiles`.

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid primary key default gen_random_uuid()` | |
| `user_id` | `uuid not null references auth.users(id) on delete cascade` | denormalized, matches child-table convention |
| `profile_id` | `uuid not null` | composite FK `(profile_id, user_id)` to `(id, user_id)` on `job_hunter_search_profiles` |
| `market_id` | `text not null` | natural key from YAML's `markets[].id` |
| `query_share` | `double precision not null` | |
| `locations` | `jsonb not null default '[]'` | |
| `salary` | `jsonb not null default '{}'` | |
| `remote` | `jsonb not null default '{}'` | policy object, not a boolean — YAML's remote field is structured |
| `relocation` | `jsonb not null default '{}'` | |
| `sponsorship` | `jsonb not null default '{}'` | |
| `direct_sources` | `text[] not null default '{}'` | consumed unchanged by `sources/__init__.py`'s devjobs/wellfound gating |
| `discovery_domains` | `text[] not null default '{}'` | |
| `query_templates` | `jsonb not null default '[]'` | |

Natural key `unique (profile_id, market_id)`.

### RLS

Both tables get the standard four policies from the #66 pattern (`select_own`,
`insert_own`, `update_own`, `delete_own`, wrapped `(select auth.uid()) = user_id`,
`to authenticated`).

## Pydantic models and validation

New module `apps/job-hunter/src/job_hunter/search_profile.py`:

- `SearchProfileMarket(BaseModel)` — mirrors the child table columns, validated types
  (e.g. `query_share: float = Field(ge=0, le=1)`).
- `SearchProfile(BaseModel)` — mirrors the parent table columns plus
  `markets: list[SearchProfileMarket]`.
- A `from_row(profile_row, market_rows)` constructor and a `to_rows()` method that
  split/join between the two tables for the store layer.

Validation happens in this module only; it does not become a general dependency of
`config.py` beyond the `SearchProfile` type it returns.

## Config loading

`config.py`'s `load_settings` stops taking a `Path` to YAML. It takes the resolved
`SupabaseClient`/`user_id` (already available at the same point `AccessTokenMinter` is
constructed today, `cli.py:37`) and:

1. Selects the user's row from `job_hunter_search_profiles` and its child rows from
   `job_hunter_search_profile_markets` via the existing `SupabaseClient.select`
   (`supabase_client.py:77-193`), scoped by the per-user JWT — no explicit `user_id`
   filter needed, RLS does it.
2. Validates through `SearchProfile.from_row`.
3. Builds the same `SearchPolicy` shape `discovery.py`, `sources/__init__.py`,
   `aggregator_detection.py`, and `sources/learned_ats.py` already consume, so those
   call sites are unchanged.

`cli.py`'s `--config` argument (`cli.py:47,65`) and its `default="config/search.yml"`
are removed.

## `ranking.py` changes

`_SPECIALIST_BOARD_HOSTS`, `_FRONTEND_SIGNALS`, `_BACKEND_HEAVY_SIGNALS` stop being
module-level constants (`ranking.py:18-29,207-244`). Functions that use them
(`_backend_transition_penalty` and the specialist-board check) take the loaded
`SearchPolicy` and read the corresponding fields from it.

## `sources/devjobs.py` / gating changes

No change to `devjobs.py` itself or to the gating check in `sources/__init__.py:199-217`
(`"devjobs" in (market.direct_sources or [])`). The list it checks now originates from
`job_hunter_search_profile_markets.direct_sources` instead of the YAML's
`markets[].direct_sources`.

## Migration of the existing owner's data

A throwaway script (not part of the app, run once, deleted after use or kept outside
`apps/job-hunter/src`) reads `apps/job-hunter/config/search.yml`, builds a
`SearchProfile` from it, and inserts the profile + market rows via the Postgres store.
After it runs successfully and the row is confirmed, `apps/job-hunter/config/search.yml`
is deleted.

## Testing

- `apps/job-hunter/tests/test_sources.py`, which currently resolves
  `config/search.yml` on disk (`_REPO_CONFIG_PATH`, `test_sources.py:25`), switches to
  building a `SearchPolicy` via `SearchProfile` fixtures instead of reading the file.
- New unit tests for `SearchProfile`/`SearchProfileMarket` validation (required fields,
  range checks).
- New unit tests for `ranking.py` proving the specialist-board and frontend/backend
  penalty behavior is driven by the passed-in `SearchPolicy`, not module constants —
  two profiles with different lists must produce different ranking output for the same
  candidate job.
- pgTAP isolation test for both new tables, following the existing
  `supabase/tests/pgtap/job_hunter_isolation.sql` pattern (RLS enabled, four policies
  present, cross-user read/write denied).

## Verification

- `supabase db reset` applies the new migration cleanly.
- `pnpm db:test` passes, including the new isolation assertions.
- `apps/job-hunter` test suite passes with no reference to `config/search.yml` remaining.
- A manual run against two seeded profiles (different occupation/market) produces
  different discovery source selection and different ranking penalties, confirming the
  epic's acceptance criterion.

## Explicitly not done here

- No changes to discovery, evaluation, Telegram, Gmail, or credential handling.
- No profile-editing UI or API endpoint — this issue is the data model and the read
  path a run uses, not a management surface.
- No versioning/history of past search profiles.
- No multi-user run orchestration (#76) or credential storage (#72).
