-- Sources become an entity, and crawling follows measured novelty (issue #184).
--
-- PLACEHOLDER is deliberately not a timestamp. Migration timestamps are
-- allocated at pull-request open, in merge order (PR #202).

-- The source registry ------------------------------------------------------
--
-- Until now a source was a bare text label on a posting and a Python class
-- with a `source_label`. Two of this ticket's requirements need a row: a
-- source has a *kind*, and a source can oblige a surface to display
-- something.
--
-- `kind` exists because a licensed API is a first-class source kind and not
-- a variant of a crawl. Its rate accounting is a contractual quota of calls
-- rather than a politeness delay, its cadence is bounded by that call budget
-- rather than by how hard we dare hit it, and its rows overlap heavily with
-- scraped rows for the same posting -- which the fingerprint already handles.
-- Carrying `kind` from the start is what stops "source" from silently
-- meaning "crawl". No licensed source is enabled here; the shape is present
-- and the portfolio stays scraped.
--
-- `display_credit` is NOT called `attribution`. That word already means
-- market attribution throughout `discovery.py` -- `_cheap_market_attribution`,
-- `_record_reattribution`, `stats.reattributed_*` -- and a second meaning on
-- one word would be misread by everybody including us. "Credit" is the
-- licensing term of art and covers both halves of the obligation: the
-- required text and the required badge.
--
-- Shape of `display_credit`, written as the general rule rather than as one
-- provider's special case:
--
--   {"required": true,
--    "text": "Jobs by Adzuna",
--    "link_text": "Jobs",
--    "link_url": "https://...",
--    "badge_url": "https://.../logo.png",
--    "badge_min_px": [116, 23]}
--
-- A surface that cannot render `badge_url` -- Telegram's sendMessage has no
-- image entity -- contributes `text` and `link_url` and never the advert
-- body. #188 is where that is read.
--
-- This is shared *knowledge* in the #179 sense: what a source says is true
-- for everybody, so select is open to authenticated and every write is
-- revoked from every role a user can hold.
create table public.job_hunter_sources (
  id uuid primary key default gen_random_uuid(),
  source_key text not null unique,
  kind text not null default 'crawl' check (kind in ('crawl', 'licensed')),
  display_credit jsonb not null default '{}'::jsonb
    check (jsonb_typeof(display_credit) = 'object'),
  enabled boolean not null default true,
  first_seen_at timestamptz not null default now(),
  created_at timestamptz not null default now()
);

comment on table public.job_hunter_sources is
  'Every source the engine crawls or licenses, keyed by the same string the '
  'Python adapters expose as source_label (issue #184). Carries the source '
  'kind and any display obligation the source imposes on a surface.';
comment on column public.job_hunter_sources.display_credit is
  'What a surface is obliged to display for a posting from this source. A '
  'property of the source, never of the reader: nothing in the path that '
  'resolves it takes a user_id. Empty object means no obligation.';

alter table public.job_hunter_sources enable row level security;
create policy select_authenticated on public.job_hunter_sources
  for select to authenticated using (true);
revoke insert, update, delete on public.job_hunter_sources
  from anon, authenticated, service_role;

-- Seed the registry from the corpus the crawl has already produced, so a
-- source that exists in postings has a row without anyone typing it in.
insert into public.job_hunter_sources (source_key, first_seen_at)
select distinct p.source, min(p.first_seen_at)
  from public.job_hunter_postings p
 where coalesce(p.source, '') <> ''
 group by p.source
on conflict (source_key) do nothing;

-- The display obligation, resolved with no user identity in the path -------
--
-- security definer because job_hunter_sources' select policy is open to
-- authenticated but the join runs from a posting, and a surface rendering a
-- digest should not need a session at all. It takes a posting id and
-- nothing else: there is deliberately no overload that accepts a user.
create or replace function public.job_hunter_posting_display_credit(
  p_posting_id uuid
)
returns jsonb
language sql
security definer
stable
set search_path = ''
as $$
  select case
           when s.display_credit = '{}'::jsonb then null
           else s.display_credit
         end
    from public.job_hunter_postings p
    join public.job_hunter_sources s on s.source_key = p.source
   where p.id = p_posting_id;
$$;

comment on function public.job_hunter_posting_display_credit(uuid) is
  'What a surface must display alongside this posting, or null when its '
  'source imposes nothing. No user_id anywhere in the path -- the obligation '
  'belongs to the posting''s source and not to whoever is reading it '
  '(issue #184, read by #188).';

grant execute on function public.job_hunter_posting_display_credit(uuid)
  to authenticated;

-- The crawl ledger ---------------------------------------------------------
--
-- The shared, identity-free yield signal the scheduler bands on.
--
-- "Measured yield" split in two when #203 landed. Board health became shared
-- knowledge on job_hunter_ats_boards; eligible_jobs_seen and last_eligible_at
-- stayed per-user on job_hunter_ats_registry, and that migration's comment
-- says "#184 is built on it". That line is stale, for two reasons.
--
-- Mechanically: the crawl_source scheduler runs as the privileged owning
-- role with no auth.uid() and cannot read a per-user column. Threading a
-- user through to reach one is the mistake #174 and #175 spent four tickets
-- undoing.
--
-- And substantively: with one user, an aggregate of per-user eligibility IS
-- that user's search profile. The engine would learn to visit only the
-- boards matching the current job hunt, and would then present a narrowing
-- corpus as a quiet job market. #203 already warns that a shared row
-- carrying one user's yield misleads budgeting for everyone else; the same
-- argument applied to scheduling gives the same answer.
--
-- So the signal here is corpus novelty: how much of what a source returned
-- the corpus did not already have. Same measure for every user, scales with
-- jobs rather than with subscribers.
--
-- Shared *machinery* in the #183 sense, not shared knowledge: this is the
-- engine's own operational state, no user reads it, so row level security is
-- on with no policy and the grants are revoked as well. Neither half is
-- load-bearing alone.
create table public.job_hunter_source_crawls (
  id uuid primary key default gen_random_uuid(),
  source_key text not null,
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  outcome text not null check (outcome in (
    'fetched', 'not_modified', 'rate_limited', 'failed'
  )),
  fetched integer not null default 0 check (fetched >= 0),
  new_to_corpus integer not null default 0 check (new_to_corpus >= 0),
  changed integer not null default 0 check (changed >= 0),
  unchanged_by_hash integer not null default 0 check (unchanged_by_hash >= 0),
  requests integer not null default 0 check (requests >= 0),
  elapsed_ms integer not null default 0 check (elapsed_ms >= 0),
  error text not null default ''
);

comment on table public.job_hunter_source_crawls is
  'One row per crawl attempt, written unconditionally -- especially for the '
  'attempt that produced nothing. `outcome` is what stops a stalled source '
  'or a schedule that never fired from presenting as "nothing new today" '
  '(issue #184).';

-- The scheduler's only read: this source's recent crawls, newest first.
create index job_hunter_source_crawls_recent_idx
  on public.job_hunter_source_crawls (source_key, started_at desc);

-- The crawl cursor ---------------------------------------------------------
--
-- Held apart from job_hunter_sources because the two take different policy
-- shapes: the registry is knowledge a user may read, this is hot machinery
-- nobody reads. Folding them would force one shape onto both.
--
-- For most of the current portfolio -- remotive, arbeitnow, jobicy,
-- himalayas, remoteok, weworkremotely, lever, greenhouse, ashby -- there is
-- no pagination cursor to resume from: the whole board arrives in one GET.
-- For those the honest cursor is the HTTP validator, which is what makes the
-- *next* fetch conditional and therefore cheap. `high_water_at` serves the
-- sources that accept a since-style parameter. Inventing a synthetic cursor
-- for a source that has none would satisfy a checkbox and change no cost.
create table public.job_hunter_source_cursors (
  source_key text primary key,
  etag text not null default '',
  last_modified text not null default '',
  high_water_at timestamptz,
  updated_at timestamptz not null default now()
);

comment on table public.job_hunter_source_cursors is
  'Where each source resumes from: its HTTP cache validators, and a '
  'high-water timestamp for the sources that accept one (issue #184).';

alter table public.job_hunter_source_crawls enable row level security;
alter table public.job_hunter_source_cursors enable row level security;

revoke all on table public.job_hunter_source_crawls
  from public, anon, authenticated, service_role;
revoke all on table public.job_hunter_source_cursors
  from public, anon, authenticated, service_role;

-- One cron entry per source, not one per stage -----------------------------
--
-- #183 shipped job_hunter_schedule_stage_enqueue deriving its cron job name
-- from the stage alone:
--
--   cron.schedule('job-hunter-enqueue-' || replace(p_stage, '_', '-'), ...)
--
-- cron.schedule replaces by name. This ticket installs one schedule per
-- source, so under that name every schedule would overwrite the last and
-- exactly one source would ever be crawled. Nothing raises; the corpus just
-- stops growing, which reads as a quiet job market. The pgTAP asserting the
-- literal stage-derived name locked it in, so the fix is the signature and
-- the assertion together.
create or replace function public.job_hunter_source_schedule_slug(p_key text)
returns text
language sql
immutable
set search_path = ''
as $$
  -- Lower-cased, punctuation collapsed to single hyphens, trimmed, for a
  -- human-readable prefix. Collapsing every run of non-alnum characters to
  -- one hyphen is not injective -- 'lever:acme', 'lever.acme', 'lever_acme'
  -- and 'lever acme' all collapse to 'lever-acme', and real ATS board
  -- tokens use both '-' and '_' -- so the hash of the ORIGINAL key (never
  -- the collapsed slug) is appended unconditionally, not only past a length
  -- threshold, to guarantee two different keys never produce the same job
  -- name. Only a key with no alnum characters at all (or the empty string)
  -- yields '', which the caller treats as "no usable job-name form".
  select case
           when v.slug = '' then ''
           else left(v.slug, 31) || '-' ||
                left(encode(sha256(convert_to(p_key, 'UTF8')), 'hex'), 8)
         end
    from (
      select trim(both '-' from
               regexp_replace(lower(coalesce(p_key, '')), '[^a-z0-9]+', '-', 'g')
             ) as slug
    ) v;
$$;

comment on function public.job_hunter_source_schedule_slug(text) is
  'A source key rendered as a pg_cron job-name fragment: a lower-cased, '
  'punctuation-collapsed prefix plus an unconditional hash of the original '
  'key, since the collapsed prefix alone is not injective (issue #184).';

drop function if exists public.job_hunter_schedule_stage_enqueue(text, text, jsonb);

create or replace function public.job_hunter_schedule_stage_enqueue(
  p_stage text,
  p_schedule text,
  p_payload jsonb default '{}'::jsonb,
  p_schedule_key text default null
)
returns bigint
language plpgsql
security invoker
set search_path = ''
as $$
declare
  v_queue_name text;
  v_job_name text;
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

  v_job_name := 'job-hunter-enqueue-' || replace(p_stage, '_', '-');
  if p_schedule_key is not null then
    if public.job_hunter_source_schedule_slug(p_schedule_key) = '' then
      raise exception 'schedule key % has no usable job-name form', p_schedule_key
        using errcode = '22023';
    end if;
    v_job_name := v_job_name || '-'
      || public.job_hunter_source_schedule_slug(p_schedule_key);
  end if;

  select cron.schedule(
    v_job_name,
    p_schedule,
    format('select pgmq.send(%L, %L::jsonb);', v_queue_name, p_payload::text)
  ) into v_job_id;
  return v_job_id;
end;
$$;

comment on function public.job_hunter_schedule_stage_enqueue(text, text, jsonb, text) is
  'Store a pg_cron schedule whose whole command is one pgmq.send. The cron '
  'session enqueues due work and never performs stage work itself (#183). '
  'p_schedule_key names one schedule within a stage: cron.schedule replaces '
  'by name, so without it N per-source schedules collapse into one and '
  'exactly one source is ever visited (#184).';

revoke all on function
  public.job_hunter_schedule_stage_enqueue(text, text, jsonb, text)
  from public, anon, authenticated, service_role;
revoke all on function public.job_hunter_source_schedule_slug(text)
  from public, anon, authenticated, service_role;

-- The external search allowance becomes a platform ledger ------------------
--
-- Owner's decision on #184. job_hunter_search_api_usage was per-user, but a
-- shared crawl runs as the privileged role with no user identity and then
-- the budget has no user to charge. A per-user ledger has exactly two
-- possible behaviours and both are wrong: charge one arbitrary user for
-- everyone's crawl, or fan the crawl out per user and burn the same cap N
-- times for identical results. The quota belongs to the API key -- Adzuna's
-- is 2,500 calls a month on the key -- so the budget scales with postings
-- rather than with subscribers, which is the same shape as shared
-- extraction.
--
-- Keeping both ledgers was explicitly rejected: two ledgers over one key is
-- a bug already live on the Gemini side, where two of them jointly authorise
-- about 160% of the key's real quota. Per-user search accounting returns if
-- and when a user-triggered search exists, and not before.
--
-- Shape copied from job_hunter_platform_ai_usage: the runner claim, and no
-- delete policy, because a ledger that can be rewritten is not a ledger.
create table public.job_hunter_platform_search_usage (
  id uuid primary key default gen_random_uuid(),
  provider text not null,
  occurred_at timestamptz not null,
  created_at timestamptz not null default now(),
  constraint job_hunter_platform_search_usage_provider_at_key
    unique (provider, occurred_at)
);

create index job_hunter_platform_search_usage_window_idx
  on public.job_hunter_platform_search_usage (provider, occurred_at desc);

comment on table public.job_hunter_platform_search_usage is
  'Consumption of the platform-owned external search keys (issue #184). '
  'Deliberately not keyed by user_id: the key has one allowance and the '
  'crawl it pays for belongs to no one user.';

alter table public.job_hunter_platform_search_usage enable row level security;

create policy runner_select on public.job_hunter_platform_search_usage
  for select to authenticated
  using (coalesce((select auth.jwt() -> 'job_hunter_runner'), 'false'::jsonb) = 'true'::jsonb);
create policy runner_insert on public.job_hunter_platform_search_usage
  for insert to authenticated
  with check (coalesce((select auth.jwt() -> 'job_hunter_runner'), 'false'::jsonb) = 'true'::jsonb);
-- Update is what makes the ledger's upsert converge rather than fail on a
-- duplicate key; it is not an invitation to rewrite history.
create policy runner_update on public.job_hunter_platform_search_usage
  for update to authenticated
  using (coalesce((select auth.jwt() -> 'job_hunter_runner'), 'false'::jsonb) = 'true'::jsonb)
  with check (coalesce((select auth.jwt() -> 'job_hunter_runner'), 'false'::jsonb) = 'true'::jsonb);

-- Carry this month's spend across.
--
-- The owner's call, and the reason: the Brave monthly cap is drawn against
-- the key, so calls already spent this month are real spend no matter whose
-- row recorded them. Starting the platform ledger empty would hand the
-- engine a fresh 1,000-query allowance on a key already drawn down.
--
-- Distinct users who happened to record the same occurred_at collapse to one
-- row, which is correct: the ledger counts calls against the key, and two
-- rows at one microsecond were one reservation being retried.
insert into public.job_hunter_platform_search_usage (provider, occurred_at, created_at)
select provider, occurred_at, min(created_at)
  from public.job_hunter_search_api_usage
 group by provider, occurred_at
on conflict (provider, occurred_at) do nothing;

drop table public.job_hunter_search_api_usage;

-- The yield-driven scheduler -----------------------------------------------
--
-- Reads the crawl ledger, bands each enabled source on its recent novelty,
-- and installs one pg_cron entry per source through the fixed helper. No
-- operator sets these frequencies: a source earns a faster band by producing
-- material the corpus did not already have, and loses one by producing none.
-- Configuration may pin one source as an override; it is never the
-- mechanism.
--
-- The band ladder is mirrored in `source_schedule.py`, where the same policy
-- is unit-tested without a database. The two must agree; the Python side is
-- the one with the exhaustive tests.
create or replace function public.job_hunter_reschedule_sources()
returns integer
language plpgsql
security invoker
set search_path = ''
as $$
declare
  v_bands int[] := array[15, 60, 360, 1440, 4320, 10080];
  v_source record;
  v_index int;
  v_minute int;
  v_hour int;
  v_schedule text;
  v_count int := 0;
begin
  -- Unschedule everything this function owns first, so a source that has
  -- been disabled or removed stops firing rather than being left behind by
  -- a loop that only ever adds.
  for v_source in
    select jobname from cron.job
     where jobname like 'job-hunter-enqueue-crawl-source-%'
  loop
    perform cron.unschedule(v_source.jobname);
  end loop;

  for v_source in
    select s.source_key,
           coalesce((
             select count(*)
               from (
                 select c.outcome, c.new_to_corpus + c.changed as novelty
                   from public.job_hunter_source_crawls c
                  where c.source_key = s.source_key
                  order by c.started_at desc
                  limit 6
               ) recent
              where recent.outcome in ('rate_limited', 'failed')
                 or recent.novelty = 0
           ), 0) as demotions,
           coalesce((
             select count(*)
               from (
                 select c.outcome, c.new_to_corpus + c.changed as novelty
                   from public.job_hunter_source_crawls c
                  where c.source_key = s.source_key
                  order by c.started_at desc
                  limit 6
               ) recent
              where recent.outcome not in ('rate_limited', 'failed')
                and recent.novelty > 0
           ), 0) as promotions
      from public.job_hunter_sources s
     where s.enabled
  loop
    -- A source with no history starts in the middle of the ladder: fast
    -- enough to prove itself within a day, slow enough that eighteen unknown
    -- sources do not open at fifteen-minute intervals.
    v_index := greatest(0, least(
      array_length(v_bands, 1) - 1,
      3 + v_source.demotions - v_source.promotions
    ));

    -- A stable per-source offset, so sources sharing a band do not stampede.
    v_minute := abs(hashtext(v_source.source_key)) % 60;
    v_hour := abs(hashtext(v_source.source_key || ':hour')) % 24;

    v_schedule := case v_bands[v_index + 1]
      when 15 then format('%s-59/15 * * * *', v_minute % 15)
      when 60 then format('%s * * * *', v_minute)
      when 360 then format('%s %s-23/6 * * *', v_minute, v_hour % 6)
      when 1440 then format('%s %s * * *', v_minute, v_hour)
      when 4320 then format('%s %s 1-31/3 * *', v_minute, v_hour)
      -- The slowest band, like the 72-hour one above, varies only minute and
      -- hour by source: 1,440 combinations already keep sources from
      -- stampeding, so the weekday is a fixed literal rather than a second
      -- computed offset. Must match source_schedule.py's cron_expression
      -- day/month/weekday shape for every band, verified by
      -- test_every_python_cron_rendering_appears_in_the_sql.
      else format('%s %s * * 4', v_minute, v_hour)
    end;

    perform public.job_hunter_schedule_stage_enqueue(
      'crawl_source',
      v_schedule,
      jsonb_build_object('source_key', v_source.source_key),
      v_source.source_key
    );
    v_count := v_count + 1;
  end loop;

  return v_count;
end;
$$;

comment on function public.job_hunter_reschedule_sources() is
  'Install one pg_cron entry per enabled source, banded on measured corpus '
  'novelty rather than on an operator setting (issue #184). Unschedules '
  'first, so a disabled source stops firing rather than being left behind.';

revoke all on function public.job_hunter_reschedule_sources()
  from public, anon, authenticated, service_role;

-- The scheduler reschedules itself. One meta-entry, deliberately not
-- per-source: it reads the ledger for every source at once.
select cron.schedule(
  'job-hunter-reschedule-sources',
  '7 * * * *',
  'select public.job_hunter_reschedule_sources();'
);

select public.job_hunter_reschedule_sources();
