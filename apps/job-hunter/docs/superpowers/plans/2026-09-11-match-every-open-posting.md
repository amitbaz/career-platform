# Match every open posting (#243) — implementation plan

Design: `docs/superpowers/specs/2026-09-11-match-every-open-posting-design.md`.
Branch: `243-match-every-open-posting`.

Work red -> green per task; run the focused test after each SQL/Python change, the full
`pnpm db:test` and `pnpm job-hunter:test` before opening the PR.

## Task 1: `job_hunter_regions_for_locations`

**Files:** new migration `29999999000000_job_hunter_regions_for_locations.sql` (placeholder
timestamp per AGENTS.md; renumbered at PR time), pgtap
`supabase/tests/pgtap/job_hunter_regions_for_locations.sql` (new).

Port `hiring_scope._REGION_ALIASES`/`_REGION_ACRONYMS` (the alias-to-region table only, not
the prose scope-cue regex) as a SQL function `job_hunter_regions_for_locations(p_locations
text[]) returns text[]`, using `job_hunter_normalize_text` and the `\m...\M` word-boundary
match `job_hunter_salary_floor_for_job` already uses. Case-insensitive, matching
`regions_for_locations`'s "configuration, not prose" behavior (acronyms match regardless of
case, unlike the prose-only bare-`US` guard).

Write the pgtap cases first: one location per region resolves; a location naming two
regions' aliases (e.g. "Berlin, remote from Israel") resolves both; an unrecognised location
resolves to `{}`; empty input resolves to `{}`.
Run: `pnpm db:test` (whole suite; there is no single-file pgtap runner in this repo — confirm
in `scripts/pgtap_stage.py` before assuming otherwise).
Then implement the function to pass.

## Task 2: extend `job_hunter_hard_blockers` with the hiring-region check

