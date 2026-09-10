-- Posting freshness (issue #186).
--
-- The schedule half lives here: how long a posting waits before it is
-- re-checked, which postings the cron tick sends to the recheck_freshness
-- queue, and that a closed posting stops being delivered and a crawl reopens
-- it. What one re-check does with one posting is the stage's seam, in
-- apps/job-hunter/tests/test_recheck_freshness_stage.py.
begin;
create extension if not exists pgtap with schema extensions;
select no_plan();

-- Every other posting on the shared local stack is pushed out of the way
-- first: the enqueuer reads the whole table, and a posting some other test
-- left behind is due too. Rolled back with everything else.
update public.job_hunter_postings
   set freshness_next_check_at = now() + interval '30 days';
delete from pgmq.q_job_hunter_recheck_freshness;


-- 1. Columns --------------------------------------------------------------------

select has_column('public', 'job_hunter_postings', 'closed_at',
  'a posting records when it was found to be gone');
select has_column('public', 'job_hunter_postings', 'closed_reason',
  'a closed posting says why, so an empty digest can be explained');
select has_column('public', 'job_hunter_postings', 'freshness_checked_at',
  'a posting records when it was last re-checked');
select has_column('public', 'job_hunter_postings', 'freshness_next_check_at',
  'a posting records when it is next due');
select has_column('public', 'job_hunter_postings', 'freshness_etag',
  'a posting keeps the validator that makes its next check conditional');
select has_column('public', 'job_hunter_postings', 'freshness_last_modified',
  'a posting keeps the date validator too');


-- 2. The interval widens with age -------------------------------------------------

select is(public.job_hunter_freshness_interval(interval '1 hour'), interval '6 hours',
  'a posting found an hour ago is re-checked six hours on, not sooner');
select is(public.job_hunter_freshness_interval(interval '8 days'), interval '2 days',
  'between the bounds the interval is a quarter of the posting''s age');
select is(public.job_hunter_freshness_interval(interval '400 days'), interval '7 days',
  'an old posting is re-checked weekly, never less often');
select ok(
  public.job_hunter_freshness_interval(interval '20 days')
    > public.job_hunter_freshness_interval(interval '4 days'),
  'an older posting waits longer between checks than a newer one');


-- 3. A posting is not due the moment it is discovered -----------------------------

insert into public.job_hunter_postings (id, fingerprint, url, first_seen_at, last_seen_at)
values ('f7e50000-0000-0000-0000-000000000001', 'pgtap-freshness-new',
        'https://example.test/new', now(), now());

select ok(
  (select freshness_next_check_at >= now() + interval '6 hours'
     from public.job_hunter_postings where id = 'f7e50000-0000-0000-0000-000000000001'),
  'a newly discovered posting is first due six hours after it is found');
select is(
  (select closed_at from public.job_hunter_postings
    where id = 'f7e50000-0000-0000-0000-000000000001'),
  null,
  'a newly discovered posting is open');


-- 4. The enqueuer sends only what is due, open, checkable and surviving ----------

insert into public.job_hunter_postings
  (id, fingerprint, url, canonical_url, ats_provider, ats_board, ats_job_id,
   first_seen_at, last_seen_at, freshness_next_check_at, closed_at)
values
  -- due, open, has a URL: the only one that should be sent
  ('f7e50000-0000-0000-0000-000000000002', 'pgtap-freshness-due', 'https://example.test/due', '',
   null, null, null, now() - interval '3 days', now(), now() - interval '1 minute', null),
  -- due, open, no URL but an ATS identity: checkable through its board
  ('f7e50000-0000-0000-0000-000000000003', 'pgtap-freshness-ats', '', '',
   'greenhouse', 'pgtap-board', '12345', now() - interval '3 days', now(), now() - interval '1 minute', null),
  -- not due yet
  ('f7e50000-0000-0000-0000-000000000004', 'pgtap-freshness-later', 'https://example.test/later', '',
   null, null, null, now() - interval '3 days', now(), now() + interval '1 day', null),
  -- due but already closed
  ('f7e50000-0000-0000-0000-000000000005', 'pgtap-freshness-closed', 'https://example.test/closed', '',
   null, null, null, now() - interval '3 days', now(), now() - interval '1 minute', now()),
  -- due but merged away: its survivor is the one that gets checked
  ('f7e50000-0000-0000-0000-000000000006', 'pgtap-freshness-merged', 'https://example.test/merged', '',
   null, null, null, now() - interval '3 days', now(), now() - interval '1 minute', null),
  -- due but uncheckable: nothing to fetch
  ('f7e50000-0000-0000-0000-000000000007', 'pgtap-freshness-nowhere', '', '',
   null, null, null, now() - interval '3 days', now(), now() - interval '1 minute', null);

insert into public.job_hunter_posting_merges (duplicate_id, survivor_id)
values ('f7e50000-0000-0000-0000-000000000006', 'f7e50000-0000-0000-0000-000000000004');

select is(public.job_hunter_enqueue_due_freshness(100), 2,
  'the tick enqueues exactly the due, open, checkable, surviving postings');

select results_eq(
  $$ select message->>'posting_id' from pgmq.q_job_hunter_recheck_freshness
      order by message->>'posting_id' $$,
  $$ values ('f7e50000-0000-0000-0000-000000000002'::text),
            ('f7e50000-0000-0000-0000-000000000003'::text) $$,
  'each message names one posting and nothing else identifies anyone');

select is(
  (select count(*)::int from pgmq.q_job_hunter_recheck_freshness
    where message - 'posting_id' <> '{}'::jsonb),
  0,
  'a freshness message carries a posting id and no user identity');

select ok(
  (select freshness_next_check_at > now() + interval '12 hours'
     from public.job_hunter_postings where id = 'f7e50000-0000-0000-0000-000000000002'),
  'an enqueued posting is leased, so the next tick does not resend it');

-- Even with the lease undone, a posting still waiting on the queue is not
-- sent a second time: the lease is what stops a dead-lettered posting being
-- retried every tick, and the queue itself is what stops duplicates.
update public.job_hunter_postings
   set freshness_next_check_at = now() - interval '1 minute'
 where id = 'f7e50000-0000-0000-0000-000000000002';

select is(public.job_hunter_enqueue_due_freshness(100), 0,
  'a posting already waiting on the queue is not enqueued twice');

delete from pgmq.q_job_hunter_recheck_freshness;
update public.job_hunter_postings
   set freshness_next_check_at = now() - interval '1 minute'
 where id = 'f7e50000-0000-0000-0000-000000000002';
update public.job_hunter_postings
   set freshness_next_check_at = now() - interval '2 hours'
 where id = 'f7e50000-0000-0000-0000-000000000003';

select is(public.job_hunter_enqueue_due_freshness(1), 1,
  'the tick honours its limit');
select is(
  (select message->>'posting_id' from pgmq.q_job_hunter_recheck_freshness),
  'f7e50000-0000-0000-0000-000000000003',
  'the posting overdue longest is sent first');


-- 5. Only ingestion can run it ----------------------------------------------------

select is_empty(
  $$ select 1 where has_function_privilege('authenticated',
       'public.job_hunter_enqueue_due_freshness(integer)', 'execute') $$,
  'a user cannot enqueue re-checks');
select is_empty(
  $$ select 1 where has_function_privilege('anon',
       'public.job_hunter_enqueue_due_freshness(integer)', 'execute') $$,
  'an anonymous caller cannot enqueue re-checks');


-- 6. The tick is scheduled --------------------------------------------------------

select is(
  (select count(*)::int from cron.job
    where jobname = 'job-hunter-enqueue-recheck-freshness'
      and command like '%job_hunter_enqueue_due_freshness%'),
  1,
  'pg_cron enqueues due re-checks, and only enqueues');


-- 7. A closed posting is not delivered --------------------------------------------

insert into auth.users (id, email, instance_id, aud, role, raw_app_meta_data, raw_user_meta_data, created_at, updated_at)
values ('f7e5aaaa-0000-0000-0000-000000000001', 'freshness@test.local',
        '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated',
        '{}', '{}', now(), now())
on conflict (id) do nothing;

insert into public.job_hunter_postings (id, fingerprint, url, first_seen_at, last_seen_at)
values
  ('f7e50000-0000-0000-0000-000000000010', 'pgtap-freshness-live', 'https://example.test/live', now(), now()),
  ('f7e50000-0000-0000-0000-000000000011', 'pgtap-freshness-gone', 'https://example.test/gone', now(), now());

insert into public.job_hunter_jobs (id, user_id, posting_id, first_seen_at, last_seen_at)
values
  ('f7e5bbbb-0000-0000-0000-000000000001', 'f7e5aaaa-0000-0000-0000-000000000001',
   'f7e50000-0000-0000-0000-000000000010', now(), now()),
  ('f7e5bbbb-0000-0000-0000-000000000002', 'f7e5aaaa-0000-0000-0000-000000000001',
   'f7e50000-0000-0000-0000-000000000011', now(), now());

insert into public.job_hunter_evaluations (user_id, job_id, total_score, decision, evaluated_at)
values
  ('f7e5aaaa-0000-0000-0000-000000000001', 'f7e5bbbb-0000-0000-0000-000000000001',
   90, 'possible_match', now()),
  ('f7e5aaaa-0000-0000-0000-000000000001', 'f7e5bbbb-0000-0000-0000-000000000002',
   90, 'possible_match', now());

update public.job_hunter_postings
   set closed_at = now(), closed_reason = 'http_404'
 where id = 'f7e50000-0000-0000-0000-000000000011';

create function pg_temp.authenticate_as(p_user uuid) returns void
language plpgsql as $$
begin
  execute 'reset role';
  perform set_config('request.jwt.claims',
    json_build_object('sub', p_user, 'role', 'authenticated')::text, true);
  execute 'set local role authenticated';
end $$;

select pg_temp.authenticate_as('f7e5aaaa-0000-0000-0000-000000000001');

select results_eq(
  $$ select job_id from public.job_hunter_pending_delivery_jobs(80) $$,
  $$ values ('f7e5bbbb-0000-0000-0000-000000000001'::uuid) $$,
  'a job whose posting was found gone is no longer pending delivery');

select is(
  (select count(*)::int from public.job_hunter_postings
    where id = 'f7e50000-0000-0000-0000-000000000011'),
  1,
  'a closed posting is still readable by a user who holds it');

reset role;


-- 8. The employer's own board listing it again reopens it; an aggregator does not --
--
-- An aggregator is routinely slower to drop an advertisement than the board
-- it copied it from. If its listing reopened a posting the board had already
-- dropped, the posting would flip open every day, be delivered from that
-- day's crawl, and be closed again by the next re-check -- failing the one
-- promise closing makes.

insert into public.job_hunter_postings
  (id, fingerprint, url, first_seen_at, last_seen_at, closed_at, closed_reason)
values
  ('f7e50000-0000-0000-0000-000000000012', 'pgtap-freshness-relisted',
   'https://example.test/relisted', now() - interval '9 days', now() - interval '2 days',
   now() - interval '1 day', 'absent_from_board'),
  ('f7e50000-0000-0000-0000-000000000013', 'pgtap-freshness-echoed',
   'https://example.test/echoed', now() - interval '9 days', now() - interval '2 days',
   now() - interval '1 day', 'absent_from_board');

insert into public.job_hunter_posting_staging
  (batch_id, ordinal, fingerprint, url, description, content_confidence)
values
  ('f7e5cccc-0000-0000-0000-000000000001', 0, 'pgtap-freshness-relisted',
   'https://example.test/relisted', 'Still hiring.', 'official_ats'),
  ('f7e5cccc-0000-0000-0000-000000000001', 1, 'pgtap-freshness-echoed',
   'https://example.test/echoed', 'Still hiring, says the aggregator.', 'aggregator_text');

select ok(
  (select count(*) = 2
     from public.job_hunter_merge_posting_batch('f7e5cccc-0000-0000-0000-000000000001')),
  'the crawl batch folds onto both closed postings');

select is(
  (select closed_at from public.job_hunter_postings
    where id = 'f7e50000-0000-0000-0000-000000000012'),
  null,
  'the employer''s own board listing a closed posting again reopens it');
select is(
  (select closed_reason from public.job_hunter_postings
    where id = 'f7e50000-0000-0000-0000-000000000012'),
  null,
  'a reopened posting no longer carries the reason it was closed');
select isnt(
  (select closed_at from public.job_hunter_postings
    where id = 'f7e50000-0000-0000-0000-000000000013'),
  null,
  'an aggregator still listing a closed posting does not reopen it');


select * from finish();
rollback;
