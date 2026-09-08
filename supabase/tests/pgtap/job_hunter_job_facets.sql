-- Objective facets on jobs (issue #125).
--
-- The acceptance criterion this file exists for is that hiring-eligible
-- regions, remote policy, seniority and compensation are *filterable in a
-- query* -- a future dashboard or filter must not have to load and parse
-- every row to answer "remote senior roles open to Europe paying at least X".
-- These assertions pin the shape that makes that true: the columns, their
-- value domains, the indexes behind each of the four filters, and that the
-- facets of a job disappear with the job rather than outliving it.
--
-- Cross-user isolation for this table is proved generically in
-- job_hunter_isolation.sql, which now includes it in its table list.

begin;
create extension if not exists pgtap with schema extensions;
select plan(13);

select has_table('public', 'job_hunter_job_facets', 'facets live on their own table');

select columns_are('public', 'job_hunter_job_facets', array[
  'id',
  'user_id',
  'job_id',
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

-- Filterability. Each of the four facets named in the acceptance criteria
-- needs an index a WHERE clause can use; regions need GIN because set
-- membership is not a btree operation.
select has_index('public', 'job_hunter_job_facets',
                 'job_hunter_job_facets_user_remote_policy_idx',
                 'remote policy is filterable');
select has_index('public', 'job_hunter_job_facets',
                 'job_hunter_job_facets_user_seniority_idx',
                 'seniority is filterable');
select has_index('public', 'job_hunter_job_facets',
                 'job_hunter_job_facets_user_compensation_idx',
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

-- One set of facets per job. Re-extraction after a description change
-- replaces the answer; it must not be able to leave two.
select col_is_unique('public', 'job_hunter_job_facets', array['job_id'],
                     'a job has at most one set of facets');

-- The value domains. A free-text seniority or remote policy would make the
-- filters above meaningless -- "Senior" and "senior" would be two answers.
select col_has_check('public', 'job_hunter_job_facets', 'seniority',
                     'seniority is a checked domain, not free text');
select col_has_check('public', 'job_hunter_job_facets', 'remote_policy',
                     'remote policy is a checked domain, not free text');
select col_has_check('public', 'job_hunter_job_facets', 'relocation_policy',
                     'relocation policy is a checked domain, not free text');

-- Facets describe a posting. When the posting row goes, so do they.
select col_is_fk('public', 'job_hunter_job_facets', array['job_id', 'user_id'],
                 'facets hang off the job row they describe');

insert into auth.users (id, email, instance_id, aud, role, raw_app_meta_data, raw_user_meta_data, created_at, updated_at)
values ('cccccccc-0000-0000-0000-000000000003', 'facets@test.local',
        '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated',
        '{}', '{}', now(), now())
on conflict (id) do nothing;

do $$
declare
  v_job uuid;
begin
  insert into public.job_hunter_jobs (user_id, fingerprint, first_seen_at, last_seen_at)
  values ('cccccccc-0000-0000-0000-000000000003', gen_random_uuid()::text, now(), now())
  returning id into v_job;

  insert into public.job_hunter_job_facets
    (user_id, job_id, remote_policy, seniority, hiring_regions, compensation_max, extracted_at)
  values ('cccccccc-0000-0000-0000-000000000003', v_job, 'remote', 'senior',
          array['europe'], 120000, now());

  delete from public.job_hunter_jobs where id = v_job;
end $$;

select is_empty(
  $$ select 1 from public.job_hunter_job_facets
      where user_id = 'cccccccc-0000-0000-0000-000000000003' $$,
  'deleting the job cascades its facets away'
);

select * from finish();
rollback;
