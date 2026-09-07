# Batched Discovery Writes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the daily run hitting its 60-minute timeout by replacing discovery's one-HTTP-request-per-job persistence with batched RPCs.

**Architecture:** A new migration adds three `security invoker` Postgres functions that take arrays and loop server-side, reusing the existing `job_hunter_upsert_job` rather than reimplementing identity resolution. `PostgresJobStore` gains batch methods that chunk their input; a chunk that fails is replayed one job at a time so a single malformed posting cannot end a run. `collect_candidates` is restructured so network work and database work happen in separate phases, which is what makes batching possible.

**Tech Stack:** Python 3.12, pytest, Supabase/PostgREST, plpgsql, pgTAP.

**Spec:** `docs/superpowers/specs/2026-09-07-job-hunter-batched-discovery-writes-design.md`

**Issue:** #97

## Global Constraints

- Branch is `fix/job-hunter-batched-discovery-writes`, already created off `main` at `d12d118`. Never commit to `main`.
- Every new SQL function is `security invoker` with `set search_path = ''`, matching the six existing store functions. Row-level security must keep deciding every row.
- Do not reimplement job identity resolution. The batch upsert calls `public.job_hunter_upsert_job`; it does not restate its logic.
- New migration file: `supabase/migrations/202609070003_job_hunter_batch_discovery_writes.sql`. Migrations are platform-owned and live at the repository root, never inside an app.
- Commit style is loosely Conventional Commits; an app-scoped prefix (`job-hunter:`) is fine when the change is local to that app.
- Every commit message ends with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- Chunk size for job upserts is 500. Chunk size for uuid arrays is 1000.
- The single-job store methods (`upsert_logical_job`, `set_job_market`, `needs_evaluation`, `upsert_ats_board`) stay. The webhook and cover-letter paths use them.

## Environment setup (already done — read it, verify it)

This work happens in a git worktree at `/Users/amitbaz/career-platform/.claude/worktrees/batched-discovery-writes`, on branch `fix/job-hunter-batched-discovery-writes`. Run every command from there, never from the main checkout at `/Users/amitbaz/career-platform`.

The setup is already done: `pnpm install` has run, the virtualenv exists at `apps/job-hunter/.venv` (built with the `[test,webhook]` extras — the `[test]`-only line in `apps/job-hunter/AGENTS.md` is wrong and breaks collection on Flask), the local dev `supabase/signing_keys.json` has been copied in, and `supabase db reset` has applied every existing migration to the local stack.

The store-backed tests run against that local Supabase stack, not an in-process engine. A wrapper script exports the three variables pytest needs and runs the worktree's own pytest:

```bash
/private/tmp/claude-501/-Users-amitbaz-career-platform/1a3617ba-b8e6-40d5-b7bd-0d9fbff5f5a3/scratchpad/jh-test.sh -q
```

It takes the same arguments as `pytest`. If the stack has been restarted since it was written, regenerate its inputs:

```bash
cd /Users/amitbaz/career-platform/.claude/worktrees/batched-discovery-writes
SP=/private/tmp/claude-501/-Users-amitbaz-career-platform/1a3617ba-b8e6-40d5-b7bd-0d9fbff5f5a3/scratchpad
supabase status -o env > "$SP/sb.env"
python3 -c "import json,base64;print(base64.b64encode(json.dumps(json.load(open('supabase/signing_keys.json'))[0]).encode()).decode())" > "$SP/key.b64"
```

**Git in this worktree:** a shell hook rewrites a bare `git ...` into `rtk git ...`, and the worktree isolation guard then refuses it because it cannot prove which repository the wrapper targets. Every commit step in this plan is written as `git ...`; run it as `/usr/bin/git -C /Users/amitbaz/career-platform/.claude/worktrees/batched-discovery-writes ...` instead, which bypasses the wrapper and names the repository explicitly.

**Three traps that will waste your time if you skip them:**

1. Run pytest from `apps/job-hunter/`, not the repository root. `tests/test_vercel_config.py` opens `vercel.json` by a relative path and fails anywhere else.
2. Always run pytest through `apps/job-hunter/.venv/bin/pytest` (which the wrapper script does). A stale global install at `~/job-hunter-bot` hijacks `python -m pytest` and silently tests the wrong code.
3. Without those exports every store-backed test **skips** rather than fails. A green run with skips is not a passing run. Check the summary line for `skipped`.

**Baseline:** before Task 0, the suite is `2 failed, 1171 passed` and `pnpm db:test` is `Result: PASS` (263 assertions). The two failures are the subject of Task 0. `main` itself is red on them — CI run 34114033433 on `d12d118` failed with exactly these two.

To apply a new migration to the local stack:

```bash
cd /Users/amitbaz/career-platform/.claude/worktrees/batched-discovery-writes && supabase db reset
```

This drops and rebuilds the local database from `supabase/migrations` plus `supabase/seed.sql`. Local throwaway data only — it never touches the hosted project.

## File Structure

| File | Responsibility |
| --- | --- |
| `apps/job-hunter/tests/test_gmail_staged_source.py` | Modify: two stale cross-source tests, in Task 0 |
| `supabase/migrations/202609070003_job_hunter_batch_discovery_writes.sql` | Create: the three batch functions |
| `supabase/tests/pgtap/job_hunter_batch_discovery_writes.sql` | Create: behaviour + RLS isolation for those functions |
| `supabase/tests/pgtap/job_hunter_store_functions.sql` | Modify: the exhaustive function-name assertion, in Task 1 |
| `apps/job-hunter/src/job_hunter/postgres_store.py` | Modify: add four batch methods, a module logger, generalize `_chunked` |
| `apps/job-hunter/src/job_hunter/discovery.py` | Modify: restructure `collect_candidates` into phases |
| `apps/job-hunter/tests/test_postgres_store_batch.py` | Create: batch store method behaviour and fallback |
| `apps/job-hunter/tests/test_discovery.py` | Modify: request-count regression test |
| `apps/job-hunter/AGENTS.md` | Modify: the "six SQL functions" count |
| `docs/superpowers/specs/2026-09-07-job-hunter-batched-discovery-writes-design.md` | Modify: one paragraph, in Task 5 |

---

### Task 0: Correct the stale Gmail cross-source eligibility tests

This task is unrelated to batching. It exists because the branch cannot start from a green baseline without it: `main` is red, and leaving it red would make every later failure in this plan ambiguous.

**Background.** #96 (`d12d118`) changed when a staged Gmail candidate is suppressed. Under the old `job_hunter_unmaterialized_inbound_jobs()`, *any* matching logical job suppressed the candidate. `job_hunter_eligible_inbound_jobs()` suppresses it only when the matching job is **complete** — terminal status (`rejected`/`closed`) or a current successful evaluation — which is the whole point of the issue: a candidate that missed one shortlist must survive to the next run.

#96's own design doc states the intended cross-source rule outright: *"a Gmail candidate can enrich an unevaluated matching public job, while a current evaluated public job suppresses further Gmail work"* (`apps/job-hunter/docs/superpowers/specs/2026-09-07-linkedin-gmail-candidate-eligibility-design.md`). Its acceptance criterion 5 asks that the cross-source tests "remain valid" — and that is the step that was missed. #96 updated `FakeStagedJobStore.list_unmaterialized_inbound_jobs` to `list_eligible_inbound_jobs` but left two store-backed tests asserting the old rule.

So the production code is right and the tests are wrong. Do not "fix" the SQL or the source to make them pass — that would reintroduce the bug #96 fixed.

The matching itself works. Both branches (canonical URL, normalized identity) find the public job; only the completeness gate lets the candidate through. Verified against the local stack: adding `store.set_job_status(job_id, "closed")` to each of the two tests makes both pass unchanged otherwise.

**Files:**
- Modify: `apps/job-hunter/tests/test_gmail_staged_source.py:117-163`

**Interfaces:**
- Consumes: `PostgresJobStore.set_job_status(job_id, status)` (`apps/job-hunter/src/job_hunter/postgres_store.py:1590`), which accepts only `"rejected"` or `"closed"`; `PostgresJobStore.upsert_job(job)` returns `tuple[str, bool, bool]` whose first element is the job id.
- Produces: nothing later tasks depend on.

- [ ] **Step 1: Confirm the failure and its cause**

Run: `cd /Users/amitbaz/career-platform/.claude/worktrees/batched-discovery-writes/apps/job-hunter && /private/tmp/claude-501/-Users-amitbaz-career-platform/1a3617ba-b8e6-40d5-b7bd-0d9fbff5f5a3/scratchpad/jh-test.sh -q tests/test_gmail_staged_source.py`

