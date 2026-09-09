-- Behaviour and isolation for the six Job Hunter store functions.
--
-- Each function replaces a multi-statement SQLite query in
-- apps/job-hunter/src/job_hunter/store.py that PostgREST cannot express in
-- one HTTP call. Every function is `security invoker`, so row level
-- security still applies inside it and the caller's own token decides what
-- it can see.
--
-- Two users are seeded. Both own a comparable fixture set, so every check
-- proves two things at once: the caller gets its OWN rows back (not an
-- empty result that would pass vacuously), and it never gets the other
-- user's rows. Nothing here disables RLS or seeds rows as a superuser --
-- every fixture row is inserted while acting as its owner, so it must pass
-- that table's insert_own policy to exist at all.
begin;
create extension if not exists pgtap with schema extensions;
select no_plan();

-- Seed users ----------------------------------------------------------------

insert into auth.users (id, email, instance_id, aud, role, raw_app_meta_data, raw_user_meta_data, created_at, updated_at)
values
  ('11111111-0000-0000-0000-00000000000a', 'store-fn-a@test.local', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', '{}', '{}', now(), now()),
  ('22222222-0000-0000-0000-00000000000b', 'store-fn-b@test.local', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', '{}', '{}', now(), now())
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

-- Signatures ------------------------------------------------------------------

select has_function('public', 'job_hunter_upsert_job', array['jsonb'],
  'job_hunter_upsert_job exists');
select has_function('public', 'job_hunter_upsert_posting', array['jsonb'],
  'job_hunter_upsert_posting exists');
select has_function('public', 'job_hunter_pending_delivery_jobs', array['integer'],
  'job_hunter_pending_delivery_jobs exists');
select has_function('public', 'job_hunter_pending_review_events', array['double precision'],
  'job_hunter_pending_review_events exists');
select has_function('public', 'job_hunter_merge_jobs', array['uuid', 'uuid'],
  'job_hunter_merge_jobs exists');
select has_function('public', 'job_hunter_eligible_inbound_jobs', array[]::text[],
  'job_hunter_eligible_inbound_jobs exists');
select has_function('public', 'job_hunter_find_job_by_identity', array['text', 'text', 'text'],
  'job_hunter_find_job_by_identity exists');
select has_function('public', 'job_hunter_get_provider_credentials', array[]::text[],
  'job_hunter_get_provider_credentials exists');

-- Three security-definer exceptions, and no more. Every store and normalizer
-- function must continue to run with the caller's own privileges so one
-- user's call cannot read another user's rows.
--
--   * job_hunter_get_provider_credentials, the runner-only retrieval RPC.
--   * job_hunter_merge_postings (#176), which must re-point every affected
--     user's job row at the surviving posting and cannot as an invoker:
--     row-level security scopes one to its own rows, under which the update
--     would silently touch nothing and leave other users pointing at a
--     posting nobody maintains. It is revoked from anon, authenticated and
--     service_role, so the only way in is the function below.
--   * job_hunter_merge_jobs (#176), definer so that it -- and only it -- can
--     execute the above. It never relied on RLS: every statement in it
--     carries its own `user_id = (select auth.uid())` predicate, and RLS on
--     each per-user table it touches is exactly that same predicate, so the
--     two express one restriction. Asserted below rather than assumed.
select is(
  (select array_agg(p.proname::text order by p.proname)
     from pg_proc p
     join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public'
      and p.proname like 'job\_hunter\_%'
      and p.prosecdef),
  array['job_hunter_get_provider_credentials', 'job_hunter_merge_jobs', 'job_hunter_merge_postings'],
  'credential retrieval and the two merges are the only public.job_hunter_* security definers');

-- The revoke is the point of making the merge definer, so pin it: no role a
-- user can hold may reach job_hunter_merge_postings directly.
select is(
  (select array_agg(g.grantee::text order by g.grantee)
     from information_schema.routine_privileges g
    where g.specific_schema = 'public'
      and g.routine_name = 'job_hunter_merge_postings'
      and g.grantee in ('anon', 'authenticated', 'service_role', 'PUBLIC')),
  null,
  'no user-reachable role may execute the posting merge directly');

-- Every one must pin an empty search_path so an attacker-controlled
-- search_path cannot swap a table out from under it.
select is(
  (select array_agg(p.proname::text order by p.proname)
     from pg_proc p
     join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public'
      and p.proname like 'job\_hunter\_%'
      and coalesce(array_to_string(p.proconfig, ','), '') not like '%search_path=%'),
  null,
  'every public.job_hunter_* function pins search_path');

-- Pin the full population asserted over above: every existing store and
-- normalizer function plus the runner-only retrieval RPC. A new function must
-- be added here deliberately, which forces someone to review both checks.
select is(
  (select array_agg(p.proname::text order by p.proname)
     from pg_proc p
     join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public' and p.proname like 'job\_hunter\_%'),
  array[
    'job_hunter_canonicalize_url',
    'job_hunter_confidence_rank',
    'job_hunter_eligible_inbound_jobs',
    'job_hunter_find_job_by_identity',
    'job_hunter_get_provider_credentials',
    'job_hunter_gmail_candidate_complete',
    'job_hunter_locations_compatible',
    'job_hunter_merge_jobs',
    'job_hunter_merge_posting_batch',
    'job_hunter_merge_postings',
    'job_hunter_needs_evaluation',
    'job_hunter_normalize_company',
    'job_hunter_normalize_text',
    'job_hunter_normalize_tokens',
    'job_hunter_pending_delivery_jobs',
    'job_hunter_pending_review_events',
    'job_hunter_preferred_description',
    'job_hunter_record_ats_eligible_jobs',
    'job_hunter_resolve_posting',
    'job_hunter_schedule_stage_enqueue',
    'job_hunter_set_job_markets',
    'job_hunter_stage_queue_metrics',
    'job_hunter_upsert_job',
    'job_hunter_upsert_jobs',
    'job_hunter_upsert_posting'],
  'exactly the twenty-five expected public.job_hunter_* functions exist, so the two checks above are not asserting over an empty set');

-- Fixtures for user A ------------------------------------------------------------

select pg_temp.authenticate_as('11111111-0000-0000-0000-00000000000a');

insert into public.job_hunter_jobs
  (id, user_id, fingerprint, source, source_job_id, url, canonical_url,
   company, title, location, description, description_hash, content_confidence,
   first_seen_at, last_seen_at)
values
  ('10000000-0000-0000-0000-000000000001', '11111111-0000-0000-0000-00000000000a', 'fp-deliver',
   'greenhouse', 'g-1', 'https://deliver.example/1', 'https://deliver.example/1',
   'Deliver Co', 'Staff Engineer', 'Vienna', 'deliver desc', 'h-deliver', 'official_ats',
   '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'),
  ('10000000-0000-0000-0000-000000000002', '11111111-0000-0000-0000-00000000000a', 'fp-stale',
   'lever', 'l-2', 'https://stale.example/2', 'https://stale.example/2',
   'Stale Co', 'Site Reliability Engineer', 'Graz', 'stale desc', 'h-stale', 'official_ats',
   '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'),
  ('10000000-0000-0000-0000-000000000003', '11111111-0000-0000-0000-00000000000a', 'fp-delivered',
   'ashby', 'a-3', 'https://delivered.example/3', 'https://delivered.example/3',
   'Delivered Co', 'Principal Engineer', 'Linz', 'delivered desc', 'h-delivered', 'official_ats',
   '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'),
  ('10000000-0000-0000-0000-000000000004', '11111111-0000-0000-0000-00000000000a', 'fp-identity',
   'greenhouse', 'g-4', 'https://acme.example/jobs/9', 'https://acme.example/jobs/9',
   'Acme GmbH', 'Senior  Backend Engineer!', 'Berlin, Germany', 'acme desc', 'h-acme', 'official_ats',
   '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'),
  ('10000000-0000-0000-0000-000000000005', '11111111-0000-0000-0000-00000000000a', 'fp-merge-old',
   '', null, 'https://m.example/old', '',
   'Merge Co', 'Merge Engineer', 'Zurich', 'short', 'h-old', 'aggregator_text',
   '2026-01-01T00:00:00Z', '2026-01-05T00:00:00Z'),
  ('10000000-0000-0000-0000-000000000006', '11111111-0000-0000-0000-00000000000a', 'fp-merge-new',
   'greenhouse', 'g-6', 'https://m.example/new', 'https://m.example/new',
   '', 'Merge Engineer', '', 'a much longer description than the other one', 'h-new', 'aggregator_text',
   '2026-01-03T00:00:00Z', '2026-01-04T00:00:00Z'),
  ('10000000-0000-0000-0000-000000000007', '11111111-0000-0000-0000-00000000000a', 'fp-inbound',
   'gmail:greenhouse', 'cand-key-1', 'https://inbound.example/7', 'https://inbound.example/7',
   'Inbound Materialized Co', 'Inbound Engineer', 'Salzburg', 'inbound desc', 'h-inbound', 'aggregator_text',
   '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z');

-- Latest evaluation is decided by evaluated_at, not by insertion order, so
-- the stale job's GOOD evaluation is inserted last on purpose.
insert into public.job_hunter_evaluations
  (user_id, job_id, total_score, decision, evaluated_at)
values
  ('11111111-0000-0000-0000-00000000000a', '10000000-0000-0000-0000-000000000001', 10, 'reject', '2026-01-01T00:00:00Z'),
  ('11111111-0000-0000-0000-00000000000a', '10000000-0000-0000-0000-000000000001', 90, 'high_priority', '2026-01-02T00:00:00Z'),
  ('11111111-0000-0000-0000-00000000000a', '10000000-0000-0000-0000-000000000003', 90, 'high_priority', '2026-01-02T00:00:00Z'),
  ('11111111-0000-0000-0000-00000000000a', '10000000-0000-0000-0000-000000000005', 5, 'reject', '2026-01-02T00:00:00Z'),
  ('11111111-0000-0000-0000-00000000000a', '10000000-0000-0000-0000-000000000002', 95, 'high_priority', '2026-01-01T00:00:00Z'),
  ('11111111-0000-0000-0000-00000000000a', '10000000-0000-0000-0000-000000000002', 5, 'reject', '2026-01-02T00:00:00Z');

insert into public.job_hunter_deliveries (user_id, job_id, delivery_type, delivered_at)
values ('11111111-0000-0000-0000-00000000000a', '10000000-0000-0000-0000-000000000003',
        'telegram_message', '2026-01-03T00:00:00Z');

insert into public.job_hunter_gmail_messages
  (user_id, message_id, subject, occurred_at, classification, confidence, processed_at)
values
  ('11111111-0000-0000-0000-00000000000a', 'm1', 'Subject One', '2026-02-01T00:00:00Z', 'review', 0.9, now()),
  ('11111111-0000-0000-0000-00000000000a', 'm2', 'Subject Two', '2026-02-02T00:00:00Z', 'applied', 0.2, now()),
  ('11111111-0000-0000-0000-00000000000a', 'm3', 'Subject Three', '2026-02-03T00:00:00Z', 'applied', 0.99, now()),
  ('11111111-0000-0000-0000-00000000000a', 'm4', 'Subject Four', '2026-02-04T00:00:00Z', 'review', 0.9, now()),
  ('11111111-0000-0000-0000-00000000000a', 'm5', 'Subject Five', '2026-02-05T00:00:00Z', 'applied', 0.99, now());

insert into public.job_hunter_application_events
  (id, user_id, job_id, event_type, occurred_at, source_message_id, confidence)
values
  ('30000000-0000-0000-0000-000000000001', '11111111-0000-0000-0000-00000000000a', null,
   'REVIEW_NEEDED', '2026-02-01T00:00:00Z', 'm1', 0.99),
  ('30000000-0000-0000-0000-000000000002', '11111111-0000-0000-0000-00000000000a', null,
   'APPLIED', '2026-02-02T00:00:00Z', 'm2', 0.2),
  ('30000000-0000-0000-0000-000000000003', '11111111-0000-0000-0000-00000000000a',
   '10000000-0000-0000-0000-000000000001', 'APPLIED', '2026-02-03T00:00:00Z', 'm3', 0.99),
  ('30000000-0000-0000-0000-000000000004', '11111111-0000-0000-0000-00000000000a', null,
   'REVIEW_NEEDED', '2026-02-04T00:00:00Z', 'm4', 0.99),
  ('30000000-0000-0000-0000-000000000005', '11111111-0000-0000-0000-00000000000a',
   '10000000-0000-0000-0000-000000000006', 'APPLIED', '2026-02-05T00:00:00Z', 'm5', 0.99);

-- Already surfaced, so it must not come back.
insert into public.job_hunter_review_deliveries (user_id, event_id, delivered_at)
values ('11111111-0000-0000-0000-00000000000a', '30000000-0000-0000-0000-000000000004', now());

insert into public.job_hunter_inbound_job_candidates
  (id, user_id, source_message_id, source_candidate_key, source_platform,
   url, company, title, location, last_seen_at)
values
  -- excluded: a job already carries source 'gmail:greenhouse' + this key
  ('40000000-0000-0000-0000-000000000001', '11111111-0000-0000-0000-00000000000a',
   'm1', 'cand-key-1', 'greenhouse', '', 'Inbound Matched Co', 'Inbound Engineer', 'Salzburg', now()),
  -- returned: nothing materialized matches it
  ('40000000-0000-0000-0000-000000000002', '11111111-0000-0000-0000-00000000000a',
   'm2', 'cand-key-2', 'lever', 'https://fresh.example/j/2', 'Fresh Startup', 'Platform Engineer', 'Remote', now()),
  -- excluded by URL alone: the tracking parameter must be canonicalized away
  ('40000000-0000-0000-0000-000000000003', '11111111-0000-0000-0000-00000000000a',
   'm3', 'cand-key-3', '', 'https://acme.example/jobs/9?utm_source=newsletter#apply', '', '', '', now());

insert into public.job_hunter_job_sources
  (user_id, job_id, source, source_job_id, source_url, identity_key, first_seen_at, last_seen_at)
values
  ('11111111-0000-0000-0000-00000000000a', '10000000-0000-0000-0000-000000000005',
   'lever', null, 'https://m.example/old', 'url:https://m.example/old',
   '2026-01-01T00:00:00Z', '2026-01-05T00:00:00Z'),
  ('11111111-0000-0000-0000-00000000000a', '10000000-0000-0000-0000-000000000005',
   'lever', null, 'https://m.example/shared', 'url:https://m.example/shared',
   '2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z'),
  ('11111111-0000-0000-0000-00000000000a', '10000000-0000-0000-0000-000000000006',
   'greenhouse', 'g-6', 'https://m.example/new', 'url:https://m.example/new',
   '2026-01-03T00:00:00Z', '2026-01-04T00:00:00Z'),
  ('11111111-0000-0000-0000-00000000000a', '10000000-0000-0000-0000-000000000006',
   'greenhouse', 'g-6', 'https://m.example/shared', 'url:https://m.example/shared',
   '2026-01-04T00:00:00Z', '2026-01-06T00:00:00Z');

-- Fixtures for user B ------------------------------------------------------------
-- B's set mirrors A's shape so every "as B" assertion below returns B's own
-- rows. An empty result would not prove isolation, only that the fixture
-- failed to load.

select pg_temp.authenticate_as('22222222-0000-0000-0000-00000000000b');

insert into public.job_hunter_jobs
  (id, user_id, fingerprint, source, source_job_id, url, canonical_url,
   company, title, location, description, description_hash, content_confidence,
   first_seen_at, last_seen_at)
values
  ('20000000-0000-0000-0000-000000000001', '22222222-0000-0000-0000-00000000000b', 'fp-b-deliver',
   'greenhouse', 'gb-1', 'https://b.example/1', 'https://b.example/1',
   'B Deliver Co', 'B Staff Engineer', 'Berlin', 'b desc', 'h-b', 'official_ats',
   '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z');

insert into public.job_hunter_evaluations (user_id, job_id, total_score, decision, evaluated_at)
values ('22222222-0000-0000-0000-00000000000b', '20000000-0000-0000-0000-000000000001',
        88, 'package_match', '2026-01-02T00:00:00Z');

insert into public.job_hunter_gmail_messages
  (user_id, message_id, subject, occurred_at, classification, confidence, processed_at)
values ('22222222-0000-0000-0000-00000000000b', 'bm1', 'B Subject', '2026-02-01T00:00:00Z', 'review', 0.9, now());

insert into public.job_hunter_application_events
  (id, user_id, job_id, event_type, occurred_at, source_message_id, confidence)
values ('30000000-0000-0000-0000-0000000000b1', '22222222-0000-0000-0000-00000000000b', null,
        'REVIEW_NEEDED', '2026-02-01T00:00:00Z', 'bm1', 0.99);

insert into public.job_hunter_inbound_job_candidates
  (id, user_id, source_message_id, source_candidate_key, source_platform,
   url, company, title, location, last_seen_at)
values ('40000000-0000-0000-0000-0000000000b1', '22222222-0000-0000-0000-00000000000b',
        'bm1', 'b-cand-1', 'lever', 'https://b.example/cand/1', 'B Fresh Co', 'B Platform Engineer', 'Remote', now());

-- 1. job_hunter_pending_delivery_jobs -----------------------------------------------
-- store.py:2126-2141. Only the LATEST evaluation counts, "latest" is now
-- newest evaluated_at rather than highest autoincrement id.

select pg_temp.authenticate_as('11111111-0000-0000-0000-00000000000a');

select results_eq(
  $$ select job_id::text from public.job_hunter_pending_delivery_jobs(50) $$,
  $$ values ('10000000-0000-0000-0000-000000000001'::text) $$,
  'pending_delivery_jobs: A gets the job whose latest evaluation is deliverable, and only that one');

select is_empty(
  $$ select job_id from public.job_hunter_pending_delivery_jobs(50)
     where job_id = '10000000-0000-0000-0000-000000000002' $$,
  'pending_delivery_jobs: a job whose newest evaluation rejects is excluded even though an older one passed');

select is_empty(
  $$ select job_id from public.job_hunter_pending_delivery_jobs(50)
     where job_id = '10000000-0000-0000-0000-000000000003' $$,
  'pending_delivery_jobs: a job already delivered by telegram_message is excluded');

select is_empty(
  $$ select job_id from public.job_hunter_pending_delivery_jobs(95) $$,
  'pending_delivery_jobs: the score floor is exclusive');

select pg_temp.authenticate_as('22222222-0000-0000-0000-00000000000b');
select results_eq(
  $$ select job_id::text from public.job_hunter_pending_delivery_jobs(50) $$,
  $$ values ('20000000-0000-0000-0000-000000000001'::text) $$,
  'pending_delivery_jobs: B gets only its own job, never A''s');

-- 2. job_hunter_pending_review_events ------------------------------------------------
-- store.py:1907-1932.

select pg_temp.authenticate_as('11111111-0000-0000-0000-00000000000a');

select results_eq(
  $$ select (e->>'id') from public.job_hunter_pending_review_events(0.8) e $$,
  $$ values ('30000000-0000-0000-0000-000000000001'::text),
            ('30000000-0000-0000-0000-000000000002'::text) $$,
  'pending_review_events: A gets the unreviewed events, ordered by occurred_at');

select is(
  (select e->>'subject' from public.job_hunter_pending_review_events(0.8) e
    where e->>'id' = '30000000-0000-0000-0000-000000000001'),
  'Subject One',
  'pending_review_events: the joined gmail subject travels with the event');

select pg_temp.authenticate_as('22222222-0000-0000-0000-00000000000b');
select results_eq(
  $$ select (e->>'id') from public.job_hunter_pending_review_events(0.8) e $$,
  $$ values ('30000000-0000-0000-0000-0000000000b1'::text) $$,
  'pending_review_events: B gets only its own event, never A''s');

-- 3. job_hunter_eligible_inbound_jobs -------------------------------------------------
-- store.py:1805-1822.

select pg_temp.authenticate_as('11111111-0000-0000-0000-00000000000a');

select results_eq(
  $$ select (c->>'id') from public.job_hunter_eligible_inbound_jobs() c $$,
  $$ values ('40000000-0000-0000-0000-000000000001'::text),
            ('40000000-0000-0000-0000-000000000002'::text),
            ('40000000-0000-0000-0000-000000000003'::text) $$,
  'eligible_inbound_jobs: unevaluated materialized candidates remain eligible');

select pg_temp.authenticate_as('22222222-0000-0000-0000-00000000000b');
select results_eq(
  $$ select (c->>'id') from public.job_hunter_eligible_inbound_jobs() c $$,
  $$ values ('40000000-0000-0000-0000-0000000000b1'::text) $$,
  'eligible_inbound_jobs: B gets only its own candidate, never A''s');

-- 4. job_hunter_find_job_by_identity --------------------------------------------------
-- store.py:1180-1222. Normalization drops a safe legal suffix, collapses
-- punctuation and whitespace, and compares locations as whole words.

select pg_temp.authenticate_as('11111111-0000-0000-0000-00000000000a');

select results_eq(
  $$ select id::text from public.job_hunter_find_job_by_identity('acme', 'Senior Backend Engineer', 'Berlin') id $$,
  $$ values ('10000000-0000-0000-0000-000000000004'::text) $$,
  'find_job_by_identity: matches on normalized company/title with a compatible location');

select is_empty(
  $$ select * from public.job_hunter_find_job_by_identity('Acme', '', 'Berlin') $$,
  'find_job_by_identity: an empty normalized title matches nothing');

select is_empty(
  $$ select * from public.job_hunter_find_job_by_identity('Acme', 'Senior Backend Engineer', 'Tokyo') $$,
  'find_job_by_identity: an incompatible location matches nothing');

select pg_temp.authenticate_as('22222222-0000-0000-0000-00000000000b');
select is_empty(
  $$ select * from public.job_hunter_find_job_by_identity('acme', 'Senior Backend Engineer', 'Berlin') $$,
  'find_job_by_identity: B never sees A''s job');

-- 5. job_hunter_upsert_job -------------------------------------------------------------
-- store.py:649-743 and 744-905.

select pg_temp.authenticate_as('11111111-0000-0000-0000-00000000000a');

select results_eq(
  $$ select is_new, description_changed from public.job_hunter_upsert_job(
       '{"fingerprint":"fp-upsert-1","source":"greenhouse","source_job_id":"u1",
         "url":"https://up.example/1","canonical_url":"https://up.example/1",
         "company":"Upsert Test Co","title":"Data Engineer","location":"Vienna",
         "description":"first","content_confidence":"official_ats"}'::jsonb) $$,
  $$ values (true, false) $$,
  'upsert_job: a fingerprint never seen before inserts a new job');

select results_eq(
  $$ select is_new, description_changed from public.job_hunter_upsert_job(
       '{"fingerprint":"fp-upsert-1","source":"greenhouse","source_job_id":"u1",
         "url":"https://up.example/1","canonical_url":"https://up.example/1",
         "company":"Upsert Test Co","title":"Data Engineer","location":"Vienna",
         "description":"a materially longer description than the first one",
         "content_confidence":"official_ats"}'::jsonb) $$,
  $$ values (false, true) $$,
  'upsert_job: the same job with a better description updates in place and reports the change');

select is(
  (select count(*)::int from public.job_hunter_jobs where fingerprint = 'fp-upsert-1'),
  1,
  'upsert_job: two calls leave exactly one job row');

select is(
  (select count(*)::int from public.job_hunter_job_sources s
     join public.job_hunter_jobs j on j.id = s.job_id
    where j.fingerprint = 'fp-upsert-1'),
  1,
  'upsert_job: the discovery source is recorded once, not once per call');

select is(
  (select description from public.job_hunter_jobs where fingerprint = 'fp-upsert-1'),
  'a materially longer description than the first one',
  'upsert_job: the better description wins at equal confidence');

select is(
  (select description_hash from public.job_hunter_jobs where fingerprint = 'fp-upsert-1'),
  -- The literal is what Python's normalize.py:description_hash returns for
  -- that exact string. Hard-coded on purpose: recomputing it in SQL here
  -- would only prove the function agrees with itself.
  'ecfa7abb00d22b6fb180f6d924b84c5a51bc6236bff4374cb505e57920b16998',
  'upsert_job: description_hash is the sha256 hex digest the Python store computes');

-- The identity path must find the same row even when the fingerprint changes.
select results_eq(
  $$ select is_new from public.job_hunter_upsert_job(
       '{"fingerprint":"fp-upsert-1-changed","source":"lever","source_job_id":"u2",
         "url":"https://up.example/1","canonical_url":"https://up.example/1",
         "company":"Upsert Test Co Ltd","title":"Data  Engineer","location":"Vienna",
         "description":"x","content_confidence":"aggregator_text"}'::jsonb) $$,
  $$ values (false) $$,
  'upsert_job: a changed fingerprint still resolves to the existing job by canonical URL and identity');

-- match_mode: 'fingerprint' (store.py:649-743) and 'logical'
-- (store.py:744-905) are NOT interchangeable. The same payload, differing
-- only in match_mode, must resolve differently: a job reachable by canonical
-- URL and identity but carrying a DIFFERENT fingerprint is found by the
-- logical mode and missed by the fingerprint mode.

select results_eq(
  $$ select is_new from public.job_hunter_upsert_job(
       '{"fingerprint":"fp-mode-base","source":"greenhouse","source_job_id":"mb",
         "url":"https://mode.example/1","canonical_url":"https://mode.example/1",
         "company":"Mode Test Co","title":"Mode Engineer","location":"Vienna",
         "description":"base","content_confidence":"official_ats"}'::jsonb) $$,
  $$ values (true) $$,
  'match_mode: the baseline job is inserted');

select results_eq(
  $$ select is_new from public.job_hunter_upsert_job(
       '{"match_mode":"fingerprint","fingerprint":"fp-mode-other","source":"greenhouse",
         "source_job_id":"mo","url":"https://mode.example/1",
         "canonical_url":"https://mode.example/1",
         "company":"Mode Test Co","title":"Mode Engineer","location":"Vienna",
         "description":"other","content_confidence":"official_ats"}'::jsonb) $$,
  $$ values (true) $$,
  'match_mode fingerprint: a different fingerprint inserts a second job even though the canonical URL and identity already match one');

select is(
  (select count(*)::int from public.job_hunter_jobs
    where canonical_url = 'https://mode.example/1'),
  2,
  'match_mode fingerprint: it really did create a second row, it did not merge');

select is(
  (select count(*)::int from public.job_hunter_job_sources s
     join public.job_hunter_jobs j on j.id = s.job_id
    where j.fingerprint = 'fp-mode-other'),
  0,
  'match_mode fingerprint: no discovery source is recorded, matching upsert_job which never called _record_job_source');

select results_eq(
  $$ select is_new from public.job_hunter_upsert_job(
       '{"fingerprint":"fp-mode-third","source":"greenhouse","source_job_id":"mt",
         "url":"https://mode.example/1","canonical_url":"https://mode.example/1",
         "company":"Mode Test Co","title":"Mode Engineer","location":"Vienna",
         "description":"third","content_confidence":"official_ats"}'::jsonb) $$,
  $$ values (false) $$,
  'match_mode logical: the same third fingerprint resolves onto the existing job instead of inserting');

select is(
  (select count(*)::int from public.job_hunter_jobs
    where canonical_url = 'https://mode.example/1'),
  1,
  'match_mode logical: the duplicate the fingerprint mode created is merged away');

select throws_ok(
  $$ select * from public.job_hunter_upsert_job(
       '{"match_mode":"nonsense","fingerprint":"fp-mode-bad"}'::jsonb) $$,
  null, null,
  'match_mode: an unrecognized mode raises rather than silently defaulting');

select pg_temp.authenticate_as('22222222-0000-0000-0000-00000000000b');
select results_eq(
  $$ select is_new from public.job_hunter_upsert_job(
       '{"fingerprint":"fp-upsert-1","source":"greenhouse","source_job_id":"u1",
         "url":"https://up.example/1","canonical_url":"https://up.example/1",
         "company":"Upsert Test Co","title":"Data Engineer","location":"Vienna",
         "description":"first","content_confidence":"official_ats"}'::jsonb) $$,
  $$ values (true) $$,
  'upsert_job: B cannot see or update A''s job with the same fingerprint, it gets its own');

select is(
  (select count(*)::int from public.job_hunter_jobs where fingerprint = 'fp-upsert-1'),
  1,
  'upsert_job: B sees exactly one job with that fingerprint, its own');

-- 6. job_hunter_merge_jobs ---------------------------------------------------------------
-- store.py:906-1090. Run last: it deletes a fixture job.

select pg_temp.authenticate_as('22222222-0000-0000-0000-00000000000b');
select throws_ok(
  $$ select public.job_hunter_merge_jobs('10000000-0000-0000-0000-000000000005',
                                         '10000000-0000-0000-0000-000000000006') $$,
  null, null,
  'merge_jobs: B cannot merge A''s jobs');

select pg_temp.authenticate_as('11111111-0000-0000-0000-00000000000a');

select is(
  (select public.job_hunter_merge_jobs('10000000-0000-0000-0000-000000000005',
                                       '10000000-0000-0000-0000-000000000006')::text),
  '10000000-0000-0000-0000-000000000006',
  'merge_jobs: the job carrying application-event history survives even when passed as the duplicate');

select is_empty(
  $$ select 1 from public.job_hunter_jobs where id = '10000000-0000-0000-0000-000000000005' $$,
  'merge_jobs: the losing job is deleted');

select is(
  (select company from public.job_hunter_jobs where id = '10000000-0000-0000-0000-000000000006'),
  'Merge Co',
  'merge_jobs: an empty survivor field is backfilled from the duplicate');

select is(
  (select description from public.job_hunter_jobs where id = '10000000-0000-0000-0000-000000000006'),
  'a much longer description than the other one',
  'merge_jobs: the longer description wins at equal confidence');

select is(
  (select first_seen_at from public.job_hunter_jobs where id = '10000000-0000-0000-0000-000000000006'),
  '2026-01-01T00:00:00Z'::timestamptz,
  'merge_jobs: first_seen_at is the earlier of the two (LEAST, not an aggregate MIN)');

select is(
  (select last_seen_at from public.job_hunter_jobs where id = '10000000-0000-0000-0000-000000000006'),
  '2026-01-05T00:00:00Z'::timestamptz,
  'merge_jobs: last_seen_at is the later of the two (GREATEST)');

select is(
  (select count(*)::int from public.job_hunter_evaluations
    where job_id = '10000000-0000-0000-0000-000000000006'),
  1,
  'merge_jobs: the duplicate''s evaluation is reassigned to the survivor');

select is(
  (select count(*)::int from public.job_hunter_job_sources
    where job_id = '10000000-0000-0000-0000-000000000006'),
  3,
  'merge_jobs: sources are unioned on (job_id, identity_key)');

select is(
  (select first_seen_at from public.job_hunter_job_sources
    where job_id = '10000000-0000-0000-0000-000000000006'
      and identity_key = 'url:https://m.example/shared'),
  '2026-01-01T00:00:00Z'::timestamptz,
  'merge_jobs: a colliding source keeps the earliest first_seen_at');

select is(
  (select last_seen_at from public.job_hunter_job_sources
    where job_id = '10000000-0000-0000-0000-000000000006'
      and identity_key = 'url:https://m.example/shared'),
  '2026-01-06T00:00:00Z'::timestamptz,
  'merge_jobs: a colliding source keeps the latest last_seen_at');

select is(
  (select public.job_hunter_merge_jobs('10000000-0000-0000-0000-000000000006',
                                       '10000000-0000-0000-0000-000000000006')::text),
  '10000000-0000-0000-0000-000000000006',
  'merge_jobs: merging a job with itself is a no-op');

select throws_ok(
  $$ select public.job_hunter_merge_jobs('10000000-0000-0000-0000-000000000006',
                                         '99999999-0000-0000-0000-000000000099') $$,
  null, null,
  'merge_jobs: a missing job raises rather than silently half-merging');

-- 7. merge redirects (#145) --------------------------------------------------------------
-- The deleted duplicate's id has to keep resolving: a run that selected it
-- before the merge is still holding it when it writes the evaluation.

select is(
  (select survivor_id::text from public.job_hunter_job_merges
    where duplicate_id = '10000000-0000-0000-0000-000000000005'),
  '10000000-0000-0000-0000-000000000006',
  'merge_jobs: the deleted duplicate''s id redirects to the survivor');

select is(
  (select count(*)::int from public.job_hunter_job_merges
    where duplicate_id = '10000000-0000-0000-0000-000000000006'),
  0,
  'merge_jobs: a self-merge records nothing, since nothing was deleted');

-- A survivor can itself be merged away later. Redirects are repointed when
-- that happens, so a reader never has to walk a chain to a deleted row.
insert into public.job_hunter_jobs
  (id, user_id, fingerprint, source, url, company, title, location,
   description, description_hash, content_confidence, first_seen_at, last_seen_at)
values
  ('10000000-0000-0000-0000-000000000009', '11111111-0000-0000-0000-00000000000a',
   'fp-merge-third', '', 'https://m.example/third', 'Merge Co', 'Merge Engineer',
   'Zurich', 'third description', 'h-third', 'aggregator_text',
   '2025-12-31T00:00:00Z', '2025-12-31T00:00:00Z');

-- The merge above left every history signal on 006, so this row has to match
-- it on all of them and win on the first_seen_at tie-break, which is what
-- makes the survivor here deterministic rather than an id comparison.
insert into public.job_hunter_application_events
  (user_id, job_id, event_type, occurred_at, source_message_id, confidence)
values
  ('11111111-0000-0000-0000-00000000000a', '10000000-0000-0000-0000-000000000009',
   'APPLIED', '2025-12-31T00:00:00Z', 'm-third', 1.0);

insert into public.job_hunter_evaluations (user_id, job_id, evaluated_at)
values
  ('11111111-0000-0000-0000-00000000000a', '10000000-0000-0000-0000-000000000009',
   '2025-12-31T00:00:00Z');

select is(
  (select public.job_hunter_merge_jobs('10000000-0000-0000-0000-000000000009',
                                       '10000000-0000-0000-0000-000000000006')::text),
  '10000000-0000-0000-0000-000000000009',
  'merge_jobs: the earlier first_seen_at decides when history is equal');

select is(
  (select survivor_id::text from public.job_hunter_job_merges
    where duplicate_id = '10000000-0000-0000-0000-000000000005'),
  '10000000-0000-0000-0000-000000000009',
  'merge_jobs: an existing redirect is repointed when its survivor is merged away');

select is(
  (select survivor_id::text from public.job_hunter_job_merges
    where duplicate_id = '10000000-0000-0000-0000-000000000006'),
  '10000000-0000-0000-0000-000000000009',
  'merge_jobs: the new duplicate gets its own redirect');

select pg_temp.become_postgres();
select * from finish();
rollback;
