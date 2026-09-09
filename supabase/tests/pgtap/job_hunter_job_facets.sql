-- Objective facets, read once per posting for everyone (issues #125, #175).
--
-- Two acceptance criteria meet in this table.
--
-- #125's is that hiring-eligible regions, remote policy, seniority and
-- compensation are *filterable in a query* -- a future dashboard or filter
-- must not have to load and parse every row to answer "remote senior roles
-- open to Europe paying at least X". The columns, their value domains and
-- the four indexes behind those filters are asserted below.
--
-- #175's is that the answer is shared: one row per posting, readable by
-- every authenticated user, so two users who discover the same
-- advertisement cause one extraction between them. That is a property of
-- the schema, not of the pipeline, so it is proved here -- and it is why
-- this table, like job_hunter_postings, is deliberately excluded from
-- job_hunter_isolation.sql. Cross-user isolation is what it must NOT have.

begin;
create extension if not exists pgtap with schema extensions;
select no_plan();

-- Seed users ----------------------------------------------------------------

insert into auth.users (id, email, instance_id, aud, role, raw_app_meta_data, raw_user_meta_data, created_at, updated_at)
values
  ('cccccccc-0000-0000-0000-000000000003', 'facets-a@test.local', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', '{}', '{}', now(), now()),
  ('cccccccc-0000-0000-0000-000000000004', 'facets-b@test.local', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', '{}', '{}', now(), now())
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

-- Shape ---------------------------------------------------------------------

select has_table('public', 'job_hunter_job_facets', 'facets live on their own table');

select columns_are('public', 'job_hunter_job_facets', array[
  'id',
  'posting_id',
  'description_hash_at_extraction',
  'seniority',
  'remote_policy',
  'relocation_policy',
  'hiring_regions',
  'stack',
  'compensation_disclosed',
  'compensation_currency',
  'compensation_min',
  'compensation_max',
  'compensation_period',
  'requirements_json',
  'source_supplied',
  'model',
  'extracted_at',
  'created_at'
], 'the facet columns are exactly the objective facts CONTEXT.md names');

-- Naming the two absences separately from columns_are keeps the reason
-- readable when someone later wonders where the owner and the job went.
select hasnt_column('public', 'job_hunter_job_facets', 'user_id',
                    'facets have no owner: the posting says the same thing to everyone');
select hasnt_column('public', 'job_hunter_job_facets', 'job_id',
                    'facets hang off the advertisement, not off one user''s copy of it');

select col_is_fk('public', 'job_hunter_job_facets', array['posting_id'],
                 'facets point at the posting they were read from');
select col_is_unique('public', 'job_hunter_job_facets', array['posting_id'],
                     'a posting has at most one set of facets');
select col_not_null('public', 'job_hunter_job_facets', 'posting_id',
                    'a facet row without a posting would be facts about nothing');

-- Filterability. Each of the four facets named in #125's acceptance criteria
-- needs an index a WHERE clause can use; regions need GIN because set
-- membership is not a btree operation. None of them names a user any more:
-- a facet is not filtered inside one user's rows, because it is not one
-- user's row.
select has_index('public', 'job_hunter_job_facets',
                 'job_hunter_job_facets_remote_policy_idx',
                 'remote policy is filterable');
select has_index('public', 'job_hunter_job_facets',
                 'job_hunter_job_facets_seniority_idx',
                 'seniority is filterable');
select has_index('public', 'job_hunter_job_facets',
                 'job_hunter_job_facets_compensation_idx',
                 'compensation is filterable');
select has_index('public', 'job_hunter_job_facets',
                 'job_hunter_job_facets_hiring_regions_idx',
                 'hiring-eligible regions are filterable');
select is(
  (select amname from pg_class c
     join pg_am am on am.oid = c.relam
    where c.relname = 'job_hunter_job_facets_hiring_regions_idx'),
  'gin',
  'the hiring-regions index is GIN, so `hiring_regions && ...` can use it'
);

-- The value domains. A free-text seniority or remote policy would make the
-- filters above meaningless -- "Senior" and "senior" would be two answers.
select col_has_check('public', 'job_hunter_job_facets', 'seniority',
                     'seniority is a checked domain, not free text');
select col_has_check('public', 'job_hunter_job_facets', 'remote_policy',
                     'remote policy is a checked domain, not free text');
select col_has_check('public', 'job_hunter_job_facets', 'relocation_policy',
                     'relocation policy is a checked domain, not free text');

-- Access --------------------------------------------------------------------

select is(
  (select relrowsecurity from pg_class where oid = 'public.job_hunter_job_facets'::regclass),
  true,
  'row level security is on');

select is(
  (select array_agg(polname::text order by polname)
     from pg_policy where polrelid = 'public.job_hunter_job_facets'::regclass),
  array['insert_authenticated', 'select_authenticated', 'update_authenticated'],
  'read and write are open to authenticated users; nobody may delete a shared facet row');

-- Behaviour -----------------------------------------------------------------

insert into public.job_hunter_postings (fingerprint, description, description_hash, first_seen_at, last_seen_at)
values ('facets-shared-fixture', 'React and TypeScript.', 'hash-one', now(), now());

-- User A discovers the advertisement and reads it once.
select pg_temp.authenticate_as('cccccccc-0000-0000-0000-000000000003');

select lives_ok(
  $$ insert into public.job_hunter_job_facets
       (posting_id, description_hash_at_extraction, remote_policy, seniority,
        hiring_regions, compensation_max, extracted_at)
     select p.id, 'hash-one', 'remote', 'senior', array['europe'], 120000, now()
       from public.job_hunter_postings p
      where p.fingerprint = 'facets-shared-fixture' $$,
  'the user who read the posting may store what it says');

-- User B, who never read it, gets the answer anyway. This is the whole
-- issue: the second user's run costs no provider call.
select pg_temp.authenticate_as('cccccccc-0000-0000-0000-000000000004');

select is(
  (select f.seniority from public.job_hunter_job_facets f
     join public.job_hunter_postings p on p.id = f.posting_id
    where p.fingerprint = 'facets-shared-fixture'),
  'senior',
  'a second user reads the facets the first user paid for');

-- ...and may replace them when the advertisement is edited, so a re-read is
-- one call whoever makes it.
select lives_ok(
  $$ update public.job_hunter_job_facets f
        set seniority = 'staff', description_hash_at_extraction = 'hash-two'
       from public.job_hunter_postings p
      where p.id = f.posting_id and p.fingerprint = 'facets-shared-fixture' $$,
  'a second user may replace the facets after the posting changes');

select is(
  (select count(*)::int from public.job_hunter_job_facets f
     join public.job_hunter_postings p on p.id = f.posting_id
    where p.fingerprint = 'facets-shared-fixture'),
  1,
  're-extraction replaces the answer rather than appending a second one');

-- Nobody may take a shared row away from the others.
select lives_ok(
  $$ delete from public.job_hunter_job_facets f
      using public.job_hunter_postings p
      where p.id = f.posting_id and p.fingerprint = 'facets-shared-fixture' $$,
  'a delete is refused silently by row-level security rather than erroring');

select is(
  (select count(*)::int from public.job_hunter_job_facets f
     join public.job_hunter_postings p on p.id = f.posting_id
    where p.fingerprint = 'facets-shared-fixture'),
  1,
  'and the row is still there: no user may delete another user''s reading');

-- Facets describe a posting. When the posting goes, so do they.
select pg_temp.become_postgres();

delete from public.job_hunter_postings where fingerprint = 'facets-shared-fixture';

select is_empty(
  $$ select 1 from public.job_hunter_job_facets
      where description_hash_at_extraction in ('hash-one', 'hash-two') $$,
  'deleting the posting cascades its facets away');

select * from finish();
rollback;
