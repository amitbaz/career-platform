# Job Hunter Postgres Store Port — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Postgres the only source of truth for Job Hunter, migrate the owner's existing history across, and remove the SQLite state artifact from both GitHub workflows.

**Architecture:** The store reaches Postgres over PostgREST using a short-lived per-user ES256 token, so row-level security — not application care — decides which rows are visible. Six operations that PostgREST cannot express in one call become `security invoker` SQL functions. Tests run against a real local Supabase stack rather than a fake, because the semantics this port depends on most (conflict resolution, ordering ties, RLS) are what a fake models worst.

**Tech Stack:** Python 3.12+, pytest, `requests` (via the existing `HttpClient`), Supabase CLI, PostgREST, Postgres 17, pgTAP.

**Spec:** `docs/superpowers/specs/2026-09-06-job-hunter-postgres-store-port-design.md`

## Global Constraints

- Run tests with `pnpm job-hunter:test` or `apps/job-hunter/.venv/bin/python -m pytest`. **Never** bare `python -m pytest` — a stale global install at `~/job-hunter-bot` shadows the source tree and tests will pass against the wrong code.
- Install with `pip install -e '.[test,webhook]'`. The `[test]`-only form breaks collection on the webhook tests.
- Supabase migrations live in `supabase/migrations/`, never inside an app. Name them `YYYYMMDDNNNN_<topic>.sql`.
- The local stack must be running for the store suite: `pnpm db:key` once, then `supabase start` from the repository root.
- Job ids are `str` (uuid) everywhere after Task 10. Before Task 10 they are `int`. Do not mix.
- Timestamps are `timestamptz` in Postgres. Python sends ISO-8601 strings with an explicit UTC offset and never compares timestamps as strings.
- All new SQL functions are `security invoker` and `set search_path = ''`, with fully qualified `public.` table names.
- Every table write that can be retried must be idempotent. `HttpClient` retries POST on `{429, 500, 502, 503, 504}`.
- One branch, one PR: `feat/70-port-store-to-postgres`.

---

## File Structure

**Created:**

| Path | Responsibility |
| --- | --- |
| `supabase/migrations/202609060003_job_hunter_write_idempotency.sql` | Unique constraints on the five tables that lack them |
| `supabase/migrations/202609060004_job_hunter_store_functions.sql` | The six `security invoker` SQL functions |
| `supabase/tests/pgtap/job_hunter_write_idempotency.sql` | Asserts the five constraints exist and reject duplicates |
| `supabase/tests/pgtap/job_hunter_store_functions.sql` | Asserts each function returns correct rows and leaks nothing cross-user |
| `apps/job-hunter/src/job_hunter/postgres_store.py` | `PostgresJobStore` — the ported persistence layer |
| `apps/job-hunter/src/job_hunter/store_mapping.py` | Row ⇄ domain-object conversion, timestamp and boolean coercion |
| `apps/job-hunter/tests/conftest.py` | The shared `store` fixture and per-test truncation |
| `apps/job-hunter/scripts/migrate_sqlite_to_postgres.py` | One-shot data migration with id remapping |
| `apps/job-hunter/tests/test_migrate_sqlite_to_postgres.py` | Migration script tests, including card id remapping |

**Modified:** `supabase_client.py` (upsert, paging, rpc), `config.py`, `cli.py`, `pipeline.py`, `models.py`, `telegram_navigation.py`, `telegram_webhook.py`, `navigation_repository.py`, `search_budget.py`, `gmail_linkedin_cleanup.py`, `gmail_matching.py`, `gemini_usage.py`, `sources/company_watch.py`, `.github/workflows/job-hunter-daily.yml`, `.github/workflows/job-hunter-generate-cover-letter.yml`, `.github/workflows/job-hunter-ci.yml`, `apps/job-hunter/AGENTS.md`, `apps/job-hunter/README.md`, root `AGENTS.md`.

**Deleted:** `store.py`, `navigation_store.py`, `github_state.py`, `scripts/restore_state.py`, `tests/test_store_read_only.py`, `tests/test_github_state.py`, `tests/test_restore_state.py`.

---

## Phase 1 — Foundations (Tasks 1–6)

Nothing in this phase touches `JobStore`. Each task is independently reviewable and leaves the tree green.

### Task 1: Close the five duplicate-write holes

Five of the eighteen tables have no unique constraint. `HttpClient` retries POST on 5xx, so one transient 502 writes the row twice — corrupting quota accounting, double-sending deliveries, and skewing "latest evaluation".

**Files:**
- Create: `supabase/migrations/202609060003_job_hunter_write_idempotency.sql`
- Create: `supabase/tests/pgtap/job_hunter_write_idempotency.sql`

**Interfaces:**
- Consumes: nothing.
- Produces: five named unique constraints that Task 2's `upsert(on_conflict=...)` targets by column list:
  - `job_hunter_evaluations (user_id, job_id, evaluated_at)`
  - `job_hunter_materials (user_id, job_id, generated_at)`
  - `job_hunter_deliveries (user_id, job_id, delivery_type, delivered_at)`
  - `job_hunter_ai_usage (user_id, run_id, model, purpose, occurred_at)`
  - `job_hunter_search_api_usage (user_id, provider, occurred_at)`

- [ ] **Step 1: Write the failing pgTAP test**

Create `supabase/tests/pgtap/job_hunter_write_idempotency.sql`:

```sql
begin;
select plan(6);

select has_index(
  'public', 'job_hunter_evaluations', 'job_hunter_evaluations_user_job_evaluated_key',
  'evaluations has a user-scoped natural key'
);
select has_index(
  'public', 'job_hunter_materials', 'job_hunter_materials_user_job_generated_key',
  'materials has a user-scoped natural key'
);
select has_index(
  'public', 'job_hunter_deliveries', 'job_hunter_deliveries_user_job_type_at_key',
  'deliveries has a user-scoped natural key'
);
select has_index(
  'public', 'job_hunter_ai_usage', 'job_hunter_ai_usage_user_run_model_purpose_at_key',
  'ai_usage has a user-scoped natural key'
);
select has_index(
  'public', 'job_hunter_search_api_usage', 'job_hunter_search_api_usage_user_provider_at_key',
  'search_api_usage has a user-scoped natural key'
);

-- run_id is nullable; nulls do not collide in a unique index, so the
-- constraint must be built on a coalesced expression to actually bite.
select col_not_null(
  'public', 'job_hunter_ai_usage', 'run_id',
  'ai_usage.run_id is NOT NULL so the natural key cannot be defeated by nulls'
);

select * from finish();
rollback;
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd /Users/amitbaz/career-platform && pnpm db:test
```
Expected: FAIL — five `has_index` failures and one `col_not_null` failure.

