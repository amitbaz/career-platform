-- Persisting a crawl batch with one set-based merge (issue #182).
--
-- The merge replaces a per-listing loop, so what has to be proved is that it
-- reaches the same postings the loop reached -- not merely that it is fast.
-- Every behavioural assertion below therefore either states one of the
-- resolution rules directly, or runs the same listings both ways and compares
-- the rows.
--
-- Cross-user isolation is not asserted here, for the same reason
-- job_hunter_postings.sql does not assert it: a posting is shared. What IS
-- asserted is that no user can reach the staging table or the merge at all --
-- staging is ingestion's scratch space, held over a privileged connection,
-- and nothing about it belongs to anybody.

begin;
create extension if not exists pgtap with schema extensions;
select no_plan();

insert into auth.users (id, email, instance_id, aud, role, raw_app_meta_data, raw_user_meta_data, created_at, updated_at)
values
  ('eeeeeeee-0000-0000-0000-00000000000a', 'batch-a@test.local', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', '{}', '{}', now(), now())
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

-- Stage one listing. Defaults keep the call sites below to the columns each
-- assertion is actually about.
create function pg_temp.stage(
  p_batch uuid,
  p_ordinal int,
  p_fingerprint text,
  p_source text default 'test',
  p_source_job_id text default null,
  p_url text default '',
  p_canonical_url text default '',
  p_company text default '',
  p_title text default '',
  p_location text default '',
  p_remote boolean default null,
  p_description text default '',
  p_content_confidence text default '',
  p_ats_provider text default null,
  p_ats_board text default null,
  p_ats_job_id text default null)
returns void language sql as $$
  insert into public.job_hunter_posting_staging
    (batch_id, ordinal, fingerprint, source, source_job_id, url, canonical_url,
     company, title, location, remote, description, content_confidence,
     ats_provider, ats_board, ats_job_id)
  values (p_batch, p_ordinal, p_fingerprint, p_source, p_source_job_id, p_url,
          p_canonical_url, p_company, p_title, p_location, p_remote,
          p_description, p_content_confidence, p_ats_provider, p_ats_board,
          p_ats_job_id);
$$;

-- Shape -----------------------------------------------------------------------

select has_table('public', 'job_hunter_posting_staging',
                 'a crawl batch has somewhere to be bulk-loaded to');

select columns_are('public', 'job_hunter_posting_staging', array[
  'batch_id',
  'ordinal',
  'fingerprint',
  'source',
  'source_job_id',
  'url',
  'canonical_url',
  'company',
  'title',
  'location',
  'remote',
  'description',
  'content_confidence',
  'ats_provider',
  'ats_board',
  'ats_job_id'
], 'staging carries the posting-shaped subset of a job payload and nothing else');

select col_is_pk('public', 'job_hunter_posting_staging', array['batch_id', 'ordinal'],
                 'a listing is identified by its batch and its position in it');

select is(
  (select relrowsecurity from pg_class
    where oid = 'public.job_hunter_posting_staging'::regclass),
  true,
  'staging has row level security on');

select is_empty(
  $$ select polname from pg_policy
      where polrelid = 'public.job_hunter_posting_staging'::regclass $$,
  'and no policy, so row level security denies every non-owner');

select is_empty(
  $$ select 1 where has_table_privilege('authenticated',
       'public.job_hunter_posting_staging', 'select, insert, update, delete') $$,
  'authenticated holds no privilege on staging either');

select is_empty(
  $$ select 1 where has_table_privilege('anon',
       'public.job_hunter_posting_staging', 'select, insert, update, delete') $$,
  'nor does anon');

select is_empty(
  $$ select 1 where has_table_privilege('service_role',
       'public.job_hunter_posting_staging', 'select, insert, update, delete') $$,
  'nor service_role, which nothing in this application uses');

select is_empty(
  $$ select 1 where has_function_privilege('authenticated',
       'public.job_hunter_merge_posting_batch(uuid)', 'execute') $$,
  'and the merge is not a function a user may call');

-- A batch of new postings -----------------------------------------------------

select pg_temp.become_postgres();

select pg_temp.stage('11111111-0000-0000-0000-000000000001', 0, 'jhpb-new-1',
                     p_url => 'https://example.test/jhpb/1',
                     p_company => 'Acme', p_title => 'Frontend Engineer',
                     p_location => 'Remote', p_remote => true,
                     p_description => 'first', p_content_confidence => 'source_detail_page');
select pg_temp.stage('11111111-0000-0000-0000-000000000001', 1, 'jhpb-new-2',
                     p_url => 'https://example.test/jhpb/2',
                     p_company => 'Acme', p_title => 'Backend Engineer',
                     p_description => 'second');

create temp table batch_one_result as
select * from public.job_hunter_merge_posting_batch('11111111-0000-0000-0000-000000000001'::uuid);

select is(
  (select count(*)::int from batch_one_result),
  2,
  'the merge reports one row per distinct fingerprint in the batch');

select is(
  (select (count(*) filter (where is_new))::int from batch_one_result),
  2,
  'and reports both of them as genuinely new');

select is(
  (select p.company || '/' || p.title || '/' || p.location || '/' || p.description
     from public.job_hunter_postings p where p.fingerprint = 'jhpb-new-1'),
  'Acme/Frontend Engineer/Remote/first',
  'the posting carries what the listing said');

select is(
  (select p.canonical_url from public.job_hunter_postings p
    where p.fingerprint = 'jhpb-new-1'),
  public.job_hunter_canonicalize_url('https://example.test/jhpb/1'),
  'and a listing with no canonical URL gets one derived from its URL');

select is(
  (select p.description_hash from public.job_hunter_postings p
    where p.fingerprint = 'jhpb-new-1'),
  encode(sha256(convert_to('first', 'UTF8')), 'hex'),
  'the description hash is of the description that was kept');

select is(
  (select p.first_seen_at = p.last_seen_at from public.job_hunter_postings p
    where p.fingerprint = 'jhpb-new-1'),
  true,
  'a posting a batch discovered was first and last seen at the same moment');

-- Staging is emptied by the merge ---------------------------------------------

select is_empty(
  $$ select 1 from public.job_hunter_posting_staging
      where batch_id = '11111111-0000-0000-0000-000000000001' $$,
  'the batch is cleared from staging once it is merged');

select is_empty(
  $$ select 1 from public.job_hunter_merge_posting_batch(
       '11111111-0000-0000-0000-000000000001'::uuid) $$,
  're-merging a cleared batch returns nothing');

select is(
  (select p.description from public.job_hunter_postings p
    where p.fingerprint = 'jhpb-new-1'),
  'first',
  'and changes nothing, so a retry after a lost acknowledgement is harmless');

-- Duplicates inside one batch --------------------------------------------------
--
-- Three listings of one advertisement, arriving in the order a crawl would
-- produce them: a weak aggregator copy first, then a better one, then a bare
-- sighting that knows only the location.

select pg_temp.stage('11111111-0000-0000-0000-000000000002', 0, 'jhpb-dup',
                     p_url => 'https://example.test/jhpb/dup',
                     p_company => 'Globex', p_title => 'Staff Engineer',
                     p_description => 'a long aggregator paraphrase of the posting',
                     p_content_confidence => 'aggregator_text');
select pg_temp.stage('11111111-0000-0000-0000-000000000002', 1, 'jhpb-dup',
                     p_company => 'Globex Incorporated', p_title => 'Staff Engineer',
                     p_description => 'short but official',
                     p_content_confidence => 'official_ats',
                     p_ats_provider => 'greenhouse', p_ats_board => 'globex',
                     p_ats_job_id => '42');
select pg_temp.stage('11111111-0000-0000-0000-000000000002', 2, 'jhpb-dup',
                     p_location => 'Berlin', p_remote => false);

create temp table batch_two_result as
select * from public.job_hunter_merge_posting_batch('11111111-0000-0000-0000-000000000002'::uuid);

select is(
  (select count(*)::int from batch_two_result),
  1,
  'three listings of one advertisement collapse to one posting');

select is(
  (select count(*)::int from public.job_hunter_postings where fingerprint = 'jhpb-dup'),
  1,
  'and one row is what the table holds');

select is(
  (select p.description || '/' || p.content_confidence from public.job_hunter_postings p
    where p.fingerprint = 'jhpb-dup'),
  'short but official/official_ats',
  'the better tier wins the description however much longer the weaker text is');

select is(
  (select p.company from public.job_hunter_postings p where p.fingerprint = 'jhpb-dup'),
  'Globex',
  'an identity column keeps the first non-empty value, never the latest');

select is(
  (select p.location from public.job_hunter_postings p where p.fingerprint = 'jhpb-dup'),
  'Berlin',
  'and an empty one is still backfilled by a later listing');

select is(
  (select p.ats_provider || '/' || p.ats_board || '/' || p.ats_job_id
     from public.job_hunter_postings p where p.fingerprint = 'jhpb-dup'),
  'greenhouse/globex/42',
  'ATS identity is backfilled from whichever listing carried it');

select is(
  (select p.remote from public.job_hunter_postings p where p.fingerprint = 'jhpb-dup'),
  false,
  'and remote takes the first listing that stated anything at all');

-- One ATS job, two source labels (#249) -----------------------------------------
--
-- job_hunter/normalize.py's job_fingerprint now keys on the ATS triple when
-- it is present, so a board crawled directly (`ashby`) and the same job
-- rediscovered through a company watch (`watch:ashby`) stage under the same
-- fingerprint here -- that is the application-level half of the fix, and
-- this is its persistence-level consequence: the fold below is the same
-- fold `jhpb-dup` above already exercises, run on listings whose only
-- difference is the source label that discovered them.

select pg_temp.stage('11111111-0000-0000-0000-00000000000a', 0, 'jhpb-two-labels',
                     p_source => 'ashby', p_source_job_id => 'ashby-job-1',
                     p_url => 'https://jobs.ashbyhq.com/bjak/ashby-job-1',
                     p_company => 'Bjak', p_title => 'Backend Engineer',
                     p_description => 'seen directly on the board',
                     p_content_confidence => 'official_ats',
                     p_ats_provider => 'ashby', p_ats_board => 'bjak', p_ats_job_id => 'ashby-job-1');
select pg_temp.stage('11111111-0000-0000-0000-00000000000a', 1, 'jhpb-two-labels',
                     p_source => 'watch:ashby', p_source_job_id => 'ashby-job-1',
                     p_url => 'https://jobs.ashbyhq.com/bjak/ashby-job-1',
                     p_company => 'Bjak', p_title => 'Backend Engineer',
                     p_description => 'rediscovered through a company watch',
                     p_content_confidence => 'official_ats',
                     p_ats_provider => 'ashby', p_ats_board => 'bjak', p_ats_job_id => 'ashby-job-1');

create temp table two_labels_result as
select * from public.job_hunter_merge_posting_batch('11111111-0000-0000-0000-00000000000a'::uuid);

select is(
  (select count(*)::int from two_labels_result),
  1,
  'the same ATS job arriving under two source labels reports one fingerprint');

select is(
  (select count(*)::int from public.job_hunter_postings where fingerprint = 'jhpb-two-labels'),
  1,
  'and one posting is what the table holds -- whichever source label discovered it');

select is(
  (select p.source from public.job_hunter_postings p where p.fingerprint = 'jhpb-two-labels'),
  'ashby',
  'the posting keeps the first source label that reached it');

-- A batch resolved against a posting that already exists -----------------------

select pg_temp.stage('11111111-0000-0000-0000-000000000003', 0, 'jhpb-dup',
                     p_canonical_url => 'https://boards.greenhouse.io/globex/jobs/42',
                     p_description => 'short but official, and now longer',
                     p_content_confidence => 'official_ats');

create temp table batch_three_result as
select * from public.job_hunter_merge_posting_batch('11111111-0000-0000-0000-000000000003'::uuid);

select is(
  (select (count(*) filter (where is_new))::int from batch_three_result),
  0,
  'a fingerprint already on the table is not reported as newly discovered');

select is(
  (select r.posting_id from batch_three_result r),
  (select p.id from public.job_hunter_postings p where p.fingerprint = 'jhpb-dup'),
  'and the batch resolves to the posting that was already there');

select is(
  (select p.description from public.job_hunter_postings p where p.fingerprint = 'jhpb-dup'),
  'short but official, and now longer',
  'at equal tiers the longer text wins');

select is(
  (select p.url from public.job_hunter_postings p where p.fingerprint = 'jhpb-dup'),
  'https://boards.greenhouse.io/globex/jobs/42',
  'a resolved canonical URL replaces whatever URL the posting was first seen under');

select ok(
  (select p.last_seen_at > p.first_seen_at from public.job_hunter_postings p
    where p.fingerprint = 'jhpb-dup'),
  'last_seen_at moves and first_seen_at does not');

-- The same listings, both ways --------------------------------------------------
--
-- The acceptance criterion is that identity resolution produces the same
-- postings the per-listing loop produced for the same input, so this runs one
-- set of listings through job_hunter_upsert_posting in order and the same set
-- through the batch merge, and compares every column that describes the
-- advertisement.

create function pg_temp.listings(p_fingerprint text)
returns table (
  ordinal int, fingerprint text, source text, source_job_id text, url text,
  canonical_url text, company text, title text, location text, remote boolean,
  description text, content_confidence text, ats_provider text, ats_board text,
  ats_job_id text)
language sql as $$
  select * from (values
    (0, p_fingerprint, 'aggregator', null::text, 'https://example.test/jhpb/cmp?utm_source=x', '',
     '', 'Senior Engineer', 'Remote', null::boolean,
     'aggregated text that is quite long indeed', 'aggregator_text', null::text, null::text, null::text),
    (1, p_fingerprint, 'greenhouse', 'gh-77', 'https://example.test/jhpb/cmp', 'https://boards.greenhouse.io/initech/jobs/77',
     'Initech', 'Senior Engineer', '', true,
     'official', 'official_ats', 'greenhouse', 'initech', '77'),
    (2, p_fingerprint, '', null, '', '',
     'Initech Holdings', '', 'Warsaw', false,
     '', '', null, null, null)
  ) as t(ordinal, fingerprint, source, source_job_id, url, canonical_url, company,
         title, location, remote, description, content_confidence, ats_provider,
         ats_board, ats_job_id);
$$;

-- The loop.
do $$
declare
  v_listing record;
begin
  for v_listing in select * from pg_temp.listings('jhpb-loop') order by ordinal loop
    perform public.job_hunter_upsert_posting(to_jsonb(v_listing) - 'ordinal');
  end loop;
end $$;

-- The merge.
insert into public.job_hunter_posting_staging
  (batch_id, ordinal, fingerprint, source, source_job_id, url, canonical_url,
   company, title, location, remote, description, content_confidence,
   ats_provider, ats_board, ats_job_id)
select '11111111-0000-0000-0000-000000000004'::uuid, l.*
  from pg_temp.listings('jhpb-batch') l;

select is(
  (select (count(*) filter (where is_new))::int
     from public.job_hunter_merge_posting_batch('11111111-0000-0000-0000-000000000004'::uuid)),
  1,
  'the merge discovers the same one advertisement the loop discovered');

select is(
  (select (p.source, p.source_job_id, p.url, p.canonical_url, p.company, p.title,
           p.location, p.remote, p.description, p.description_hash,
           p.content_confidence, p.ats_provider, p.ats_board, p.ats_job_id)::text
     from public.job_hunter_postings p where p.fingerprint = 'jhpb-batch'),
  (select (p.source, p.source_job_id, p.url, p.canonical_url, p.company, p.title,
           p.location, p.remote, p.description, p.description_hash,
           p.content_confidence, p.ats_provider, p.ats_board, p.ats_job_id)::text
     from public.job_hunter_postings p where p.fingerprint = 'jhpb-loop'),
  'and reaches a posting identical to the one the per-listing loop reached');

-- A job payload that already names its posting ---------------------------------
--
-- Since #179 the job upsert is a shared-table write, so it runs as the
-- privileged ingestion role with the user it acts for supplied. The scenario
-- is the same one: a staged batch has already resolved this listing's posting,
-- and the upsert must keep it rather than resolving a second.

-- Written in two statements deliberately: a job row the function inserts is
-- invisible to a join in the same statement, so reading it back has to be a
-- statement of its own.
create temp table preresolved as
select * from public.job_hunter_upsert_job(jsonb_build_object(
  'fingerprint', 'jhpb-job-preresolved',
  'source', 'test',
  'title', 'Data Engineer',
  'company', 'Initech',
  'url', 'https://example.test/jhpb/job',
  'posting_id', (select p.id from public.job_hunter_postings p
                  where p.fingerprint = 'jhpb-dup')::text),
  'eeeeeeee-0000-0000-0000-00000000000a'::uuid);

select is(
  (select j.posting_id from public.job_hunter_jobs j
    where j.id = (select id from preresolved)),
  (select p.id from public.job_hunter_postings p where p.fingerprint = 'jhpb-dup'),
  'a job payload carrying a posting_id keeps it rather than resolving another');

select is_empty(
  $$ select 1 from public.job_hunter_postings where fingerprint = 'jhpb-job-preresolved' $$,
  'and no second posting is created for the fingerprint it did not need to resolve');

create temp table unresolved as
select * from public.job_hunter_upsert_job(jsonb_build_object(
  'fingerprint', 'jhpb-job-unresolved',
  'source', 'test',
  'title', 'Data Scientist',
  'company', 'Initech',
  'url', 'https://example.test/jhpb/job2'),
  'eeeeeeee-0000-0000-0000-00000000000a'::uuid);

select isnt(
  (select j.posting_id from public.job_hunter_jobs j
    where j.id = (select id from unresolved)),
  null,
  'a payload without one still resolves its own posting, exactly as before');

select is(
  (select count(*)::int from public.job_hunter_postings
    where fingerprint = 'jhpb-job-unresolved'),
  1,
  'which is the posting that fingerprint names');

select pg_temp.become_postgres();

select * from finish();
rollback;