**Files:** new migration (same placeholder-timestamp file as Task 1, or a second one — keep
one function change per file per this repo's migration style, so a second placeholder file),
`supabase/tests/pgtap/job_hunter_hard_blockers.sql` if it exists (check first;
`grep -rl job_hunter_hard_blockers supabase/tests/pgtap` before assuming the filename).

Add `p_hiring_regions text[]`, `p_market_regions text[]` parameters (drop-and-recreate,
since the signature changes — same pattern `20260910170000` used). New array element:

```sql
case when array_length(p_hiring_regions, 1) is not null
      and array_length(p_market_regions, 1) is not null
      and not (p_hiring_regions && p_market_regions)
     then format('posting states hiring regions {%s}, outside the market''s {%s}',
                  array_to_string(p_hiring_regions, ', '), array_to_string(p_market_regions, ', '))
end
```

Write pgtap cases first (fails open when either array is empty/null; fires when disjoint;
silent when they overlap), then implement.

## Task 3: `job_hunter_ensure_job_membership`

**Files:** same migration file as Task 2 or a new placeholder file;
`supabase/tests/pgtap/job_hunter_ensure_job_membership.sql` (new);
`supabase/tests/pgtap/job_hunter_store_functions.sql` (pin the new function name — read it
first to see the exact assertion shape before editing).

```sql
create or replace function public.job_hunter_ensure_job_membership(
  p_posting_id uuid, p_market_id text default ''
) returns uuid
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_uid uuid := (select auth.uid());
  v_now timestamptz := clock_timestamp();
  v_job_id uuid;
begin
  if v_uid is null then
    raise exception 'job_hunter_ensure_job_membership requires an authenticated user';
  end if;

  insert into public.job_hunter_jobs as ins
    (user_id, posting_id, market_id, status, first_seen_at, last_seen_at, created_at)
  values (v_uid, p_posting_id, coalesce(p_market_id, ''), 'new', v_now, v_now, v_now)
  on conflict (user_id, posting_id) do nothing
  returning ins.id into v_job_id;

  if v_job_id is null then
    select j.id into v_job_id
      from public.job_hunter_jobs j
     where j.user_id = v_uid and j.posting_id = p_posting_id;
  end if;

  return v_job_id;
end $$;

revoke all on function public.job_hunter_ensure_job_membership(uuid, text)
  from public, anon, service_role;
grant execute on function public.job_hunter_ensure_job_membership(uuid, text) to authenticated;
```

pgtap: calling it twice for the same posting returns the same id and does not update
`last_seen_at` on the second call (unlike `upsert_job`, this is a pure "make sure it exists"
call, not a re-sighting) — assert this explicitly, since it's the one deliberate behavioral
difference from `job_hunter_upsert_job`'s insert-or-touch. RLS: a second user gets their own
row for the same posting. Add it to `job_hunter_shared_writes.sql`'s definer-function
inventory if that test enumerates definer functions reachable by `authenticated` — check the
test file first.

## Task 4: rewrite `job_hunter_match_jobs` and add `job_hunter_match_state_counts`

**Files:** new placeholder migration; `supabase/tests/pgtap/job_hunter_match_jobs.sql`
(extend — read the existing file first for its exact fixture helpers before adding cases,
the same way `test_matching.py`'s `_insert_membership` pattern was read before reuse);
`supabase/tests/pgtap/job_hunter_store_functions.sql` (pin `job_hunter_match_state_counts`).

This is the core change described in the design doc's "Matching without a prior membership
row" and "Reporting counts and reasons" sections. Concretely:

1. Change the `rows` CTE's driving table from `job_hunter_jobs j join job_hunter_postings p`
   to `job_hunter_postings p left join job_hunter_jobs j on j.posting_id = p.id and
   j.user_id = (select auth.uid())`. Drop the `j.status not in ('rejected', 'closed')`
   predicate entirely (keep `p.closed_at is null`).
2. Replace the single `left join markets mk on mk.market_id = j.market_id` with the `left
   join lateral` described in the design doc: exact stored match when `j` exists, every
   configured market (or the single null row) when it does not.
3. Insert a new reduction CTE, `market_reduced`, between the per-(posting,market) scoring
   step and the existing variant-group grouping: `row_number() over (partition by
   row_posting_id order by (row_has_facets and cardinality(row_hard_blockers) = 0) desc,
   row_score desc, ...) = 1`. Feed its output into the unchanged `grouped_locations`/`ranked`
   pipeline (rename any column references from `j.*` to the CTE's own names as needed —
   read the full current file, not just the excerpt in the design doc, before touching it).
4. Add `p_limit integer` and apply it as `limit p_limit` on the final `qualified`-only
   select (a row belongs in this result only when `row_has_facets and cardinality
   (row_hard_blockers) = 0` after reduction — `ineligible` and `unresolved` rows are never
   returned by this function at all, only counted by state_counts).
5. Add `job_hunter_match_state_counts(<same six preference args>)` returning `(state text,
   reason text, count integer)`: same classification, no `p_limit`, `group by` state and
   (for `ineligible`) each element of the winning row's `hard_blockers` unnested, (for
   `unresolved`) a reason of `'no_facets'` or `'low_content_confidence'`.
6. `job_id` in the return row is `j.id`, `null` when no membership exists. `market_id` is
   whichever market produced the winning row (`null` for the no-market synthetic case) — the
   Python side needs it for `ensure_membership`.

Write the pgtap cases first (see design doc's Tests section for the list), confirm they fail
against the current function, then make the change. Run existing
`job_hunter_match_jobs.sql` cases after every edit — this file already has coverage for
scoring/blocking equivalence and variant folding that must not regress.

Run: `pnpm db:test`.

## Task 5: `PostgresJobStore` wrapper changes

**Files:** `apps/job-hunter/src/job_hunter/postgres_store.py` (read the existing
`match_jobs` method at its current location — line numbers may have shifted since the fork
research pass — before editing), `apps/job-hunter/tests/test_postgres_store.py` if a
directly-relevant test file exists (`grep -rl "def match_jobs" apps/job-hunter/tests`
first).

- `match_jobs(..., limit: int)` — thread the new `p_limit` RPC argument through; keep the
  existing six preference arguments unchanged in name and order (backward compatible for
  `test_matching.py`'s direct calls).
- New `match_state_counts(...)` — thin RPC wrapper, same preference args, no limit, returns
  the SQL function's rows as-is (list of dicts).
- New `ensure_membership(posting_id: str, market_id: str = "") -> str` — thin RPC wrapper
  around `job_hunter_ensure_job_membership`.

## Task 6: `matching.match_jobs` Python changes

**Files:** `apps/job-hunter/src/job_hunter/matching.py`, `apps/job-hunter/tests/
test_matching.py`.

Per the design doc's "Membership as an output" section:

1. `store.match_jobs(...)` call passes `limit=limit` (was previously implicit — the SQL
   returned everything and Python did the slicing; now the SQL does the slicing, so read
   `job_hunter_match_jobs`'s own `p_limit` semantics carefully: it bounds *qualified* rows,
   which is not quite the same as "rows scored so far" once delivered/reused rows are
   subtracted — decide during implementation whether the wrapper needs to request
   `limit + len(anticipated reused rows)` or whether reuse capacity is now handled
   differently; write this decision down as a comment at the call site once made, since it
   is exactly the kind of load-bearing subtlety AGENTS.md asks to document, not silently
   pick one interpretation).
2. Filter `None` job ids out of the `all_job_ids`/`stale_facet_ids` bulk-lookup inputs (see
   design doc: a job with no history cannot be stale or delivered).
3. At each of the two `store.get_job(job_id)` call sites, when `job_id is None`: call
   `store.ensure_membership(row["posting_id"], row.get("market_id") or "")`, use the
   returned id as `job_id` for the rest of that branch.
4. Call `store.match_state_counts(...)` once, build `MatchResult.state_counts` (and
   `ineligible_reasons`/`unresolved_reasons` if the counts function returns per-reason rows
   — reduce them into whatever shape callers actually need; do not over-design a shape no
   caller reads yet, per YAGNI).
5. Add `skipped_without_facets_posting_ids` alongside the existing job-id list, appended for
   every unresolved row regardless of whether it has a job_id.

Write the new tests from the design doc's Tests section first (red), then make the code
changes (green):
- `test_match_jobs_considers_a_posting_with_no_prior_membership`
- `test_match_jobs_never_creates_membership_for_an_unresolved_posting`
- `test_match_jobs_reports_state_counts_on_a_short_result`

Run: `pytest apps/job-hunter/tests/test_matching.py -q` after each, then the full suite.

## Task 7: full verification

1. `pnpm job-hunter:test` (fails loudly if `SUPABASE_TEST_*` is missing — see AGENTS.md).
2. `pnpm db:test`.
3. `pnpm test` (both suites) before opening the PR.
4. Self-review the diff with `Read`/`Grep`, never `git diff` (AGENTS.md: the hook
   paraphrases raw file output).
5. Confirm `supabase/tests/pgtap/job_hunter_isolation.sql` /
   `job_hunter_shared_writes.sql` still pass unmodified in intent — the new function is
   `security definer` writing only to `job_hunter_jobs`, a table `authenticated` could
   already reach through `job_hunter_upsert_job`, so no new write surface should need
   documenting, but confirm rather than assume.
6. Rename every placeholder-timestamped migration to a real `YYYYMMDDHHMMSS` at PR time,
   after checking `main` and every other in-flight worktree for the highest timestamp
   either carries (AGENTS.md's migration-numbering rule).

## PR description must state explicitly

- The two deferred hard constraints (CV-office/knowledge-work, seniority) and why, per the
  design doc's table — this is a scope decision the reviewer/owner should see named, not
  discover.
- That #79's persona fixture does not exist, and this ticket's answer to that AC is
  structural (no profession branch) rather than a persona-specific test.
- That `prefilter.py`/`discovery.py` are untouched by design (#189's job).