- [ ] **Step 3: Write the migration**

Create `supabase/migrations/202609060003_job_hunter_write_idempotency.sql`:

```sql
-- Five job_hunter tables were created without a unique key. The shared
-- HttpClient retries POST on 5xx, so a transient error against any of them
-- silently writes the row twice. Give each a natural key so the retry
-- conflicts instead of duplicating, and so #70's writes can be upserts.

-- run_id is the only discriminator separating two identical AI calls in one
-- run from the same call retried. The daily workflow always passes
-- GEMINI_RUN_ID; backfill anything historical to a sentinel before the
-- NOT NULL lands.
update public.job_hunter_ai_usage set run_id = 'unknown' where run_id is null;
alter table public.job_hunter_ai_usage alter column run_id set not null;

alter table public.job_hunter_evaluations
  add constraint job_hunter_evaluations_user_job_evaluated_key
  unique (user_id, job_id, evaluated_at);

alter table public.job_hunter_materials
  add constraint job_hunter_materials_user_job_generated_key
  unique (user_id, job_id, generated_at);

alter table public.job_hunter_deliveries
  add constraint job_hunter_deliveries_user_job_type_at_key
  unique (user_id, job_id, delivery_type, delivered_at);

alter table public.job_hunter_ai_usage
  add constraint job_hunter_ai_usage_user_run_model_purpose_at_key
  unique (user_id, run_id, model, purpose, occurred_at);

alter table public.job_hunter_search_api_usage
  add constraint job_hunter_search_api_usage_user_provider_at_key
  unique (user_id, provider, occurred_at);
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
cd /Users/amitbaz/career-platform && supabase db reset && pnpm db:test
```
Expected: PASS, and the existing `job_hunter_isolation.sql` suite still passes.

- [ ] **Step 5: Commit**

```bash
git add supabase/migrations/202609060003_job_hunter_write_idempotency.sql supabase/tests/pgtap/job_hunter_write_idempotency.sql
git commit -m "fix: give five job_hunter tables natural keys so retried writes cannot duplicate"
```

---

### Task 2: Add upsert to the Supabase client

**Files:**
- Modify: `apps/job-hunter/src/job_hunter/supabase_client.py:96-106` (`_headers`), and add an `upsert` method after `insert`
- Test: `apps/job-hunter/tests/integration/test_supabase_client_writes.py` (create)

**Interfaces:**
- Consumes: the constraints from Task 1.
- Produces: `SupabaseClient.upsert(table: str, rows: list[dict[str, Any]], *, on_conflict: str) -> list[dict[str, Any]]` — `on_conflict` is a comma-separated column list matching a unique constraint.

- [ ] **Step 1: Write the failing test**

Create `apps/job-hunter/tests/integration/test_supabase_client_writes.py`. Follow the skip-guard pattern already in `tests/integration/test_supabase_isolation.py`:

```python
"""Live-stack tests for the client capabilities #70 adds.

Skipped unless SUPABASE_TEST_URL, SUPABASE_TEST_PUBLISHABLE_KEY and
SUPABASE_TEST_SIGNING_KEY_B64 are exported. CI exports them from
`supabase status`.
"""

from __future__ import annotations

import uuid

import pytest

from job_hunter.supabase_client import SupabaseClient


def test_upsert_is_idempotent_on_the_natural_key(client: SupabaseClient) -> None:
    job = client.insert(
        "job_hunter_jobs",
        [{"fingerprint": f"fp-{uuid.uuid4()}", "source": "test", "url": "https://x"}],
    )[0]
    row = {
        "job_id": job["id"],
        "total_score": 70,
        "decision": "possible_match",
        "evaluated_at": "2026-09-06T10:00:00+00:00",
    }

    first = client.upsert("job_hunter_evaluations", [row], on_conflict="user_id,job_id,evaluated_at")
    second = client.upsert("job_hunter_evaluations", [row], on_conflict="user_id,job_id,evaluated_at")

    assert first[0]["id"] == second[0]["id"]
    stored = client.select("job_hunter_evaluations", params={"job_id": f"eq.{job['id']}"})
    assert len(stored) == 1


def test_upsert_updates_the_conflicting_row(client: SupabaseClient) -> None:
    job = client.insert(
        "job_hunter_jobs",
        [{"fingerprint": f"fp-{uuid.uuid4()}", "source": "test", "url": "https://x"}],
    )[0]
    base = {
        "job_id": job["id"],
        "total_score": 70,
        "decision": "possible_match",
        "evaluated_at": "2026-09-06T10:00:00+00:00",
    }
    client.upsert("job_hunter_evaluations", [base], on_conflict="user_id,job_id,evaluated_at")
    client.upsert(
        "job_hunter_evaluations",
        [{**base, "total_score": 91}],
        on_conflict="user_id,job_id,evaluated_at",
    )

    stored = client.select("job_hunter_evaluations", params={"job_id": f"eq.{job['id']}"})
    assert len(stored) == 1
    assert stored[0]["total_score"] == 91
```

The `client` fixture does not exist yet. Add it to the same file for now; Task 6 moves it to `conftest.py`:

```python
@pytest.fixture
def client() -> SupabaseClient:
    import base64, json, os
    from job_hunter.config import SupabaseSettings
    from job_hunter.http import HttpClient
    from job_hunter.supabase_auth import AccessTokenMinter

    for name in ("SUPABASE_TEST_URL", "SUPABASE_TEST_PUBLISHABLE_KEY", "SUPABASE_TEST_SIGNING_KEY_B64"):
        if not os.environ.get(name):
            pytest.skip(f"{name} is not set; start the local stack and export it")

    user_id = "00000000-0000-0000-0000-000000000001"  # seed.sql user A
    settings = SupabaseSettings(
        url=os.environ["SUPABASE_TEST_URL"],
        publishable_key=os.environ["SUPABASE_TEST_PUBLISHABLE_KEY"],
        user_id=user_id,
        signing_key_jwk=json.loads(base64.b64decode(os.environ["SUPABASE_TEST_SIGNING_KEY_B64"])),
    )
    return SupabaseClient(HttpClient(), settings, AccessTokenMinter(settings))
```

Confirm the seed user's UUID against `supabase/seed.sql` before running; use whatever id that file actually defines.

- [ ] **Step 2: Run it to verify it fails**

```bash
cd /Users/amitbaz/career-platform && supabase start
eval "$(supabase status -o env | sed 's/^/export /')" 2>/dev/null || true
cd apps/job-hunter && .venv/bin/python -m pytest tests/integration/test_supabase_client_writes.py -v
```
Expected: FAIL with `AttributeError: 'SupabaseClient' object has no attribute 'upsert'`.

