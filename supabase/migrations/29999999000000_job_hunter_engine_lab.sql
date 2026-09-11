-- Engine Lab measurement ledger (issue #257).
--
-- 29999999000000 is a placeholder, not a timestamp -- see root AGENTS.md
-- "Migration filenames are allocated across the whole repository". Renumber
-- to the real YYYYMMDDHHMMSS at merge time.
--
-- Full design: apps/job-hunter/docs/superpowers/specs/2026-09-11-engine-lab-review-ledger-design.md
--
-- Superseded (2026-09-11): the original design paired this ledger with a
-- bespoke Flask review page with its own Supabase Auth login, invite and
-- ownership model. The owner tried it, found the login flow "useless" for
-- a single-owner tool, and is instead evaluating an off-the-shelf internal
-- tool (Retool or similar, tracked as #283) to browse and judge cards. That
-- removed an entire identity layer this migration used to define
-- (a collaborators table, an owner-bootstrap RPC, invite/claim RPCs, and
-- RLS keyed off a reviewer's own `auth.uid()`) -- none of it applies once
-- the consuming tool brings its own login and its own trusted database
-- credential instead of a per-reviewer Supabase Auth session.
--
-- What is left is the ledger itself: two tables, no identity scheme.
-- `reviewer_id` is a free-form string supplied by whatever judges a card --
-- there is no `auth.users` row it has to resolve to. RLS stays on (every
-- table in this schema has it), but there are deliberately no policies for
-- `authenticated`/`anon` at all: only a trusted connection (an admin/service
-- credential, which bypasses RLS) can read or write here, the same way
-- Retool or any other internal tool would connect to Postgres directly.

create table public.job_hunter_engine_lab_impressions (
  id                    uuid primary key default gen_random_uuid(),
  reviewer_id           text not null,
  posting_id            uuid not null references public.job_hunter_postings(id),
  cohort                text not null,
  profile_version       text not null,
  posting_version       text not null,
  matching_version      text not null,
  explanation_version   text not null,
  configuration_version text not null,
  shown_at              timestamptz not null default now(),

  constraint job_hunter_engine_lab_impressions_cohort_check
    check (cohort in ('intended', 'audit_hard_excluded', 'audit_unresolved', 'audit_below_threshold'))
);

comment on table public.job_hunter_engine_lab_impressions is
  'One row per Engine Lab review card actually shown, written before the '
  'card is rendered (#257) -- an ignored card still counts. cohort is '
  'written here but never returned to the reviewer until a judgement '
  'exists (concealment is enforced by whatever tool renders the card, not '
  'by this table). reviewer_id is a free-form string -- there is no '
  'per-reviewer identity or login scheme in this schema (see the '
  '"Superseded" note above); only a trusted database connection can reach '
  'this table at all.';

create index job_hunter_engine_lab_impressions_reviewer_day_idx
  on public.job_hunter_engine_lab_impressions (reviewer_id, shown_at);

alter table public.job_hunter_engine_lab_impressions enable row level security;
-- No policies at all: `authenticated`/`anon` have zero access. Only a
-- trusted connection (service_role, or the `postgres` role migrations run
-- as) can read or write, since RLS does not apply to those.

create table public.job_hunter_engine_lab_judgements (
  id                 uuid primary key default gen_random_uuid(),
  impression_id      uuid not null unique
    references public.job_hunter_engine_lab_impressions(id),
  reviewer_id        text not null,
  worth_applying     boolean not null,
  why_line_judgement text not null,
  problem_reason     text,
  judged_at          timestamptz not null default now(),

  constraint job_hunter_engine_lab_judgements_why_line_check
    check (why_line_judgement in ('helpful', 'flawed'))
);

comment on table public.job_hunter_engine_lab_judgements is
  'At most one judgement per impression (#257). worth_applying (the '
  'internal match verdict) and why_line_judgement (the explanation verdict) '
  'are separate not-null columns on purpose -- they are never allowed to '
  'share a metric, and neither can be silently omitted.';

alter table public.job_hunter_engine_lab_judgements enable row level security;
-- No policies here either, for the same reason as impressions.
