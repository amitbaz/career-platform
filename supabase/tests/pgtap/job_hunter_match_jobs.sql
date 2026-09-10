-- One matching operation over stored facets (issue #187).
--
-- job_hunter_match_jobs is a term-for-term SQL port of ranking.profile_priority_score
-- and hard_blockers.hard_blockers_from_facets; the Python-side equivalence test
-- (apps/job-hunter/tests/test_matching.py) is what actually proves the two agree on a
-- realistic corpus. This file proves the SQL half's own contract: every membership row
-- the caller holds comes back, ordered by score, hard blockers cost no provider call and
-- are flagged rather than dropped, a posting with no facets is flagged unscoreable, and
-- RLS still scopes everything to the caller.
begin;
create extension if not exists pgtap with schema extensions;
select plan(20);

-- Seed users ------------------------------------------------------------------

insert into auth.users (id, email, instance_id, aud, role, raw_app_meta_data, raw_user_meta_data, created_at, updated_at)
values
  ('aaaaaaaa-1111-0000-0000-000000000001', 'match-a@test.local', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', '{}', '{}', now(), now()),
  ('bbbbbbbb-1111-0000-0000-000000000002', 'match-b@test.local', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', '{}', '{}', now(), now())
on conflict (id) do nothing;

create function pg_temp.authenticate_as(p_user uuid) returns void
language plpgsql as $$
begin
  execute 'reset role';
  perform set_config('request.jwt.claims',
    json_build_object('sub', p_user, 'role', 'authenticated')::text, true);
  execute 'set local role authenticated';
end $$;

create function pg_temp.become_postgres() returns void
language plpgsql as $$
begin
  execute 'reset role';
  perform set_config('request.jwt.claims', '', true);
end $$;

-- Signature -----------------------------------------------------------------

select has_function('public', 'job_hunter_match_jobs',
  array['text[]', 'text[]', 'text[]', 'text[]', 'text[]', 'text[]'],
  'job_hunter_match_jobs exists');

-- Fixtures for user A ---------------------------------------------------------
--
-- Postings and companies are shared tables, seeded as the privileged role
-- exactly as job_hunter_store_functions.sql does.

select pg_temp.become_postgres();

insert into public.job_hunter_postings
  (id, fingerprint, source, source_job_id, url, canonical_url,
   company, title, location, remote, description, description_hash, content_confidence,
   first_seen_at, last_seen_at)
values
  -- Strong title/keyword match, no facets read yet: unscoreable, never blocked.
  ('c1000000-0000-0000-0000-000000000001', 'match-fp-unread',
   'greenhouse', 'g-1', 'https://boards.greenhouse.io/acme/jobs/1', 'https://boards.greenhouse.io/acme/jobs/1',
   'Acme', 'Senior Backend Engineer', 'Berlin', true,
   'we use kubernetes and postgres every day', 'h-unread', 'official_ats',
   '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'),
  -- Same strong match, facets read and clean: scores highest, has_facets true.
  ('c1000000-0000-0000-0000-000000000002', 'match-fp-clean',
   'greenhouse', 'g-2', 'https://boards.greenhouse.io/acme/jobs/2', 'https://boards.greenhouse.io/acme/jobs/2',
   'Acme', 'Senior Backend Engineer', 'Berlin', true,
   'we use kubernetes and postgres every day', 'h-clean', 'official_ats',
   '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'),
  -- Facets read but disqualify on salary: blocked, costs nothing.
  ('c1000000-0000-0000-0000-000000000003', 'match-fp-blocked',
   'greenhouse', 'g-3', 'https://boards.greenhouse.io/acme/jobs/3', 'https://boards.greenhouse.io/acme/jobs/3',
   'Acme', 'Senior Backend Engineer', 'Berlin', true,
   'we use kubernetes and postgres every day', 'h-blocked', 'official_ats',
   '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'),
  -- Weak match: lowest score, no facets.
  ('c1000000-0000-0000-0000-000000000004', 'match-fp-weak',
   'hackernews', null, 'https://weak.example/4', 'https://weak.example/4',
   'Weak Co', 'Marketing Manager', 'Nowhere', false,
   'sell things to people', 'h-weak', 'aggregator_text',
   '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'),
  -- Thin content: facets exist but content_confidence is insufficient, so the
  -- comparison fails open (never blocked) even though it would otherwise be.
  ('c1000000-0000-0000-0000-000000000005', 'match-fp-thin',
   'yc', null, 'https://thin.example/5', 'https://thin.example/5',
   'Acme', 'Senior Backend Engineer', 'Berlin', true,
   'we use kubernetes and postgres every day', 'h-thin', 'partial_unknown',
   '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z');

-- Closed posting (issue #186): a freshness re-check found it gone. #188's
-- fix is that this row must never reach job_hunter_match_jobs's results at
-- all, since the caller can no longer be trusted to filter it out itself.
insert into public.job_hunter_postings
  (id, fingerprint, source, source_job_id, url, canonical_url,
   company, title, location, remote, description, description_hash, content_confidence,
   closed_at, closed_reason, first_seen_at, last_seen_at)
values
  ('c1000000-0000-0000-0000-000000000006', 'match-fp-closed',
   'greenhouse', 'g-6', 'https://boards.greenhouse.io/acme/jobs/6', 'https://boards.greenhouse.io/acme/jobs/6',
   'Acme', 'Senior Backend Engineer', 'Berlin', true,
   'we use kubernetes and postgres every day', 'h-closed', 'official_ats',
   now(), 'http_404', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z');

-- Rejected/closed membership rows (issue #188): two open postings, otherwise
-- identical to the clean/scoreable fixture above, whose *membership* row
-- (not the posting) carries a non-default status -- prefilter's own verdict
-- (`discovery.py`'s `set_job_statuses`), never a fact about the posting.
insert into public.job_hunter_postings
  (id, fingerprint, source, source_job_id, url, canonical_url,
   company, title, location, remote, description, description_hash, content_confidence,
   first_seen_at, last_seen_at)
values
  ('c1000000-0000-0000-0000-000000000007', 'match-fp-rejected',
   'greenhouse', 'g-7', 'https://boards.greenhouse.io/acme/jobs/7', 'https://boards.greenhouse.io/acme/jobs/7',
   'Acme', 'Senior Backend Engineer', 'Berlin', true,
   'we use kubernetes and postgres every day', 'h-rejected', 'official_ats',
   '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'),
  ('c1000000-0000-0000-0000-000000000008', 'match-fp-membership-closed',
   'greenhouse', 'g-8', 'https://boards.greenhouse.io/acme/jobs/8', 'https://boards.greenhouse.io/acme/jobs/8',
   'Acme', 'Senior Backend Engineer', 'Berlin', true,
   'we use kubernetes and postgres every day', 'h-membership-closed', 'official_ats',
   '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z');

insert into public.job_hunter_job_facets
  (posting_id, description_hash_at_extraction, seniority, remote_policy, relocation_policy,
   compensation_disclosed, compensation_currency, compensation_max, compensation_period, extracted_at)
values
  ('c1000000-0000-0000-0000-000000000002', 'h-clean', 'senior', 'remote', 'not_offered',
   true, 'EUR', 120000, 'year', now()),
  ('c1000000-0000-0000-0000-000000000003', 'h-blocked', 'senior', 'remote', 'not_offered',
   true, 'EUR', 50000, 'year', now()),
  ('c1000000-0000-0000-0000-000000000005', 'h-thin', 'senior', 'onsite', 'not_offered',
   true, 'EUR', 50000, 'year', now());

select pg_temp.authenticate_as('aaaaaaaa-1111-0000-0000-000000000001'::uuid);

insert into public.job_hunter_search_profiles
  (user_id, timezone, scheduled_hour, max_jobs_per_run, source_minimum_per_run,
   source_max_share, salary_floor_eur, max_search_queries_per_run,
   max_canonical_resolutions_per_run, max_learned_ats_boards_per_run)
values
  ('aaaaaaaa-1111-0000-0000-000000000001', 'Europe/Berlin', 9, 35, 0, 0.5, 90000, 30, 80, 75)
returning id as profile_id \gset

insert into public.job_hunter_jobs (id, user_id, posting_id, market_id, first_seen_at, last_seen_at)
values
  ('d1000000-0000-0000-0000-000000000001', 'aaaaaaaa-1111-0000-0000-000000000001',
   'c1000000-0000-0000-0000-000000000001', '', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'),
  ('d1000000-0000-0000-0000-000000000002', 'aaaaaaaa-1111-0000-0000-000000000001',
   'c1000000-0000-0000-0000-000000000002', '', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'),
  ('d1000000-0000-0000-0000-000000000003', 'aaaaaaaa-1111-0000-0000-000000000001',
   'c1000000-0000-0000-0000-000000000003', '', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'),
  ('d1000000-0000-0000-0000-000000000004', 'aaaaaaaa-1111-0000-0000-000000000001',
   'c1000000-0000-0000-0000-000000000004', '', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'),
  ('d1000000-0000-0000-0000-000000000005', 'aaaaaaaa-1111-0000-0000-000000000001',
   'c1000000-0000-0000-0000-000000000005', '', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'),
  ('d1000000-0000-0000-0000-000000000006', 'aaaaaaaa-1111-0000-0000-000000000001',
   'c1000000-0000-0000-0000-000000000006', '', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z');

insert into public.job_hunter_jobs (id, user_id, posting_id, market_id, status, first_seen_at, last_seen_at)
values
  ('d1000000-0000-0000-0000-000000000007', 'aaaaaaaa-1111-0000-0000-000000000001',
   'c1000000-0000-0000-0000-000000000007', '', 'rejected', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'),
  ('d1000000-0000-0000-0000-000000000008', 'aaaaaaaa-1111-0000-0000-000000000001',
   'c1000000-0000-0000-0000-000000000008', '', 'closed', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z');

-- Behaviour ---------------------------------------------------------------------

select results_eq(
  $$ select job_id::text from public.job_hunter_match_jobs(
       p_preferred_roles => array['Backend Engineer'],
       p_must_have_signals => array['kubernetes', 'postgres']
     ) $$,
  $$ values
       ('d1000000-0000-0000-0000-000000000001'::text),
       ('d1000000-0000-0000-0000-000000000002'::text),
       ('d1000000-0000-0000-0000-000000000003'::text),
       ('d1000000-0000-0000-0000-000000000005'::text),
       ('d1000000-0000-0000-0000-000000000004'::text) $$,
  -- 001/002/003 tie (same title/company/source/location) and break on job_id;
  -- 005 shares everything but its source ("yc", a lower source_quality tier
  -- than greenhouse) so it ranks below them despite matching content; 004 is
  -- an unrelated posting and ranks last.
  'match_jobs: every membership row comes back, ordered by score descending, tied scores broken by company/title/job_id'
);

select is(
  (select score from public.job_hunter_match_jobs(
     p_preferred_roles => array['Backend Engineer'], p_must_have_signals => array['kubernetes', 'postgres']
   ) where job_id = 'd1000000-0000-0000-0000-000000000001'),
  (select score from public.job_hunter_match_jobs(
     p_preferred_roles => array['Backend Engineer'], p_must_have_signals => array['kubernetes', 'postgres']
   ) where job_id = 'd1000000-0000-0000-0000-000000000002'),
  'match_jobs: an unread posting scores identically to its read twin -- ranking never needed the facets'
);

select is(
  (select has_facets from public.job_hunter_match_jobs() where job_id = 'd1000000-0000-0000-0000-000000000001'),
  false,
  'match_jobs: a posting with no facets row is flagged has_facets = false'
);

select is(
  (select has_facets from public.job_hunter_match_jobs() where job_id = 'd1000000-0000-0000-0000-000000000002'),
  true,
  'match_jobs: a posting with a facets row is flagged has_facets = true'
);

select is(
  (select hard_blockers from public.job_hunter_match_jobs() where job_id = 'd1000000-0000-0000-0000-000000000002'),
  '{}'::text[],
  'match_jobs: clean facets produce no hard blockers'
);

select ok(
  (select array_length(hard_blockers, 1) from public.job_hunter_match_jobs()
    where job_id = 'd1000000-0000-0000-0000-000000000003') = 1,
  'match_jobs: compensation below this user''s floor is a hard blocker'
);

select ok(
  (select hard_blockers[1] from public.job_hunter_match_jobs()
    where job_id = 'd1000000-0000-0000-0000-000000000003') like '%below the EUR 90000 floor%',
  'match_jobs: the blocker names the floor it compared against'
);

select is(
  (select hard_blockers from public.job_hunter_match_jobs() where job_id = 'd1000000-0000-0000-0000-000000000005'),
  '{}'::text[],
  'match_jobs: thin content_confidence fails open even though the facets would otherwise block'
);

select ok(
  (select score from public.job_hunter_match_jobs(
     p_preferred_roles => array['Backend Engineer'], p_must_have_signals => array['kubernetes', 'postgres']
   ) where job_id = 'd1000000-0000-0000-0000-000000000002')
  >
  (select score from public.job_hunter_match_jobs(
     p_preferred_roles => array['Backend Engineer'], p_must_have_signals => array['kubernetes', 'postgres']
   ) where job_id = 'd1000000-0000-0000-0000-000000000004'),
  'match_jobs: a matching title/signal set outscores an unrelated posting'
);

select is(
  (select count(*) from public.job_hunter_match_jobs()
    where job_id = 'd1000000-0000-0000-0000-000000000006'),
  0::bigint,
  'match_jobs: a membership row on a closed posting is excluded outright (#188)'
);

select is(
  (select count(*) from public.job_hunter_match_jobs()
    where job_id = 'd1000000-0000-0000-0000-000000000007'),
  0::bigint,
  'match_jobs: a membership row prefilter already rejected is excluded outright (#188)'
);

select is(
  (select count(*) from public.job_hunter_match_jobs()
    where job_id = 'd1000000-0000-0000-0000-000000000008'),
  0::bigint,
  'match_jobs: a membership row with status=closed is excluded outright (#188)'
);

-- Company preferences (#198) ----------------------------------------------------
--
-- Compared before vs. after on the SAME posting, not against a different one:
-- two different postings also differ in source/remote/location, which would
-- confound a cross-posting diff with more than company_fit's contribution.

select score as before_score from public.job_hunter_match_jobs()
 where job_id = 'd1000000-0000-0000-0000-000000000002' \gset

select pg_temp.become_postgres();

-- `job_hunter_companies` is shared and never cleaned between runs (like
-- `job_hunter_postings`), and the pytest suite's own fixtures write this
-- same identity for real -- upsert rather than insert, so this file stays
-- runnable on a stack that already has an 'acme' row.
insert into public.job_hunter_companies (identity, display_name, industry, business_model)
values ('acme', 'Acme', 'fintech', 'b2b_saas')
on conflict (identity) do update
  set display_name = excluded.display_name,
      industry = excluded.industry,
      business_model = excluded.business_model;

select pg_temp.authenticate_as('aaaaaaaa-1111-0000-0000-000000000001'::uuid);

update public.job_hunter_search_profiles
   set preferred_industries = array['fintech']
 where user_id = 'aaaaaaaa-1111-0000-0000-000000000001';

select is(
  (select score from public.job_hunter_match_jobs() where job_id = 'd1000000-0000-0000-0000-000000000002')
  - :before_score,
  6,
  'match_jobs: a preferred-industry employer adds exactly the company_fit industry bonus'
);

update public.job_hunter_search_profiles
   set preferred_industries = '{}'
 where user_id = 'aaaaaaaa-1111-0000-0000-000000000001';

-- Market priority (#198's position column) ---------------------------------------

insert into public.job_hunter_search_profile_markets
  (user_id, profile_id, market_id, query_share, locations, currency, gross_base_floor,
   remote_policy, relocation_policy, sponsorship_policy, position)
values
  ('aaaaaaaa-1111-0000-0000-000000000001', :'profile_id', 'eu', 0.5, array['Berlin'], 'EUR', 80000,
   'preferred', 'selective', 'not_required', 0),
  ('aaaaaaaa-1111-0000-0000-000000000001', :'profile_id', 'other', 0.5, array['Nowhere'], 'EUR', 80000,
   'allowed', 'none', 'not_required', 1);

update public.job_hunter_jobs set market_id = 'eu'
 where id = 'd1000000-0000-0000-0000-000000000002';
update public.job_hunter_jobs set market_id = 'other'
 where id = 'd1000000-0000-0000-0000-000000000003';

select ok(
  (select score from public.job_hunter_match_jobs() where job_id = 'd1000000-0000-0000-0000-000000000002')
  >
  (select score from public.job_hunter_match_jobs() where job_id = 'd1000000-0000-0000-0000-000000000003'),
  'match_jobs: the first-declared market outscores a later one via market_priority_bonus, all else being closer'
);

-- RLS isolation -------------------------------------------------------------------

select pg_temp.authenticate_as('bbbbbbbb-1111-0000-0000-000000000002'::uuid);

select is_empty(
  $$ select * from public.job_hunter_match_jobs() $$,
  'match_jobs: a user with no search profile gets nothing, never another user''s rows'
);

insert into public.job_hunter_search_profiles
  (user_id, timezone, scheduled_hour, max_jobs_per_run, source_minimum_per_run,
   source_max_share, salary_floor_eur, max_search_queries_per_run,
   max_canonical_resolutions_per_run, max_learned_ats_boards_per_run)
values
  ('bbbbbbbb-1111-0000-0000-000000000002', 'Europe/Berlin', 9, 35, 0, 0.5, 90000, 30, 80, 75);

select is_empty(
  $$ select * from public.job_hunter_match_jobs() $$,
  'match_jobs: B has a profile but no membership rows, so still nothing -- never A''s postings'
);

-- Helper-level regression cases ----------------------------------------------
--
-- Both below exercise a helper directly rather than the whole `rows` join,
-- because the bug each guards against is internal to that one helper and a
-- full-corpus fixture would not make either failure mode legible.

-- `job_hunter_role_seniority_fit` must intersect deduplicated word sets, like
-- Python's `set(title_words) & set(role_words)`: a role phrase with a
-- repeated word must not inflate the ratio's denominator. Undeduplicated,
-- this call returns 8 (overlap 1 of 3 undeduplicated role words); the
-- Python-equivalent answer, deduplicating both sides, is 12 (overlap 1 of 2).
select is(
  public.job_hunter_role_seniority_fit(
    'Senior Product Engineer', array['Backend Backend Engineer'], array[]::text[]
  ),
  12,
  'role_seniority_fit: a repeated word in the role phrase does not change the ratio'
);

-- `job_hunter_salary_floor_for_job` must escape a `location_floors` key
-- before building a regex from it, like Python's `re.escape`: unescaped, a
-- key with a regex metacharacter either matches more than the literal
-- phrase, or -- an unbalanced paren or bracket -- raises instead of
-- returning. Both assertions would error out entirely on the unescaped port.
select is(
  public.job_hunter_salary_floor_for_job(
    'St.Louis, MO (Remote)', '{"st.louis": 120000}'::jsonb, 90000
  ),
  120000::bigint,
  'salary_floor_for_job: a location_floors key with a literal period matches only that phrase'
);
select is(
  public.job_hunter_salary_floor_for_job(
    'Berlin, DE', '{"st.louis": 120000}'::jsonb, 90000
  ),
  90000::bigint,
  'salary_floor_for_job: a location that does not name the configured city falls back to the global floor'
);

select * from finish();
rollback;