- [ ] **Step 3: Implement upsert**

In `supabase_client.py`, change `_headers` to take the `Prefer` value instead of hardcoding it:

```python
    def _headers(self, *, write: bool = False, prefer: str | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._minter.token()}",
            "apikey": self._settings.publishable_key,
            "Accept": "application/json",
        }
        if write:
            headers["Content-Type"] = "application/json"
            headers["Prefer"] = prefer or "return=representation"
        return headers
```

Add after `insert`:

```python
    def upsert(
        self, table: str, rows: list[dict[str, Any]], *, on_conflict: str
    ) -> list[dict[str, Any]]:
        """Insert rows, updating any that collide on ``on_conflict``.

        ``on_conflict`` is a comma-separated column list naming a unique
        constraint. Every write on the hot path goes through here rather than
        ``insert``: HttpClient retries POST on 5xx, and an upsert makes that
        retry converge instead of duplicating the row.
        """
        if not on_conflict:
            raise ValueError("upsert requires on_conflict naming a unique constraint")
        response = self._http.post(
            self._url(table),
            headers=self._headers(
                write=True, prefer="resolution=merge-duplicates,return=representation"
            ),
            params={"on_conflict": on_conflict},
            json=rows,
        )
        return self._parse(response)
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd /Users/amitbaz/career-platform/apps/job-hunter && .venv/bin/python -m pytest tests/integration/test_supabase_client_writes.py -v
```
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add apps/job-hunter/src/job_hunter/supabase_client.py apps/job-hunter/tests/integration/test_supabase_client_writes.py
git commit -m "feat: add conflict-aware upsert to the Supabase client"
```

---

### Task 3: Make select page past the 1000-row cap

PostgREST returns at most 1000 rows and does **not** signal truncation. Several ported methods read whole tables, so a silent short read is a correctness bug, not a performance one.

**Files:**
- Modify: `apps/job-hunter/src/job_hunter/supabase_client.py` (`select`)
- Test: `apps/job-hunter/tests/integration/test_supabase_client_writes.py`

**Interfaces:**
- Produces: `select` returns **all** matching rows, transparently. Signature unchanged.

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_supabase_client_writes.py`:

```python
def test_select_pages_past_the_postgrest_row_cap(client: SupabaseClient) -> None:
    marker = f"page-{uuid.uuid4()}"
    rows = [
        {"fingerprint": f"{marker}-{i}", "source": marker, "url": f"https://x/{i}"}
        for i in range(1100)
    ]
    for chunk in range(0, len(rows), 500):
        client.insert("job_hunter_jobs", rows[chunk : chunk + 500])

    found = client.select("job_hunter_jobs", params={"source": f"eq.{marker}"})

    assert len(found) == 1100, "select must page rather than silently truncate at 1000"
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd /Users/amitbaz/career-platform/apps/job-hunter && .venv/bin/python -m pytest tests/integration/test_supabase_client_writes.py::test_select_pages_past_the_postgrest_row_cap -v
```
Expected: FAIL — `assert 1000 == 1100`.

- [ ] **Step 3: Implement paging**

Replace `select` in `supabase_client.py`:

```python
_PAGE_SIZE = 1000


    def select(
        self, table: str, *, params: dict[str, str] | None = None
    ) -> list[dict[str, Any]]:
        """Read rows, following PostgREST's row cap to completion.

        PostgREST caps a response at max-rows (1000 locally) and gives no
        signal that it truncated, so a caller reading a whole table would
        silently see a prefix. Page with Range headers until a short page
        arrives.

        A caller that passes its own ``limit`` means it, and gets one request.
        """
        query = dict(params or {})
        if "limit" in query:
            response = self._http.get(self._url(table), headers=self._headers(), params=query)
            return self._parse(response)

        collected: list[dict[str, Any]] = []
        offset = 0
        while True:
            headers = self._headers()
            headers["Range-Unit"] = "items"
            headers["Range"] = f"{offset}-{offset + _PAGE_SIZE - 1}"
            page = self._parse(
                self._http.get(self._url(table), headers=headers, params=query)
            )
            collected.extend(page)
            if len(page) < _PAGE_SIZE:
                return collected
            offset += _PAGE_SIZE
```

Note: `_parse` treats 206 Partial Content as success because it only rejects `>= 400`.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd /Users/amitbaz/career-platform/apps/job-hunter && .venv/bin/python -m pytest tests/integration/ -v
```
Expected: all pass, including the six existing isolation tests.

- [ ] **Step 5: Commit**

```bash
git add apps/job-hunter/src/job_hunter/supabase_client.py apps/job-hunter/tests/integration/test_supabase_client_writes.py
git commit -m "fix: page Supabase reads so a whole-table select cannot truncate at 1000 rows"
```

---

### Task 4: Add the six SQL functions

**Files:**
- Create: `supabase/migrations/202609060004_job_hunter_store_functions.sql`
- Create: `supabase/tests/pgtap/job_hunter_store_functions.sql`

**Interfaces:**
- Produces, all `security invoker` with `set search_path = ''`:
  - `public.job_hunter_upsert_job(p_job jsonb) returns table (id uuid, is_new boolean, description_changed boolean)`
  - `public.job_hunter_pending_delivery_jobs(p_score_floor int) returns table (job_id uuid)`
  - `public.job_hunter_pending_review_events(p_confidence_threshold double precision) returns setof jsonb`
  - `public.job_hunter_merge_jobs(p_survivor uuid, p_duplicate uuid) returns uuid`
  - `public.job_hunter_unmaterialized_inbound_jobs() returns setof jsonb`
  - `public.job_hunter_find_job_by_identity(p_company text, p_title text, p_location text) returns setof uuid`

Each is a direct translation of the SQLite query it replaces. Read the original before writing each one:

| Function | Source to translate |
| --- | --- |
| `job_hunter_upsert_job` | `store.py:649-743` and `store.py:744-905` |
| `job_hunter_pending_delivery_jobs` | `store.py:2126-2141` |
| `job_hunter_pending_review_events` | `store.py:1907-1932` |
| `job_hunter_merge_jobs` | `store.py:906-1090` |
| `job_hunter_unmaterialized_inbound_jobs` | `store.py:1805-1822` |
| `job_hunter_find_job_by_identity` | `store.py:1180-1222` |

Translation rules that apply throughout:
- `MAX(id)` for "latest evaluation" becomes `order by evaluated_at desc, created_at desc, id desc limit 1`.
- Two-argument `MIN(a, b)` / `MAX(a, b)` become `LEAST` / `GREATEST`.
- `LIKE` becomes `ilike` — Postgres `LIKE` is case-sensitive, SQLite's is not.
- Every function filters on `user_id = (select auth.uid())` in addition to relying on RLS, so a bug in one cannot read across users even if a policy is later loosened.

- [ ] **Step 1: Write the failing pgTAP test**

Create `supabase/tests/pgtap/job_hunter_store_functions.sql`. For each of the six: assert the function exists with the right signature, insert fixture rows as user A, assert the function returns them, then assert that calling it as user B returns zero rows. Follow the two-user pattern already established in `supabase/tests/pgtap/job_hunter_isolation.sql:151-204`.

```sql
begin;
select plan(18);  -- 6 functions x (exists, returns-for-owner, empty-for-other)

