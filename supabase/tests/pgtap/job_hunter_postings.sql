-- One row per posting, shared by every user who discovers it (issue #174).
--
-- A posting is one job advertisement in the world. Until now the only
-- record of one was a private copy in job_hunter_jobs per user who found
-- it, which is why objective extraction is paid once per user instead of
-- once per posting. This file pins the properties that make the shared row
-- usable as that single record:
--
--   * identity is the fingerprint, which is computed from the
--     advertisement (source + source job id, else canonical URL, else
--     company/title/location) and never from the discovering user, so two
--     users resolve to the same row;
--   * every job row written through job_hunter_upsert_job points at its
--     posting, in the same call;
--   * the better description wins, decided by content confidence exactly
--     as the job-level merge decides it -- a lower-confidence fetch by a
--     second user must never overwrite a better one;
--   * any authenticated user can read any posting, because a posting is
--     not private to whoever discovered it.
--
-- Cross-user isolation is deliberately NOT asserted here, and this table
-- is excluded from job_hunter_isolation.sql for the same reason: shared
-- readability is the property it has.
begin;
create extension if not exists pgtap with schema extensions;
select no_plan();

-- The migration backfilled every job row that existed before it, matching
-- them by fingerprint. #178 then closed the hole the backfill could not:
-- posting_id is `not null`, so a row without an advertisement is no longer
-- writable at all rather than merely absent in practice.
select col_not_null('public', 'job_hunter_jobs', 'posting_id',
  'a job row cannot exist without the posting it is a membership of');

-- Seed users ----------------------------------------------------------------

insert into auth.users (id, email, instance_id, aud, role, raw_app_meta_data, raw_user_meta_data, created_at, updated_at)
values
  ('dddddddd-0000-0000-0000-00000000000a', 'posting-a@test.local', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', '{}', '{}', now(), now()),
  ('dddddddd-0000-0000-0000-00000000000b', 'posting-b@test.local', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', '{}', '{}', now(), now())
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

-- Since #179 a posting is written only by the privileged ingestion role, and
-- the job upsert that writes one takes the user it acts for as an argument
-- rather than reading auth.uid(). These helpers are that transport in
-- miniature: drop to the owner, make the call for the named user, hand the
-- session back to them. Every scenario below is still "this user's crawl
-- discovers this advertisement"; only the role making the write moved.
create function pg_temp.upsert_job_as(p_user uuid, p_job jsonb)
returns table (id uuid, is_new boolean, description_changed boolean)
language plpgsql as $$
declare
  v_row record;
begin
  perform pg_temp.become_postgres();
  select * into v_row from public.job_hunter_upsert_job(p_job, p_user);
  perform pg_temp.authenticate_as(p_user);
  id := v_row.id;
  is_new := v_row.is_new;
  description_changed := v_row.description_changed;
  return next;
end $$;

create function pg_temp.merge_jobs_as(p_user uuid, p_survivor uuid, p_duplicate uuid)
returns uuid
language plpgsql as $$
declare
  v_id uuid;
begin
  perform pg_temp.become_postgres();
  v_id := public.job_hunter_merge_jobs(p_survivor, p_duplicate, p_user);
  perform pg_temp.authenticate_as(p_user);
  return v_id;
end $$;

-- Shape ---------------------------------------------------------------------

select has_table('public', 'job_hunter_postings',
                 'a posting has a row of its own');

select columns_are('public', 'job_hunter_postings', array[
  'id',
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
  'description_hash',
  'content_confidence',
  'ats_provider',
  'ats_board',
  'ats_job_id',
  'first_seen_at',
  'last_seen_at',
  'created_at',
  -- The four stored generated columns identity resolution compares against,
  -- moved here from job_hunter_jobs with the columns they are computed from
  -- (#178). See job_hunter_lookup_indexes.sql for why they are columns
  -- rather than expressions.
  'normalized_identity',
  'canonical_url_of_url',
  'normalized_company',
  'normalized_title'
], 'the posting carries what the advertisement says and how it was fetched');

-- No user_id. Naming its absence separately from columns_are keeps the
-- reason readable when someone later wonders where the owner went.
select hasnt_column('public', 'job_hunter_postings', 'user_id',
                    'a posting has no owner: it is the same advertisement for everyone');

select col_is_unique('public', 'job_hunter_postings', array['fingerprint'],
                     'the fingerprint identifies the posting, so it can only be there once');

select has_column('public', 'job_hunter_jobs', 'posting_id',
                  'a job points at the posting it is a private copy of');
select col_is_fk('public', 'job_hunter_jobs', array['posting_id'],
                 'that pointer is a real foreign key');

select has_index('public', 'job_hunter_jobs', 'job_hunter_jobs_posting_idx',
                 'jobs are reachable from their posting without a sequential scan');

-- Access --------------------------------------------------------------------

select is(
  (select relrowsecurity from pg_class where oid = 'public.job_hunter_postings'::regclass),
  true,
  'row level security is on');

-- Reads open, writes closed (#179). The hazard the original table comment
-- described is what this closes: identity columns are only backfilled when
-- empty, so a row inserted first with a made-up company or title kept them
-- against every later real discovery, and nobody could delete it.
select is(
  (select array_agg(polname::text order by polname)
     from pg_policy where polrelid = 'public.job_hunter_postings'::regclass),
  array['select_authenticated'],
  'reads are open to authenticated users; nobody but the privileged role writes a posting');

-- Behaviour -----------------------------------------------------------------

-- User A discovers the posting on an aggregator: a short description from a
-- source that is not authoritative.
select pg_temp.authenticate_as('dddddddd-0000-0000-0000-00000000000a');

select lives_ok(
  $$ select pg_temp.upsert_job_as('dddddddd-0000-0000-0000-00000000000a'::uuid, jsonb_build_object(
       'fingerprint', 'fp-shared-posting',
       'source', 'remoteok',
       'source_job_id', 'ro-1',
       'url', 'https://remoteok.example/jobs/1',
       'canonical_url', 'https://acme.example/careers/1',
       'company', 'Acme GmbH',
       'title', 'Staff Engineer',
       'location', 'Vienna',
       'remote', true,
       'description', 'short aggregator blurb',
       'content_confidence', 'aggregator_text')) $$,
  'A discovers the posting');

select is(
  (select count(*)::int from public.job_hunter_postings p
    where p.fingerprint = 'fp-shared-posting'),
  1,
  'discovering a posting creates exactly one posting row');

select isnt(
  (select j.posting_id from public.job_hunter_jobs j
     join public.job_hunter_postings p on p.id = j.posting_id
    where j.user_id = 'dddddddd-0000-0000-0000-00000000000a'
      and p.fingerprint = 'fp-shared-posting'),
  null,
  'the upsert wrote the job row and the posting it points at in one call');

select is(
  (select p.description from public.job_hunter_postings p
    where p.fingerprint = 'fp-shared-posting'),
  'short aggregator blurb',
  'the posting carries the only description anyone has fetched so far');

-- User B discovers the same advertisement on the employer's own ATS: the
-- same fingerprint, a better description.
select pg_temp.authenticate_as('dddddddd-0000-0000-0000-00000000000b');

select lives_ok(
  $$ select pg_temp.upsert_job_as('dddddddd-0000-0000-0000-00000000000b'::uuid, jsonb_build_object(
       'fingerprint', 'fp-shared-posting',
       'source', 'greenhouse',
       'source_job_id', 'ro-1',
       'url', 'https://boards.greenhouse.io/acme/jobs/1',
       'canonical_url', 'https://acme.example/careers/1',
       'company', 'Acme GmbH',
       'title', 'Staff Engineer',
       'location', 'Vienna',
       'remote', true,
       'description', 'the full posting text straight from the employer ATS',
       'content_confidence', 'official_ats',
       'ats_provider', 'greenhouse',
       'ats_board', 'acme',
       'ats_job_id', '1')) $$,
  'B discovers the same advertisement');

-- Counted as postgres: RLS would otherwise hide the other user's job row
-- and every count below would come back 1 whether the two users share a
-- posting or not.
select pg_temp.become_postgres();

select is(
  (select count(*)::int from public.job_hunter_postings p
    where p.fingerprint = 'fp-shared-posting'),
  1,
  'a posting discovered by two users is still one posting row');

select is(
  (select count(*)::int from public.job_hunter_jobs j
     join public.job_hunter_postings p on p.id = j.posting_id
    where p.fingerprint = 'fp-shared-posting'),
  2,
  'each user holds one membership row of it, and that is all a second user costs');

select is(
  (select count(distinct j.posting_id)::int from public.job_hunter_jobs j
     join public.job_hunter_postings p on p.id = j.posting_id
    where p.fingerprint = 'fp-shared-posting'),
  1,
  'both users'' job rows point at that one row');

select is(
  (select p.description from public.job_hunter_postings p
    where p.fingerprint = 'fp-shared-posting'),
  'the full posting text straight from the employer ATS',
  'a higher-confidence description replaces a lower-confidence one');

select is(
  (select p.content_confidence from public.job_hunter_postings p
    where p.fingerprint = 'fp-shared-posting'),
  'official_ats',
  'the tier follows the text it belongs to');

select is(
  (select p.description_hash from public.job_hunter_postings p
    where p.fingerprint = 'fp-shared-posting'),
  encode(sha256(convert_to('the full posting text straight from the employer ATS', 'UTF8')), 'hex'),
  'so does the description hash');

-- A re-fetch of the aggregator copy must not undo that.
select pg_temp.authenticate_as('dddddddd-0000-0000-0000-00000000000a');

select lives_ok(
  $$ select pg_temp.upsert_job_as('dddddddd-0000-0000-0000-00000000000a'::uuid, jsonb_build_object(
       'fingerprint', 'fp-shared-posting',
       'source', 'remoteok',
       'source_job_id', 'ro-1',
       'url', 'https://remoteok.example/jobs/1',
       'canonical_url', 'https://acme.example/careers/1',
       'company', 'Acme GmbH',
       'title', 'Staff Engineer',
       'location', 'Vienna',
       'remote', true,
       'description', 'a much longer aggregator blurb, padded out so that length alone would win it the argument if confidence were not consulted first',
       'content_confidence', 'aggregator_text')) $$,
  'A re-fetches its lower-confidence copy');

select is(
  (select p.description from public.job_hunter_postings p
    where p.fingerprint = 'fp-shared-posting'),
  'the full posting text straight from the employer ATS',
  'a lower-confidence fetch does not overwrite a better one, however much longer it is');

-- An equally-confident but fuller fetch does win, matching the job-level
-- merge rule: tier first, length only as the tiebreak.
select lives_ok(
  $$ select pg_temp.upsert_job_as('dddddddd-0000-0000-0000-00000000000a'::uuid, jsonb_build_object(
       'fingerprint', 'fp-shared-posting',
       'source', 'greenhouse',
       'source_job_id', 'ro-1',
       'url', 'https://boards.greenhouse.io/acme/jobs/1',
       'canonical_url', 'https://acme.example/careers/1',
       'company', 'Acme GmbH',
       'title', 'Staff Engineer',
       'location', 'Vienna',
       'remote', true,
       'description', 'the full posting text straight from the employer ATS, now including the benefits section',
       'content_confidence', 'official_ats')) $$,
  'A fetches a fuller copy at the same confidence');

select is(
  (select p.description from public.job_hunter_postings p
    where p.fingerprint = 'fp-shared-posting'),
  'the full posting text straight from the employer ATS, now including the benefits section',
  'at equal confidence the fuller text wins');

-- Seeing a posting again does not move its first_seen_at, and does move
-- its last_seen_at: they bracket when *anyone* saw the advertisement.
select cmp_ok(
  (select p.last_seen_at from public.job_hunter_postings p
    where p.fingerprint = 'fp-shared-posting'),
  '>',
  (select p.first_seen_at from public.job_hunter_postings p
    where p.fingerprint = 'fp-shared-posting'),
  'first and last seen bracket every discovery of the posting, by anyone');

-- The narrow upsert writes the posting too. It is a different branch of
-- job_hunter_upsert_job -- identity is the fingerprint alone, nothing is
-- merged and no discovery source is recorded -- so it needs its own check
-- rather than inheriting the logical mode's.
select pg_temp.authenticate_as('dddddddd-0000-0000-0000-00000000000b');

select lives_ok(
  $$ select pg_temp.upsert_job_as('dddddddd-0000-0000-0000-00000000000b'::uuid, jsonb_build_object(
       'match_mode', 'fingerprint',
       'fingerprint', 'fp-narrow',
       'source', 'ashby',
       'source_job_id', 'as-3',
       'url', 'https://jobs.ashbyhq.com/narrow/3',
       'company', 'Narrow Co',
       'title', 'Platform Engineer',
       'location', 'Remote',
       'description', 'narrow upsert description',
       'content_confidence', 'official_ats')) $$,
  'the narrow, fingerprint-matched upsert runs');

select is(
  (select p.title from public.job_hunter_postings p where p.fingerprint = 'fp-narrow'),
  'Platform Engineer',
  'it writes the posting as well');

select is(
  (select p.id from public.job_hunter_postings p where p.fingerprint = 'fp-narrow'),
  (select j.posting_id from public.job_hunter_jobs j
     join public.job_hunter_postings p on p.id = j.posting_id
    where j.user_id = 'dddddddd-0000-0000-0000-00000000000b'
      and p.fingerprint = 'fp-narrow'),
  'and points the job row it wrote at it');

-- A merge keeps the pointer with the text. The fingerprint is
-- source-scoped, so one user can hold two job rows that are copies of two
-- different postings -- the same advertisement seen on an aggregator and on
-- the employer's ATS. Merging them deletes one row and its pointer, and the
-- survivor must be left pointing at the posting whose description it kept.
select pg_temp.authenticate_as('dddddddd-0000-0000-0000-00000000000a');

select lives_ok(
  $$ select pg_temp.upsert_job_as('dddddddd-0000-0000-0000-00000000000a'::uuid, jsonb_build_object(
       'match_mode', 'fingerprint',
       'fingerprint', 'fp-merge-weak',
       'source', 'remoteok',
       'source_job_id', 'ro-77',
       'url', 'https://remoteok.example/jobs/77',
       'company', 'Merge Co',
       'title', 'Merge Engineer',
       'location', 'Vienna',
       'description', 'thin aggregator copy',
       'content_confidence', 'aggregator_text')) $$,
  'A holds the aggregator copy');

select lives_ok(
  $$ select pg_temp.upsert_job_as('dddddddd-0000-0000-0000-00000000000a'::uuid, jsonb_build_object(
       'match_mode', 'fingerprint',
       'fingerprint', 'fp-merge-strong',
       'source', 'greenhouse',
       'source_job_id', 'gh-77',
       'url', 'https://boards.greenhouse.io/mergeco/jobs/77',
       'company', 'Merge Co',
       'title', 'Merge Engineer',
       'location', 'Vienna',
       'description', 'the employer''s own copy',
       'content_confidence', 'official_ats')) $$,
  'and the ATS copy, as a separate job row with its own posting');

select isnt(
  (select j.posting_id from public.job_hunter_jobs j
     join public.job_hunter_postings p on p.id = j.posting_id
    where j.user_id = 'dddddddd-0000-0000-0000-00000000000a' and p.fingerprint = 'fp-merge-weak'),
  (select j.posting_id from public.job_hunter_jobs j
     join public.job_hunter_postings p on p.id = j.posting_id
    where j.user_id = 'dddddddd-0000-0000-0000-00000000000a' and p.fingerprint = 'fp-merge-strong'),
  'two source-scoped fingerprints are two postings, before the merge');

select lives_ok(
  $$ select pg_temp.merge_jobs_as('dddddddd-0000-0000-0000-00000000000a'::uuid, 
       (select j.id from public.job_hunter_jobs j
          join public.job_hunter_postings p on p.id = j.posting_id
         where j.user_id = 'dddddddd-0000-0000-0000-00000000000a' and p.fingerprint = 'fp-merge-weak'),
       (select j.id from public.job_hunter_jobs j
          join public.job_hunter_postings p on p.id = j.posting_id
         where j.user_id = 'dddddddd-0000-0000-0000-00000000000a' and p.fingerprint = 'fp-merge-strong')) $$,
  'merging the two job rows');

select is(
  (select p.fingerprint from public.job_hunter_postings p
     join public.job_hunter_jobs j on j.posting_id = p.id
    where j.user_id = 'dddddddd-0000-0000-0000-00000000000a'
      and p.description = 'the employer''s own copy'),
  'fp-merge-strong',
  'the survivor points at the posting whose description it kept');

-- And A is left with one row, not two: merging the postings collapsed the
-- membership rows behind them, which `unique (user_id, posting_id)` now
-- requires (#178).
select is(
  (select count(*)::int from public.job_hunter_jobs j
     join public.job_hunter_postings p on p.id = j.posting_id
    where j.user_id = 'dddddddd-0000-0000-0000-00000000000a'
      and p.fingerprint = 'fp-merge-strong'),
  1,
  'the merge left one membership row over the surviving posting');

-- Readability. B never discovered this one.
select pg_temp.authenticate_as('dddddddd-0000-0000-0000-00000000000a');
select lives_ok(
  $$ select pg_temp.upsert_job_as('dddddddd-0000-0000-0000-00000000000a'::uuid, jsonb_build_object(
       'fingerprint', 'fp-a-only',
       'source', 'lever',
       'source_job_id', 'lv-9',
       'url', 'https://jobs.lever.co/other/9',
       'company', 'Other Co',
       'title', 'Backend Engineer',
       'location', 'Berlin',
       'description', 'only A ever saw this one',
       'content_confidence', 'official_ats')) $$,
  'A discovers a posting B has never seen');

select pg_temp.authenticate_as('dddddddd-0000-0000-0000-00000000000b');
select is(
  (select p.title from public.job_hunter_postings p where p.fingerprint = 'fp-a-only'),
  'Backend Engineer',
  'any authenticated user can read any posting');

select is_empty(
  $$ select 1 from public.job_hunter_jobs j
      join public.job_hunter_postings p on p.id = j.posting_id
     where p.fingerprint = 'fp-a-only' $$,
  'the job row behind it stays private to A');

-- Before #179 this was a silent no-op: row-level security filtered the delete
-- to zero rows and the caller was told nothing. Now the grant refuses it
-- outright, which is the louder and the more honest of the two.
select throws_ok(
  $$ delete from public.job_hunter_postings where fingerprint = 'fp-a-only' $$,
  '42501', null,
  'no user can delete a posting out from under everyone else');

select pg_temp.become_anon();
select is_empty(
  $$ select 1 from public.job_hunter_postings $$,
  'anon reads nothing: shared means shared between authenticated users');

select pg_temp.become_postgres();

select * from finish();
rollback;
