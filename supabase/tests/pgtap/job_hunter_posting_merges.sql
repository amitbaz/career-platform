-- Cross-identity merging at the posting level (issue #176).
--
-- The fingerprint is source-scoped, so the same advertisement seen on an
-- employer's ATS board and on an aggregator hashes twice and produces two
-- postings. Collapsing those was job_hunter_merge_jobs, which is per-user
-- throughout: every user repeated the decision, and two users could reach
-- different conclusions about the same pair of records.
--
-- job_hunter_merge_postings makes the decision once, for everyone. This
-- file pins the properties that make that safe:
--
--   * every affected user's job row is re-pointed at the survivor, not only
--     the merging user's;
--   * the survivor keeps the higher-confidence description, by the same
--     job_hunter_preferred_description ladder every other posting write
--     uses;
--   * the merged-away posting's facets are discarded rather than stamped
--     onto the survivor, because stamping them would pin one posting's
--     facts to another posting's text as permanently current (#125,
--     restated at the posting level);
--   * a caller holding a stale posting id still resolves to the survivor,
--     and a re-crawl of the merged-away source cannot resurrect a
--     competing posting;
--   * merging is idempotent and order-independent, so two users who call
--     it with the arguments the other way round converge.
begin;
create extension if not exists pgtap with schema extensions;
select no_plan();

-- Seed users ----------------------------------------------------------------
--
-- Two users of this file's own, so a run here cannot disturb -- or be
-- disturbed by -- the users job_hunter_postings.sql seeds.