select has_function('public', 'job_hunter_upsert_job', array['jsonb'],
  'job_hunter_upsert_job exists');
select has_function('public', 'job_hunter_pending_delivery_jobs', array['integer'],
  'job_hunter_pending_delivery_jobs exists');
select has_function('public', 'job_hunter_pending_review_events', array['double precision'],
  'job_hunter_pending_review_events exists');
select has_function('public', 'job_hunter_merge_jobs', array['uuid', 'uuid'],
  'job_hunter_merge_jobs exists');
select has_function('public', 'job_hunter_unmaterialized_inbound_jobs', array[]::text[],
  'job_hunter_unmaterialized_inbound_jobs exists');
select has_function('public', 'job_hunter_find_job_by_identity', array['text', 'text', 'text'],
  'job_hunter_find_job_by_identity exists');

-- Then, per function: set the JWT to user A, seed, assert results;
-- set it to user B, assert zero rows. See job_hunter_isolation.sql:151-204
-- for the set_config('request.jwt.claims', ...) idiom this file reuses.

select * from finish();
rollback;
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd /Users/amitbaz/career-platform && pnpm db:test
```
Expected: FAIL — six `has_function` failures.

- [ ] **Step 3: Write the migration**

Create `supabase/migrations/202609060004_job_hunter_store_functions.sql`. Every function follows this shape:

```sql
create or replace function public.job_hunter_pending_delivery_jobs(p_score_floor int)
returns table (job_id uuid)
language sql
security invoker
set search_path = ''
as $$
  select j.id
  from public.job_hunter_jobs j
  join lateral (
    select e.decision, e.total_score
    from public.job_hunter_evaluations e
    where e.job_id = j.id and e.user_id = j.user_id
    order by e.evaluated_at desc, e.created_at desc, e.id desc
    limit 1
  ) e on true
  where j.user_id = (select auth.uid())
    and e.total_score > p_score_floor
    and e.decision in ('possible_match', 'high_priority', 'package_match')
    and not exists (
      select 1 from public.job_hunter_deliveries d
      where d.job_id = j.id
        and d.user_id = j.user_id
        and d.delivery_type = 'telegram_message'
    );
$$;
```

Write the other five by translating their sources per the table and rules above.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd /Users/amitbaz/career-platform && supabase db reset && pnpm db:test
```
Expected: PASS, all suites.

- [ ] **Step 5: Commit**

```bash
git add supabase/migrations/202609060004_job_hunter_store_functions.sql supabase/tests/pgtap/job_hunter_store_functions.sql
git commit -m "feat: add job_hunter store functions for the queries PostgREST cannot express"
```

---

### Task 5: Add rpc to the Supabase client

**Files:**
- Modify: `apps/job-hunter/src/job_hunter/supabase_client.py`
- Test: `apps/job-hunter/tests/integration/test_supabase_client_writes.py`

**Interfaces:**
- Produces: `SupabaseClient.rpc(function: str, payload: dict[str, Any] | None = None, *, retry: bool = True) -> list[dict[str, Any]]`

- [ ] **Step 1: Write the failing test**

```python
def test_rpc_calls_a_store_function_and_respects_rls(client: SupabaseClient) -> None:
    result = client.rpc("job_hunter_pending_delivery_jobs", {"p_score_floor": 0})
    assert isinstance(result, list)
```

Extend this once Task 4's fixtures are available: seed a job with a high-scoring evaluation and no delivery, then assert its id comes back.

- [ ] **Step 2: Run it to verify it fails**

```bash
cd /Users/amitbaz/career-platform/apps/job-hunter && .venv/bin/python -m pytest tests/integration/test_supabase_client_writes.py::test_rpc_calls_a_store_function_and_respects_rls -v
```
Expected: FAIL with `AttributeError: 'SupabaseClient' object has no attribute 'rpc'`.

- [ ] **Step 3: Implement rpc**

```python
    def rpc(
        self,
        function: str,
        payload: dict[str, Any] | None = None,
        *,
        retry: bool = True,
    ) -> list[dict[str, Any]]:
        """Call a Postgres function through PostgREST.

        The functions are ``security invoker``, so row-level security still
        applies and the minted token still decides which rows are visible.

        ``retry=False`` is for functions that mutate without being idempotent
        — ``job_hunter_merge_jobs`` is the one such caller.
        """
        response = self._http.post(
            f"{self._settings.url}/rest/v1/rpc/{function}",
            headers=self._headers(write=True),
            json=payload or {},
            retry=retry,
        )
        return self._parse(response)
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd /Users/amitbaz/career-platform/apps/job-hunter && .venv/bin/python -m pytest tests/integration/ -v
```
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add apps/job-hunter/src/job_hunter/supabase_client.py apps/job-hunter/tests/integration/test_supabase_client_writes.py
git commit -m "feat: let the Supabase client call store functions over RPC"
```

---

### Task 6: Build the shared test fixture

There is no `conftest.py` anywhere in the app. 294 sites across 28 files build a store themselves, 89 as `JobStore(":memory:")`. Every one of those becomes a fixture consumer.

**Files:**
- Create: `apps/job-hunter/tests/conftest.py`
- Modify: `.github/workflows/job-hunter-ci.yml` — the `test` job boots the stack

**Interfaces:**
- Produces: fixtures `supabase_client` (user A), `other_supabase_client` (user B), and `store` (a `PostgresJobStore` for user A, truncated between tests). `store` is defined here but not usable until Task 7 creates the class; write it now so Task 7 has a harness to fail against.

- [ ] **Step 1: Write conftest.py**

```python
"""Shared fixtures for the Job Hunter suite.

The store is Postgres now, so store-backed tests run against the local
Supabase stack rather than an in-process engine. Start it with
`supabase start` from the repository root (`pnpm db:key` once first).

Each test gets a clean slate: the fixture deletes the acting user's rows
from every job_hunter_* table in foreign-key-safe order after the test.
Deletes are scoped by the user's own token, so a truncation bug cannot
reach another user's data.
"""

from __future__ import annotations

