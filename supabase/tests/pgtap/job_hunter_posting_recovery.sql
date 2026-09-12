-- Recoverable unresolved-posting enrichment (issue #259).
--
-- The schedule half lives here: the interval, the trigger that keeps
-- recovery_next_attempt_at current, which postings the cron tick sends to
-- the recover_posting queue, and the analytics backlog read. What one
-- attempt does with one posting is the stage's seam, in
-- apps/job-hunter/tests/test_recover_posting_stage.py.
begin;
create extension if not exists pgtap with schema extensions;
select no_plan();

-- Every other posting on the shared local stack is pushed out of the way
-- first: the enqueuer reads the whole table, and a posting some other test
-- left behind is due too. Rolled back with everything else.
update public.job_hunter_postings
   set recovery_next_attempt_at = now() + interval '30 days'
 where recovery_next_attempt_at is not null;
delete from pgmq.q_job_hunter_recover_posting;


-- 1. Columns --------------------------------------------------------------------

select has_column('public', 'job_hunter_postings', 'recovery_attempts',
  'a posting counts how many recovery attempts have completed against it');
select has_column('public', 'job_hunter_postings', 'recovery_last_attempt_at',
  'a posting records when the last attempt completed');
select has_column('public', 'job_hunter_postings', 'recovery_last_outcome',
  'a posting records what the last attempt found');
select has_column('public', 'job_hunter_postings', 'recovery_next_attempt_at',
  'a posting records when it is next due');


-- 2. The interval is aggressive for a day, then decays, capped -------------------

select is(public.job_hunter_recovery_interval(interval '1 hour'), interval '20 minutes',
  'a posting found an hour ago is retried on the aggressive interval');
select is(public.job_hunter_recovery_interval(interval '23 hours 59 minutes'), interval '20 minutes',
  'still aggressive right up to the window''s edge');
select is(public.job_hunter_recovery_interval(interval '36 hours'), interval '40 minutes',
  'one full doubling period past the window, the interval has doubled');
select is(public.job_hunter_recovery_interval(interval '400 days'), interval '7 days',
  'an old, still-unresolved posting is retried weekly, never less often');
select ok(
  public.job_hunter_recovery_interval(interval '10 days')
    > public.job_hunter_recovery_interval(interval '2 days'),
  'a posting further past the aggressive window waits longer between attempts');


-- 3. The trigger: due at once on insert, reset on change, cleared on success ------

insert into public.job_hunter_postings
  (id, fingerprint, url, company, content_confidence, first_seen_at, last_seen_at)
values ('f7e60000-0000-0000-0000-000000000001', 'pgtap-recovery-new',
        'https://example.test/new', 'Acme', 'partial_unknown', now(), now());

select ok(
  (select recovery_next_attempt_at <= now() from public.job_hunter_postings
    where id = 'f7e60000-0000-0000-0000-000000000001'),
  'a newly discovered insufficient posting is due for recovery at once');

insert into public.job_hunter_postings
  (id, fingerprint, url, company, content_confidence, first_seen_at, last_seen_at)
values ('f7e60000-0000-0000-0000-000000000002', 'pgtap-recovery-sufficient',
        'https://example.test/sufficient', 'Acme', 'official_ats', now(), now());

select is(
  (select recovery_next_attempt_at from public.job_hunter_postings
    where id = 'f7e60000-0000-0000-0000-000000000002'),
  null,
  'a posting with sufficient content from the start needs no recovery schedule');

update public.job_hunter_postings
   set recovery_next_attempt_at = now() + interval '20 minutes',
       recovery_attempts = 1, recovery_last_attempt_at = now(),
       recovery_last_outcome = 'unresolved'
 where id = 'f7e60000-0000-0000-0000-000000000001';

select ok(
  (select recovery_next_attempt_at > now() + interval '10 minutes'
     from public.job_hunter_postings where id = 'f7e60000-0000-0000-0000-000000000001'),
  'a completed, still-unresolved attempt''s own schedule is left alone by the trigger');

-- A merge that changes the description while the posting stays insufficient
-- (a richer source variant, still too thin to be sufficient) resets the
-- schedule to now, immediately -- AC3.
update public.job_hunter_postings
   set description = 'a few more words', description_hash = 'changed-hash'
 where id = 'f7e60000-0000-0000-0000-000000000001';

select ok(
  (select recovery_next_attempt_at <= now() from public.job_hunter_postings
    where id = 'f7e60000-0000-0000-0000-000000000001'),
  'a changed description while still insufficient makes the posting due again immediately');

update public.job_hunter_postings
   set recovery_next_attempt_at = now() + interval '20 minutes'
 where id = 'f7e60000-0000-0000-0000-000000000001';

select ok(
  (select recovery_next_attempt_at > now() + interval '10 minutes'
     from public.job_hunter_postings where id = 'f7e60000-0000-0000-0000-000000000001'),
  'sanity: the schedule set just above actually stuck before the next check');

-- Content becoming sufficient clears the schedule -- AC7.
update public.job_hunter_postings
   set content_confidence = 'official_ats'
 where id = 'f7e60000-0000-0000-0000-000000000001';

select is(
  (select recovery_next_attempt_at from public.job_hunter_postings
    where id = 'f7e60000-0000-0000-0000-000000000001'),
  null,
  'a posting whose content just became sufficient needs no more recovery');


-- 4. Arbitrary retry count never becomes a permanent exclusion (AC9) -------------
--
-- job_hunter_recovery_interval and job_hunter_content_confidence_sufficient
-- (which is what job_hunter_match_state_counts's `unresolved` classification
-- reads) take content_confidence and age alone -- neither one's signature nor
-- its body can see recovery_attempts at all, so no retry count can reach
-- either. A posting that has failed five hundred times is still merely
-- unresolved, and is still due again -- never permanently excluded.

update public.job_hunter_postings
   set content_confidence = 'partial_unknown',
       recovery_attempts = 500,
       recovery_last_outcome = 'unresolved',
       recovery_next_attempt_at = now() - interval '1 minute'
 where id = 'f7e60000-0000-0000-0000-000000000001';

select ok(
  public.job_hunter_recovery_interval(interval '400 days') < interval '8 days',
  'the interval has a fixed ceiling regardless of attempt count');
select ok(
  not public.job_hunter_content_confidence_sufficient(
    (select content_confidence from public.job_hunter_postings
      where id = 'f7e60000-0000-0000-0000-000000000001')),
  'five hundred failed attempts still read as merely unresolved, not excluded');
select ok(
  (select recovery_next_attempt_at is not null and recovery_next_attempt_at <= now()
     from public.job_hunter_postings where id = 'f7e60000-0000-0000-0000-000000000001'),
  'and the posting is still due for another attempt -- no attempt count ever stops scheduling it');


-- 5. The enqueuer sends only what is due, open, insufficient and checkable --------

-- Sections 3 and 4's postings are done with; left in place they would also
-- read as due, open, insufficient and checkable and inflate this count.
delete from public.job_hunter_postings
 where id in ('f7e60000-0000-0000-0000-000000000001', 'f7e60000-0000-0000-0000-000000000002');

insert into public.job_hunter_postings
  (id, fingerprint, url, company, content_confidence,
   first_seen_at, last_seen_at, recovery_next_attempt_at, closed_at)
values
  -- due, open, insufficient, has a URL: the only one that should be sent
  ('f7e60000-0000-0000-0000-000000000010', 'pgtap-recovery-due',
   'https://example.test/due', '', 'partial_unknown',
   now(), now(), now() - interval '1 minute', null),
  -- not due yet
  ('f7e60000-0000-0000-0000-000000000011', 'pgtap-recovery-later',
   'https://example.test/later', '', 'partial_unknown',
   now(), now(), now() + interval '1 day', null),
  -- due but already sufficient (a race the enqueuer must still exclude)
  ('f7e60000-0000-0000-0000-000000000012', 'pgtap-recovery-sufficient-race',
   'https://example.test/sufficient-race', '', 'official_ats',
   now(), now(), now() - interval '1 minute', null),
  -- due but closed
  ('f7e60000-0000-0000-0000-000000000013', 'pgtap-recovery-closed',
   'https://example.test/closed', '', 'partial_unknown',
   now(), now(), now() - interval '1 minute', now()),
  -- due but uncheckable: nothing to fetch and no company name either
  ('f7e60000-0000-0000-0000-000000000014', 'pgtap-recovery-nowhere',
   '', '', 'partial_unknown',
   now(), now(), now() - interval '1 minute', null);

select is(public.job_hunter_enqueue_due_recover_posting(100), 1,
  'the tick enqueues exactly the due, open, insufficient, checkable posting');

select is(
  (select message->>'posting_id' from pgmq.q_job_hunter_recover_posting),
  'f7e60000-0000-0000-0000-000000000010',
  'the message names the one posting that qualified');

select is(
  (select count(*)::int from pgmq.q_job_hunter_recover_posting
    where message - 'posting_id' <> '{}'::jsonb),
  0,
  'a recovery message carries a posting id and no user identity');

select ok(
  (select recovery_next_attempt_at > now() + interval '12 hours'
     from public.job_hunter_postings where id = 'f7e60000-0000-0000-0000-000000000010'),
  'an enqueued posting is leased, so the next tick does not resend it');

update public.job_hunter_postings
   set recovery_next_attempt_at = now() - interval '1 minute'
 where id = 'f7e60000-0000-0000-0000-000000000010';

select is(public.job_hunter_enqueue_due_recover_posting(100), 0,
  'a posting already waiting on the queue is not enqueued twice');


-- 6. Only ingestion can run the cron functions ------------------------------------

select is_empty(
  $$ select 1 where has_function_privilege('authenticated',
       'public.job_hunter_enqueue_due_recover_posting(integer)', 'execute') $$,
  'a user cannot enqueue recovery attempts');
select is_empty(
  $$ select 1 where has_function_privilege('anon',
       'public.job_hunter_enqueue_due_recover_posting(integer)', 'execute') $$,
  'an anonymous caller cannot enqueue recovery attempts');
select is_empty(
  $$ select 1 where has_function_privilege('authenticated',
       'public.job_hunter_recovery_backlog(timestamptz)', 'execute') $$,
  'a user cannot read the recovery backlog directly (#264 grants a staff role later)');


-- 7. The tick is scheduled --------------------------------------------------------

select is(
  (select count(*)::int from cron.job
    where jobname = 'job-hunter-enqueue-recover-posting'
      and command like '%job_hunter_enqueue_due_recover_posting%'),
  1,
  'pg_cron enqueues due recovery attempts, and only enqueues');


-- 8. The analytics backlog counts age and outcome, not rows -----------------------

delete from public.job_hunter_postings where fingerprint like 'pgtap-recovery-backlog-%';
insert into public.job_hunter_postings
  (fingerprint, url, content_confidence, first_seen_at, last_seen_at, recovery_last_outcome)
values
  ('pgtap-recovery-backlog-fresh', 'https://example.test/backlog-fresh', 'partial_unknown',
   now() - interval '1 hour', now(), ''),
  ('pgtap-recovery-backlog-old', 'https://example.test/backlog-old', '',
   now() - interval '10 days', now(), 'unresolved');

select ok(
  (select count > 0 from public.job_hunter_recovery_backlog()
    where age_bucket = 'under_24h' and recovery_last_outcome = ''),
  'a posting less than a day old is counted in the freshest age bucket');
select ok(
  (select count > 0 from public.job_hunter_recovery_backlog()
    where age_bucket = 'over_7d' and recovery_last_outcome = 'unresolved'),
  'an old posting that has been tried and failed is counted in the oldest bucket');


select * from finish();
rollback;
