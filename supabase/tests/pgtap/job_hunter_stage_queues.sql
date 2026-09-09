-- Durable queue substrate for the four engine stages (issue #183).
--
-- These checks stay at the database seam: queue visibility, scheduling,
-- privileged access, and operational depth are properties of Postgres, not
-- of the Python adapter that calls it.
begin;
create extension if not exists pgtap with schema extensions;
select no_plan();

-- Extensions and one queue per stage -----------------------------------------

select has_extension('pgmq', 'the durable queue extension is installed');
select has_extension('pg_cron', 'the enqueue scheduler extension is installed');

select results_eq(
  $$ select queue_name::text
       from pgmq.meta
      where queue_name like 'job_hunter_%'
      order by queue_name $$,
  $$ values
       ('job_hunter_crawl_source'::text),
       ('job_hunter_extract_facets'::text),
       ('job_hunter_recheck_freshness'::text),
       ('job_hunter_resolve_persist'::text) $$,
  'each engine stage has exactly one durable queue');

select has_table('public', 'job_hunter_stage_attempts',
                 'retry attempts survive worker processes');
select has_table('public', 'job_hunter_stage_dead_letters',
                 'failed work has a durable dead-letter record');

select hasnt_column('public', 'job_hunter_stage_attempts', 'user_id',
                    'queue retry state is shared rather than per-user');
select hasnt_column('public', 'job_hunter_stage_dead_letters', 'user_id',
                    'dead letters are shared rather than per-user');

select is(
  (select relrowsecurity from pg_class
    where oid = 'public.job_hunter_stage_attempts'::regclass),
  true,
  'attempt metadata has row level security enabled');
select is_empty(
  $$ select polname from pg_policy
      where polrelid = 'public.job_hunter_stage_attempts'::regclass $$,
  'attempt metadata has no user policy');
select is(
  (select relrowsecurity from pg_class
    where oid = 'public.job_hunter_stage_dead_letters'::regclass),
  true,
  'dead letters have row level security enabled');
select is_empty(
  $$ select polname from pg_policy
      where polrelid = 'public.job_hunter_stage_dead_letters'::regclass $$,
  'dead letters have no user policy');

select is_empty(
  $$ select 1 where has_schema_privilege('authenticated', 'pgmq', 'usage') $$,
  'authenticated users cannot reach the queues');
select is_empty(
  $$ select 1 where has_schema_privilege('anon', 'pgmq', 'usage') $$,
  'anonymous users cannot reach the queues');
-- Row level security with no policy denies a non-owner, and the grants are
-- revoked as well, so neither half is load-bearing alone. Asserted for every
-- role a user can hold, against both operational tables, in the shape #179
-- fixed for shared tables: the refusal is the property, so it is proved
-- rather than inferred from the migration.
select is_empty(
  $$ select 1 where has_table_privilege('authenticated',
       'public.job_hunter_stage_attempts',
       'select, insert, update, delete') $$,
  'authenticated holds no privilege on attempt metadata');
select is_empty(
  $$ select 1 where has_table_privilege('anon',
       'public.job_hunter_stage_attempts',
       'select, insert, update, delete') $$,
  'nor does anon');
select is_empty(
  $$ select 1 where has_table_privilege('service_role',
       'public.job_hunter_stage_attempts',
       'select, insert, update, delete') $$,
  'nor service_role, which nothing in this application uses');

select is_empty(
  $$ select 1 where has_table_privilege('authenticated',
       'public.job_hunter_stage_dead_letters',
       'select, insert, update, delete') $$,
  'authenticated holds no privilege on dead letters');
select is_empty(
  $$ select 1 where has_table_privilege('anon',
       'public.job_hunter_stage_dead_letters',
       'select, insert, update, delete') $$,
  'nor does anon');
select is_empty(
  $$ select 1 where has_table_privilege(
       'service_role', 'public.job_hunter_stage_dead_letters',
       'select, insert, update, delete') $$,
  'the unused service role cannot reach dead letters');

-- The two stage functions are privileged-side too. Both are security invoker,
-- so a user who could execute one would run it as themselves and get nothing;
-- revoking execute means they cannot reach the queue state at all.
select is_empty(
  $$ select 1 where has_function_privilege('authenticated',
       'public.job_hunter_schedule_stage_enqueue(text, text, jsonb)',
       'execute') $$,
  'scheduling an enqueue is not a function a user may call');
select is_empty(
  $$ select 1 where has_function_privilege('anon',
       'public.job_hunter_schedule_stage_enqueue(text, text, jsonb)',
       'execute') $$,
  'nor may anon');
select is_empty(
  $$ select 1 where has_function_privilege('authenticated',
       'public.job_hunter_stage_queue_metrics()', 'execute') $$,
  'nor is reading queue depth');
select is_empty(
  $$ select 1 where has_function_privilege('anon',
       'public.job_hunter_stage_queue_metrics()', 'execute') $$,
  'nor may anon read it');

-- Visibility timeout and crash recovery -------------------------------------

create temporary table claimed_message as
select * from pgmq.send(
  'job_hunter_resolve_persist',
  '{"batch_id":"18300000-0000-0000-0000-000000000001"}'::jsonb
) as msg_id;

create temporary table first_claim as
select * from pgmq.read('job_hunter_resolve_persist', 60, 1);

select is(
  (select count(*)::int from first_claim),
  1,
  'a visible resolve_persist message can be claimed');
select is_empty(
  $$ select * from pgmq.read('job_hunter_resolve_persist', 60, 1) $$,
  'a claimed message is hidden rather than removed');

select * from pgmq.set_vt(
  'job_hunter_resolve_persist',
  (select msg_id from claimed_message),
  0
);

select is(
  (select count(*)::int
     from pgmq.read('job_hunter_resolve_persist', 60, 1)),
  1,
  'unfinished work becomes visible again after its visibility timeout');

-- pg_cron only enqueues -------------------------------------------------------

select lives_ok(
  $$ select public.job_hunter_schedule_stage_enqueue(
       'crawl_source', '* * * * *', '{"source":"test"}'::jsonb) $$,
  'a stage enqueue can be scheduled');

select alike(
  (select command from cron.job
    where jobname = 'job-hunter-enqueue-crawl-source'),
  'select pgmq.send(%',
  'the scheduled command only sends a queue message');
select unalike(
  (select command from cron.job
    where jobname = 'job-hunter-enqueue-crawl-source'),
  '%job_hunter_merge_posting_batch%',
  'pg_cron performs no resolve_persist work');

-- Operational metrics, with no user identity anywhere in the path -----------

-- Cleared first, inside this transaction, so the assertion below is about the
-- one row this test writes. The dead-letter table is shared operational state
-- with no user dimension and nothing truncates it between runs, so a Job
-- Hunter suite that legitimately dead-lettered a message -- a facet response
-- with a seniority outside the vocabulary, say -- leaves rows behind that make
-- a global "depth is zero" assertion fail for a reason that has nothing to do
-- with the metrics function. The whole file rolls back, so this deletes
-- nothing anyone keeps, and `pnpm db:test` holds the stack lock exclusively,
-- so there is no concurrent writer to block.
delete from public.job_hunter_stage_dead_letters;

insert into public.job_hunter_stage_dead_letters
  (stage, message_id, payload, failure_class, error, attempt_count)
values
  ('resolve_persist', 18301, '{"batch_id":"failed"}', 'permanent',
   'invalid batch id', 1);

select results_eq(
  $$ select stage, dead_letter_depth
       from public.job_hunter_stage_queue_metrics()
      order by stage $$,
  $$ values
       ('crawl_source'::text, 0::bigint),
       ('extract_facets'::text, 0::bigint),
       ('recheck_freshness'::text, 0::bigint),
       ('resolve_persist'::text, 1::bigint) $$,
  'dead-letter depth is observable per stage');

select is(
  (select count(*)::int from public.job_hunter_stage_queue_metrics()),
  4,
  'queue depth is observable for all four stages');

select * from finish();
rollback;