import base64
import json
import os

import pytest

from job_hunter.config import SupabaseSettings
from job_hunter.http import HttpClient
from job_hunter.supabase_auth import AccessTokenMinter
from job_hunter.supabase_client import SupabaseClient

_REQUIRED = (
    "SUPABASE_TEST_URL",
    "SUPABASE_TEST_PUBLISHABLE_KEY",
    "SUPABASE_TEST_SIGNING_KEY_B64",
)

# Children before parents, so cascades never fight a foreign key.
_TABLES_CHILD_FIRST = (
    "job_hunter_review_deliveries",
    "job_hunter_application_events",
    "job_hunter_inbound_job_candidates",
    "job_hunter_gmail_messages",
    "job_hunter_gmail_sync_state",
    "job_hunter_telegram_navigation_sessions",
    "job_hunter_search_api_usage",
    "job_hunter_candidate_context_cache",
    "job_hunter_ai_quota_state",
    "job_hunter_ai_usage",
    "job_hunter_pending_ai_work",
    "job_hunter_deliveries",
    "job_hunter_materials",
    "job_hunter_evaluations",
    "job_hunter_ats_registry",
    "job_hunter_company_watch",
    "job_hunter_job_sources",
    "job_hunter_jobs",
)


def _settings_for(user_id: str) -> SupabaseSettings:
    return SupabaseSettings(
        url=os.environ["SUPABASE_TEST_URL"],
        publishable_key=os.environ["SUPABASE_TEST_PUBLISHABLE_KEY"],
        user_id=user_id,
        signing_key_jwk=json.loads(
            base64.b64decode(os.environ["SUPABASE_TEST_SIGNING_KEY_B64"])
        ),
    )


def _client_for(user_id: str) -> SupabaseClient:
    settings = _settings_for(user_id)
    return SupabaseClient(HttpClient(), settings, AccessTokenMinter(settings))


@pytest.fixture(scope="session")
def _stack_env() -> None:
    missing = [name for name in _REQUIRED if not os.environ.get(name)]
    if missing:
        pytest.skip(
            "local Supabase stack not configured; missing " + ", ".join(missing)
        )


@pytest.fixture
def supabase_client(_stack_env: None) -> SupabaseClient:
    return _client_for(SEED_USER_A)


@pytest.fixture
def other_supabase_client(_stack_env: None) -> SupabaseClient:
    return _client_for(SEED_USER_B)


def _truncate(client: SupabaseClient) -> None:
    for table in _TABLES_CHILD_FIRST:
        client.delete(table, params={"id": "not.is.null"})


@pytest.fixture
def store(supabase_client: SupabaseClient):
    from job_hunter.postgres_store import PostgresJobStore

    _truncate(supabase_client)
    instance = PostgresJobStore(supabase_client)
    try:
        yield instance
    finally:
        _truncate(supabase_client)
```

Read `supabase/seed.sql` and define `SEED_USER_A` / `SEED_USER_B` at the top of the file with the two fixed UUIDs it creates. Do not invent them.

- [ ] **Step 2: Run it to verify the fixture skips cleanly without a stack**

```bash
cd /Users/amitbaz/career-platform/apps/job-hunter && env -u SUPABASE_TEST_URL .venv/bin/python -m pytest tests/integration/ -v
```
Expected: skipped, not errored.

- [ ] **Step 3: Boot the stack in the CI test job**

In `.github/workflows/job-hunter-ci.yml`, the `test` job currently runs `pip install` then `pytest -q` with no database. Copy the stack setup that the `isolation` job already uses (`supabase/setup-cli@v1`, `bash supabase/local-signing-key.sh`, `supabase start`, all with `working-directory: .`), and export `SUPABASE_TEST_URL`, `SUPABASE_TEST_PUBLISHABLE_KEY`, `SUPABASE_TEST_SIGNING_KEY_B64` from `supabase status` before `pytest`.

- [ ] **Step 4: Verify the fixture connects when the stack is up**

```bash
cd /Users/amitbaz/career-platform && supabase start && eval "$(supabase status -o env | sed 's/^/export /')"
cd apps/job-hunter && .venv/bin/python -m pytest tests/integration/ -v
```
Expected: integration tests run rather than skip.

- [ ] **Step 5: Commit**

```bash
git add apps/job-hunter/tests/conftest.py .github/workflows/job-hunter-ci.yml
git commit -m "test: add a shared Postgres-backed store fixture and boot the stack in CI"
```

---

## Phase 2 — The store port (Tasks 7–12)

Tasks 8–11 all follow the identical cycle given in full in Task 8. They differ only in which methods they cover. Port a group, move its tests onto the `store` fixture, keep the suite green, commit.

### Task 7: PostgresJobStore skeleton and row mapping

**Files:**
- Create: `apps/job-hunter/src/job_hunter/postgres_store.py`
- Create: `apps/job-hunter/src/job_hunter/store_mapping.py`
- Test: `apps/job-hunter/tests/test_store_mapping.py` (create)

**Interfaces:**
- Produces:
  - `PostgresJobStore(client: SupabaseClient)` with `close()`, `__enter__`, `__exit__` (all no-ops except `close`, kept so call sites are unchanged).
  - `store_mapping.to_iso(value: datetime | None) -> str | None` — UTC-normalised ISO-8601 with offset.
  - `store_mapping.from_iso(value: str | None) -> datetime | None`
  - `store_mapping.job_from_row(row: dict) -> Job`, `evaluation_from_row`, `material_from_row`, `ats_entry_from_row`, `navigation_session_from_row`
  - `store_mapping.touch(values: dict) -> dict` — returns `values` with `updated_at` set to now. #66 deliberately added no trigger, so **Python must set `updated_at` on every write** to the four tables that have the column: `job_hunter_company_watch`, `job_hunter_ats_registry`, `job_hunter_pending_ai_work`, `job_hunter_gmail_sync_state`. Every write to those four goes through `touch()`; a test asserts `updated_at` advances on a second write.
- Note: there is no `read_only` parameter. It existed so the webhook could open an artifact snapshot without creating tables; there is no snapshot now.

- [ ] **Step 1: Write the failing test** for `to_iso`/`from_iso` round-tripping a naive datetime as UTC, and for `job_from_row` mapping a PostgREST dict to a `Job` — including `remote` as a nullable boolean (Postgres gives `True`/`False`/`None`; SQLite gave `1`/`0`/`None`).
- [ ] **Step 2: Run it** — `.venv/bin/python -m pytest tests/test_store_mapping.py -v`. Expected: FAIL, module not found.
- [ ] **Step 3: Implement** both modules. `PostgresJobStore.__init__` stores the client and nothing else — no schema creation, no migration machinery. Read `store.py:301-310` for the `_now_iso` / `_normalize_utc` helpers being replaced.
- [ ] **Step 4: Run it** — Expected: PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat: add the PostgresJobStore skeleton and row mapping helpers"`