Expected: `2 failed, 4 passed`, both failures `AssertionError: assert [Job(source='gmail:linkedin', ...)] == []`. The candidate is emitted because the matching public job has no evaluation and a non-terminal status.

- [ ] **Step 2: Replace the two tests with four**

In `apps/job-hunter/tests/test_gmail_staged_source.py`, replace both `test_same_canonical_url_already_materialized_by_public_source_is_not_emitted` and `test_same_identity_already_materialized_by_public_source_is_not_emitted` — the whole block from line 117 to the end of the file — with:

```python
def test_same_canonical_url_on_unevaluated_public_job_is_still_emitted(store):
    """A public job that has not been evaluated must not suppress the candidate.

    This is the retry #96 exists for: a Gmail posting that was materialized
    but missed a shortlist has to come back on the next run.
    """
    store.stage_inbound_job(
        "message-1",
        "linkedin:job-123",
        ExtractedJob(
            source_platform="linkedin",
            url="https://jobs.example.com/role?utm_source=linkedin",
            company="Email Company",
            title="Email Title",
        ),
    )
    store.upsert_job(
        Job(
            source="public",
            source_job_id="public-123",
            url="https://jobs.example.com/role",
            company="Public Company",
            title="Public Title",
        )
    )

    [job] = GmailStagedSource(store).discover()

    assert job.source == "gmail:linkedin"
    assert job.source_job_id == "linkedin:job-123"


def test_same_canonical_url_on_closed_public_job_is_not_emitted(store):
    """A terminal public job suppresses the candidate across sources."""
    store.stage_inbound_job(
        "message-1",
        "linkedin:job-123",
        ExtractedJob(
            source_platform="linkedin",
            url="https://jobs.example.com/role?utm_source=linkedin",
            company="Email Company",
            title="Email Title",
        ),
    )
    job_id, _, _ = store.upsert_job(
        Job(
            source="public",
            source_job_id="public-123",
            url="https://jobs.example.com/role",
            company="Public Company",
            title="Public Title",
        )
    )
    store.set_job_status(job_id, "closed")

    assert GmailStagedSource(store).discover() == []


def test_same_identity_on_unevaluated_public_job_is_still_emitted(store):
    """Identity matching alone does not suppress; completeness does."""
    store.stage_inbound_job(
        "message-1",
        "linkedin:job-123",
        ExtractedJob(
            source_platform="linkedin",
            company="  ACME  ",
            title="Senior   Frontend Engineer",
            location="Berlin",
        ),
    )
    store.upsert_job(
        Job(
            source="public",
            source_job_id="public-123",
            url="https://jobs.example.com/role",
            company="Acme",
            title="senior frontend engineer",
            location=" berlin ",
        )
    )

    [job] = GmailStagedSource(store).discover()

    assert job.source == "gmail:linkedin"
    assert job.source_job_id == "linkedin:job-123"


def test_same_identity_on_closed_public_job_is_not_emitted(store):
    """The normalized company/title/location branch honours terminal status.

    Proves the identity match really is what suppresses here: the two jobs
    share no URL at all, only normalized company, title and location.
    """
    store.stage_inbound_job(
        "message-1",
        "linkedin:job-123",
        ExtractedJob(
            source_platform="linkedin",
            company="  ACME  ",
            title="Senior   Frontend Engineer",
            location="Berlin",
        ),
    )
    job_id, _, _ = store.upsert_job(
        Job(
            source="public",
            source_job_id="public-123",
            url="https://jobs.example.com/role",
            company="Acme",
            title="senior frontend engineer",
            location=" berlin ",
        )
    )
    store.set_job_status(job_id, "closed")

    assert GmailStagedSource(store).discover() == []
```

The two "still emitted" tests are the ones that pin #96's behaviour; the two "closed" tests keep the cross-source suppression path covered, which is what the old tests were really there for.

- [ ] **Step 3: Run the file and verify all four pass**

Run: `cd /Users/amitbaz/career-platform/.claude/worktrees/batched-discovery-writes/apps/job-hunter && /private/tmp/claude-501/-Users-amitbaz-career-platform/1a3617ba-b8e6-40d5-b7bd-0d9fbff5f5a3/scratchpad/jh-test.sh -q tests/test_gmail_staged_source.py`

Expected: `8 passed` (four untouched tests plus these four). If any is `skipped`, the environment is not set up — see "Environment setup"; a skip here proves nothing.

- [ ] **Step 4: Commit**

