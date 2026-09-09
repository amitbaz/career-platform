-- Posting-level facts are read from the posting, not from the job row (#177).
--
-- 20260909100000 gave every job row a posting to point at; 20260909150000
-- moved the SQL readers of a posting-level fact onto it. Both copies of
-- every such fact still exist and still agree, so a test that only ever saw
-- them agree could not tell which one was read. Every assertion below drives
-- them apart first -- it corrupts the job row's duplicate, or moves the
-- posting's value on its own -- and then asserts which one the function
-- followed.
--
-- What moved is "is the work done against this description still current":
-- the description hash and the content confidence. Which of the user's rows
-- a Gmail candidate matches did not move, and the last assertion here pins
-- that: the match is still made against the job row, which covers every
-- posting merged into it.
--
-- Nothing in the application writes a job row that disagrees with its
-- posting. These updates exist only to make the question answerable.
begin;
create extension if not exists pgtap with schema extensions;
select no_plan();

-- Shape ----------------------------------------------------------------------

select has_column('public', 'job_hunter_jobs', 'posting_id',
                  'a job row names the posting its facts are read from');

-- Seed user -------------------------------------------------------------------

insert into auth.users (id, email, instance_id, aud, role, raw_app_meta_data, raw_user_meta_data, created_at, updated_at)
values ('eeeeeeee-0000-0000-0000-00000000000a', 'posting-reads-a@test.local',
        '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', '{}', '{}', now(), now())
on conflict (id) do nothing;

create function pg_temp.authenticate_as(p_user uuid) returns void
language plpgsql as $$
begin
  execute 'reset role';
  perform set_config('request.jwt.claims',
    json_build_object('sub', p_user, 'role', 'authenticated')::text, true);
  execute 'set local role authenticated';
end $$;

select pg_temp.authenticate_as('eeeeeeee-0000-0000-0000-00000000000a');

-- One job, written through the RPC so it has a posting and points at it.
create temp table subject as
select id from public.job_hunter_upsert_job(jsonb_build_object(
  'fingerprint', 'fp-177',
  'match_mode', 'fingerprint',
  'source', 'greenhouse',
  'source_job_id', 'g-177',
  'url', 'https://acme177.example/jobs/1',
  'canonical_url', 'https://acme177.example/jobs/1',
  'company', 'Acme 177 GmbH',
  'title', 'Senior Backend Engineer',
  'location', 'Berlin',
  'description', 'the shared description',
  'content_confidence', 'official_ats'));

create temp view subject_posting as
  select p.* from public.job_hunter_postings p
    join public.job_hunter_jobs j on j.posting_id = p.id
   where j.id = (select id from subject);

select isnt_empty($$ select 1 from subject_posting $$,
                  'the upsert RPC gave the job a posting to read from');

-- An evaluation that is current against the posting.
insert into public.job_hunter_evaluations
  (user_id, job_id, total_score, decision, evaluated_at,
   description_hash_at_eval, content_confidence_at_eval)
select 'eeeeeeee-0000-0000-0000-00000000000a', (select id from subject), 90, 'high_priority',
       '2026-02-01T00:00:00Z', p.description_hash, p.content_confidence
  from subject_posting p;

-- Re-evaluation ----------------------------------------------------------------

select is(
  (select needs from public.job_hunter_needs_evaluation(array[(select id from subject)])),
  false,
  'needs_evaluation: an evaluation current against the posting is current');

-- There is no duplicate left to disagree with the posting: #178 took the job
-- row's copy of the description state away entirely, so the posting is not
-- merely preferred here, it is the only answer.
select is_empty(
  $$ select column_name from information_schema.columns
      where table_schema = 'public' and table_name = 'job_hunter_jobs'
        and column_name in ('description_hash', 'content_confidence') $$,
  'needs_evaluation: the job row has no description state of its own to go stale');

-- Moving the posting's own hash triggers re-evaluation.
update public.job_hunter_postings set description_hash = 'moved-on'
 where id = (select id from subject_posting);

select is(
  (select needs from public.job_hunter_needs_evaluation(array[(select id from subject)])),
  true,
  'needs_evaluation: the posting''s description hash decides re-evaluation');

update public.job_hunter_postings set content_confidence = 'aggregator_text'
 where id = (select id from subject_posting);
update public.job_hunter_postings set description_hash = (
  select description_hash_at_eval from public.job_hunter_evaluations
   where job_id = (select id from subject))
 where id = (select id from subject_posting);

select is(
  (select needs from public.job_hunter_needs_evaluation(array[(select id from subject)])),
  true,
  'needs_evaluation: the posting''s content confidence decides re-evaluation too');

-- Back to current, so the inbound assertions below start from a job whose
-- evaluation is complete.
update public.job_hunter_postings set content_confidence = (
  select content_confidence_at_eval from public.job_hunter_evaluations
   where job_id = (select id from subject))
 where id = (select id from subject_posting);

-- A job row without a posting used to be writable, and needs_evaluation fell
-- back to its own columns for one. #178 removed both halves of that: the
-- column is `not null`, so the row is refused at the door rather than
-- answering from a copy that no longer exists.
select throws_ok(
  $$ insert into public.job_hunter_jobs (id, user_id, first_seen_at, last_seen_at)
     values ('50000000-0000-0000-0000-000000000001',
             'eeeeeeee-0000-0000-0000-00000000000a',
             '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z') $$,
  '23502', null,
  'a job row without a posting cannot be written at all');

-- Inbound candidates -------------------------------------------------------------

insert into public.job_hunter_gmail_messages
  (user_id, message_id, subject, occurred_at, classification, confidence, processed_at)
values
  ('eeeeeeee-0000-0000-0000-00000000000a', 'm177-url', 'By URL', '2026-02-01T00:00:00Z', 'review', 0.9, now()),
  ('eeeeeeee-0000-0000-0000-00000000000a', 'm177-identity', 'By identity', '2026-02-02T00:00:00Z', 'review', 0.9, now());

insert into public.job_hunter_inbound_job_candidates
  (id, user_id, source_message_id, source_candidate_key, source_platform,
   url, company, title, location, last_seen_at)
values
  -- Matches the job row by URL alone: the tracking parameter canonicalizes away.
  ('60000000-0000-0000-0000-000000000001', 'eeeeeeee-0000-0000-0000-00000000000a',
   'm177-url', 'cand-177-url', 'lever',
   'https://acme177.example/jobs/1?utm_source=newsletter#apply', '', '', '', now()),
  -- Matches the job row by company/title/location alone: no URL to match on,
  -- and 'gmail:lever' is not the job row's source.
  ('60000000-0000-0000-0000-000000000002', 'eeeeeeee-0000-0000-0000-00000000000a',
   'm177-identity', 'cand-177-identity', 'lever',
   '', 'acme  177 GmbH', 'Senior  Backend Engineer', 'BERLIN', now());

-- The posting's description state is current and its evaluation is complete,
-- so both candidates are excluded: the completeness check read the posting.
select is_empty(
  $$ select c->>'id' from public.job_hunter_eligible_inbound_jobs() c
      where c->>'id' in ('60000000-0000-0000-0000-000000000001',
                         '60000000-0000-0000-0000-000000000002') $$,
  'eligible_inbound_jobs: a candidate whose posting has a current evaluation is not eligible');

-- Moving the posting's description does: the advertisement changed, so the
-- evaluation is no longer current and the candidate needs materializing again.
update public.job_hunter_postings set description_hash = 'moved-on-again'
 where id = (select id from subject_posting);

select results_eq(
  $$ select c->>'id' from public.job_hunter_eligible_inbound_jobs() c
      where c->>'id' in ('60000000-0000-0000-0000-000000000001',
                         '60000000-0000-0000-0000-000000000002')
      order by 1 $$,
  $$ values ('60000000-0000-0000-0000-000000000001'::text),
            ('60000000-0000-0000-0000-000000000002'::text) $$,
  'eligible_inbound_jobs: a changed posting description makes both matches eligible again');

-- Matching is the posting's too since #178: the job row has no url or
-- identity of its own left to match on, and the postings are merged (#176),
-- so the survivor carries the resolved link and the folded identity that a
-- merged job row used to be the only holder of. Put the posting's description
-- back, then junk the posting's url and identity: both candidates stop
-- matching anything the user holds and become eligible again.
update public.job_hunter_postings set
  description_hash = (select description_hash_at_eval from public.job_hunter_evaluations
                       where job_id = (select id from subject)),
  url = 'https://not-the-advert.example/9',
  canonical_url = 'https://not-the-advert.example/9',
  company = 'Some Other Co',
  title = 'Some Other Title',
  location = 'Some Other City'
 where id = (select id from subject_posting);

select results_eq(
  $$ select c->>'id' from public.job_hunter_eligible_inbound_jobs() c
      where c->>'id' in ('60000000-0000-0000-0000-000000000001',
                         '60000000-0000-0000-0000-000000000002')
      order by 1 $$,
  $$ values ('60000000-0000-0000-0000-000000000001'::text),
            ('60000000-0000-0000-0000-000000000002'::text) $$,
  'eligible_inbound_jobs: a candidate matches on the posting''s url and identity, so junking them makes it eligible');

select * from finish();
rollback;