---

### Task 8: Port the jobs group

**Files:**
- Modify: `apps/job-hunter/src/job_hunter/postgres_store.py`
- Modify: `apps/job-hunter/tests/test_store.py` (jobs tests onto the fixture)

**Interfaces:**
- Produces, on `PostgresJobStore` — signatures identical to `store.py` except every `int` job id becomes `str`:
  - `upsert_job(job: Job) -> tuple[str, bool, bool]` → `rpc("job_hunter_upsert_job", ...)`
  - `upsert_logical_job(job: Job) -> tuple[str, bool, bool]` → same function, logical-identity branch
  - `merge_jobs(survivor_id: str, duplicate_id: str) -> str` → `rpc("job_hunter_merge_jobs", ..., retry=False)`
  - `record_job_source(...) -> None` → `upsert(on_conflict="job_id,identity_key")`
  - `list_job_sources(job_id: str) -> list[dict]`
  - `find_job_by_canonical_url(url: str) -> str | None`
  - `find_job_by_ats(...) -> str | None`
  - `find_job_by_identity(...) -> str | None` → `rpc("job_hunter_find_job_by_identity", ...)`
  - `set_job_market(job_id: str, market_id: str | None) -> None`
  - `count_jobs() -> int`
  - `get_job(job_id: str) -> Job | None`
  - `list_jobs_for_matching() -> list[dict]`
  - `backfill_ats_identity() -> int` — uses `ilike`, not `like`

- [ ] **Step 1: Write the failing tests.** Move the existing jobs tests in `tests/test_store.py` onto the `store` fixture, deleting their local `JobStore(...)` construction. Add one new test that did not exist before, because the ordering semantics changed:

```python
def test_upsert_job_reports_description_change(store):
    job = make_job(fingerprint="fp-1", description="first")
    job_id, is_new, changed = store.upsert_job(job)
    assert is_new is True and changed is False

    same_id, is_new, changed = store.upsert_job(
        make_job(fingerprint="fp-1", description="second")
    )
    assert same_id == job_id
    assert is_new is False
    assert changed is True
```

- [ ] **Step 2: Run them to verify they fail** — `.venv/bin/python -m pytest tests/test_store.py -k job -v`. Expected: FAIL, methods missing.
- [ ] **Step 3: Implement the group** on `PostgresJobStore`.
- [ ] **Step 4: Run them to verify they pass** — Expected: PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat: port the jobs group of the store to Postgres"`

---

### Task 9: Port the evaluation and delivery group

Same five-step cycle as Task 8.

**Interfaces produced:** `save_evaluation`, `get_evaluation`, `needs_evaluation(job_id: str) -> bool`, `save_material`, `get_material`, `mark_delivered`, `has_delivery`, `pending_delivery_job_ids() -> list[str]` (via `rpc("job_hunter_pending_delivery_jobs", {"p_score_floor": _DELIVERABLE_SCORE_FLOOR})`).

**The behaviour change to test explicitly:** "latest evaluation" now means newest `evaluated_at`, not highest id. Add a test that writes two evaluations with the *same* `evaluated_at` and asserts `get_evaluation` returns a deterministic one (newest `created_at`, then id) rather than an arbitrary row.

`save_evaluation`, `save_material` and `mark_delivered` all use `upsert` against the Task 1 constraints, never `insert`.

- [ ] **Step 1: Write the failing tests** — [ ] **Step 2: Run, expect FAIL** — [ ] **Step 3: Implement** — [ ] **Step 4: Run, expect PASS** — [ ] **Step 5: Commit** `"feat: port the evaluation and delivery group of the store to Postgres"`

---

### Task 10: Port the discovery-state group

**Interfaces produced:** `upsert_company_watch`, `get_company_watch`, `list_due_company_watches`, `record_watch_success`, `record_watch_failure`, `upsert_ats_board`, `reject_ats_board`, `clear_ats_board_rejection`, `list_due_ats_boards`, `list_rejected_ats_boards`, `record_ats_scan_success`, `record_ats_scan_failure`, `record_ats_eligible_job`, `count_ats_boards`.

The `julianday()` due-checks become PostgREST filters: `active=eq.true&or=(paused_until.is.null,paused_until.lte.<iso>)`. The `CASE WHEN` updates become read-then-update over two requests — safe because both workflows share `concurrency: group: job-hunter-state`, so there is one writer.

`sources/company_watch.py:98,109` indexes a returned row as `watch["id"]`; that keeps working with dicts.

- [ ] **Step 1: Write the failing tests** — [ ] **Step 2: Run, expect FAIL** — [ ] **Step 3: Implement** — [ ] **Step 4: Run, expect PASS** — [ ] **Step 5: Commit** `"feat: port the company-watch and ATS registry groups of the store to Postgres"`

---

### Task 11: Port the Gmail and AI-accounting groups

**Interfaces produced:** `get_gmail_sync_state`, `save_gmail_sync_state`, `record_gmail_message`, `has_processed_gmail_message`, `stage_inbound_job`, `list_unmaterialized_inbound_jobs` (via RPC), `save_application_event`, `list_application_events`, `current_application_state`, `pending_review_events` (via RPC), `mark_review_delivered`, `release_legacy_gmail_semantic_failures`, `record_gemini_usage`, `gemini_usage_rows`, `set_gemini_pause`, `get_gemini_pause`, `clear_gemini_pause`, `get_candidate_context`, `save_candidate_context`, `enqueue_ai_work`, `list_pending_ai_work`, `complete_ai_work`.

Two renames to absorb: `gemini_usage` → `job_hunter_ai_usage` and `gemini_quota_state` → `job_hunter_ai_quota_state`, both with `provider` defaulting to `'gemini'`. Method names stay as they are — the provider-neutral rename is #73, not this ticket.

`record_gemini_usage` must pass a non-null `run_id` (Task 1 made it NOT NULL); fall back to `'unknown'` when `GEMINI_RUN_ID` is unset.

`gemini_usage.py:84-131` and `gmail_matching.py:38,57` annotate parameters as `sqlite3.Row`; retype to `dict[str, Any]`. `tests/test_store.py:196` asserts `"prompt" not in rows[0].keys()` — dicts have `.keys()`, so the assertion survives, but confirm `gemini_usage_rows` still projects away `prompt`/`response`.

- [ ] **Step 1: Write the failing tests** — [ ] **Step 2: Run, expect FAIL** — [ ] **Step 3: Implement** — [ ] **Step 4: Run, expect PASS** — [ ] **Step 5: Commit** `"feat: port the Gmail and AI-accounting groups of the store to Postgres"`

