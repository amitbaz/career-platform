-- Give the shared tables a single writing role (#179).
--
-- Five Job Hunter tables have no user dimension and are read by everybody:
-- job_hunter_postings, job_hunter_job_facets, job_hunter_companies,
-- job_hunter_posting_merges and job_hunter_ats_boards. Until now four of them
-- could also be *written* by anybody authenticated. A fabricated compensation
-- figure or hiring region on a shared facet row changes another user's digest
-- and their hard blockers, silently, with nothing in their own data to show
-- for it. Reads staying open is the point of a shared row; writes staying open
-- is not.
--
-- There are two doors into a shared table, and closing one without the other
-- closes nothing:
--
--   1. A direct write, governed by the table's GRANTs and its row-level
--      security policies.
--   2. A SECURITY DEFINER function, which runs as the function's owner and
--      therefore consults neither. Revoking a caller's table grants has no
--      effect at all on what a definer function will write on their behalf.
--
-- Both doors are closed here. Door two is the reason this migration is not a
-- page of REVOKE statements: job_hunter_upsert_job is SECURITY DEFINER and
-- granted to `authenticated`, and behind it job_hunter_merge_postings is
-- reachable, so a user could upsert two job rows of their own that resolve to
-- two chosen postings and collapse those postings for every other user (#201
-- records this as reduced and tracked rather than closed, and names this
-- migration as what closes it).
--
-- Which shape, and why -------------------------------------------------------
--
-- #179 offers two defensible shapes for door two and leaves the choice to the
-- implementer. **This migration moves the path.** job_hunter_upsert_job,
-- job_hunter_upsert_jobs and job_hunter_merge_jobs stop being reachable by
-- `authenticated` at all; each takes the user it acts for as an argument and is
-- called over ingestion's direct, privileged Postgres connection, where there
-- is no auth.uid() to read.
--
-- The alternative was to keep job_hunter_upsert_job reachable and constrain its
-- body so a caller could not write an arbitrary shared row through it. That
-- would mean removing the identity ladder -- canonical URL, then the ATS
-- triple, then normalized company/title/location -- from the function and
-- re-homing it, because resolving a listing against the shared corpus and
-- merging what it finds *is* the shared write. Removing it changes which
-- posting a listing attaches to, which is a change to what the engine
-- delivers, and it does not belong in a ticket about who may write.
--
-- Moving the path changes no behaviour at all: the ladder, the merge it
-- triggers, the membership row, the provenance row and the description_changed
-- sampling are copied across verbatim, and the only difference is that v_uid
-- comes from an argument instead of from auth.uid().
--
-- The pattern of record ------------------------------------------------------
--
-- This is the first shared table set to adopt the privileged-writer pattern
-- deliberately, so it is stated here for later tickets to cite:
--
--   * `select` stays open to `authenticated`.
--   * `insert`, `update` and `delete` are revoked from `anon`, `authenticated`
--     and `service_role`, and the write policies are dropped as well, so
--     neither half is load-bearing alone. This is what 20260909130000 did for
--     job_hunter_posting_staging (:86) and 20260909190000 for the stage-attempt
--     tables (:78-80); the shared tables adopt it rather than inventing a
--     second pattern.
--   * Writes arrive over the direct Postgres connection as the privileged role.
--   * **A SECURITY DEFINER function is a write path.** A shared table is not
--     closed until every definer function that can write it is either
--     unreachable by `authenticated` or provably unable to write a row the
--     caller was not entitled to.
--   * A pgTAP test proves the refusal as `authenticated` **and** as `anon`, per
--     table, and through every definer function reachable by `authenticated`.
--
-- supabase/tests/pgtap/job_hunter_shared_writes.sql is that test. It is the
-- mechanism behind every claim above: it fails if a write grant comes back, if
-- a write policy is re-added, or if one of these functions becomes callable by
-- `authenticated` again.
--
-- What this costs ------------------------------------------------------------
--
-- The direct connection stops being optional for writing. A deployment with no
-- SUPABASE_DB_URL still starts and still delivers, but it is now scoring-only
-- over a corpus that can never update -- a different thing from the old
-- fallback, which was merely slower and converged on the same state. That mode
-- is deliberately retained and made legible in the application: cli.py warns at
-- startup, the run skips ingestion and enrichment rather than attempting writes
-- that would be refused, and the run summary reports zero postings written and
-- zero facets extracted so a stale corpus cannot be mistaken for a quiet job
-- market.


-- 1. The shared tables: reads open, writes revoked ---------------------------
--
-- Per table: drop the insert and update policies, then revoke the three write
-- verbs from every role a user can hold. `select` is untouched, and no delete
-- policy ever existed on any of them.
--
-- service_role is revoked with the rest because nothing in this application
-- uses that key, and a table only ingestion writes should not become reachable
-- the day something does.

drop policy insert_authenticated on public.job_hunter_postings;
drop policy update_authenticated on public.job_hunter_postings;
revoke insert, update, delete on table public.job_hunter_postings
  from anon, authenticated, service_role;

drop policy insert_authenticated on public.job_hunter_job_facets;
drop policy update_authenticated on public.job_hunter_job_facets;
revoke insert, update, delete on table public.job_hunter_job_facets
  from anon, authenticated, service_role;

drop policy insert_authenticated on public.job_hunter_companies;
drop policy update_authenticated on public.job_hunter_companies;
revoke insert, update, delete on table public.job_hunter_companies
  from anon, authenticated, service_role;

drop policy insert_authenticated on public.job_hunter_ats_boards;
drop policy update_authenticated on public.job_hunter_ats_boards;
revoke insert, update, delete on table public.job_hunter_ats_boards
  from anon, authenticated, service_role;

-- job_hunter_posting_merges (#176) arrived with no write policy at all,
-- because every write to it happens inside job_hunter_merge_postings. There is
-- nothing to drop; the grants are revoked so its refusal rests on the same two
-- halves as its four siblings rather than on a policy nobody wrote later being
-- absent, and the pgTAP suite asserts the absence rather than assuming it.
revoke insert, update, delete on table public.job_hunter_posting_merges
  from anon, authenticated, service_role;

comment on table public.job_hunter_postings is
  'One job advertisement, shared by every user who discovers it. Readable by '
  'any authenticated user; writable only by the privileged ingestion role '
  '(#179).';
comment on table public.job_hunter_job_facets is
  'What one advertisement states, read once for everybody. Readable by any '
  'authenticated user; writable only by the privileged ingestion role (#179).';
comment on table public.job_hunter_companies is
  'What is known about one employer, read once for everybody. Readable by any '
  'authenticated user; writable only by the privileged ingestion role (#179).';
comment on table public.job_hunter_ats_boards is
  'One learned ATS board''s identity and health, shared by every user. '
  'Readable by any authenticated user; writable only by the privileged '
  'ingestion role (#179).';


-- 2. job_hunter_upsert_posting: revoked rather than merely refused -----------
--
-- It is SECURITY INVOKER, so revoking the table's write grants above already
-- stops an authenticated caller writing a posting through it. Revoking execute
-- as well means the refusal does not depend on remembering that: the function
-- is a shared-table writer, and shared-table writers are reachable by the
-- privileged role only. Its two real callers -- job_hunter_upsert_job below
-- and job_hunter_merge_posting_batch -- are both owner-side.

revoke all on function public.job_hunter_upsert_posting(jsonb)
  from public, anon, authenticated, service_role;


-- 3. The identity ladder's weakest rung follows the user -----------------------
--
-- job_hunter_find_posting_by_identity answers "does *this caller* already hold
-- a row covering this advertisement", and it read auth.uid() to know who that
-- was. Under the old definer job_hunter_upsert_job that still worked: a
-- definer function does not change auth.uid(), which stays the caller's.
--
-- Over the privileged connection there is no caller. auth.uid() is null, the
-- join finds no rows, and the rung returns nothing -- silently, because
-- "returns nothing" is also its honest answer for a listing nobody has seen
-- before. Cross-source deduplication would have quietly stopped happening,
-- and the only sign would have been a slowly growing corpus of the same
-- advertisement seen on three aggregators.
--
-- So the user comes in as an argument here too. The scoping this function
-- documents is unchanged and is the whole point of it: the weakest rung is
-- deliberately willing to call two renderings of "Acme / Senior Product
-- Engineer / Remote" the same advertisement, which is a reasonable bet about
-- one user's own corpus and a catastrophic one about everybody's.

create or replace function public.job_hunter_find_posting_by_identity(
  p_company text, p_title text, p_location text, p_user_id uuid
) returns setof uuid
language plpgsql
stable
security invoker
set search_path = ''
as $$
declare
  v_uid uuid := p_user_id;
  v_company text := public.job_hunter_normalize_company(p_company);
  v_title text := public.job_hunter_normalize_tokens(p_title);
begin
  if v_uid is null or v_company = '' or v_title = '' then
    return;
  end if;

  return query
  with matches as (
    select p.id as posting_id,
           p.location as posting_location,
           j.created_at as job_created_at,
           j.id as job_id
      from public.job_hunter_postings p
      join public.job_hunter_jobs j
        on j.posting_id = p.id and j.user_id = v_uid
     where p.normalized_company = v_company
       and p.normalized_title = v_title
       and public.job_hunter_locations_compatible(p_location, p.location)
  )
  select m.posting_id
    from matches m
   where not exists (
           select 1
             from matches l
             join matches r on l.job_id < r.job_id
            where not public.job_hunter_locations_compatible(
                        l.posting_location, r.posting_location)
         )
   order by m.job_created_at, m.job_id;
end $$;

comment on function public.job_hunter_find_posting_by_identity(text, text, text, uuid) is
  'The postings p_user_id already holds a membership row for whose normalized '
  'company, title and compatible location identify one advertisement -- '
  'nothing when the matches disagree about location. The identity itself '
  'lives on the posting since #178; the question it answers is still the '
  'per-user coverage question 202609070002 asked of job rows, because this '
  'rung is weak enough that asking it of the whole shared corpus would '
  'collapse unrelated advertisements irreversibly. Since #179 the user is an '
  'argument rather than auth.uid(), because its only caller now runs as the '
  'privileged ingestion role, where auth.uid() is null and the rung would '
  'silently match nothing.';

revoke all on function public.job_hunter_find_posting_by_identity(text, text, text, uuid)
  from public, anon, authenticated, service_role;

-- job_hunter_find_job_by_identity is the store's read of the same question,
-- reached over PostgREST as the user, and it has to be re-created here: its
-- body named the three-argument function that is dropped below, and a body
-- referring to a function that no longer exists fails at call time rather than
-- at drop time -- which surfaces as a 404 from PostgREST long after the
-- migration looked clean.
--
-- It becomes SECURITY DEFINER, which is the sixth and last definer in this
-- schema, and the reason is worth stating because "add a definer" is exactly
-- what this migration spends its length narrowing:
--
--   * The four-argument posting lookup must NOT be callable by a user. Its
--     new argument names whose corpus to search, so a user who could call it
--     could ask which advertisements any other user holds. That is why it is
--     revoked above.
--   * This function asks the same question but cannot be misdirected: it takes
--     no user argument at all and derives the user from auth.uid(), which
--     inside a definer function is still the caller's id, not the owner's.
--     Both the delegate call and the join carry that value.
--   * It writes nothing. It is `stable`, it selects, and there is no shared
--     table anywhere in its reach.
--
-- The alternative was to inline the identity rule here -- the normalization,
-- the location-compatibility test and the ambiguity rule that returns nothing
-- when two matches disagree about location. Two copies of that rule would
-- drift, and the copy nobody noticed drifting is the one that merges two
-- different jobs at one employer.

create or replace function public.job_hunter_find_job_by_identity(
  p_company text, p_title text, p_location text
) returns setof uuid
language sql
stable
security definer
set search_path = ''
as $$
  select j.id
    from public.job_hunter_find_posting_by_identity(
           p_company, p_title, p_location, (select auth.uid())) as f(posting_id)
    join public.job_hunter_jobs j
      on j.posting_id = f.posting_id
     and j.user_id = (select auth.uid())
   order by j.created_at, j.id;
$$;

comment on function public.job_hunter_find_job_by_identity(text, text, text) is
  'The caller''s membership rows for the postings a normalized company, title '
  'and compatible location identify. The identity itself is a fact about the '
  'advertisement and is matched on job_hunter_postings (#178); which rows are '
  'searched is still the caller''s own, because the question is whether they '
  'already hold this advertisement. SECURITY DEFINER since #179 only so it can '
  'reach job_hunter_find_posting_by_identity, which is revoked from users '
  'because its user argument would otherwise let one user search another''s '
  'corpus. This function takes no such argument: it reads auth.uid(), which is '
  'still the caller''s inside a definer, and it writes nothing.';


-- 4. The job-upsert path moves to the privileged connection ------------------
--
-- job_hunter_upsert_job is re-created with the caller's user id as an argument
-- rather than read from auth.uid(). Everything else is 20260909210000's body,
-- unchanged: the same identity ladder, the same merges, the same two-sample
-- description_changed, the same membership and provenance writes. Every
-- statement that touches a per-user table still carries `user_id = v_uid`, so
-- the predicates express exactly what row-level security expressed when this
-- ran as the user.
--
-- It stays SECURITY DEFINER. That is now belt and braces rather than the load-
-- bearing part -- execute is revoked from every role a user can hold, so only
-- the owner can call it at all -- but it keeps the posting merge working
-- regardless of which privileged role ingestion connects as.
--
-- A caller who passes a user id that is not their own is, by construction, the
-- privileged role, which could write those rows directly anyway. There is no
-- privilege to escalate here: the argument replaces an identity the function
-- can no longer read, it does not add one.

create or replace function public.job_hunter_upsert_job(p_job jsonb, p_user_id uuid)
returns table (id uuid, is_new boolean, description_changed boolean)
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_uid uuid := p_user_id;
  v_fingerprint text := coalesce(p_job->>'fingerprint', '');
  v_raw_canonical text := coalesce(p_job->>'canonical_url', '');
  v_lookup_canonical text;
  v_ats_provider text := lower(coalesce(p_job->>'ats_provider', ''));
  v_match_mode text := coalesce(p_job->>'match_mode', 'logical');
  v_supplied_posting uuid;
  v_posting_id uuid;
  v_candidates uuid[] := '{}'::uuid[];
  v_candidate uuid;
  v_resolved uuid;
  v_previous_hash text;
  v_own_previous_hash text;
  v_previous_posting uuid;
  v_current_hash text;
  v_job_id uuid;
  v_is_new boolean;
  v_now timestamptz := clock_timestamp();
  v_source text := coalesce(p_job->>'source', '');
  v_source_job_id text := p_job->>'source_job_id';
  v_source_url text;
  v_identity_key text;
begin
  if v_uid is null then
    raise exception 'job_hunter_upsert_job requires the user id it is writing for';
  end if;
  if v_fingerprint = '' then
    raise exception 'p_job must carry a non-empty fingerprint';
  end if;
  if v_match_mode not in ('logical', 'fingerprint') then
    raise exception 'p_job.match_mode must be ''logical'' or ''fingerprint'', got %', v_match_mode;
  end if;

  v_lookup_canonical := case when v_raw_canonical <> ''
                             then public.job_hunter_canonicalize_url(v_raw_canonical)
                             else '' end;

  -- The advertisement. A staged batch (#182) has usually resolved and folded
  -- it already and passes the id in; a payload without one resolves its own,
  -- in this same call, so a membership row can never exist without the
  -- advertisement it is a membership of.
  v_supplied_posting := nullif(coalesce(p_job->>'posting_id', ''), '')::uuid;
  if v_supplied_posting is null then
    -- Sampled before the write, because the write is what may change it.
    select p.description_hash into v_own_previous_hash
      from public.job_hunter_postings p
     where p.id = public.job_hunter_resolve_posting(
             (select q.id from public.job_hunter_postings q
               where q.fingerprint = v_fingerprint));
    v_previous_hash := v_own_previous_hash;
    v_posting_id := public.job_hunter_upsert_posting(p_job);
  else
    v_posting_id := public.job_hunter_resolve_posting(v_supplied_posting);
  end if;

  -- match_mode = 'fingerprint': identity is the fingerprint and nothing else,
  -- so there are no candidates to gather and nothing is ever merged. It also
  -- records no discovery source -- upsert_job never called _record_job_source.
  -- Collapsing this into the logical mode would hand a caller
  -- duplicate-merging it did not ask for.
  if v_match_mode = 'logical' then
    -- Identity resolution, strongest evidence first, preserving order and
    -- dropping repeats exactly as _append_unique_id did. The columns compared
    -- are the posting's now, and the rows searched are still this user's --
    -- the question is "do I already hold a row covering this advertisement",
    -- which is what decided the pairs before #178 too. What changed is the
    -- answer's reach: the merge those pairs trigger is recorded once for
    -- everyone. See job_hunter_find_posting_by_identity for why the weakest
    -- rung in particular must not be asked of the whole shared corpus.
    if v_lookup_canonical <> '' then
      for v_candidate in
        select p.id from public.job_hunter_postings p
          join public.job_hunter_jobs j on j.posting_id = p.id and j.user_id = v_uid
         where p.canonical_url = v_lookup_canonical
         order by j.created_at, j.id
      loop
        v_resolved := public.job_hunter_resolve_posting(v_candidate);
        if not (v_resolved = any(v_candidates)) then
          v_candidates := v_candidates || v_resolved;
        end if;
      end loop;
    end if;

    if v_ats_provider in ('ashby', 'greenhouse', 'lever')
       and coalesce(p_job->>'ats_board', '') <> ''
       and coalesce(p_job->>'ats_job_id', '') <> '' then
      for v_candidate in
        select p.id from public.job_hunter_postings p
          join public.job_hunter_jobs j on j.posting_id = p.id and j.user_id = v_uid
         where p.ats_provider = v_ats_provider
           and p.ats_board = p_job->>'ats_board'
           and p.ats_job_id = p_job->>'ats_job_id'
         order by j.created_at, j.id
      loop
        v_resolved := public.job_hunter_resolve_posting(v_candidate);
        if not (v_resolved = any(v_candidates)) then
          v_candidates := v_candidates || v_resolved;
        end if;
      end loop;
    end if;

    for v_candidate in
      select f from public.job_hunter_find_posting_by_identity(
        coalesce(p_job->>'company', ''),
        coalesce(p_job->>'title', ''),
        coalesce(p_job->>'location', ''),
        v_uid) f
    loop
      v_resolved := public.job_hunter_resolve_posting(v_candidate);
      if not (v_resolved = any(v_candidates)) then
        v_candidates := v_candidates || v_resolved;
      end if;
    end loop;

    -- What this user was reading before the merges, for description_changed.
    -- The row that answers is their earliest one among everything this
    -- payload resolved to -- the same row the job-level merge used to pick as
    -- survivor, and therefore the same "before" the old comparison used.
    --
    -- Two samples rather than one because the write above has already
    -- happened: if that earliest row sits on the posting this payload just
    -- wrote, its stored hash is the new value, and the pre-write sample is
    -- the honest "before". If it sits on a different posting -- a listing
    -- arriving under a new fingerprint that resolves onto one the user has
    -- held all along -- that posting is untouched so far and its hash is.
    select j.posting_id, p.description_hash
      into v_previous_posting, v_previous_hash
      from public.job_hunter_jobs j
      join public.job_hunter_postings p on p.id = j.posting_id
     where j.user_id = v_uid
       and j.posting_id = any(v_candidates || v_posting_id)
     order by j.created_at, j.id
     limit 1;

    if v_previous_posting is null or v_previous_posting = v_posting_id then
      v_previous_hash := v_own_previous_hash;
    end if;

    -- Every candidate collapses into the posting this payload resolved to.
    -- job_hunter_merge_postings chooses the survivor from the two rows rather
    -- than from argument order, so which of them the payload arrived as does
    -- not decide the outcome.
    foreach v_candidate in array v_candidates loop
      if v_candidate <> v_posting_id then
        v_posting_id := public.job_hunter_merge_postings(v_posting_id, v_candidate);
      end if;
    end loop;
  end if;

  select p.description_hash into v_current_hash
    from public.job_hunter_postings p where p.id = v_posting_id;

  -- The membership row. One statement for the row that does not exist yet and
  -- one for the row that does, both keyed on the pair this table is now unique
  -- on. Nothing else about the row is rewritten on a re-sighting: market_id
  -- and status are this user's answers and are set by their own writers
  -- (job_hunter_set_job_markets, set_job_status).
  insert into public.job_hunter_jobs as ins
    (user_id, posting_id, market_id, status, first_seen_at, last_seen_at, created_at)
  values (v_uid, v_posting_id, '', 'new', v_now, v_now, v_now)
  on conflict (user_id, posting_id) do nothing
  returning ins.id into v_job_id;

  if v_job_id is not null then
    v_is_new := true;
  else
    update public.job_hunter_jobs j set last_seen_at = v_now
     where j.user_id = v_uid and j.posting_id = v_posting_id
    returning j.id into v_job_id;
    v_is_new := false;
  end if;

  if v_job_id is null then
    raise exception 'membership row for posting % could not be written', v_posting_id;
  end if;

  if v_match_mode = 'logical' then
    -- _record_job_source
    v_source_url := coalesce(nullif(coalesce(p_job->>'original_url', ''), ''),
                             coalesce(p_job->>'url', ''), '');
    v_identity_key := case
      when coalesce(v_source_job_id, '') <> ''
        then 'id:' || v_source || ':' || v_source_job_id
      else 'url:' || public.job_hunter_canonicalize_url(v_source_url)
    end;

    insert into public.job_hunter_job_sources
      (user_id, job_id, source, source_job_id, source_url, identity_key,
       first_seen_at, last_seen_at)
    values (v_uid, v_job_id, v_source, v_source_job_id, v_source_url,
            v_identity_key, v_now, v_now)
    on conflict (job_id, identity_key) do update set last_seen_at = excluded.last_seen_at;
  end if;

  return query select
    v_job_id,
    v_is_new,
    v_previous_hash is not null and v_previous_hash is distinct from v_current_hash;
end
$$;

comment on function public.job_hunter_upsert_job(jsonb, uuid) is
  'Write the posting once and one user''s membership of it once (#178), for '
  'the user named in p_user_id. Identity is resolved against '
  'job_hunter_postings -- canonical URL, ATS triple, normalized '
  'company/title/location, fingerprint -- and every duplicate found is merged '
  'there, so the decision is made once for everyone rather than once per user. '
  'A payload carrying a posting_id keeps it rather than resolving the posting '
  'again, which is how a staged batch (#182) pays for the whole batch once. '
  'Since #179 the user arrives as an argument rather than from auth.uid(): '
  'this writes shared rows, so it runs on ingestion''s privileged connection '
  'where there is no authenticated user to read. Timestamps come from '
  'clock_timestamp(), not now(), so multiple calls inside one transaction '
  '(job_hunter_upsert_jobs) still get strictly-ordered first_seen_at values '
  'matching input order.';


-- job_hunter_upsert_jobs ------------------------------------------------------
--
-- Unchanged apart from carrying the user through to each element. It is
-- SECURITY INVOKER and it calls a definer function, which is a door in its own
-- right: an invoker wrapper around a definer runs that definer as the owner
-- just the same. It is revoked below for that reason and not only for symmetry.

create or replace function public.job_hunter_upsert_jobs(p_jobs jsonb, p_user_id uuid)
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
  if p_user_id is null then
    raise exception 'job_hunter_upsert_jobs requires the user id it is writing for';
  end if;

  -- Ordered, one element at a time, deliberately. Two jobs in one batch can
  -- resolve to the same identity, and the second must merge into the first
  -- exactly as two sequential single-job calls would.
  for v_element, v_index in
    select value, (ordinality - 1)::int
      from jsonb_array_elements(p_jobs) with ordinality as t(value, ordinality)
     order by ordinality
  loop
    select * into v_result from public.job_hunter_upsert_job(v_element, p_user_id);
    input_index := v_index;
    id := v_result.id;
    is_new := v_result.is_new;
    description_changed := v_result.description_changed;
    return next;
  end loop;
end;
$$;

comment on function public.job_hunter_upsert_jobs(jsonb, uuid) is
  'Upsert an ordered array of jobs for one user in one round trip. Returns one '
  'row per input element, tagged with its zero-based input_index so a caller '
  'can zip results back onto what it sent -- ids alone cannot, because two '
  'elements may resolve to the same job. No exception handling: a failure '
  'aborts the whole call, and the caller replays the batch one job at a time '
  'to isolate the bad element.';


-- job_hunter_merge_jobs -------------------------------------------------------
--
-- The per-user entry point to a posting merge. Since #179 it is no longer an
-- entry point `authenticated` has: merging two postings is a shared write, so
-- it moves to the privileged connection with the rest and takes its user as an
-- argument. Both statements that read job rows keep their explicit
-- `user_id = v_uid` predicate, so naming another user's row still raises
-- 'survivor and duplicate jobs must both exist' rather than merging it.

create or replace function public.job_hunter_merge_jobs(
  p_survivor uuid, p_duplicate uuid, p_user_id uuid
)
returns uuid
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_uid uuid := p_user_id;
  v_survivor_posting uuid;
  v_duplicate_posting uuid;
  v_posting_id uuid;
  v_job_id uuid;
begin
  if v_uid is null then
    raise exception 'job_hunter_merge_jobs requires the user id it is merging for';
  end if;
  if p_survivor = p_duplicate then
    return p_survivor;
  end if;

  select j.posting_id into v_survivor_posting
    from public.job_hunter_jobs j
   where j.id = p_survivor and j.user_id = v_uid;
  select j.posting_id into v_duplicate_posting
    from public.job_hunter_jobs j
   where j.id = p_duplicate and j.user_id = v_uid;

  if v_survivor_posting is null or v_duplicate_posting is null then
    raise exception 'survivor and duplicate jobs must both exist';
  end if;

  if v_survivor_posting = v_duplicate_posting then
    -- Unreachable while `unique (user_id, posting_id)` holds, and cheaper to
    -- answer than to reason about: two rows on one posting are two rows this
    -- user should never have had, so collapse them directly.
    return public.job_hunter_collapse_job_rows(v_uid, p_survivor, p_duplicate);
  end if;

  v_posting_id := public.job_hunter_merge_postings(v_survivor_posting, v_duplicate_posting);

  select j.id into v_job_id
    from public.job_hunter_jobs j
   where j.user_id = v_uid and j.posting_id = v_posting_id;

  return v_job_id;
end $$;

comment on function public.job_hunter_merge_jobs(uuid, uuid, uuid) is
  'Merge two of one user''s membership rows by merging the postings behind '
  'them, and return the row that user is left with. Since #178 a job row '
  'carries no fact of its own, so there is nothing else to merge: the posting '
  'merge folds the advertisements, collapses every affected user''s duplicate '
  'rows, and records the redirect. Since #179 the user arrives as an argument '
  'and the function is reachable only by the privileged ingestion role, '
  'because collapsing two postings is a write every other user sees.';


-- 5. The old signatures go -----------------------------------------------------
--
-- Dropped rather than left revoked. A revoked overload is still a function a
-- future migration can grant back by accident, and PostgREST would still list
-- it; a dropped one fails at the call site, which is where a caller that was
-- missed should fail. The plural goes first because it is the only caller of
-- the singular.

drop function public.job_hunter_upsert_jobs(jsonb);
drop function public.job_hunter_upsert_job(jsonb);
drop function public.job_hunter_merge_jobs(uuid, uuid);
drop function public.job_hunter_find_posting_by_identity(text, text, text);


-- 6. Nobody but the owner may execute any of them -----------------------------
--
-- `revoke all ... from public` is the one that matters: a newly created
-- function is executable by PUBLIC by default, so listing only anon,
-- authenticated and service_role would leave it wide open through role
-- inheritance. The named roles are revoked as well so a later default-privilege
-- change cannot quietly re-grant them.

revoke all on function public.job_hunter_upsert_job(jsonb, uuid)
  from public, anon, authenticated, service_role;
revoke all on function public.job_hunter_upsert_jobs(jsonb, uuid)
  from public, anon, authenticated, service_role;
revoke all on function public.job_hunter_merge_jobs(uuid, uuid, uuid)
  from public, anon, authenticated, service_role;