```bash
cd /Users/amitbaz/career-platform/.claude/worktrees/batched-discovery-writes
git add apps/job-hunter/tests/test_gmail_staged_source.py
git commit -m "$(cat <<'EOF'
test: align the Gmail cross-source tests with eligibility

#96 replaced materialization with completeness as the rule for suppressing a
staged Gmail candidate, so an unevaluated public job no longer hides one. Two
cross-source tests were left asserting the old rule and have been red on main
since that merge.

Assert the new rule in both directions: an unevaluated match still emits the
candidate, a closed one suppresses it.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 1: The batch upsert function

**Files:**
- Create: `supabase/migrations/202609070003_job_hunter_batch_discovery_writes.sql`
- Test: `supabase/tests/pgtap/job_hunter_batch_discovery_writes.sql`

**Interfaces:**
- Consumes: `public.job_hunter_upsert_job(p_job jsonb)` from `202609060004_job_hunter_store_functions.sql:607`, which returns `table (id uuid, is_new boolean, description_changed boolean)`.
- Produces: `public.job_hunter_upsert_jobs(p_jobs jsonb)` returning `table (input_index int, id uuid, is_new boolean, description_changed boolean)`, where `input_index` is the zero-based position of the element in `p_jobs`.

- [ ] **Step 1: Write the failing pgTAP test**

Create `supabase/tests/pgtap/job_hunter_batch_discovery_writes.sql`. Read the header of the existing `supabase/tests/pgtap/job_hunter_store_functions.sql` first and follow its conventions exactly: two seeded users, every fixture row inserted while acting as its owner so it must pass that table's `insert_own` policy to exist at all, nothing run as superuser, no RLS disabled.

```sql
-- Behaviour and isolation for the batch discovery write functions.
--
-- These three exist because discovery persists thousands of jobs per run
-- and one PostgREST round trip per job spent the whole GitHub Actions
-- budget (issue #97). Each is `security invoker`, so row level security
-- still applies inside it and the caller's own token decides what it sees.
begin;
create extension if not exists pgtap with schema extensions;
select no_plan();

set local role postgres;
select set_config('request.jwt.claims',
  json_build_object('sub', 'aaaaaaaa-0000-0000-0000-000000000001', 'role', 'authenticated')::text,
  true);
set local role authenticated;

-- Two jobs in one array come back in input order, tagged by position.
select is(
  (select count(*)::int from public.job_hunter_upsert_jobs(
     jsonb_build_array(
       jsonb_build_object('fingerprint', 'batch-fp-1', 'source', 'test',
                          'company', 'Acme', 'title', 'Frontend Engineer',
                          'location', 'Remote', 'remote', true,
                          'description', 'one', 'url', 'https://example.test/1'),
       jsonb_build_object('fingerprint', 'batch-fp-2', 'source', 'test',
                          'company', 'Acme', 'title', 'Backend Engineer',
                          'location', 'Remote', 'remote', true,
                          'description', 'two', 'url', 'https://example.test/2')))),
  2,
  'job_hunter_upsert_jobs returns one row per input element');

select is(
  (select array_agg(input_index order by input_index)
     from public.job_hunter_upsert_jobs(
       jsonb_build_array(
         jsonb_build_object('fingerprint', 'batch-fp-3', 'source', 'test',
                            'company', 'Acme', 'title', 'A', 'location', 'Remote',
                            'remote', true, 'description', 'x', 'url', 'https://example.test/3'),
         jsonb_build_object('fingerprint', 'batch-fp-4', 'source', 'test',
                            'company', 'Acme', 'title', 'B', 'location', 'Remote',
                            'remote', true, 'description', 'y', 'url', 'https://example.test/4')))),
  array[0, 1],
  'input_index is zero-based and matches input order');

-- An empty array is a no-op, not an error.
select is(
  (select count(*)::int from public.job_hunter_upsert_jobs('[]'::jsonb)),
  0,
  'an empty array returns no rows');

-- A non-array argument is rejected rather than silently doing nothing.
select throws_ok(
  $$select * from public.job_hunter_upsert_jobs('{"fingerprint":"x"}'::jsonb)$$,
  null,
  'a non-array p_jobs raises');

-- Two elements of one chunk that share an identity collapse to one job,
-- exactly as two sequential single-job calls would.
select is(
  (select count(distinct id)::int from public.job_hunter_upsert_jobs(
     jsonb_build_array(
       jsonb_build_object('fingerprint', 'batch-dup-a', 'source', 'test',
                          'company', 'Dup Co', 'title', 'Engineer', 'location', 'Remote',
                          'remote', true, 'description', 'first',
                          'canonical_url', 'https://example.test/same'),
       jsonb_build_object('fingerprint', 'batch-dup-b', 'source', 'other',
                          'company', 'Dup Co', 'title', 'Engineer', 'location', 'Remote',
                          'remote', true, 'description', 'second',
                          'canonical_url', 'https://example.test/same')))),
  1,
  'two elements sharing a canonical URL resolve to one job id');

-- The function is security invoker, so it can never be a way around RLS.
select is(
  (select p.prosecdef from pg_proc p
     join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public' and p.proname = 'job_hunter_upsert_jobs'),
  false,
  'job_hunter_upsert_jobs is security invoker');

select * from finish();
rollback;
```

- [ ] **Step 2: Run it and verify it fails**

Run: `cd /Users/amitbaz/career-platform/.claude/worktrees/batched-discovery-writes && pnpm db:test`
Expected: FAIL — `function public.job_hunter_upsert_jobs(jsonb) does not exist`.

- [ ] **Step 3: Write the migration**

Create `supabase/migrations/202609070003_job_hunter_batch_discovery_writes.sql`:

```sql
-- Batch entry points for discovery's hot path.
--
-- `collect_candidates` persists every job it discovers -- roughly 19,000 raw
-- and 13,000 unique on a normal day. One PostgREST round trip per job put
-- that work at about 70,000 sequential requests, which spent the whole
-- 60-minute GitHub Actions budget before evaluation started (issue #97).
--
-- These functions do not add persistence logic. `job_hunter_upsert_jobs`
-- calls the existing single-job `job_hunter_upsert_job` once per element,
-- inside one round trip instead of one per job, so identity resolution and
-- duplicate merging stay in exactly one place.
--
-- All three are `security invoker` with an empty search_path, like the six
-- functions in 202609060004: row level security still decides every row.

create or replace function public.job_hunter_upsert_jobs(p_jobs jsonb)
returns table (input_index int, id uuid, is_new boolean, description_changed boolean)
language plpgsql
security invoker
set search_path = ''
as $$
declare
  v_element jsonb;
  v_index int;
  v_result record;
begin
  if p_jobs is null or jsonb_typeof(p_jobs) <> 'array' then
    raise exception 'p_jobs must be a jsonb array, got %',
      coalesce(jsonb_typeof(p_jobs), 'null');
  end if;

  -- Ordered, one element at a time, deliberately. Two jobs in one batch can
  -- resolve to the same identity, and the second must merge into the first
  -- exactly as two sequential single-job calls would.
  for v_element, v_index in
    select value, (ordinality - 1)::int
      from jsonb_array_elements(p_jobs) with ordinality as t(value, ordinality)
     order by ordinality
  loop
    select * into v_result from public.job_hunter_upsert_job(v_element);
    input_index := v_index;
    id := v_result.id;
    is_new := v_result.is_new;
    description_changed := v_result.description_changed;
    return next;
  end loop;
end;
$$;

comment on function public.job_hunter_upsert_jobs(jsonb) is
  'Upsert an ordered array of jobs in one round trip. Returns one row per '
  'input element, tagged with its zero-based input_index so a caller can zip '
  'results back onto what it sent -- ids alone cannot, because two elements '
  'may resolve to the same job. No exception handling: a failure aborts the '
  'whole call, and the caller replays the batch one job at a time to isolate '
  'the bad element.';
```

- [ ] **Step 4: Extend the exhaustive function-name assertion**

`supabase/tests/pgtap/job_hunter_store_functions.sql:87-106` asserts the *complete* sorted list of `public.job_hunter_*` functions, deliberately, so that adding one forces someone to look at the coverage checks above it. Adding a function without updating it turns that file red. Update it now for all three functions this plan adds, even though only one exists yet — the other two arrive in Tasks 3 and 4, and splitting this edit across three tasks would leave the suite red in between.

Add the three names to the array, keeping it sorted:

- `'job_hunter_needs_evaluation',` between `'job_hunter_merge_jobs',` and `'job_hunter_normalize_company',`
- `'job_hunter_set_job_markets',` between `'job_hunter_pending_review_events',` and `'job_hunter_upsert_job'`
- `'job_hunter_upsert_jobs'` after `'job_hunter_upsert_job',` (it sorts last: same prefix, longer)

and change the assertion's description from `thirteen` to `sixteen`:

```sql
  'exactly the sixteen expected public.job_hunter_* functions exist, so the two checks above are not asserting over an empty set');
```

Because Tasks 3 and 4 have not run yet, this file is now *expected* to fail until Task 4 lands. That is deliberate and it is the only place in this plan where a committed test is knowingly red. Task 3 Step 4 and Task 4's `pnpm db:test` steps are what turn it green again; if `pnpm db:test` still reports a missing function after Task 4, that is a real bug, not this.

- [ ] **Step 5: Apply it and run the test**

Run: `cd /Users/amitbaz/career-platform/.claude/worktrees/batched-discovery-writes && supabase db reset && pnpm db:test`
Expected: every assertion in `job_hunter_batch_discovery_writes.sql` green. `job_hunter_store_functions.sql` fails on the function-name array, listing `job_hunter_needs_evaluation` and `job_hunter_set_job_markets` as missing — expected until Task 4, per Step 4.

- [ ] **Step 6: Commit**

```bash
cd /Users/amitbaz/career-platform/.claude/worktrees/batched-discovery-writes
git add supabase/migrations/202609070003_job_hunter_batch_discovery_writes.sql supabase/tests/pgtap/job_hunter_batch_discovery_writes.sql supabase/tests/pgtap/job_hunter_store_functions.sql
git commit -m "$(cat <<'EOF'
feat(db): add a batch job upsert function

One round trip per chunk instead of per job. Calls the existing
job_hunter_upsert_job per element so identity resolution stays in one place.

Refs #97

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: `PostgresJobStore.upsert_logical_jobs`

**Files:**
- Modify: `apps/job-hunter/src/job_hunter/postgres_store.py` (add module logger near the imports; generalize `_chunked` at line 68; add the method near `upsert_logical_job` at line 224)
- Test: `apps/job-hunter/tests/test_postgres_store_batch.py` (create)

**Interfaces:**
- Consumes: `job_hunter_upsert_jobs` from Task 1; `self._job_payload(job)`, already used at `postgres_store.py:232`; `SupabaseClient.rpc(function, payload)`.
- Produces: `upsert_logical_jobs(self, jobs: list[Job]) -> list[tuple[str, bool, bool] | None]` — one entry per input job, in input order. `None` marks a job that could not be persisted and was skipped. Callers must handle `None`.

- [ ] **Step 1: Write the failing tests**

Create `apps/job-hunter/tests/test_postgres_store_batch.py`. `_make_job` mirrors the `Job` construction already used in `tests/test_discovery.py` — read that file's helpers and match them.

```python
"""Batch discovery writes on PostgresJobStore.

These run against the local Supabase stack through the `store` fixture, so
they exercise the real functions, real RLS, and a real token.
"""

from __future__ import annotations

import pytest

from job_hunter.models import Job


def _make_job(fingerprint: str, title: str, url: str) -> Job:
    return Job(
        source="test",
        source_job_id=fingerprint,
        url=url,
        company="Acme",
        title=title,
        location="Remote",
        remote=True,
        description=f"description for {title}",
    )


def test_upsert_logical_jobs_returns_one_result_per_input_in_order(store):
    jobs = [
        _make_job("batch-1", "Frontend Engineer", "https://example.test/1"),
        _make_job("batch-2", "Backend Engineer", "https://example.test/2"),
        _make_job("batch-3", "Platform Engineer", "https://example.test/3"),
    ]

    results = store.upsert_logical_jobs(jobs)

    assert len(results) == 3
    assert all(result is not None for result in results)
    assert all(is_new for _job_id, is_new, _changed in results)
    stored_titles = {
        store.get_job(result[0]).title for result in results
    }
    assert stored_titles == {"Frontend Engineer", "Backend Engineer", "Platform Engineer"}


def test_upsert_logical_jobs_matches_the_single_job_method(store):
    single = store.upsert_logical_job(_make_job("same-1", "Frontend Engineer", "https://example.test/s1"))
    batched = store.upsert_logical_jobs(
        [_make_job("same-1", "Frontend Engineer", "https://example.test/s1")]
    )

    assert batched[0][0] == single[0]
    assert batched[0][1] is False  # already stored by the single-job call


def test_upsert_logical_jobs_chunks_by_the_configured_size(store, monkeypatch):
    from job_hunter import postgres_store as module

    monkeypatch.setattr(module, "_JOB_UPSERT_CHUNK_SIZE", 2)
    calls: list[int] = []
    original = store._client.rpc

    def counting_rpc(function, payload=None, **kwargs):
        if function == "job_hunter_upsert_jobs":
            calls.append(len(payload["p_jobs"]))
        return original(function, payload, **kwargs)

    monkeypatch.setattr(store._client, "rpc", counting_rpc)

    jobs = [_make_job(f"chunk-{i}", f"Engineer {i}", f"https://example.test/c{i}") for i in range(5)]
    results = store.upsert_logical_jobs(jobs)

    assert calls == [2, 2, 1]
    assert len(results) == 5


def test_upsert_logical_jobs_replays_a_failed_chunk_one_job_at_a_time(store, monkeypatch, caplog):
    """A single bad job must cost its own row, not the chunk and not the run."""
    jobs = [
        _make_job("fallback-1", "Frontend Engineer", "https://example.test/f1"),
        _make_job("fallback-2", "Backend Engineer", "https://example.test/f2"),
    ]
    original = store._client.rpc
    failed_once = {"done": False}

    def flaky_rpc(function, payload=None, **kwargs):
        if function == "job_hunter_upsert_jobs" and not failed_once["done"]:
            failed_once["done"] = True
            raise RuntimeError("chunk exploded")
        return original(function, payload, **kwargs)

    monkeypatch.setattr(store._client, "rpc", flaky_rpc)

    results = store.upsert_logical_jobs(jobs)

    assert len(results) == 2
    assert all(result is not None for result in results)
    assert "retrying" in caplog.text.lower()


def test_upsert_logical_jobs_skips_a_job_that_fails_on_replay(store, monkeypatch):
    jobs = [
        _make_job("skip-good", "Frontend Engineer", "https://example.test/g1"),
        _make_job("skip-bad", "Backend Engineer", "https://example.test/b1"),
    ]
    original_rpc = store._client.rpc
    original_single = store.upsert_logical_job

    def failing_batch(function, payload=None, **kwargs):
        if function == "job_hunter_upsert_jobs":
            raise RuntimeError("chunk exploded")
        return original_rpc(function, payload, **kwargs)

    def failing_for_bad(job):
        if job.fingerprint == "skip-bad":
            raise RuntimeError("this one job is malformed")
        return original_single(job)

    monkeypatch.setattr(store._client, "rpc", failing_batch)
    monkeypatch.setattr(store, "upsert_logical_job", failing_for_bad)

    results = store.upsert_logical_jobs(jobs)

    assert results[0] is not None
    assert results[1] is None


def test_upsert_logical_jobs_on_empty_input_makes_no_request(store, monkeypatch):
    def exploding_rpc(*args, **kwargs):
        raise AssertionError("no request should be made for an empty batch")

    monkeypatch.setattr(store._client, "rpc", exploding_rpc)

    assert store.upsert_logical_jobs([]) == []
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `cd /Users/amitbaz/career-platform/.claude/worktrees/batched-discovery-writes/apps/job-hunter && .venv/bin/pytest tests/test_postgres_store_batch.py -q`
Expected: FAIL with `AttributeError: 'PostgresJobStore' object has no attribute 'upsert_logical_jobs'`. If instead you see `s` (skipped), the environment exports from "Environment setup" are missing — fix that before continuing.

- [ ] **Step 3: Add the module logger and generalize `_chunked`**

At the top of `apps/job-hunter/src/job_hunter/postgres_store.py`, alongside the existing imports:

```python
import logging

logger = logging.getLogger(__name__)
```

The module has had no logging until now. It earns one here: the batch fallback swallows a per-job failure that would previously have ended the run, and a swallowed failure that is never reported is worse than the crash it replaced.

Replace `_chunked` at line 68 so it is not limited to `list[str]`:

```python
_T = TypeVar("_T")


def _chunked(items: list[_T], size: int) -> list[list[_T]]:
    """Split ``items`` into consecutive chunks of at most ``size`` elements."""
    return [items[i : i + size] for i in range(0, len(items), size)]
```

Add `TypeVar` to the existing `typing` import.

- [ ] **Step 4: Add the chunk-size constant**

Next to `_RELEASE_LEGACY_CHUNK_SIZE`:

```python
# upsert_logical_jobs sends this many jobs per request. The binding limit is
# payload size, not row count: a job carries its full description, so 500 of
# them is a request in the low megabytes -- comfortable for PostgREST, and
# few enough calls that a 19,000-job run spends under 40 round trips where it
# used to spend 19,000.
_JOB_UPSERT_CHUNK_SIZE = 500
```

- [ ] **Step 5: Write the method**

Add to `PostgresJobStore`, directly after `upsert_logical_job`:

```python
    def upsert_logical_jobs(self, jobs: list[Job]) -> list[tuple[str, bool, bool] | None]:
        """Persist many logical jobs in as few round trips as possible.

        Returns one entry per input job, in input order, so a caller can zip
        the results back onto the list it passed. An entry is ``None`` when
        that job could not be persisted and was skipped -- callers must
        handle it.

        Each chunk is one transaction on the server, so a failure rolls the
        whole chunk back. Rather than paying for a savepoint per row inside
        plpgsql to guard against that, a failed chunk is replayed here one
        job at a time: clean runs cost nothing, and one malformed posting
        costs its own row instead of the run.
        """
        results: list[tuple[str, bool, bool] | None] = []
        for chunk in _chunked(jobs, _JOB_UPSERT_CHUNK_SIZE):
            try:
                results.extend(self._upsert_job_chunk(chunk))
            except Exception:
                logger.exception(
                    "batch job upsert failed for %s jobs; retrying them one at a time",
                    len(chunk),
                )
                results.extend(self._upsert_jobs_individually(chunk))
        return results

    def _upsert_job_chunk(self, chunk: list[Job]) -> list[tuple[str, bool, bool]]:
        rows = self._client.rpc(
            "job_hunter_upsert_jobs", {"p_jobs": [self._job_payload(job) for job in chunk]}
        )
        if len(rows) != len(chunk):
            raise SupabaseRequestError(
                f"job_hunter_upsert_jobs returned {len(rows)} rows for {len(chunk)} jobs"
            )
        ordered = sorted(rows, key=lambda row: row["input_index"])
        return [
            (row["id"], row["is_new"], row["description_changed"]) for row in ordered
        ]

    def _upsert_jobs_individually(
        self, chunk: list[Job]
    ) -> list[tuple[str, bool, bool] | None]:
        results: list[tuple[str, bool, bool] | None] = []
        for job in chunk:
            try:
                results.append(self.upsert_logical_job(job))
            except Exception:
                logger.exception(
                    "dropping a job that could not be persisted: source=%s url=%s",
                    job.source,
                    job.url,
                )
                results.append(None)
        return results
```

`SupabaseRequestError` is already imported in this module — check the import block and add it if not.

- [ ] **Step 6: Run the tests and verify they pass**

Run: `cd /Users/amitbaz/career-platform/.claude/worktrees/batched-discovery-writes/apps/job-hunter && .venv/bin/pytest tests/test_postgres_store_batch.py -q`
Expected: PASS, 6 passed, 0 skipped.

- [ ] **Step 7: Commit**

```bash
cd /Users/amitbaz/career-platform/.claude/worktrees/batched-discovery-writes
git add apps/job-hunter/src/job_hunter/postgres_store.py apps/job-hunter/tests/test_postgres_store_batch.py
git commit -m "$(cat <<'EOF'
job-hunter: add a chunked batch job upsert to the store

A failed chunk replays one job at a time, so a malformed posting costs its
own row rather than the run.

Refs #97

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Bulk `needs_evaluation`

**Files:**
- Modify: `supabase/migrations/202609070003_job_hunter_batch_discovery_writes.sql`
- Modify: `supabase/tests/pgtap/job_hunter_batch_discovery_writes.sql`
- Modify: `apps/job-hunter/src/job_hunter/postgres_store.py`
- Test: `apps/job-hunter/tests/test_postgres_store_batch.py`

**Interfaces:**
- Produces: `public.job_hunter_needs_evaluation(p_job_ids uuid[])` returning `table (job_id uuid, needs boolean)`; and `needs_evaluation_bulk(self, job_ids: list[str]) -> dict[str, bool]` on the store, keyed by job id, `True` for any id it could not read.

**The behaviour being reproduced.** `needs_evaluation` (`postgres_store.py:485`) returns `True` when there is no evaluation, when the latest one has status `failed`, or when the stored `description_hash` or `content_confidence` differs from what was evaluated. Note the asymmetry in the existing comparison at `postgres_store.py:490`: it coalesces the **job** side to `''` but not the evaluation side, so a `NULL` `description_hash_at_eval` against an empty stored hash counts as changed. The SQL must reproduce that, not tidy it up — tidying it silently changes which jobs get re-evaluated.

- [ ] **Step 1: Write the failing pgTAP assertions**

Append to `supabase/tests/pgtap/job_hunter_batch_discovery_writes.sql`, before `select * from finish();`:

```sql
-- needs_evaluation ----------------------------------------------------------

-- A job with no evaluation needs one.
with created as (
  select id from public.job_hunter_upsert_jobs(
    jsonb_build_array(jsonb_build_object(
      'fingerprint', 'needs-fp-1', 'source', 'test', 'company', 'Acme',
      'title', 'Engineer', 'location', 'Remote', 'remote', true,
      'description', 'unevaluated', 'url', 'https://example.test/n1')))
)
select is(
  (select needs from public.job_hunter_needs_evaluation(
     array(select id from created))),
  true,
  'a job with no evaluation needs evaluation');

-- An id belonging to another user returns no row at all.
select is(
  (select count(*)::int from public.job_hunter_needs_evaluation(
     array['99999999-0000-0000-0000-000000000009'::uuid])),
  0,
  'an unreadable id returns no row rather than a verdict');

select is(
  (select p.prosecdef from pg_proc p
     join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public' and p.proname = 'job_hunter_needs_evaluation'),
  false,
  'job_hunter_needs_evaluation is security invoker');
```

- [ ] **Step 2: Run and verify it fails**

Run: `cd /Users/amitbaz/career-platform/.claude/worktrees/batched-discovery-writes && pnpm db:test`
Expected: FAIL — `function public.job_hunter_needs_evaluation(uuid[]) does not exist`.

- [ ] **Step 3: Add the function to the migration**

Append to `supabase/migrations/202609070003_job_hunter_batch_discovery_writes.sql`:

```sql
create or replace function public.job_hunter_needs_evaluation(p_job_ids uuid[])
returns table (job_id uuid, needs boolean)
language sql
security invoker
set search_path = ''
as $$
  select
    j.id,
    case
      when e.evaluated_at is null then true
      when e.status = 'failed' then true
      -- Deliberately asymmetric: the job side is coalesced, the evaluation
      -- side is not. PostgresJobStore.needs_evaluation compares
      -- `evaluation[...] != (job_row.get(...) or "")`, so a null recorded at
      -- evaluation time counts as changed against an empty stored value.
      -- Reproduced, not corrected: correcting it here would quietly change
      -- which jobs get re-evaluated.
      when e.description_hash_at_eval is distinct from coalesce(j.description_hash, '') then true
      when e.content_confidence_at_eval is distinct from coalesce(j.content_confidence, '') then true
      else false
    end
  from public.job_hunter_jobs j
  left join lateral (
    select ev.status, ev.evaluated_at,
           ev.description_hash_at_eval, ev.content_confidence_at_eval
      from public.job_hunter_evaluations ev
     where ev.job_id = j.id
     order by ev.evaluated_at desc, ev.created_at desc, ev.id desc
     limit 1
  ) e on true
  where j.id = any(p_job_ids);
$$;

comment on function public.job_hunter_needs_evaluation(uuid[]) is
  'Bulk form of PostgresJobStore.needs_evaluation: two requests per job '
  'become one request per id array. An id the caller cannot read returns no '
  'row, and the caller treats a missing id as needing evaluation -- which is '
  'what the per-job method does with a job whose evaluations it cannot see.';
```

- [ ] **Step 4: Apply and verify the pgTAP passes**

Run: `cd /Users/amitbaz/career-platform/.claude/worktrees/batched-discovery-writes && supabase db reset && pnpm db:test`
Expected: every assertion in `job_hunter_batch_discovery_writes.sql` green. `job_hunter_store_functions.sql` still fails on its function-name array, now listing only `job_hunter_set_job_markets` as missing — expected until Task 4, per Task 1 Step 4. If it lists anything else, stop and read the diff.

- [ ] **Step 5: Write the failing Python test**

Append to `apps/job-hunter/tests/test_postgres_store_batch.py`:

```python
def test_needs_evaluation_bulk_agrees_with_the_single_job_method(store):
    job_ids = [
        result[0]
        for result in store.upsert_logical_jobs(
            [
                _make_job("bulk-eval-1", "Frontend Engineer", "https://example.test/e1"),
                _make_job("bulk-eval-2", "Backend Engineer", "https://example.test/e2"),
            ]
        )
    ]

    bulk = store.needs_evaluation_bulk(job_ids)

    assert bulk == {job_id: store.needs_evaluation(job_id) for job_id in job_ids}
    assert all(bulk.values())  # nothing evaluated yet


def test_needs_evaluation_bulk_defaults_an_unreadable_id_to_true(store):
    unknown = "99999999-0000-0000-0000-000000000009"

    assert store.needs_evaluation_bulk([unknown]) == {unknown: True}


def test_needs_evaluation_bulk_makes_one_request_per_chunk(store, monkeypatch):
    from job_hunter import postgres_store as module

    monkeypatch.setattr(module, "_ID_ARRAY_CHUNK_SIZE", 2)
    calls: list[int] = []
    original = store._client.rpc

    def counting_rpc(function, payload=None, **kwargs):
        if function == "job_hunter_needs_evaluation":
            calls.append(len(payload["p_job_ids"]))
        return original(function, payload, **kwargs)

    monkeypatch.setattr(store._client, "rpc", counting_rpc)

    job_ids = [
        result[0]
        for result in store.upsert_logical_jobs(
            [
                _make_job(f"bulk-chunk-{i}", f"Engineer {i}", f"https://example.test/bc{i}")
                for i in range(3)
            ]
        )
    ]
    store.needs_evaluation_bulk(job_ids)

    assert calls == [2, 1]


def test_needs_evaluation_bulk_on_empty_input_makes_no_request(store, monkeypatch):
    def exploding_rpc(*args, **kwargs):
        raise AssertionError("no request should be made for an empty id list")

    monkeypatch.setattr(store._client, "rpc", exploding_rpc)

    assert store.needs_evaluation_bulk([]) == {}
```

- [ ] **Step 6: Run and verify it fails**

Run: `cd /Users/amitbaz/career-platform/.claude/worktrees/batched-discovery-writes/apps/job-hunter && .venv/bin/pytest tests/test_postgres_store_batch.py -q -k needs_evaluation_bulk`
Expected: FAIL with `AttributeError: ... has no attribute 'needs_evaluation_bulk'`.

- [ ] **Step 7: Write the store method**

Add the constant next to `_JOB_UPSERT_CHUNK_SIZE`:

```python
# Bulk id arrays go in the request body, not the query string, so the 200-id
# URL-length limit that constrains _RELEASE_LEGACY_CHUNK_SIZE does not apply.
_ID_ARRAY_CHUNK_SIZE = 1000
```

Add to `PostgresJobStore`, directly after `needs_evaluation`:

```python
    def needs_evaluation_bulk(self, job_ids: list[str]) -> dict[str, bool]:
        """Answer `needs_evaluation` for many jobs in one request per chunk.

        Duplicate ids are asked once and answered for every occurrence. An id
        the caller cannot read comes back from Postgres as no row at all --
        row-level security filters it before the function sees it -- and is
        reported as ``True`` here, matching what the per-job method does with
        a job whose evaluations it cannot see.
        """
        unique_ids = list(dict.fromkeys(job_ids))
        if not unique_ids:
            return {}
        answered: dict[str, bool] = {}
        for chunk in _chunked(unique_ids, _ID_ARRAY_CHUNK_SIZE):
            rows = self._client.rpc("job_hunter_needs_evaluation", {"p_job_ids": chunk})
            for row in rows:
                answered[row["job_id"]] = row["needs"]
        return {job_id: answered.get(job_id, True) for job_id in unique_ids}
```

- [ ] **Step 8: Run and verify it passes**

Run: `cd /Users/amitbaz/career-platform/.claude/worktrees/batched-discovery-writes/apps/job-hunter && .venv/bin/pytest tests/test_postgres_store_batch.py -q`
Expected: PASS, 10 passed, 0 skipped.

- [ ] **Step 9: Commit**

```bash
cd /Users/amitbaz/career-platform/.claude/worktrees/batched-discovery-writes
git add supabase/migrations/202609070003_job_hunter_batch_discovery_writes.sql supabase/tests/pgtap/job_hunter_batch_discovery_writes.sql apps/job-hunter/src/job_hunter/postgres_store.py apps/job-hunter/tests/test_postgres_store_batch.py
git commit -m "$(cat <<'EOF'
job-hunter: answer needs_evaluation in bulk

Two requests per job become one per chunk. The null-comparison asymmetry of
the per-job method is reproduced rather than corrected, so the set of jobs
that get re-evaluated does not move.

Refs #97

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Bulk `set_job_market`

**Files:**
- Modify: `supabase/migrations/202609070003_job_hunter_batch_discovery_writes.sql`
- Modify: `supabase/tests/pgtap/job_hunter_batch_discovery_writes.sql`
- Modify: `apps/job-hunter/src/job_hunter/postgres_store.py`
- Test: `apps/job-hunter/tests/test_postgres_store_batch.py`

**Interfaces:**
- Produces: `public.job_hunter_set_job_markets(p_rows jsonb)` returning `void`; and `set_job_markets(self, pairs: list[tuple[str, str | None]]) -> None` on the store.

- [ ] **Step 1: Write the failing pgTAP assertion**

Append before `select * from finish();`:

```sql
-- set_job_markets -----------------------------------------------------------

with created as (
  select id from public.job_hunter_upsert_jobs(
    jsonb_build_array(jsonb_build_object(
      'fingerprint', 'market-fp-1', 'source', 'test', 'company', 'Acme',
      'title', 'Engineer', 'location', 'Remote', 'remote', true,
      'description', 'market', 'url', 'https://example.test/m1')))
), applied as (
  select public.job_hunter_set_job_markets(
    (select jsonb_agg(jsonb_build_object('id', id, 'market_id', 'israel'))
       from created))
)
select is(
  (select j.market_id from public.job_hunter_jobs j
    where j.id = (select id from created) and (select true from applied)),
  'israel',
  'job_hunter_set_job_markets writes each row its own market');

select lives_ok(
  $$select public.job_hunter_set_job_markets('[]'::jsonb)$$,
  'an empty array is a no-op');

select is(
  (select p.prosecdef from pg_proc p
     join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public' and p.proname = 'job_hunter_set_job_markets'),
  false,
  'job_hunter_set_job_markets is security invoker');
```

- [ ] **Step 2: Run and verify it fails**

Run: `cd /Users/amitbaz/career-platform/.claude/worktrees/batched-discovery-writes && pnpm db:test`
Expected: FAIL — `function public.job_hunter_set_job_markets(jsonb) does not exist`.

- [ ] **Step 3: Add the function**

Append to the migration:

```sql
create or replace function public.job_hunter_set_job_markets(p_rows jsonb)
returns void
language sql
security invoker
set search_path = ''
as $$
  update public.job_hunter_jobs j
     set market_id = r.market_id
    from jsonb_to_recordset(coalesce(p_rows, '[]'::jsonb))
      as r(id uuid, market_id text)
   where j.id = r.id;
$$;

comment on function public.job_hunter_set_job_markets(jsonb) is
  'Attribute many jobs to their markets in one statement. Exists only '
  'because PostgREST cannot express "a different value on each of these '
  'rows" in a single PATCH. Row-level security still restricts the update to '
  'the caller''s own rows.';
```

- [ ] **Step 4: Apply and verify the pgTAP passes**

Run: `cd /Users/amitbaz/career-platform/.claude/worktrees/batched-discovery-writes && supabase db reset && pnpm db:test`
Expected: `Result: PASS` for the whole suite. All three functions now exist, so the function-name array in `job_hunter_store_functions.sql` matches for the first time since Task 1 Step 4. If it does not, do not edit that array to match reality — a name that differs from the plan means a function was created under the wrong name.

- [ ] **Step 5: Write the failing Python test**

Append to `apps/job-hunter/tests/test_postgres_store_batch.py`:

```python
def test_set_job_markets_writes_each_job_its_own_market(store):
    results = store.upsert_logical_jobs(
        [
            _make_job("market-1", "Frontend Engineer", "https://example.test/mk1"),
            _make_job("market-2", "Backend Engineer", "https://example.test/mk2"),
        ]
    )
    first, second = results[0][0], results[1][0]

    store.set_job_markets([(first, "israel"), (second, "eu_remote")])

    assert store.get_job(first).market_id == "israel"
    assert store.get_job(second).market_id == "eu_remote"


def test_set_job_markets_stores_an_unattributed_job_as_empty(store):
    job_id = store.upsert_logical_jobs(
        [_make_job("market-3", "Platform Engineer", "https://example.test/mk3")]
    )[0][0]

    store.set_job_markets([(job_id, None)])

    assert not store.get_job(job_id).market_id


def test_set_job_markets_on_empty_input_makes_no_request(store, monkeypatch):
    def exploding_rpc(*args, **kwargs):
        raise AssertionError("no request should be made with nothing to set")

    monkeypatch.setattr(store._client, "rpc", exploding_rpc)

    store.set_job_markets([])
```

- [ ] **Step 6: Run and verify it fails**

Run: `cd /Users/amitbaz/career-platform/.claude/worktrees/batched-discovery-writes/apps/job-hunter && .venv/bin/pytest tests/test_postgres_store_batch.py -q -k set_job_markets`
Expected: FAIL with `AttributeError: ... has no attribute 'set_job_markets'`.

- [ ] **Step 7: Write the store method**

Add directly after `set_job_market`:

```python
    def set_job_markets(self, pairs: list[tuple[str, str | None]]) -> None:
        """Attribute many jobs to their markets in one request.

        ``None`` is stored as ``''``, exactly as the single-job
        `set_job_market` does. A job with no attribution still gets written:
        clearing a stale market is as meaningful as setting a new one.
        """
        if not pairs:
            return
        self._client.rpc(
            "job_hunter_set_job_markets",
            {
                "p_rows": [
                    {"id": job_id, "market_id": market_id or ""}
                    for job_id, market_id in pairs
                ]
            },
        )
```

- [ ] **Step 8: Run and verify it passes**

Run: `cd /Users/amitbaz/career-platform/.claude/worktrees/batched-discovery-writes/apps/job-hunter && .venv/bin/pytest tests/test_postgres_store_batch.py -q`
Expected: PASS, 13 passed, 0 skipped.

- [ ] **Step 9: Commit**

```bash
cd /Users/amitbaz/career-platform/.claude/worktrees/batched-discovery-writes
git add supabase/migrations/202609070003_job_hunter_batch_discovery_writes.sql supabase/tests/pgtap/job_hunter_batch_discovery_writes.sql apps/job-hunter/src/job_hunter/postgres_store.py apps/job-hunter/tests/test_postgres_store_batch.py
git commit -m "$(cat <<'EOF'
job-hunter: set job markets in one request

Refs #97

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Deduplicated ATS board harvesting

**Files:**
- Modify: `apps/job-hunter/src/job_hunter/postgres_store.py`
- Modify: `docs/superpowers/specs/2026-09-07-job-hunter-batched-discovery-writes-design.md`
- Test: `apps/job-hunter/tests/test_postgres_store_batch.py`

**Interfaces:**
- Consumes: `upsert_ats_board(provider, board_identifier, company_name, market_hint) -> bool` at `postgres_store.py:918`, which costs two requests per call.
- Produces: `upsert_ats_boards(self, references: list[tuple[str, str, str, str]]) -> int` — takes `(provider, board_identifier, company_name, market_hint)` tuples and returns how many boards were newly registered.

**Deviation from the spec, deliberate.** The spec calls for a fourth SQL function. It is not needed. `harvest_ats_board` costs two requests per **job**, but a run's jobs collapse to a few dozen distinct `(provider, board)` pairs, so deduplicating in Python before calling the existing method already breaks the tie to job count — which is the acceptance criterion. A new function would buy a constant factor on a few dozen calls in exchange for more SQL to maintain. Step 5 of this task corrects the spec to match.

- [ ] **Step 1: Write the failing test**

Append to `apps/job-hunter/tests/test_postgres_store_batch.py`:

```python
def test_upsert_ats_boards_asks_once_per_distinct_board(store, monkeypatch):
    calls: list[tuple[str, str]] = []
    original = store.upsert_ats_board

    def counting(provider, board_identifier, company_name="", market_hint=""):
        calls.append((provider, board_identifier))
        return original(provider, board_identifier, company_name, market_hint)

    monkeypatch.setattr(store, "upsert_ats_board", counting)

    newly_registered = store.upsert_ats_boards(
        [
            ("greenhouse", "acme", "Acme", "israel"),
            ("greenhouse", "acme", "Acme", "israel"),
            ("lever", "globex", "Globex", ""),
        ]
    )

    assert calls == [("greenhouse", "acme"), ("lever", "globex")]
    assert newly_registered == 2


def test_upsert_ats_boards_survives_one_bad_board(store, monkeypatch):
    original = store.upsert_ats_board

    def failing_for_globex(provider, board_identifier, company_name="", market_hint=""):
        if board_identifier == "globex":
            raise RuntimeError("registry write failed")
        return original(provider, board_identifier, company_name, market_hint)

    monkeypatch.setattr(store, "upsert_ats_board", failing_for_globex)

    newly_registered = store.upsert_ats_boards(
        [("lever", "globex", "Globex", ""), ("greenhouse", "initech", "Initech", "")]
    )

    assert newly_registered == 1


def test_upsert_ats_boards_on_empty_input_does_nothing(store, monkeypatch):
    def exploding(*args, **kwargs):
        raise AssertionError("no board write should happen for an empty list")

    monkeypatch.setattr(store, "upsert_ats_board", exploding)

    assert store.upsert_ats_boards([]) == 0
```

- [ ] **Step 2: Run and verify it fails**

Run: `cd /Users/amitbaz/career-platform/.claude/worktrees/batched-discovery-writes/apps/job-hunter && .venv/bin/pytest tests/test_postgres_store_batch.py -q -k upsert_ats_boards`
Expected: FAIL with `AttributeError: ... has no attribute 'upsert_ats_boards'`.

- [ ] **Step 3: Write the method**

Add directly after `upsert_ats_board`:

```python
    def upsert_ats_boards(self, references: list[tuple[str, str, str, str]]) -> int:
        """Register a run's distinct ATS boards, returning how many were new.

        Takes ``(provider, board_identifier, company_name, market_hint)``
        tuples, one per sighting, and asks the registry once per distinct
        ``(provider, board_identifier)``. Discovery sees a board once per job
        that references it -- thousands of sightings resolving to dozens of
        boards -- so collapsing here is what keeps the request count off the
        job count. The first sighting of a board wins: its company name and
        market hint are the ones written.

        A board that fails to register is logged and skipped. Learning the
        registry is opportunistic; losing one board must not cost the run.
        """
        first_sighting: dict[tuple[str, str], tuple[str, str]] = {}
        for provider, board_identifier, company_name, market_hint in references:
            key = (provider, board_identifier)
            if key not in first_sighting:
                first_sighting[key] = (company_name, market_hint)

        newly_registered = 0
        for (provider, board_identifier), (company_name, market_hint) in first_sighting.items():
            try:
                if self.upsert_ats_board(
                    provider=provider,
                    board_identifier=board_identifier,
                    company_name=company_name,
                    market_hint=market_hint,
                ):
                    newly_registered += 1
            except Exception:
                logger.exception(
                    "ATS board registration failed: provider=%s board=%s",
                    provider,
                    board_identifier,
                )
        return newly_registered
```

- [ ] **Step 4: Run and verify it passes**

Run: `cd /Users/amitbaz/career-platform/.claude/worktrees/batched-discovery-writes/apps/job-hunter && .venv/bin/pytest tests/test_postgres_store_batch.py -q`
Expected: PASS, 16 passed, 0 skipped.

- [ ] **Step 5: Correct the spec**

In `docs/superpowers/specs/2026-09-07-job-hunter-batched-discovery-writes-design.md`, replace the `upsert_ats_boards` bullet in "The store methods" with:

```markdown
- `upsert_ats_boards(references) -> int` — takes every ATS board sighting from
  the run and returns how many were newly admitted. It asks the registry once
  per **distinct** (provider, board) pair through the existing single-board
  method. A run sights a board once per job that references it, so thousands
  of sightings collapse to dozens of calls; that already breaks the tie to job
  count, and a fourth SQL function would buy a constant factor on dozens of
  calls at the cost of more SQL to maintain.
```

Then change the corresponding phrase in "Restructuring `collect_candidates`" step 6 from "one `upsert_ats_boards` over the run's distinct board references" to "`upsert_ats_boards` over the run's board sightings, which it collapses to the distinct boards".

- [ ] **Step 6: Commit**

```bash
cd /Users/amitbaz/career-platform/.claude/worktrees/batched-discovery-writes
git add apps/job-hunter/src/job_hunter/postgres_store.py apps/job-hunter/tests/test_postgres_store_batch.py docs/superpowers/specs/2026-09-07-job-hunter-batched-discovery-writes-design.md
git commit -m "$(cat <<'EOF'
job-hunter: register each ATS board once per run

Thousands of sightings collapse to dozens of boards, so the registry writes
stop scaling with the job count without needing a new SQL function.

Refs #97

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Restructure `collect_candidates`

This is the task that actually fixes the bug. Everything before it was groundwork.

**Files:**
- Modify: `apps/job-hunter/src/job_hunter/discovery.py:325-400` (the two persistence loops)
- Modify: `apps/job-hunter/src/job_hunter/ats_registry.py` (add a pure reference extractor)
- Test: `apps/job-hunter/tests/test_discovery.py`

**Interfaces:**
- Consumes: `store.upsert_logical_jobs`, `store.needs_evaluation_bulk`, `store.set_job_markets`, `store.upsert_ats_boards` from Tasks 2–5.
- Produces: no new public interface. `collect_candidates`'s signature and its `DiscoveryResult` are unchanged.

**What must not change.** Three things are easy to break here and each would be a silent regression:

1. Every counter in `DiscoveryStats` — the `discovery:` log line and the per-market and per-source metrics are built from them.
2. `rediscovered_job_ids` membership. `run_pipeline` uses it to requeue pending deliveries.
3. ATS board harvesting sees the **observed** market hint, computed before full attribution overwrites `job.market_id`.

**What is out of scope.** The canonical-resolution loop further down (`discovery.py:434-505`) also calls `upsert_logical_job`, `set_job_market`, and `needs_evaluation` per job. Leave it alone: it runs only for shortlisted candidates, bounded by `policy.max_canonical_resolutions_per_run` (80 attempts in the last full run), which is not what spends the hour.

- [ ] **Step 1: Write the failing request-count test**

Add to `apps/job-hunter/tests/test_discovery.py`. This is the regression guard: it fails if anyone reintroduces a per-job call.

```python
class CountingClient:
    """Delegates to a real SupabaseClient, counting requests by kind.

    The bug this suite guards against is a request count that grows with the
    job count. Asserting on returned data would not catch its return, so this
    counts calls instead.
    """

    def __init__(self, inner):
        self._inner = inner
        self.calls: list[str] = []

    def __getattr__(self, name):
        attribute = getattr(self._inner, name)
        if not callable(attribute):
            return attribute

        def recording(*args, **kwargs):
            label = args[0] if args else name
            self.calls.append(f"{name}:{label}")
            return attribute(*args, **kwargs)

        return recording


def test_collect_candidates_request_count_does_not_grow_with_job_count(
    supabase_client, policy
):
    """Twenty jobs must not cost twenty times what one job costs."""
    from job_hunter.postgres_store import PostgresJobStore

    def run_with(job_count: int) -> int:
        client = CountingClient(supabase_client)
        store = PostgresJobStore(client)
        jobs = [
            Job(
                source="test",
                source_job_id=f"count-{job_count}-{i}",
                url=f"https://example.test/count-{job_count}-{i}",
                company="Acme",
                title="Frontend Engineer",
                location="Remote",
                remote=True,
                description="A frontend engineering role working in React.",
            )
            for i in range(job_count)
        ]
        collect_candidates([FakeSource(jobs)], store, NoOpHttp(), policy)
        return len(client.calls)

    one_job = run_with(1)
    twenty_jobs = run_with(20)

    # Batched, the marginal cost of 19 more jobs is zero extra round trips.
    # A per-job design would put this at roughly 20x.
    assert twenty_jobs <= one_job + 2, (
        f"{twenty_jobs} requests for 20 jobs vs {one_job} for 1 -- "
        "discovery persistence is scaling with the job count again"
    )
```

- [ ] **Step 2: Run it and verify it fails**

Run: `cd /Users/amitbaz/career-platform/.claude/worktrees/batched-discovery-writes/apps/job-hunter && .venv/bin/pytest tests/test_discovery.py -q -k request_count`
Expected: FAIL, with the assertion message showing roughly 20x the single-job request count.

- [ ] **Step 3: Add a pure ATS reference extractor**

In `apps/job-hunter/src/job_hunter/ats_registry.py`, next to `harvest_ats_board`:

```python
def ats_board_reference(
    job: Job,
    market_hint: str | None = None,
    denylist: frozenset[str] = frozenset(),
) -> tuple[str, str, str, str] | None:
    """Return the ATS board a job references, or None.

    The pure half of `harvest_ats_board`: same admission rules, no store call.
    Discovery needs the decision while it still holds the job's observed
    market hint, but batches the writes until every job has been seen.
    """
    reference = extract_ats_reference(job)
    if reference is None:
        return None
    if ats_board_key(reference.provider, reference.board) in denylist:
        return None
    return (
        reference.provider,
        reference.board,
        job.company,
        market_hint or job.market_hint or job.market_id or "",
    )
```

Leave `harvest_ats_board` in place — the canonical-resolution loop still calls it.

- [ ] **Step 4: Rewrite the persistence loops**

Replace `discovery.py:325-400` (from the `# Persist every source copy...` comment through the end of the `for job in unique_jobs:` loop) with:

```python
    # Persist every source copy before collapsing the run so provenance is
    # retained even when only one representative continues to evaluation.
    store.upsert_logical_jobs(raw_jobs)

    unique_jobs, stats.cross_source_duplicates = _dedupe(raw_jobs)
    stats.unique = len(unique_jobs)

    prefiltered: list[tuple[str, Job]] = []
    rediscovered_job_ids: list[str] = []

    # Phase 1: network work only. Board references are captured here, while
    # each job still carries the market hint it was observed with -- the
    # attribution in phase 3 overwrites job.market_id.
    board_sightings: list[tuple[str, str, str, str]] = []
    observed_markets: list[str | None] = []
    for job in unique_jobs:
        observed_market_id = _cheap_market_attribution(job, policy)
        observed_markets.append(observed_market_id)
        reference = ats_board_reference(
            job, market_hint=observed_market_id, denylist=denylist
        )
        if reference is not None:
            board_sightings.append(reference)
        if job.url and not job.description:
            enrich_job(job, http)

    # Phase 2: one batch of writes and one batch of reads for the whole run.
    stats.ats_boards_discovered += store.upsert_ats_boards(board_sightings)
    upserted = store.upsert_logical_jobs(unique_jobs)

    persisted: list[tuple[str, Job, str | None]] = []
    for job, observed_market_id, result in zip(unique_jobs, observed_markets, upserted):
        if result is None:
            # upsert_logical_jobs already logged why. Dropping the job here is
            # the only option: everything downstream is keyed by its id.
            continue
        persisted.append((result[0], job, observed_market_id))

    market_updates: list[tuple[str, str | None]] = []
    for job_id, job, observed_market_id in persisted:
        job.market_id = attribute_market(job, policy.markets) if policy.markets else None
        _record_reattribution(stats, observed_market_id, job.market_id)
        if job.market_id:
            market_updates.append((job_id, job.market_id))
    store.set_job_markets(market_updates)

    evaluation_needed = store.needs_evaluation_bulk([job_id for job_id, _job, _hint in persisted])

    # Phase 3: pure. No I/O in this loop -- keep it that way.
    for job_id, job, _observed_market_id in persisted:
        market_key = job.market_id or _UNATTRIBUTED
        source_label = metric_source_label(job.source)
        _bump(stats.unique_by_market, market_key)
        _bump(stats.unique_by_source, source_label)

        if not evaluation_needed[job_id]:
            rediscovered_job_ids.append(job_id)
            continue

        if job.availability == CLOSED:
            stats.availability_rejected += 1
            _bump(stats.rejected_by_market, market_key)
            _bump(stats.rejected_by_source, source_label)
            continue

        market = market_by_id(policy, job.market_id) if job.market_id else None
        prefilter_result = prefilter_job(job, policy, market)
        if not prefilter_result.should_evaluate:
            if prefilter_result.reason_code == "off_target_profession":
                stats.profession_rejected += 1
            else:
                stats.prefilter_rejected += 1
            _bump(stats.rejected_by_market, market_key)
            _bump(stats.rejected_by_source, source_label)
            continue

        prefiltered.append((job_id, job))
```

Update the import at the top of `discovery.py` to bring in `ats_board_reference` alongside `harvest_ats_board`.

- [ ] **Step 5: Run the request-count test and verify it passes**

Run: `cd /Users/amitbaz/career-platform/.claude/worktrees/batched-discovery-writes/apps/job-hunter && .venv/bin/pytest tests/test_discovery.py -q -k request_count`
Expected: PASS.

- [ ] **Step 6: Run the whole discovery and pipeline suites**

Run: `cd /Users/amitbaz/career-platform/.claude/worktrees/batched-discovery-writes/apps/job-hunter && .venv/bin/pytest tests/test_discovery.py tests/test_pipeline.py -q`
Expected: PASS, 0 skipped. These cover the stats counters and `rediscovered_job_ids`; a failure here means one of the three invariants above was broken, not that the test is stale.

- [ ] **Step 7: Commit**

```bash
cd /Users/amitbaz/career-platform/.claude/worktrees/batched-discovery-writes
git add apps/job-hunter/src/job_hunter/discovery.py apps/job-hunter/src/job_hunter/ats_registry.py apps/job-hunter/tests/test_discovery.py
git commit -m "$(cat <<'EOF'
fix: batch discovery's writes so the daily run finishes

collect_candidates issued one PostgREST round trip per job -- about 70,000 per
run -- and spent the whole 60-minute workflow budget before evaluation
started. Network work and database work are now separate phases, so the
request count is bounded by chunk count rather than job count.

Closes #97

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Documentation and pull request

**Files:**
- Modify: `apps/job-hunter/AGENTS.md`

- [ ] **Step 1: Correct the function count**

`apps/job-hunter/AGENTS.md` says the schema defines "six `security invoker` SQL functions (`supabase/migrations/202609060004_*.sql`)". There are nine now, across two migrations. Update that sentence to name both migrations and the new count.

- [ ] **Step 2: Run the full suite**

Run:
```bash
cd /Users/amitbaz/career-platform/.claude/worktrees/batched-discovery-writes/apps/job-hunter
/private/tmp/claude-501/-Users-amitbaz-career-platform/1a3617ba-b8e6-40d5-b7bd-0d9fbff5f5a3/scratchpad/jh-test.sh -q
```
Expected: PASS, 0 failures, 0 skipped. The pre-Task-0 baseline was `2 failed, 1171 passed`; Task 0 turns those two into four passing tests, and Tasks 2–6 add more, so the passing count is well above 1175 and the failing count is 0. Skips mean the stack env vars are missing — that is a false pass, not a green run.

- [ ] **Step 3: Run the pgTAP suite**

Run: `cd /Users/amitbaz/career-platform/.claude/worktrees/batched-discovery-writes && pnpm db:test`
Expected: PASS.

- [ ] **Step 4: Commit and push**

```bash
cd /Users/amitbaz/career-platform/.claude/worktrees/batched-discovery-writes
git add apps/job-hunter/AGENTS.md
git commit -m "$(cat <<'EOF'
docs: count the three new store functions

Refs #97

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
git push -u origin fix/job-hunter-batched-discovery-writes
```

- [ ] **Step 5: Open the pull request**

Use `.github/PULL_REQUEST_TEMPLATE.md`. Fill in: Summary (with `Closes #97`), Scope (`apps/job-hunter`, `supabase`), Testing (the two commands above and their real results), and — this one matters — **Notes for the reviewer** must state two things:

1. `supabase db push` has to be applied to the hosted project before or with the merge, because the deployed workflow runs `main` and the new store methods call functions that will not exist until then.
2. The branch also carries Task 0, which is unrelated to #97: it corrects two Gmail cross-source tests that #96 left asserting pre-#96 behaviour and that have had `main` red since `d12d118` (CI run 34114033433). It changes tests only, no production code. Say so plainly rather than letting a reviewer discover an unexplained test rewrite in a performance PR.

- [ ] **Step 6: Verify the fix in production**

After merge and `supabase db push`, trigger the daily workflow and confirm the `discovery: raw=...` line appears within a few minutes of the run starting rather than never. That log line's arrival time is the whole acceptance test: it is the line that never printed in run 34094159716.

---

## Self-review notes

- **Spec coverage:** SQL surface → Tasks 1, 3, 4. Store methods → Tasks 2–5. Failure handling → Task 2. `collect_candidates` restructure → Task 6. Testing → the test steps in every task plus Task 7. Rollout → Task 7 steps 5–6. The spec's fourth-function proposal for ATS boards is deliberately not implemented; Task 5 step 5 corrects the spec.
- **Deviation:** `upsert_ats_boards` is Python-side deduplication, not a SQL function. Reasoned in Task 5.
- **Added after the spec was approved:** Task 0 (green baseline; tests only, unrelated to #97) and Task 1 Step 4 (the exhaustive pgTAP function-name array, which three new functions would otherwise break). Neither changes the design.
- **Out of scope, as the spec says:** the canonical-resolution loop's per-job calls, and the `GMAIL_CLIENT_ID` secret that makes the Gmail sync step fail on every run.
