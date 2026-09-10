-- Shared ATS board registry (issue #203, parent #118).
--
-- `job_hunter_ats_registry` holds facts about employers' job boards, not
-- facts about a user: that a Greenhouse board exists at a given identifier,
-- that it is reachable, that it is an aggregator not worth crawling. Every
-- one of those is true for everyone, and every one was discovered and
-- stored once per user. This is the #174 argument applied to a table
-- nobody had looked at: the postings work moved the *result* of discovery
-- to a shared table; this moves the knowledge that *directs* discovery.
--
-- Rejections are the valuable half. `list_rejected_ats_boards()` is
-- accumulated negative knowledge -- the boards learned not to crawl -- and
-- the most expensive thing here to rediscover, in rate-limited external
-- calls against sources whose licensing terms are already tight. A user
-- missing a posting finds it on the next crawl; a user missing a rejection
-- silently re-crawls a board already known to be worthless and gets a
-- normal-looking empty result. That is why this lands ahead of multi-user
-- fan-out rather than alongside it.
--
-- What moves and what does not -------------------------------------------
--
-- Shared here: provider, board_identifier, company_name, market_hint,
-- first_seen_at, last_seen_at, last_checked_at, last_success_at,
-- last_job_count, consecutive_failures, active, paused_until,
-- rejected_reason. Board health is a property of the board.
--
-- Stays on `job_hunter_ats_registry`, per-user: eligible_jobs_seen and
-- last_eligible_at. Eligibility is judged against a user's own search
-- profile (`record_ats_eligible_jobs`), so it is per-user *yield*, not
-- board health. A shared board carrying one user's yield would mislead
-- budgeting for every other user.
--
-- Corrected by #184: that ticket does NOT build on this column. Its
-- scheduler runs as the privileged role with no auth.uid() and cannot read
-- a per-user column at all, and with one user an aggregate of this column
-- is that user's search profile -- the engine would learn to visit only the
-- boards matching the current job hunt. #184 records corpus novelty in
-- job_hunter_source_crawls instead, which is shared by construction.
--
-- `market_hint` is shared in principle but is populated from whichever
-- user's crawl discovered the board first, and there is no stronger
-- provenance for it than that. It carries the same conservative posture
-- #198 established for supplied facts: it is free-text guidance for
-- ranking, never filtered or constrained, so a wrong or stale value costs
-- ordering quality rather than correctness, and the alternative -- one row
-- per user just for this field -- reintroduces the duplication this
-- ticket removes for a field this cheap to be wrong about.
--
-- `rejected_reason` is two different things wearing one column in the
-- source table. `sources/learned_ats.py` rejects a board for two reasons:
-- a config denylist entry (that user's own policy: "configured in
-- learned_ats_denylist (...)"), and an aggregator-detection verdict
-- (evidence about the board itself). Only the second is evidence that
-- generalizes. The backfill below classifies every existing rejection by
-- that literal prefix before promoting anything, because after promotion
-- the reason string is the only evidence left.
--
-- A board deactivated for a reason the backfill cannot classify -- e.g.
-- health-backoff deactivation, which carries no `rejected_reason` at all
-- -- promotes as **active**, never as rejected. The two error directions
-- are unequal in kind: a board wrongly promoted as active is corrected by
-- the next crawl that finds nothing; a board wrongly promoted as rejected
-- is unreachable by design, because (see `postgres_store.py`'s
-- `upsert_ats_board` docstring) a board with a `rejected_reason` is never
-- reactivated by rediscovery.
--
-- Sharing ------------------------------------------------------------
--
-- Nothing here is per-user, so nothing here carries a user_id. Reads are
-- open to every authenticated user, exactly as job_hunter_postings and
-- job_hunter_companies are and for the same reason: the work is done once
-- for everyone, so everyone must be able to read the result. There is
-- deliberately no delete policy -- a board row is shared, so no single
-- user may remove one out from under the others. #179 (the
-- privileged-writer narrowing: writes revoked from every role a user can
-- hold, proven by pgTAP) has not landed yet -- it is blocked on #178
-- moving the job-upsert path off PostgREST -- so this table's writes stay
-- as open as job_hunter_postings' and job_hunter_companies' do today, with
-- the same forward-looking note: narrowing this table's writes to the
-- platform identity is #179, when it lands, in the same pass as the rest
-- of the shared tables.

create table public.job_hunter_ats_boards (
  id uuid primary key default gen_random_uuid(),

  provider text not null,
  board_identifier text not null,
  company_name text not null default '',
  market_hint text not null default '',

  first_seen_at timestamptz not null,
  last_seen_at timestamptz not null,
  last_checked_at timestamptz,
  last_success_at timestamptz,
  last_job_count integer not null default 0,
  consecutive_failures integer not null default 0,
  active boolean not null default true,
  paused_until timestamptz,
  rejected_reason text,

  created_at timestamptz not null default now(),

  unique (provider, board_identifier)
);

alter table public.job_hunter_ats_boards enable row level security;

create policy select_authenticated on public.job_hunter_ats_boards
  for select to authenticated using (true);
create policy insert_authenticated on public.job_hunter_ats_boards
  for insert to authenticated with check (true);
create policy update_authenticated on public.job_hunter_ats_boards
  for update to authenticated using (true) with check (true);

-- "Which active boards are due for a scan" -- list_due_ats_boards.
create index job_hunter_ats_boards_due_idx
  on public.job_hunter_ats_boards (active, paused_until);
-- "Which boards are rejected, and why" -- list_rejected_ats_boards.
create index job_hunter_ats_boards_rejected_idx
  on public.job_hunter_ats_boards (rejected_reason)
  where rejected_reason is not null;

comment on table public.job_hunter_ats_boards is
  'Shared knowledge about an ATS job board -- that it exists, whether it is '
  'reachable, whether it has been rejected as an aggregator or by an '
  'operator''s denylist -- learned once and reused by every user''s '
  'discovery run. Per-user eligible-job yield stays on '
  'job_hunter_ats_registry, which now tracks only that.';

comment on column public.job_hunter_ats_boards.rejected_reason is
  'Set only for aggregator-detection verdicts, which are evidence about '
  'the board. A per-user denylist rejection is that user''s own policy and '
  'must never be written here -- see sources/learned_ats.py''s '
  '_reject_board, which now only calls the shared store for aggregator '
  'rejections.';


-- Backfill from the existing per-user rows --------------------------------
--
-- Classifies every existing rejection before promoting anything: a
-- denylist rejection (rejected_reason like the literal config-denylist
-- prefix) promotes as active, never as rejected, because it is that one
-- user's policy configuration, not evidence about the board. An
-- aggregator rejection (any other non-null rejected_reason) promotes with
-- its reason intact. Everything else -- including a board deactivated by
-- health backoff, which carries active = false but no rejected_reason --
-- is unclassifiable and promotes as active, per the rule above.
--
-- Today's corpus belongs to one user (this deployment is single-user,
-- pre-launch), but the aggregation below is written to be correct for
-- more than one: distinct users who separately discovered the same
-- (provider, board_identifier) collapse into one shared row, board health
-- taking the most informative and most recent evidence across all of
-- them, and the board is rejected in the shared row if *any* user's
-- evidence is an aggregator rejection -- board health is evidence about
-- the board, so any one user's aggregator verdict is enough to reject it
-- for everyone.
with classified as (
  select
    r.provider,
    r.board_identifier,
    r.company_name,
    r.market_hint,
    r.first_seen_at,
    r.last_seen_at,
    r.last_checked_at,
    r.last_success_at,
    r.last_job_count,
    r.consecutive_failures,
    r.paused_until,
    case
      when r.rejected_reason is not null
        and r.rejected_reason not like 'configured in learned_ats_denylist%'
      then r.rejected_reason
      else null
    end as aggregator_rejected_reason
  from public.job_hunter_ats_registry r
),
aggregated as (
  select
    provider,
    board_identifier,
    (array_agg(company_name order by (company_name <> '') desc, company_name))[1]
      as company_name,
    (array_agg(market_hint order by (market_hint <> '') desc, market_hint))[1]
      as market_hint,
    min(first_seen_at) as first_seen_at,
    max(last_seen_at) as last_seen_at,
    max(last_checked_at) as last_checked_at,
    max(last_success_at) as last_success_at,
    max(last_job_count) as last_job_count,
    max(consecutive_failures) as consecutive_failures,
    max(paused_until) as paused_until,
    bool_or(aggregator_rejected_reason is not null) as is_rejected,
    (array_agg(aggregator_rejected_reason)
       filter (where aggregator_rejected_reason is not null))[1]
      as rejected_reason
  from classified
  group by provider, board_identifier
)
insert into public.job_hunter_ats_boards (
  provider, board_identifier, company_name, market_hint,
  first_seen_at, last_seen_at, last_checked_at, last_success_at,
  last_job_count, consecutive_failures, active, paused_until, rejected_reason
)
select
  provider, board_identifier, company_name, market_hint,
  first_seen_at, last_seen_at, last_checked_at, last_success_at,
  last_job_count, consecutive_failures,
  not is_rejected as active,
  case when is_rejected then paused_until else null end as paused_until,
  rejected_reason
from aggregated
on conflict (provider, board_identifier) do nothing;


-- Contract the per-user table to its per-user columns ---------------------
--
-- Every board-health column above moved to job_hunter_ats_boards. What is
-- left is what stays per-user: eligible-job yield, plus the identity
-- columns that key the row and the foreign key that ties it to the shared
-- board it tracks yield against.
alter table public.job_hunter_ats_registry
  drop column company_name,
  drop column market_hint,
  drop column first_seen_at,
  drop column last_seen_at,
  drop column last_checked_at,
  drop column last_success_at,
  drop column last_job_count,
  drop column consecutive_failures,
  drop column active,
  drop column paused_until,
  drop column rejected_reason,
  add constraint job_hunter_ats_registry_board_fkey
    foreign key (provider, board_identifier)
    references public.job_hunter_ats_boards (provider, board_identifier);

comment on table public.job_hunter_ats_registry is
  'Per-user eligible-job yield against a shared ATS board. Board identity '
  'and health -- whether it exists, is reachable, or is rejected -- live on '
  'job_hunter_ats_boards; this table only tracks what one user''s search '
  'profile found eligible on it (issue #203).';