---

### Task 12: Fold in the three modules that bypass the store

`navigation_store.py`, `search_budget.py` and `gmail_linkedin_cleanup.py` each reach around `JobStore` — the first two by opening or borrowing a raw SQLite connection, the third by grabbing `store._conn` (`navigation_store.py:22`, `search_budget.py:32`, `gmail_linkedin_cleanup.py:32`). After this task there is exactly one way to reach the database.

**Files:**
- Delete: `apps/job-hunter/src/job_hunter/navigation_store.py`
- Modify: `search_budget.py`, `gmail_linkedin_cleanup.py`, `pipeline.py:44-45,864-866`
- Modify: `apps/job-hunter/src/job_hunter/postgres_store.py`

**Interfaces produced** — the five navigation functions become methods, so the `_StoreLike` Protocol and its `_conn` requirement disappear:
- `create_navigation_session(session: NavigationSession) -> None` → `upsert(on_conflict="user_id,session_id")`
- `attach_navigation_message_id(session_id: str, message_id: str) -> bool`
- `get_navigation_session(session_id: str) -> NavigationSession | None`
- `prune_navigation_sessions(now_iso: str) -> int`
- `ensure_navigation_schema` is **deleted** — the table is created by migration, not lazily at runtime.

`SearchUsageLedger` takes the `SupabaseClient` instead of a path. Its `BEGIN IMMEDIATE` atomic reservation (`search_budget.py:47,78`) has no PostgREST equivalent; a read-then-write is acceptable for one serialized writer, but note the weakening in the module docstring.

- [ ] **Step 1: Write the failing tests** — [ ] **Step 2: Run, expect FAIL** — [ ] **Step 3: Implement** — [ ] **Step 4: Run, expect PASS** — [ ] **Step 5: Commit** `"refactor: route navigation sessions, search budget and Gmail cleanup through the store"`

---

## Phase 3 — Cutover (Tasks 13–17)

### Task 13: Make job ids strings across the callers

**Files:**
- Modify: `models.py:358-360` (`NavigationCard.job_id: int` → `str`)
- Modify: `telegram_navigation.py:57` (`navigation_sort_key` tie-break)
- Modify: `cli.py` (`--job-id` type), `pipeline.py`, `discovery.py`, `gmail_sync.py`

**Interfaces:** every `job_id` parameter and return is `str`. `navigation_sort_key` currently returns `tuple[int, str, str, int]` ending in the integer id; it becomes `tuple[int, str, str, str]`.

- [ ] **Step 1: Write the failing test**

```python
def test_navigation_sort_key_orders_deterministically_with_uuid_ids():
    a = make_digest_item(score=90, company="Acme", title="Dev", job_id="00000000-0000-0000-0000-0000000000aa")
    b = make_digest_item(score=90, company="Acme", title="Dev", job_id="00000000-0000-0000-0000-0000000000bb")
    assert sorted([b, a], key=navigation_sort_key) == [a, b]
```

- [ ] **Step 2: Run it, expect FAIL** (`TypeError` comparing str to int, or a wrong order).
- [ ] **Step 3: Implement** the type changes.
- [ ] **Step 4: Run the whole suite, expect PASS** — `pnpm job-hunter:test`
- [ ] **Step 5: Commit** — `git commit -m "refactor: carry job ids as uuid strings across the app"`

---

### Task 14: Wire construction and delete the SQLite path

**Files:**
- Modify: `cli.py:91,94,127,130,150-152,162-165` — five construction sites, all becoming `PostgresJobStore(client)`
- Modify: `config.py:73,151` — remove `db_path`; `models.py:340` and `gmail_models.py:35` lose their `db_path` defaults
- Modify: `config.py:155-168` — remove `GITHUB_STATE_TOKEN`, `GITHUB_STATE_ARTIFACT_NAME`, `GITHUB_STATE_CACHE_DIR`
- Modify: `telegram_webhook.py:28-34`, `navigation_repository.py` — read live Postgres
- Delete: `store.py`, `github_state.py`, `scripts/restore_state.py`, `tests/test_store_read_only.py`, `tests/test_github_state.py`, `tests/test_restore_state.py`

`cli.py:150-152` builds a dry-run store as `JobStore(db_path, read_only=True)` and two `JobStore(":memory:")` instances. There is no read-only mode and no in-memory Postgres, so add a `DryRunStore` to `postgres_store.py`: it wraps a real `PostgresJobStore`, delegates every read, and discards every write, returning a synthetic `uuid4()` string where the real method would have returned an id and `False` for boolean write results. It never touches the client on a write path, so a dry run cannot mutate the live database — which is stronger than the old `:memory:` copy, since that one silently diverged from real state.

`pipeline.py:394` and `cli.py:91,127,164` derive a cover-letter directory from `Path(settings.db_path).parent`. With no `db_path` this needs its own setting; add `JOB_HUNTER_OUTPUT_DIR` defaulting to `var/`.

- [ ] **Step 1: Write the failing test** asserting `load_settings()` no longer exposes `db_path` and that `SupabaseSettings` is required at startup.
- [ ] **Step 2: Run, expect FAIL** — [ ] **Step 3: Implement and delete** — [ ] **Step 4: Run the whole suite, expect PASS** — [ ] **Step 5: Commit** `"refactor: make Postgres the only persistence path and delete the SQLite store"`

---

### Task 15: The one-shot data migration

**Files:**
- Create: `apps/job-hunter/scripts/migrate_sqlite_to_postgres.py`
- Create: `apps/job-hunter/tests/test_migrate_sqlite_to_postgres.py`

**Interfaces produced:** `migrate(sqlite_path: Path, client: SupabaseClient) -> dict[str, int]` — returns per-table row counts. Re-runnable: every write is an upsert against the table's user-scoped unique key.

Insert in foreign-key order, building an `old_int_id -> new_uuid` map as it goes: `jobs`, `job_sources`, `evaluations`, `materials`, `deliveries`, `company_watch`, `ats_registry`, `gmail_sync_state`, `gmail_messages`, `inbound_job_candidates`, `application_events`, `review_deliveries`, `search_api_usage`, `telegram_navigation_sessions`.

Skipped, because the next run rebuilds them: `pending_ai_work`, `gemini_quota_state`, `candidate_context_cache`.

- [ ] **Step 1: Write the failing tests.** The important one is the navigation cards, which is the only place an id hides inside a JSON blob:

