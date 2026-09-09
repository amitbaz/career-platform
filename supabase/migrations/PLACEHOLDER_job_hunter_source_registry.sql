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