insert into auth.users (id, email, instance_id, aud, role, raw_app_meta_data, raw_user_meta_data, created_at, updated_at)
values
  ('eeeeeeee-0000-0000-0000-00000000000a', 'posting-merge-a@test.local', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', '{}', '{}', now(), now()),
  ('eeeeeeee-0000-0000-0000-00000000000b', 'posting-merge-b@test.local', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', '{}', '{}', now(), now())
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

create function pg_temp.posting_id(p_fingerprint text) returns uuid
language sql stable as $$
  select p.id from public.job_hunter_postings p where p.fingerprint = p_fingerprint;
$$;

-- Since #179 the job upsert and the job merge write shared rows, so they run
-- as the privileged ingestion role and take the user they act for as an
-- argument. These helpers are that transport in miniature: drop to the owner,
-- make the call for the named user, hand the session back to them. Every
-- scenario below is still "this user's crawl finds this listing"; only the
-- role making the write moved.
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

select has_table('public', 'job_hunter_posting_merges',
                 'where a merged-away posting went is recorded');

select columns_are('public', 'job_hunter_posting_merges', array[
  'id',
  'duplicate_id',
  'survivor_id',
  'merged_at',
  'created_at'
], 'the redirect carries no user_id: it is one record for everyone');

select has_function('public', 'job_hunter_merge_postings', array['uuid', 'uuid'],
                    'two postings can be collapsed into one');
select has_function('public', 'job_hunter_resolve_posting', array['uuid'],
                    'a stale posting id can be resolved to its survivor');

-- The two postings ----------------------------------------------------------
--
-- One advertisement, two sources. A finds it on an aggregator; B finds the
-- same one on the aggregator too, so both users hold the aggregator posting
-- -- which is the case that matters, because only one of them will merge.
-- B also finds the employer's own Greenhouse listing, which hashes to a
-- second posting carrying the full ATS triple and the better text.

select pg_temp.authenticate_as('eeeeeeee-0000-0000-0000-00000000000a');
select lives_ok(
  $$ select pg_temp.upsert_job_as('eeeeeeee-0000-0000-0000-00000000000a'::uuid, jsonb_build_object(
       'fingerprint', 'fp-merge-aggregator',
       'source', 'remoteok',
       'source_job_id', 'ro-176',
       'url', 'https://remoteok.example/jobs/176',
       'company', 'Mergeco',
       'title', 'Platform Engineer',
       'location', 'Vienna',
       'description', 'a short aggregator blurb',
       'content_confidence', 'aggregator_text')) $$,
  'A discovers the advertisement on an aggregator');

select pg_temp.authenticate_as('eeeeeeee-0000-0000-0000-00000000000b');
select lives_ok(
  $$ select pg_temp.upsert_job_as('eeeeeeee-0000-0000-0000-00000000000b'::uuid, jsonb_build_object(
       'fingerprint', 'fp-merge-aggregator',
       'source', 'remoteok',
       'source_job_id', 'ro-176',
       'url', 'https://remoteok.example/jobs/176',
       'company', 'Mergeco',
       'title', 'Platform Engineer',
       'location', 'Vienna',
       'description', 'a short aggregator blurb',
       'content_confidence', 'aggregator_text')) $$,
  'B discovers the same aggregator listing');

-- A different company and title on the ATS side, so the identity
-- resolution inside job_hunter_upsert_job does not collapse B's two job
-- rows on its own: what is under test here is the posting merge, not the
-- per-user one that already existed.
select lives_ok(
  $$ select pg_temp.upsert_job_as('eeeeeeee-0000-0000-0000-00000000000b'::uuid, jsonb_build_object(
       'fingerprint', 'fp-merge-ats',
       'source', 'greenhouse',
       'source_job_id', 'gh-176',
       'url', 'https://boards.greenhouse.io/mergeco/jobs/176',
       'canonical_url', 'https://mergeco.example/careers/176',
       'company', 'Mergeco GmbH',
       'title', 'Platform Engineer (Core)',
       'location', 'Vienna',
       'remote', true,
       'description', 'the full advertisement text straight from the employer ATS',
       'content_confidence', 'official_ats',
       'ats_provider', 'greenhouse',
       'ats_board', 'mergeco',
       'ats_job_id', '176')) $$,
  'B discovers the employer''s own listing for the same advertisement');

select pg_temp.become_postgres();

select is(
  (select count(*)::int from public.job_hunter_postings p
    where p.fingerprint in ('fp-merge-aggregator', 'fp-merge-ats')),
  2,
  'one advertisement, two postings: the fingerprint is source-scoped');

-- Facets on both sides, so the merge has something to discard and
-- something to leave alone.
insert into public.job_hunter_job_facets (posting_id, extracted_at, seniority, model)
values
  (pg_temp.posting_id('fp-merge-aggregator'), now(), 'mid', 'facets-from-the-aggregator'),
  (pg_temp.posting_id('fp-merge-ats'), now(), 'staff', 'facets-from-the-ats');

-- The merge -----------------------------------------------------------------
--
-- Called with the aggregator posting named first, to pin that the survivor
-- is chosen from the postings themselves and not from argument order.

select is(
  public.job_hunter_merge_postings(
    pg_temp.posting_id('fp-merge-aggregator'),
    pg_temp.posting_id('fp-merge-ats')),
  pg_temp.posting_id('fp-merge-ats'),
  'the posting carrying the ATS identity survives, whichever way round it is named');

-- Two rows, not three: A held one of the two postings and B held both, and
-- one row per user per posting is a constraint since #178, so B's pair was
-- collapsed as part of the merge rather than left to violate it.
select is(
  (select count(*)::int from public.job_hunter_jobs j
    where j.posting_id = pg_temp.posting_id('fp-merge-ats')),
  2,
  'every user who held either posting now holds exactly one row over the survivor');

select is_empty(
  $$ select 1 from public.job_hunter_jobs j
      where j.posting_id = pg_temp.posting_id('fp-merge-aggregator') $$,
  'no job row is left pointing at the merged-away posting');

-- The criterion this whole ticket exists for: A never merged anything.
select pg_temp.authenticate_as('eeeeeeee-0000-0000-0000-00000000000a');

select is(
  (select j.posting_id from public.job_hunter_jobs j
    where j.user_id = 'eeeeeeee-0000-0000-0000-00000000000a'),
  pg_temp.posting_id('fp-merge-ats'),
  'A''s job row follows the merge B''s run performed, without A merging anything');

select is(
  (select p.description from public.job_hunter_postings p
     join public.job_hunter_jobs j on j.posting_id = p.id
    where j.user_id = 'eeeeeeee-0000-0000-0000-00000000000a'),
  'the full advertisement text straight from the employer ATS',
  'and A reads the surviving text, which A never fetched');

select pg_temp.become_postgres();

-- The survivor's own columns ------------------------------------------------

select is(
  (select p.description from public.job_hunter_postings p
    where p.fingerprint = 'fp-merge-ats'),
  'the full advertisement text straight from the employer ATS',
  'the survivor keeps the higher-confidence description');

select is(
  (select p.content_confidence from public.job_hunter_postings p
    where p.fingerprint = 'fp-merge-ats'),
  'official_ats',
  'the tier follows the text it belongs to');

select is(
  (select p.description_hash from public.job_hunter_postings p
    where p.fingerprint = 'fp-merge-ats'),
  encode(sha256(convert_to('the full advertisement text straight from the employer ATS', 'UTF8')), 'hex'),
  'so does the description hash');

select is(
  (select p.source_job_id from public.job_hunter_postings p
    where p.fingerprint = 'fp-merge-ats'),
  'gh-176',
  'a non-empty column on the survivor is not overwritten by the duplicate''s');

-- Facets --------------------------------------------------------------------

select is_empty(
  $$ select 1 from public.job_hunter_job_facets f
      where f.model = 'facets-from-the-aggregator' $$,
  'the merged-away posting''s facets are discarded');

select is(
  (select f.model from public.job_hunter_job_facets f
    where f.posting_id = pg_temp.posting_id('fp-merge-ats')),
  'facets-from-the-ats',
  'and are not stamped onto the survivor, which keeps its own');

-- The redirect --------------------------------------------------------------

select is(
  (select m.survivor_id from public.job_hunter_posting_merges m
    where m.duplicate_id = pg_temp.posting_id('fp-merge-aggregator')),
  pg_temp.posting_id('fp-merge-ats'),
  'the redirect records where the merged-away posting went');

select is(
  public.job_hunter_resolve_posting(pg_temp.posting_id('fp-merge-aggregator')),
  pg_temp.posting_id('fp-merge-ats'),
  'a caller holding the stale posting id resolves to the survivor');

select is(
  public.job_hunter_resolve_posting(pg_temp.posting_id('fp-merge-ats')),
  pg_temp.posting_id('fp-merge-ats'),
  'and a live posting id resolves to itself');

select isnt(
  pg_temp.posting_id('fp-merge-aggregator'),
  null,
  'the merged-away posting keeps its row, and with it its fingerprint');

-- Idempotence and order independence ----------------------------------------

select is(
  public.job_hunter_merge_postings(
    pg_temp.posting_id('fp-merge-ats'),
    pg_temp.posting_id('fp-merge-aggregator')),
  pg_temp.posting_id('fp-merge-ats'),
  'merging the same pair again is a no-op that returns the survivor');

select is(
  (select count(*)::int from public.job_hunter_posting_merges m
    where m.duplicate_id = pg_temp.posting_id('fp-merge-aggregator')),
  1,
  'and records no second redirect');

-- A re-crawl of the merged-away source ---------------------------------------
--
-- The aggregator keeps advertising the job, so its listing arrives again on
-- the next run. It must resolve to the survivor rather than resurrect the
-- posting it was merged out of -- which is what keeps the merge decision
-- from having to be made again every day.

select pg_temp.authenticate_as('eeeeeeee-0000-0000-0000-00000000000a');
select lives_ok(
  $$ select pg_temp.upsert_job_as('eeeeeeee-0000-0000-0000-00000000000a'::uuid, jsonb_build_object(
       'fingerprint', 'fp-merge-aggregator',
       'source', 'remoteok',
       'source_job_id', 'ro-176',
       'url', 'https://remoteok.example/jobs/176',
       'company', 'Mergeco',
       'title', 'Platform Engineer',
       'location', 'Vienna',
       'description', 'a short aggregator blurb',
       'content_confidence', 'aggregator_text')) $$,
  'A re-crawls the aggregator listing after the merge');

select pg_temp.become_postgres();

select is(
  (select count(*)::int from public.job_hunter_postings p
    where p.fingerprint = 'fp-merge-aggregator'),
  1,
  'the re-crawl creates no second posting for that fingerprint');

select is(
  (select j.posting_id from public.job_hunter_jobs j
    where j.user_id = 'eeeeeeee-0000-0000-0000-00000000000a'),
  pg_temp.posting_id('fp-merge-ats'),
  'and A''s job row still points at the survivor');

select is(
  (select p.description from public.job_hunter_postings p
    where p.fingerprint = 'fp-merge-ats'),
  'the full advertisement text straight from the employer ATS',
  'a weaker re-crawl of the merged-away source cannot degrade the survivor');

-- Chains --------------------------------------------------------------------
--
-- A third posting, merged into one that has itself already been merged
-- away. The redirect it produces must name the posting that still exists,
-- so one lookup is always enough.
--
-- Its company, title and location are deliberately unlike the two above:
-- job_hunter_upsert_job resolves a payload against a user's existing rows
-- by normalized identity as well as by fingerprint, and a third rendering
-- that matched would be folded into a row already under test before the
-- merge below ever ran. What is under test here is the redirect, so the
-- third posting has to arrive as a row of its own.

select pg_temp.authenticate_as('eeeeeeee-0000-0000-0000-00000000000b');
select lives_ok(
  $$ select pg_temp.upsert_job_as('eeeeeeee-0000-0000-0000-00000000000b'::uuid, jsonb_build_object(
       'fingerprint', 'fp-merge-third',
       'source', 'linkedin',
       'source_job_id', 'li-176',
       'url', 'https://linkedin.example/jobs/176',
       'company', 'Thirdco',
       'title', 'Site Reliability Engineer',
       'location', 'Zurich',
       'description', 'a third rendering of the same advertisement',
       'content_confidence', 'aggregator_text')) $$,
  'B discovers a third source for the same advertisement');

select pg_temp.become_postgres();

select is(
  public.job_hunter_merge_postings(
    pg_temp.posting_id('fp-merge-aggregator'),
    pg_temp.posting_id('fp-merge-third')),
  pg_temp.posting_id('fp-merge-ats'),
  'merging into an already-merged posting lands on the posting that still exists');

select is(
  (select m.survivor_id from public.job_hunter_posting_merges m
    where m.duplicate_id = pg_temp.posting_id('fp-merge-third')),
  pg_temp.posting_id('fp-merge-ats'),
  'the redirect is flattened: it names the survivor, never a chain');

-- B held the third posting as well, so B's rows are folded into the one row
-- the constraint allows, over the surviving posting.
select results_eq(
  $$ select distinct j.posting_id from public.job_hunter_jobs j
      where j.user_id = 'eeeeeeee-0000-0000-0000-00000000000b' $$,
  format($$ values (%L::uuid) $$, pg_temp.posting_id('fp-merge-ats')),
  'and the third posting''s job row re-points too');

select is_empty(
  $$ select 1 from public.job_hunter_jobs j
       join public.job_hunter_posting_merges m on m.duplicate_id = j.posting_id $$,
  'no job row anywhere is left pointing at a posting that was merged away');

-- Refusals ------------------------------------------------------------------

select is(
  public.job_hunter_merge_postings(
    pg_temp.posting_id('fp-merge-ats'),
    pg_temp.posting_id('fp-merge-ats')),
  pg_temp.posting_id('fp-merge-ats'),
  'merging a posting with itself is a no-op');

select throws_ok(
  format($$ select public.job_hunter_merge_postings(%L::uuid, %L::uuid) $$,
         '11111111-1111-1111-1111-111111111111',
         pg_temp.posting_id('fp-merge-ats')),
  'survivor and duplicate postings must both exist',
  'merging a posting that does not exist raises rather than writing a redirect to nowhere');

-- job_hunter_merge_jobs is no longer a second merge authority -----------------
--
-- It still exists, and #178 removes it with the rest of the duplicated
-- job-row machinery. What it no longer does is decide, per user, that two
-- advertisements are the same: when the two job rows point at different
-- postings it delegates that to job_hunter_merge_postings, so the decision
-- is recorded once and every other user's rows follow it.

select pg_temp.authenticate_as('eeeeeeee-0000-0000-0000-00000000000b');

select lives_ok(
  $$ select pg_temp.upsert_job_as('eeeeeeee-0000-0000-0000-00000000000b'::uuid, jsonb_build_object(
       'fingerprint', 'fp-merge-delegating-a',
       'source', 'remoteok',
       'source_job_id', 'ro-177',
       'url', 'https://remoteok.example/jobs/177',
       'company', 'Delegateco',
       'title', 'Data Engineer',
       'location', 'Berlin',
       'description', 'the aggregator rendering',
       'content_confidence', 'aggregator_text')) $$,
  'B holds one job row for the aggregator rendering');

select lives_ok(
  $$ select pg_temp.upsert_job_as('eeeeeeee-0000-0000-0000-00000000000b'::uuid, jsonb_build_object(
       'fingerprint', 'fp-merge-delegating-b',
       'source', 'greenhouse',
       'source_job_id', 'gh-177',
       'url', 'https://boards.greenhouse.io/delegateco/jobs/177',
       'canonical_url', 'https://delegateco.example/careers/177',
       'company', 'Delegateco GmbH',
       'title', 'Data Engineer (Platform)',
       'location', 'Berlin',
       'description', 'the employer''s own rendering, fetched from the ATS',
       'content_confidence', 'official_ats',
       'ats_provider', 'greenhouse',
       'ats_board', 'delegateco',
       'ats_job_id', '177')) $$,
  'and a second for the employer''s rendering');

-- A discovers only the aggregator rendering, so it is A's row that proves
-- the decision was recorded globally rather than inside B's merge.
select pg_temp.authenticate_as('eeeeeeee-0000-0000-0000-00000000000a');
select lives_ok(
  $$ select pg_temp.upsert_job_as('eeeeeeee-0000-0000-0000-00000000000a'::uuid, jsonb_build_object(
       'fingerprint', 'fp-merge-delegating-a',
       'source', 'remoteok',
       'source_job_id', 'ro-177',
       'url', 'https://remoteok.example/jobs/177',
       'company', 'Delegateco',
       'title', 'Data Engineer',
       'location', 'Berlin',
       'description', 'the aggregator rendering',
       'content_confidence', 'aggregator_text')) $$,
  'A holds the aggregator rendering only');

select pg_temp.authenticate_as('eeeeeeee-0000-0000-0000-00000000000b');
select lives_ok(
  format($$ select pg_temp.merge_jobs_as('eeeeeeee-0000-0000-0000-00000000000b'::uuid, 
       (select j.id from public.job_hunter_jobs j
         where j.user_id = 'eeeeeeee-0000-0000-0000-00000000000b'
           and j.posting_id = %L::uuid),
       (select j.id from public.job_hunter_jobs j
         where j.user_id = 'eeeeeeee-0000-0000-0000-00000000000b'
           and j.posting_id = %L::uuid)) $$,
       pg_temp.posting_id('fp-merge-delegating-b'),
       pg_temp.posting_id('fp-merge-delegating-a')),
  'B merges its own two job rows');

select pg_temp.become_postgres();

select is(
  public.job_hunter_resolve_posting(pg_temp.posting_id('fp-merge-delegating-a')),
  pg_temp.posting_id('fp-merge-delegating-b'),
  'merging two job rows across postings merged the postings behind them');

select is(
  (select count(*)::int from public.job_hunter_jobs j
    where j.user_id = 'eeeeeeee-0000-0000-0000-00000000000a'
      and j.posting_id = pg_temp.posting_id('fp-merge-delegating-b')),
  1,
  'so A''s row follows a merge B decided, which is what "no second authority" buys');

-- Which of B's two rows survived is a per-user question -- the older one,
-- neither having any history -- but what it says about the advertisement is
-- not a per-user question at all any more (#178): there is one row left, it
-- points at the surviving posting, and the text is read from there.
select is(
  (select count(*)::int from public.job_hunter_jobs j
    where j.user_id = 'eeeeeeee-0000-0000-0000-00000000000b'
      and j.posting_id = pg_temp.posting_id('fp-merge-delegating-b')),
  1,
  'B is left with exactly one membership row over the surviving posting');

select is(
  (select p.description from public.job_hunter_jobs j
     join public.job_hunter_postings p on p.id = j.posting_id
    where j.user_id = 'eeeeeeee-0000-0000-0000-00000000000b'
      and j.posting_id = pg_temp.posting_id('fp-merge-delegating-b')),
  'the employer''s own rendering, fetched from the ATS',
  'and the surviving job row reads the surviving posting''s description');

select is(
  (select p.content_confidence from public.job_hunter_jobs j
     join public.job_hunter_postings p on p.id = j.posting_id
    where j.user_id = 'eeeeeeee-0000-0000-0000-00000000000b'
      and j.posting_id = pg_temp.posting_id('fp-merge-delegating-b')),
  'official_ats',
  'with the tier that belongs to it');

-- A thin ATS listing that loses the description ladder ------------------------
--
-- The survivor is chosen by description first, so an ATS listing scraped
-- from a board index -- right identity, almost no text -- loses to a fat
-- aggregator rendering. The column fold then backfills the survivor's empty
-- ats_* columns from it, and the survivor would claim a Greenhouse identity
-- while linking only to the aggregator if the URL rule did not keep
-- job_hunter_merge_jobs' asymmetry: when only the merged-away side is an ATS
-- listing, its links win outright rather than merely backfilling.

select pg_temp.authenticate_as('eeeeeeee-0000-0000-0000-00000000000a');

select lives_ok(
  $$ select pg_temp.upsert_job_as('eeeeeeee-0000-0000-0000-00000000000a'::uuid, jsonb_build_object(
       'fingerprint', 'fp-thin-ats',
       'source', 'greenhouse',
       'source_job_id', 'gh-thin',
       'url', 'https://boards.greenhouse.io/thinco/jobs/9',
       'canonical_url', 'https://thinco.example/careers/9',
       'company', 'Thinco',
       'title', 'Compiler Engineer',
       'location', 'Zurich',
       'description', 'see website',
       'content_confidence', 'aggregator_text',
       'ats_provider', 'greenhouse',
       'ats_board', 'thinco',
       'ats_job_id', '9')) $$,
  'the employer''s listing is discovered with almost no text');

select lives_ok(
  $$ select pg_temp.upsert_job_as('eeeeeeee-0000-0000-0000-00000000000a'::uuid, jsonb_build_object(
       'fingerprint', 'fp-fat-aggregator',
       'source', 'remoteok',
       'source_job_id', 'ro-thin',
       'url', 'https://remoteok.example/jobs/9',
       'canonical_url', 'https://remoteok.example/jobs/9',
       'company', 'Thinco AG',
       'title', 'Compiler Engineer II',
       'location', 'Zurich',
       'description', 'a long aggregator rendering of the same advertisement, with the whole job description reproduced in it',
       'content_confidence', 'aggregator_text')) $$,
  'and an aggregator carries the same advertisement in full');

select pg_temp.become_postgres();

select is(
  public.job_hunter_merge_postings(
    pg_temp.posting_id('fp-thin-ats'),
    pg_temp.posting_id('fp-fat-aggregator')),
  pg_temp.posting_id('fp-fat-aggregator'),
  'the fuller text wins the survivor slot even against an ATS identity');

select is(
  (select p.ats_board from public.job_hunter_postings p
    where p.fingerprint = 'fp-fat-aggregator'),
  'thinco',
  'the survivor takes the ATS identity from the posting merged into it');

select is(
  (select p.canonical_url from public.job_hunter_postings p
    where p.fingerprint = 'fp-fat-aggregator'),
  'https://thinco.example/careers/9',
  'and its links too, so it cannot claim an ATS identity while linking elsewhere');

select is(
  (select p.url from public.job_hunter_postings p
    where p.fingerprint = 'fp-fat-aggregator'),
  'https://thinco.example/careers/9',
  'the usable URL is the employer''s, which is the one they will still honour');

-- A crawl of a merged-away source, through the single-listing path ------------
--
-- The redirect makes this write land on the survivor. url and canonical_url
-- are the only two columns here that overwrite rather than backfill, so
-- without an exception a re-fetch of the merged-away aggregator listing
-- would replace the survivor's employer link with the aggregator's -- and
-- store_mapping reads canonical_url off the posting, so it reaches the user.

select is(
  public.job_hunter_upsert_posting(jsonb_build_object(
    'fingerprint', 'fp-thin-ats',
    'source', 'greenhouse',
    'source_job_id', 'gh-thin',
    'url', 'https://boards.greenhouse.io/thinco/jobs/9',
    'canonical_url', 'https://boards.greenhouse.io/thinco/jobs/9',
    'company', 'Thinco',
    'title', 'Compiler Engineer',
    'location', 'Zurich',
    'description', 'see website',
    'content_confidence', 'aggregator_text')),
  pg_temp.posting_id('fp-fat-aggregator'),
  'a fetch of the merged-away source is applied to the survivor');

select is(
  (select p.canonical_url from public.job_hunter_postings p
    where p.fingerprint = 'fp-fat-aggregator'),
  'https://thinco.example/careers/9',
  'and cannot overwrite the canonical URL the merge chose');

select is(
  (select p.url from public.job_hunter_postings p
    where p.fingerprint = 'fp-fat-aggregator'),
  'https://thinco.example/careers/9',
  'nor the usable URL');

-- A crawl of a merged-away source, through the batch path ---------------------
--
-- The batch path is the one a real crawl takes: discovery stages a batch
-- whenever it holds the direct connection, and the job upsert skips the
-- single-listing path entirely for a payload that already names its posting.
-- So if the fold stayed keyed on the staged fingerprint, the aggregator's
-- text and its last_seen_at would go on being written to the row nobody
-- reads, and the survivor would look staler the longer both sources carried
-- the advertisement.

select pg_temp.become_postgres();

-- Wind the survivor's last_seen_at back, so a refresh is visible as a
-- change rather than as two values a fast test cannot tell apart.
update public.job_hunter_postings
   set last_seen_at = now() - interval '3 days'
 where fingerprint = 'fp-merge-ats';

insert into public.job_hunter_posting_staging
  (batch_id, ordinal, fingerprint, source, source_job_id, url, company, title,
   location, description, content_confidence)
values
  ('cccccccc-0000-0000-0000-00000000c002', 1, 'fp-merge-aggregator', 'remoteok',
   'ro-176', 'https://remoteok.example/jobs/176', 'Mergeco', 'Platform Engineer',
   'Vienna', 'a short aggregator blurb', 'aggregator_text');

create temporary table pg_temp_batch_result as
select * from public.job_hunter_merge_posting_batch('cccccccc-0000-0000-0000-00000000c002');

select is(
  (select r.fingerprint from pg_temp_batch_result r),
  'fp-merge-aggregator',
  'the mapping is keyed by the fingerprint the caller staged, not the survivor''s');

select is(
  (select r.posting_id from pg_temp_batch_result r),
  pg_temp.posting_id('fp-merge-ats'),
  'and hands back the survivor, so the job row it stamps points at the right posting');

select is(
  (select r.is_new from pg_temp_batch_result r),
  false,
  'a listing resolving onto an existing survivor is not a new posting');

select is(
  (select count(*)::int from public.job_hunter_postings p
    where p.fingerprint = 'fp-merge-aggregator'),
  1,
  'the batch created no second posting for the merged-away fingerprint');

select ok(
  (select p.last_seen_at from public.job_hunter_postings p
    where p.fingerprint = 'fp-merge-ats') > now() - interval '1 minute',
  'the survivor''s last_seen_at is refreshed by a crawl of the merged-away source');

select is(
  (select p.description from public.job_hunter_postings p
    where p.fingerprint = 'fp-merge-ats'),
  'the full advertisement text straight from the employer ATS',
  'and its weaker text still cannot displace the better description');

-- Access --------------------------------------------------------------------
--
-- The redirect is readable by everyone and writable by no one. Reads are
-- open for the same reason job_hunter_postings' are: a merge decided once
-- for everyone is useless if only its author can see it. Writes have no
-- policy at all, which is stricter than the two shared tables above --
-- every write happens inside job_hunter_merge_postings, and a redirect
-- written any other way would name a merge that never re-pointed anything.

select pg_temp.authenticate_as('eeeeeeee-0000-0000-0000-00000000000a');

select isnt(
  (select m.survivor_id from public.job_hunter_posting_merges m
    where m.duplicate_id = pg_temp.posting_id('fp-merge-aggregator')),
  null,
  'a user who merged nothing can still read where a posting went');

-- Since #179 the refusal comes from the grant rather than from the missing
-- policy, so all three are errors rather than silent no-ops. The absence of a
-- write policy is still asserted, in job_hunter_shared_writes.sql, because
-- neither half of the refusal may be the only one.
select throws_ok(
  format($$ insert into public.job_hunter_posting_merges (duplicate_id, survivor_id)
            values (%L::uuid, %L::uuid) $$,
         pg_temp.posting_id('fp-merge-ats'),
         pg_temp.posting_id('fp-merge-aggregator')),
  '42501',
  'permission denied for table job_hunter_posting_merges',
  'no user can write a redirect by hand');

select throws_ok(
  $$ update public.job_hunter_posting_merges set survivor_id = duplicate_id $$,
  '42501',
  'permission denied for table job_hunter_posting_merges',
  'nor rewrite one');

select throws_ok(
  $$ delete from public.job_hunter_posting_merges $$,
  '42501',
  'permission denied for table job_hunter_posting_merges',
  'nor delete one out from under everyone else');

-- Nor reach either merge. job_hunter_merge_postings was already revoked from
-- every role a user can hold; since #179 job_hunter_merge_jobs is too, because
-- collapsing two postings is a write every other user sees however the caller
-- reached it. Both now happen on ingestion's privileged connection.
select throws_ok(
  format($$ select public.job_hunter_merge_postings(%L::uuid, %L::uuid) $$,
         pg_temp.posting_id('fp-merge-ats'),
         pg_temp.posting_id('fp-merge-aggregator')),
  '42501',
  'permission denied for function job_hunter_merge_postings',
  'and no user can collapse two postings they merely know the ids of');

select throws_ok(
  format($$ select public.job_hunter_merge_jobs(%L::uuid, %L::uuid, %L::uuid) $$,
         gen_random_uuid(), gen_random_uuid(),
         'eeeeeeee-0000-0000-0000-00000000000a'),
  '42501',
  'permission denied for function job_hunter_merge_jobs',
  'nor reach the posting merge through the job-level entry point that used to be theirs');

select pg_temp.become_postgres();

select * from finish();
rollback;
