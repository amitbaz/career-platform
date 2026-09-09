-- Durable substrate for the four ingestion/enrichment stages (issue #183).
--
-- Queue messages and their operational state are platform-owned: they carry
-- no user identity and are reachable only through ingestion's privileged
-- Postgres connection.  pg_cron stores enqueue schedules; it never performs
-- stage work itself.

create extension if not exists pgmq;
create extension if not exists pg_cron;

select pgmq.create('job_hunter_crawl_source');
select pgmq.create('job_hunter_resolve_persist');
select pgmq.create('job_hunter_extract_facets');
select pgmq.create('job_hunter_recheck_freshness');

-- The extension defaults its functions to executable by public.  This app
-- deliberately uses pgmq only through the privileged ingestion connection.
revoke all on schema pgmq from public, anon, authenticated, service_role;
revoke all on all functions in schema pgmq
  from public, anon, authenticated, service_role;
revoke all on schema cron from public, anon, authenticated, service_role;
revoke all on all functions in schema cron
  from public, anon, authenticated, service_role;

create table public.job_hunter_stage_attempts (
  stage text not null check (stage in (
    'crawl_source', 'resolve_persist', 'extract_facets', 'recheck_freshness'
  )),
  message_id bigint not null,
  attempt_count smallint not null default 0 check (attempt_count >= 0),
  updated_at timestamptz not null default now(),
  primary key (stage, message_id)
);

comment on table public.job_hunter_stage_attempts is
  'Failures charged to a stage message (issue #183). pgmq read_ct counts '
  'claims, including quota pauses and crash recovery; this table counts only '
  'attempts attributable to the message and therefore drives retry limits.';

create table public.job_hunter_stage_dead_letters (
  stage text not null check (stage in (
    'crawl_source', 'resolve_persist', 'extract_facets', 'recheck_freshness'
  )),
  message_id bigint not null,
  payload jsonb not null check (jsonb_typeof(payload) = 'object'),
  failure_class text not null check (failure_class in ('transient', 'permanent')),
  error text not null,
  attempt_count smallint not null check (attempt_count > 0),
  failed_at timestamptz not null default now(),
  primary key (stage, message_id)
);

comment on table public.job_hunter_stage_dead_letters is
  'Stage messages that cannot be completed: permanent failures immediately '
  'and transient failures after their configured attempt limit (issue #183).';

-- Row level security on, with no policy, on purpose -- not an oversight and
-- not an unfinished per-user table.
--
-- Job Hunter has two kinds of table with no user_id, and they take opposite
-- policy shapes. Shared *knowledge* -- a posting, its facets, a company -- is
-- what an advertisement says to everybody, so #179 fixed its shape as: select
-- open to authenticated, writes revoked from every role a user can hold, and
-- a pgTAP test proving the refusal. Shared *machinery* -- these two tables,
-- and job_hunter_platform_ai_usage and job_hunter_platform_ai_quota_state
-- before them -- is operational state no user reads at all, so it takes the
-- platform-ledger shape instead: no policy, so row level security denies
-- every non-owner, and the grants revoked as well, so neither half is
-- load-bearing alone. Reaching for #179's "select open to authenticated" here
-- would hand a user the engine's retry and failure state, which tells them
-- nothing they can use and is not theirs to see.
--
-- job_hunter_stage_queues.sql asserts the refusal rather than inferring it
-- from these two statements.
alter table public.job_hunter_stage_attempts enable row level security;
alter table public.job_hunter_stage_dead_letters enable row level security;

revoke all on table public.job_hunter_stage_attempts
  from anon, authenticated, service_role;
revoke all on table public.job_hunter_stage_dead_letters
  from anon, authenticated, service_role;

create or replace function public.job_hunter_schedule_stage_enqueue(
  p_stage text,
  p_schedule text,
  p_payload jsonb default '{}'::jsonb
)
returns bigint
language plpgsql
security invoker
set search_path = ''
as $$
declare
  v_queue_name text;
  v_job_id bigint;
begin
  v_queue_name := case p_stage
    when 'crawl_source' then 'job_hunter_crawl_source'
    when 'resolve_persist' then 'job_hunter_resolve_persist'
    when 'extract_facets' then 'job_hunter_extract_facets'
    when 'recheck_freshness' then 'job_hunter_recheck_freshness'
    else null
  end;

  if v_queue_name is null then
    raise exception 'unknown job hunter stage: %', p_stage
      using errcode = '22023';
  end if;
  if jsonb_typeof(p_payload) <> 'object' then
    raise exception 'stage payload must be a JSON object'
      using errcode = '22023';
  end if;

  select cron.schedule(
    'job-hunter-enqueue-' || replace(p_stage, '_', '-'),
    p_schedule,
    format('select pgmq.send(%L, %L::jsonb);', v_queue_name, p_payload::text)
  ) into v_job_id;
  return v_job_id;
end;
$$;

comment on function public.job_hunter_schedule_stage_enqueue(text, text, jsonb) is
  'Store a pg_cron schedule whose whole command is one pgmq.send. The cron '
  'session enqueues due work and never performs stage work itself (issue #183).';

create or replace function public.job_hunter_stage_queue_metrics()
returns table (
  stage text,
  queue_depth bigint,
  visible_depth bigint,
  dead_letter_depth bigint
)
language sql
security invoker
set search_path = ''
as $$
  select s.stage,
         m.queue_length,
         m.queue_visible_length,
         (select count(*)
            from public.job_hunter_stage_dead_letters d
           where d.stage = s.stage)
    from (values
      ('crawl_source'::text, 'job_hunter_crawl_source'::text),
      ('resolve_persist', 'job_hunter_resolve_persist'),
      ('extract_facets', 'job_hunter_extract_facets'),
      ('recheck_freshness', 'job_hunter_recheck_freshness')
    ) as s(stage, queue_name)
    cross join lateral pgmq.metrics(s.queue_name) m;
$$;

comment on function public.job_hunter_stage_queue_metrics() is
  'Queue, currently-visible, and dead-letter depth for every engine stage. '
  'The query has no user dimension and works with zero auth users (issue #183).';

revoke all on function public.job_hunter_schedule_stage_enqueue(text, text, jsonb)
  from public, anon, authenticated, service_role;
revoke all on function public.job_hunter_stage_queue_metrics()
  from public, anon, authenticated, service_role;
