-- One writing role for the shared tables (issue #179).
--
-- job_hunter_isolation.sql owns the *inventory* -- which tables are shared, and
-- that none of them may be written by a user (the grant half). This file owns
-- the two things that inventory cannot express:
--
--   1. What actually happens at the wire. A grant table says `authenticated`
--      has no insert privilege; this proves the insert raises 42501, as
--      `authenticated` and as `anon`, per table, and that select still works.
--   2. The other door. A SECURITY DEFINER function runs as its owner and
--      consults neither the caller's grants nor the policies, so revoking table
--      grants constrains it not at all. Every definer function that can reach a
--      shared table is proven unreachable by `authenticated` here, and the
--      inventory of definer functions `authenticated` *may* execute is pinned,
--      so adding a new one is a decision somebody has to make on purpose.
--
-- The privileged side is proven too, in the same file and against the same
-- rows: the owner -- which is what ingestion's direct connection connects as --
-- still writes every one of these tables, and still runs the job-upsert path
-- end to end. A suite that only proved the refusals would pass just as well
-- against a schema where nothing can write at all.

begin;
create extension if not exists pgtap with schema extensions;
select no_plan();

-- Seed users ------------------------------------------------------------------

insert into auth.users (id, email, instance_id, aud, role, raw_app_meta_data, raw_user_meta_data, created_at, updated_at)
values
  ('eeeeeeee-0000-0000-0000-000000000001', 'shared-writes-a@test.local', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', '{}', '{}', now(), now()),
  ('eeeeeeee-0000-0000-0000-000000000002', 'shared-writes-b@test.local', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', '{}', '{}', now(), now())
on conflict (id) do nothing;

create function pg_temp.authenticate_as(p_user uuid) returns void
language plpgsql as $$
begin
  execute 'reset role';
  perform set_config('request.jwt.claims',
    json_build_object('sub', p_user, 'role', 'authenticated')::text, true);
  execute 'set local role authenticated';
end $$;

create function pg_temp.become_anon() returns void
language plpgsql as $$
begin
  execute 'reset role';
  perform set_config('request.jwt.claims', json_build_object('role', 'anon')::text, true);
  execute 'set local role anon';
end $$;

create function pg_temp.become_postgres() returns void
language plpgsql as $$
begin
  execute 'reset role';
  perform set_config('request.jwt.claims', '', true);
end $$;

-- The shared tables, named once. Every grant assertion below is driven
-- from this list rather than repeated per table, so a table added to the
-- schema's shared set and not to this file shows up as a missing row rather
-- than as silence.
--
-- job_hunter_company_watch_health (#204) joins the set: a company's
-- careers-page endpoint and its health, shared by every user who might
-- watch that company.
create view pg_temp.shared_tables as
select unnest(array[
  'job_hunter_postings',
  'job_hunter_job_facets',
  'job_hunter_companies',
  'job_hunter_posting_merges',
  'job_hunter_ats_boards',
  'job_hunter_company_watch_health'
]) as table_name;

-- Seed one row in each shared table, as the owner. These are what the refusal
-- tests below try to update and delete: proving an update is refused against a
-- table with no rows proves nothing, since a permission check that never runs
-- and a WHERE that matches nothing look identical from outside.
select pg_temp.become_postgres();

insert into public.job_hunter_postings
  (id, fingerprint, company, title, location, description, description_hash,
   first_seen_at, last_seen_at)
values
  ('eeee0001-0000-0000-0000-000000000001', 'pgtap-shared-writes-survivor',
   'Acme', 'Engineer', 'Remote', 'a description', 'hash-1', now(), now()),
  ('eeee0001-0000-0000-0000-000000000002', 'pgtap-shared-writes-duplicate',
   'Acme', 'Engineer', 'Remote', 'another description', 'hash-2', now(), now());

insert into public.job_hunter_job_facets
  (posting_id, description_hash_at_extraction, extracted_at)
values ('eeee0001-0000-0000-0000-000000000001', 'hash-1', now());

insert into public.job_hunter_companies (identity, display_name)
values ('pgtapsharedwrites', 'PgTAP Shared Writes');

insert into public.job_hunter_posting_merges (duplicate_id, survivor_id)
values ('eeee0001-0000-0000-0000-000000000002',
        'eeee0001-0000-0000-0000-000000000001');

insert into public.job_hunter_ats_boards
  (provider, board_identifier, first_seen_at, last_seen_at)
values ('greenhouse', 'pgtap-shared-writes-board', now(), now());

insert into public.job_hunter_company_watch_health
  (company_id, careers_url, first_seen_at)
select id, 'https://pgtap-shared-writes.test/careers', now()
  from public.job_hunter_companies where identity = 'pgtapsharedwrites';


-- 1. The grants ----------------------------------------------------------------

select is(
  (select array_agg(t.table_name order by t.table_name)
     from pg_temp.shared_tables t
    where has_table_privilege('authenticated', 'public.' || t.table_name, 'select')),
  (select array_agg(t.table_name order by t.table_name) from pg_temp.shared_tables t),
  'every shared table is still readable by authenticated');

select is(
  (select array_agg(t.table_name || ':' || v.verb order by t.table_name, v.verb)
     from pg_temp.shared_tables t
     cross join (values ('insert'), ('update'), ('delete')) as v(verb)
     cross join (values ('anon'), ('authenticated'), ('service_role')) as r(role_name)
    where has_table_privilege(r.role_name, 'public.' || t.table_name, v.verb)),
  null,
  'no role a user can hold may insert, update or delete a shared row');


-- 2. The policies --------------------------------------------------------------
--
-- The grants above are one half. Dropping the write policies is the other, so
-- that neither is load-bearing alone: a grant restored by accident still meets
-- a table whose only policy is a select policy.

select is(
  (select array_agg(polname::text order by polname)
     from pg_policy where polrelid = 'public.job_hunter_postings'::regclass),
  array['select_authenticated'],
  'job_hunter_postings has a read policy and nothing else');

select is(
  (select array_agg(polname::text order by polname)
     from pg_policy where polrelid = 'public.job_hunter_job_facets'::regclass),
  array['select_authenticated'],
  'job_hunter_job_facets has a read policy and nothing else');

select is(
  (select array_agg(polname::text order by polname)
     from pg_policy where polrelid = 'public.job_hunter_companies'::regclass),
  array['select_authenticated'],
  'job_hunter_companies has a read policy and nothing else');

select is(
  (select array_agg(polname::text order by polname)
     from pg_policy where polrelid = 'public.job_hunter_ats_boards'::regclass),
  array['select_authenticated'],
  'job_hunter_ats_boards has a read policy and nothing else');

-- Asserted rather than assumed: job_hunter_posting_merges arrived (#176) with
-- no write policy at all, and the claim that it still has none is only worth
-- making if something fails when it stops being true.
select is(
  (select array_agg(polname::text order by polname)
     from pg_policy where polrelid = 'public.job_hunter_posting_merges'::regclass),
  array['select_authenticated'],
  'job_hunter_posting_merges still has no insert, update or delete policy');

select is(
  (select array_agg(polname::text order by polname)
     from pg_policy where polrelid = 'public.job_hunter_company_watch_health'::regclass),
  array['select_authenticated'],
  'job_hunter_company_watch_health has a read policy and nothing else');


-- 3. What actually happens, as authenticated -----------------------------------

select pg_temp.authenticate_as('eeeeeeee-0000-0000-0000-000000000001');

select throws_ok(
  $$ insert into public.job_hunter_postings
       (fingerprint, first_seen_at, last_seen_at)
     values ('pgtap-shared-writes-forged', now(), now()) $$,
  '42501', null,
  'an authenticated user cannot insert a posting');
select throws_ok(
  $$ update public.job_hunter_postings set company = 'Forged'
      where fingerprint = 'pgtap-shared-writes-survivor' $$,
  '42501', null,
  'an authenticated user cannot rewrite a posting others are scored against');
select throws_ok(
  $$ delete from public.job_hunter_postings
      where fingerprint = 'pgtap-shared-writes-survivor' $$,
  '42501', null,
  'an authenticated user cannot delete a posting');

select throws_ok(
  $$ insert into public.job_hunter_job_facets
       (posting_id, description_hash_at_extraction)
     values ('eeee0001-0000-0000-0000-000000000002', 'forged') $$,
  '42501', null,
  'an authenticated user cannot insert facets');
select throws_ok(
  $$ update public.job_hunter_job_facets set compensation_disclosed = true,
            compensation_currency = 'EUR', compensation_max = 1
      where posting_id = 'eeee0001-0000-0000-0000-000000000001' $$,
  '42501', null,
  'an authenticated user cannot fabricate a compensation figure others are blocked on');
select throws_ok(
  $$ delete from public.job_hunter_job_facets
      where posting_id = 'eeee0001-0000-0000-0000-000000000001' $$,
  '42501', null,
  'an authenticated user cannot delete facets');

select throws_ok(
  $$ insert into public.job_hunter_companies (identity, display_name)
     values ('pgtapforged', 'Forged') $$,
  '42501', null,
  'an authenticated user cannot insert a company');
select throws_ok(
  $$ update public.job_hunter_companies set industry = 'finance'
      where identity = 'pgtapsharedwrites' $$,
  '42501', null,
  'an authenticated user cannot rewrite a company');
select throws_ok(
  $$ delete from public.job_hunter_companies where identity = 'pgtapsharedwrites' $$,
  '42501', null,
  'an authenticated user cannot delete a company');

select throws_ok(
  $$ insert into public.job_hunter_posting_merges (duplicate_id, survivor_id)
     values ('eeee0001-0000-0000-0000-000000000001',
             'eeee0001-0000-0000-0000-000000000002') $$,
  '42501', null,
  'an authenticated user cannot record a posting merge by hand');
select throws_ok(
  $$ update public.job_hunter_posting_merges
        set survivor_id = 'eeee0001-0000-0000-0000-000000000002'
      where duplicate_id = 'eeee0001-0000-0000-0000-000000000002' $$,
  '42501', null,
  'an authenticated user cannot re-point a posting redirect');
select throws_ok(
  $$ delete from public.job_hunter_posting_merges
      where duplicate_id = 'eeee0001-0000-0000-0000-000000000002' $$,
  '42501', null,
  'an authenticated user cannot undo a posting merge');

select throws_ok(
  $$ insert into public.job_hunter_ats_boards
       (provider, board_identifier, first_seen_at, last_seen_at)
     values ('lever', 'pgtap-forged-board', now(), now()) $$,
  '42501', null,
  'an authenticated user cannot register an ATS board');
select throws_ok(
  $$ update public.job_hunter_ats_boards
        set active = false, rejected_reason = 'forged'
      where board_identifier = 'pgtap-shared-writes-board' $$,
  '42501', null,
  'an authenticated user cannot reject a board for everybody else');
select throws_ok(
  $$ delete from public.job_hunter_ats_boards
      where board_identifier = 'pgtap-shared-writes-board' $$,
  '42501', null,
  'an authenticated user cannot delete a board');

select throws_ok(
  $$ insert into public.job_hunter_company_watch_health
       (company_id, careers_url, first_seen_at)
     select id, 'https://forged.test', now() from public.job_hunter_companies
      where identity = 'pgtapsharedwrites' $$,
  '42501', null,
  'an authenticated user cannot insert a company watch health row');
select throws_ok(
  $$ update public.job_hunter_company_watch_health
        set consecutive_failures = 99
      where careers_url = 'https://pgtap-shared-writes.test/careers' $$,
  '42501', null,
  'an authenticated user cannot forge another company''s watch health');
select throws_ok(
  $$ delete from public.job_hunter_company_watch_health
      where careers_url = 'https://pgtap-shared-writes.test/careers' $$,
  '42501', null,
  'an authenticated user cannot delete a company watch health row');

-- And reading all six is untouched. This is the half of the ticket that must
-- NOT change: the whole value of a shared row is that everyone can read it.
select is(
  (select count(*)::int from public.job_hunter_postings
    where fingerprint like 'pgtap-shared-writes-%'),
  2, 'an authenticated user still reads every posting');
select is(
  (select count(*)::int from public.job_hunter_job_facets
    where posting_id = 'eeee0001-0000-0000-0000-000000000001'),
  1, 'an authenticated user still reads facets they did not pay for');
select is(
  (select count(*)::int from public.job_hunter_companies
    where identity = 'pgtapsharedwrites'),
  1, 'an authenticated user still reads company facts');
select is(
  (select count(*)::int from public.job_hunter_posting_merges
    where duplicate_id = 'eeee0001-0000-0000-0000-000000000002'),
  1, 'an authenticated user still reads where a merged-away posting went');
select is(
  (select count(*)::int from public.job_hunter_ats_boards
    where board_identifier = 'pgtap-shared-writes-board'),
  1, 'an authenticated user still reads a board another user learned');
select is(
  (select count(*)::int from public.job_hunter_company_watch_health
    where careers_url = 'https://pgtap-shared-writes.test/careers'),
  1, 'an authenticated user still reads a company watch health row another user learned');


-- 4. The same, as anon ---------------------------------------------------------
--
-- anon has no session, so RLS filters its reads to nothing; the writes are
-- refused one layer earlier, at the grant, which is why these are 42501 rather
-- than the silent no-op a policy would produce.

select pg_temp.become_anon();

select throws_ok(
  $$ insert into public.job_hunter_postings
       (fingerprint, first_seen_at, last_seen_at)
     values ('pgtap-anon-posting', now(), now()) $$,
  '42501', null, 'anon cannot insert a posting');
select throws_ok(
  $$ update public.job_hunter_postings set company = 'Forged' $$,
  '42501', null, 'anon cannot update a posting');
select throws_ok(
  $$ delete from public.job_hunter_postings $$,
  '42501', null, 'anon cannot delete a posting');

select throws_ok(
  $$ insert into public.job_hunter_job_facets
       (posting_id, description_hash_at_extraction)
     values ('eeee0001-0000-0000-0000-000000000002', 'forged') $$,
  '42501', null, 'anon cannot insert facets');
select throws_ok(
  $$ update public.job_hunter_job_facets set seniority = 'principal' $$,
  '42501', null, 'anon cannot update facets');
select throws_ok(
  $$ delete from public.job_hunter_job_facets $$,
  '42501', null, 'anon cannot delete facets');

select throws_ok(
  $$ insert into public.job_hunter_companies (identity) values ('pgtapanon') $$,
  '42501', null, 'anon cannot insert a company');
select throws_ok(
  $$ update public.job_hunter_companies set industry = 'finance' $$,
  '42501', null, 'anon cannot update a company');
select throws_ok(
  $$ delete from public.job_hunter_companies $$,
  '42501', null, 'anon cannot delete a company');

select throws_ok(
  $$ insert into public.job_hunter_posting_merges (duplicate_id, survivor_id)
     values ('eeee0001-0000-0000-0000-000000000001',
             'eeee0001-0000-0000-0000-000000000002') $$,
  '42501', null, 'anon cannot record a posting merge');
select throws_ok(
  $$ update public.job_hunter_posting_merges set merged_at = now() $$,
  '42501', null, 'anon cannot update a posting merge');
select throws_ok(
  $$ delete from public.job_hunter_posting_merges $$,
  '42501', null, 'anon cannot delete a posting merge');

select throws_ok(
  $$ insert into public.job_hunter_ats_boards
       (provider, board_identifier, first_seen_at, last_seen_at)
     values ('lever', 'pgtap-anon-board', now(), now()) $$,
  '42501', null, 'anon cannot register an ATS board');
select throws_ok(
  $$ update public.job_hunter_ats_boards set active = false $$,
  '42501', null, 'anon cannot update an ATS board');
select throws_ok(
  $$ delete from public.job_hunter_ats_boards $$,
  '42501', null, 'anon cannot delete an ATS board');

select throws_ok(
  $$ insert into public.job_hunter_company_watch_health
       (company_id, careers_url, first_seen_at)
     select id, 'https://anon-forged.test', now() from public.job_hunter_companies
      where identity = 'pgtapsharedwrites' $$,
  '42501', null, 'anon cannot insert a company watch health row');
select throws_ok(
  $$ update public.job_hunter_company_watch_health set consecutive_failures = 99 $$,
  '42501', null, 'anon cannot update a company watch health row');
select throws_ok(
  $$ delete from public.job_hunter_company_watch_health $$,
  '42501', null, 'anon cannot delete a company watch health row');


-- 5. The other door: SECURITY DEFINER functions --------------------------------
--
-- A definer function does not consult the caller's grants, so every assertion
-- above says nothing about what one of these would write on a user's behalf.
-- Before #179, job_hunter_upsert_job was definer and granted to
-- `authenticated`; a caller could upsert two job rows of their own that
-- resolved to two chosen postings, and the postings collapsed for everybody
-- (#201). The close is that the function is no longer reachable at all.

select pg_temp.authenticate_as('eeeeeeee-0000-0000-0000-000000000001');

select throws_ok(
  $$ select * from public.job_hunter_upsert_job(
       jsonb_build_object('fingerprint', 'pgtap-definer-forged',
                          'company', 'Acme', 'title', 'Engineer'),
       'eeeeeeee-0000-0000-0000-000000000001'::uuid) $$,
  '42501', null,
  'an authenticated user cannot call job_hunter_upsert_job at all');

select throws_ok(
  $$ select * from public.job_hunter_upsert_jobs(
       '[]'::jsonb, 'eeeeeeee-0000-0000-0000-000000000001'::uuid) $$,
  '42501', null,
  'nor its batch wrapper, which would run the same definer as the owner');

select throws_ok(
  $$ select public.job_hunter_merge_jobs(
       'eeee0001-0000-0000-0000-000000000001'::uuid,
       'eeee0001-0000-0000-0000-000000000002'::uuid,
       'eeeeeeee-0000-0000-0000-000000000001'::uuid) $$,
  '42501', null,
  'nor job_hunter_merge_jobs, which is a posting merge wearing a job argument');

select throws_ok(
  $$ select public.job_hunter_merge_postings(
       'eeee0001-0000-0000-0000-000000000001'::uuid,
       'eeee0001-0000-0000-0000-000000000002'::uuid) $$,
  '42501', null,
  'nor job_hunter_merge_postings itself, as it already was');

select throws_ok(
  $$ select public.job_hunter_collapse_job_rows(
       'eeeeeeee-0000-0000-0000-000000000001'::uuid,
       'eeee0001-0000-0000-0000-000000000001'::uuid,
       'eeee0001-0000-0000-0000-000000000002'::uuid) $$,
  '42501', null,
  'nor job_hunter_collapse_job_rows, the other definer in the merge path');

select throws_ok(
  $$ select public.job_hunter_upsert_posting(
       jsonb_build_object('fingerprint', 'pgtap-upsert-posting-forged')) $$,
  '42501', null,
  'nor job_hunter_upsert_posting, which writes a posting directly');

select throws_ok(
  $$ select * from public.job_hunter_merge_posting_batch(gen_random_uuid()) $$,
  '42501', null,
  'nor the staged batch merge, as it already was');

-- The old one-argument signatures are gone rather than revoked. A revoked
-- overload is one a later migration can grant back by accident and one
-- PostgREST still advertises; a dropped one fails at the call site.
select hasnt_function('public', 'job_hunter_upsert_job', array['jsonb'],
                      'the auth.uid()-reading job upsert no longer exists');
select hasnt_function('public', 'job_hunter_upsert_jobs', array['jsonb'],
                      'nor its batch wrapper');
select hasnt_function('public', 'job_hunter_merge_jobs', array['uuid', 'uuid'],
                      'nor the auth.uid()-reading job merge');

-- The inventory, so a definer function added later and granted to
-- `authenticated` is a decision somebody made rather than an accident nobody
-- saw. Three read-only ones, plus five Engine Lab functions (#257) that
-- deliberately do write -- see below for why that is the intended shape
-- there, not an accident:
--
--   * job_hunter_get_provider_credentials reads the caller's own provider
--     credentials.
--   * job_hunter_find_job_by_identity reads which of the caller's rows already
--     cover an advertisement. It is definer only so it can reach
--     job_hunter_find_posting_by_identity, which is revoked from users because
--     its user argument would let one user search another's corpus; this one
--     takes no user argument and reads auth.uid(), which inside a definer is
--     still the caller's.
--   * job_hunter_posting_display_credit (issue #184) reads a source's display
--     obligation for a posting. It is definer only because job_hunter_sources'
--     select policy is open to authenticated but the join runs from a
--     posting, not a session; it takes no user argument either.
--   * job_hunter_engine_lab_caller_is_owner and _is_active_collaborator read
--     membership; job_hunter_engine_lab_bootstrap_owner, _invite and
--     _claim_invite write it. There is deliberately no insert/update policy
--     on job_hunter_engine_lab_collaborators for `authenticated` at all (see
--     job_hunter_engine_lab.sql) -- these five definer functions are the
--     entire write surface, each independently re-checking who is allowed to
--     call it rather than relying on a table-level grant: _invite and
--     _claim_invite check the caller-is-owner / self-email-match;
--     _bootstrap_owner has `execute` granted like the others (it must, to be
--     callable at all) but requires the job_hunter_runner claim, which no
--     ordinary reviewer session ever carries -- see the security fix on
--     PR #282, where it originally checked only the caller's own email and
--     was directly exploitable.
select is(
  (select array_agg(p.proname::text order by p.proname)
     from pg_proc p
     join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public'
      and p.prosecdef
      and p.proname like 'job\_hunter\_%'
      and has_function_privilege('authenticated', p.oid, 'execute')),
  array['job_hunter_engine_lab_bootstrap_owner', 'job_hunter_engine_lab_caller_is_owner',
        'job_hunter_engine_lab_claim_invite', 'job_hunter_engine_lab_invite',
        'job_hunter_engine_lab_is_active_collaborator',
        'job_hunter_find_job_by_identity', 'job_hunter_get_provider_credentials',
        'job_hunter_posting_display_credit'],
  'only the read-only security definers and Engine Lab''s self-checking ones are reachable by authenticated');

-- And the one they must not reach: the posting lookup that names whose corpus
-- to search. A user who could call this could ask which advertisements any
-- other user holds.
select throws_ok(
  $$ select * from public.job_hunter_find_posting_by_identity(
       'Acme', 'Engineer', 'Remote',
       'eeeeeeee-0000-0000-0000-000000000002'::uuid) $$,
  '42501', null,
  'a user cannot ask which advertisements another user already holds');


-- 6. The privileged role still writes everything -------------------------------
--
-- Ingestion connects as the owner. If these fail, the tables are not narrowed,
-- they are sealed, and every refusal above is passing for the wrong reason.

select pg_temp.become_postgres();

select lives_ok(
  $$ update public.job_hunter_job_facets set seniority = 'senior'
      where posting_id = 'eeee0001-0000-0000-0000-000000000001' $$,
  'the privileged role still writes facets');
select lives_ok(
  $$ update public.job_hunter_companies set industry = 'fintech'
      where identity = 'pgtapsharedwrites' $$,
  'the privileged role still writes companies');
select lives_ok(
  $$ update public.job_hunter_ats_boards set last_checked_at = now()
      where board_identifier = 'pgtap-shared-writes-board' $$,
  'the privileged role still writes board health');
select lives_ok(
  $$ update public.job_hunter_company_watch_health set consecutive_failures = 1
      where careers_url = 'https://pgtap-shared-writes.test/careers' $$,
  'the privileged role still writes company watch health');

-- The job-upsert path end to end, over the transport it now lives on: a
-- posting is written, a membership row appears, and the merge the identity
-- ladder decides on is applied -- all as the owner, with the user supplied
-- rather than read from a session that does not exist here.
select lives_ok(
  $$ select * from public.job_hunter_upsert_job(
       jsonb_build_object(
         'fingerprint', 'pgtap-privileged-upsert',
         'source', 'pgtap',
         'company', 'Privileged Co', 'title', 'Engineer', 'location', 'Remote',
         'description', 'text', 'content_confidence', 'full'),
       'eeeeeeee-0000-0000-0000-000000000002'::uuid) $$,
  'the privileged role runs the job-upsert path with an explicit user');

select is(
  (select count(*)::int from public.job_hunter_jobs j
     join public.job_hunter_postings p on p.id = j.posting_id
    where p.fingerprint = 'pgtap-privileged-upsert'
      and j.user_id = 'eeeeeeee-0000-0000-0000-000000000002'),
  1,
  'and it wrote that user''s membership row, not its own');

select * from finish();
rollback;