```python
def test_navigation_cards_get_their_job_ids_remapped(tmp_path, supabase_client):
    sqlite_path = build_legacy_db(
        tmp_path,
        jobs=[{"id": 41, "fingerprint": "fp-a", "title": "Dev"}],
        sessions=[{
            "session_id": "s1",
            "cards_json": json.dumps([{"job_id": 41, "title": "Dev", "company": "Acme",
                                       "location": "", "score": 88, "url": "https://x"}]),
            "telegram_message_id": "77",
            "created_at": "2026-09-01T00:00:00+00:00",
            "expires_at": "2026-10-01T00:00:00+00:00",
        }],
    )

    migrate(sqlite_path, supabase_client)

    stored = supabase_client.select(
        "job_hunter_telegram_navigation_sessions", params={"session_id": "eq.s1"}
    )[0]
    migrated_job = supabase_client.select("job_hunter_jobs", params={"fingerprint": "eq.fp-a"})[0]
    assert stored["cards_json"][0]["job_id"] == migrated_job["id"]
    assert stored["cards_json"][0]["job_id"] != 41


def test_cards_whose_job_did_not_migrate_are_dropped(tmp_path, supabase_client):
    sqlite_path = build_legacy_db(
        tmp_path,
        jobs=[],
        sessions=[{
            "session_id": "s2",
            "cards_json": json.dumps([{"job_id": 999, "title": "Gone", "company": "",
                                       "location": "", "score": 10, "url": ""}]),
            "telegram_message_id": None,
            "created_at": "2026-09-01T00:00:00+00:00",
            "expires_at": "2026-10-01T00:00:00+00:00",
        }],
    )

    migrate(sqlite_path, supabase_client)

    stored = supabase_client.select(
        "job_hunter_telegram_navigation_sessions", params={"session_id": "eq.s2"}
    )
    assert stored == [] or stored[0]["cards_json"] == []


def test_migration_is_rerunnable(tmp_path, supabase_client):
    sqlite_path = build_legacy_db(tmp_path, jobs=[{"id": 1, "fingerprint": "fp-x", "title": "Dev"}])
    first = migrate(sqlite_path, supabase_client)
    second = migrate(sqlite_path, supabase_client)
    assert first == second
    assert len(supabase_client.select("job_hunter_jobs", params={"fingerprint": "eq.fp-x"})) == 1
```

Write `build_legacy_db` as a test helper that creates a SQLite file with the old schema — copy the `CREATE TABLE` statements from `store.py:36-299` before that file is deleted in Task 14, or take them from git history afterwards.

- [ ] **Step 2: Run, expect FAIL** — [ ] **Step 3: Implement** — [ ] **Step 4: Run, expect PASS** — [ ] **Step 5: Commit** `"feat: add the one-shot SQLite to Postgres data migration"`

---

### Task 16: Strip the artifact from both workflows

**Files:**
- Modify: `.github/workflows/job-hunter-daily.yml` — delete lines 28-33 (restore) and 63-69 (upload); delete `permissions: actions: read` (7-9); add the four Supabase secrets
- Modify: `.github/workflows/job-hunter-generate-cover-letter.yml` — delete lines 26-30 and 45-51; add the four Supabase secrets

**Keep** `concurrency: group: job-hunter-state` in both. It no longer guards a file, but the read-then-update pairs in Tasks 10 and 12 assume a single writer.

Secrets to add to both: `JOB_HUNTER_USER_ID`, `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, `SUPABASE_SIGNING_KEY_B64`. None are wired into any workflow today — they must be created in repository settings before the first run.

- [ ] **Step 1: Write the failing check**

```bash
cd /Users/amitbaz/career-platform && ! grep -rn 'sqlite3\|upload-artifact\|restore_state' .github/workflows/
```
Expected: currently exits 1 (matches found).

- [ ] **Step 2: Edit both workflows.**
- [ ] **Step 3: Re-run the check** — Expected: exits 0, no matches.
- [ ] **Step 4: Verify no orphaned references** — `grep -rn 'job-hunter-state\|JOB_HUNTER_DB_PATH' . --exclude-dir=.git` returns only the two `concurrency:` lines.
- [ ] **Step 5: Commit** — `git commit -m "feat: remove the SQLite state artifact from both Job Hunter workflows"`

---

### Task 17: Documentation sweep

**Files:**
- Modify: `apps/job-hunter/AGENTS.md` — rules 1, 2 and 7 (`:24-30`, `:31`, `:36`); the source-of-truth statements (`:39`, `:41`); the `store.py` module description (`:137`); the whole "GitHub Actions state persistence" section (`:144-150`); the "not yet required by any runtime path" note on the Supabase variables (`:156-157`); and the stale `pip install -e '.[test]'` line, which should read `'.[test,webhook]'`
- Modify: `apps/job-hunter/README.md` — every SQLite and artifact reference (`:9,13,29,54,56,58,60,73,134,137,198,202,223,234`)
- Modify: root `AGENTS.md:57-58` — the `hashFiles`/`upload-artifact` warning, now that no workflow uploads an artifact

Rule 1 currently says application code must not target the `job_hunter_*` tables outside #70. Rewrite it to say Postgres is the persistence layer — do not simply delete the rule.

- [ ] **Step 1: Make the edits.**
- [ ] **Step 2: Verify no stale claims remain** — `grep -rn -i 'sqlite' apps/job-hunter/*.md AGENTS.md docs/` returns only historical references in `docs/superpowers/` specs and plans.
- [ ] **Step 3: Run the full suite** — `pnpm test`
- [ ] **Step 4: Commit** — `git commit -m "docs: make Postgres the documented source of truth for Job Hunter"`

---

## Acceptance

The ticket's three criteria, and how each is proven:

1. **A full daily run completes against Postgres with no SQLite file involved.** Run the daily entrypoint locally against the local stack with no `var/` directory present; confirm it completes and `find . -name '*.sqlite3'` returns nothing.
2. **No artifact upload remains in the workflow.** `grep -rn 'upload-artifact\|restore_state' .github/workflows/` returns nothing (Task 16, Step 3).
3. **Existing owner history is intact after migration.** Per-table row counts from `migrate()` match the source SQLite counts for every carried table, and a spot-check of one pre-migration Telegram digest's "Gen CL" button generates a cover letter for the right job.

## Runbook (order matters)

1. Merge order is irrelevant, but **run the data migration before deleting the artifact.** The only copy of production history is the last `job-hunter-state` artifact.
2. Create the four Supabase secrets in repository settings and in the Vercel project.
3. Apply migrations to the hosted project: `supabase db push`.
4. Download the latest artifact, run `migrate_sqlite_to_postgres.py` against it, check the row counts.
5. Trigger the daily workflow manually and confirm it completes.
6. Only then let the artifact expire. Do not delete it by hand.
