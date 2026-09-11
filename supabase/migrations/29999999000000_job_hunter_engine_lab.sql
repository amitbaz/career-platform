-- Engine Lab review page and measurement ledger (issue #257).
--
-- 29999999000000 is a placeholder, not a timestamp -- see root AGENTS.md
-- "Migration filenames are allocated across the whole repository". Renumber
-- to the real YYYYMMDDHHMMSS at merge time.
--
-- Full design: apps/job-hunter/docs/superpowers/specs/2026-09-11-engine-lab-review-ledger-design.md
--
-- Three tables:
--
--   * job_hunter_engine_lab_collaborators -- who may open or write through
--     the review page. No such concept exists anywhere else in this schema
--     (no owner_id, no allowlist), so this is the whole of it: one row per
--     invited reviewer, created only by the `engine-lab-invite` CLI command,
--     never by a signed-in user.
--   * job_hunter_engine_lab_impressions -- one row per card shown, written
--     before the card is rendered. Insert-only: an impression is a fact
--     about what was shown, and "durable before render" is only checkable
--     because nothing can edit it afterwards.
--   * job_hunter_engine_lab_judgements -- one row per impression's
--     judgement, at most once (`unique (impression_id)`). Also insert-only:
--     re-judging means a fresh impression tomorrow, never an edit to
--     history.
--
-- None of these carry a user_id in the job_hunter_jobs sense: `reviewer_id`
-- names who judged, not whose corpus is being judged (this is a single-user
-- system today, so every card is drawn from the one search profile that
-- exists). Access is gated by two custom JWT claims, the same mechanism
-- job_hunter_platform_ai_usage already established for "only a trusted
-- process may touch this":
--
--   * `job_hunter_runner` (already used elsewhere) -- lets a trusted Job
--     Hunter process read the collaborator list (to check a login token)
--     and read across all reviewers' rows (to build the daily summary).
--   * `engine_lab_admin` -- lets only the `engine-lab-invite` CLI command
--     write the collaborator list. A compromised web process holding only
--     `job_hunter_runner` can read who is invited but cannot invite or
--     revoke anyone.
--   * `engine_lab_reviewer` (plus `sub` = the reviewer's own id) -- lets one
--     authenticated reviewer write their own impressions and judgements.
--
-- A signed-in Relay/product user carries none of these claims and can reach
-- none of the three tables, by construction -- there is no policy for plain
-- `authenticated` on any of them.

create table public.job_hunter_engine_lab_collaborators (
  user_id      uuid primary key,
  email        text not null unique,
  display_name text not null default '',
  token_hash   text not null,
  invited_at   timestamptz not null default now(),
  invited_by   text not null,
  revoked_at   timestamptz
);

comment on table public.job_hunter_engine_lab_collaborators is
  'Who may open or write through the private Engine Lab review page (#257). '
  'One row per explicitly invited reviewer, created only by the '
  'engine-lab-invite CLI command. Revoking access is setting revoked_at, '
  'never deleting the row a token_hash could otherwise collide into.';

alter table public.job_hunter_engine_lab_collaborators enable row level security;

create policy runner_select on public.job_hunter_engine_lab_collaborators
  for select to authenticated
  using (coalesce((select auth.jwt() -> 'job_hunter_runner'), 'false'::jsonb) = 'true'::jsonb);
create policy admin_insert on public.job_hunter_engine_lab_collaborators
  for insert to authenticated
  with check (coalesce((select auth.jwt() -> 'engine_lab_admin'), 'false'::jsonb) = 'true'::jsonb);
create policy admin_update on public.job_hunter_engine_lab_collaborators
  for update to authenticated
  using (coalesce((select auth.jwt() -> 'engine_lab_admin'), 'false'::jsonb) = 'true'::jsonb)
  with check (coalesce((select auth.jwt() -> 'engine_lab_admin'), 'false'::jsonb) = 'true'::jsonb);
-- No delete policy: revoked collaborators keep their row as an audit trail.

create table public.job_hunter_engine_lab_impressions (
  id                    uuid primary key default gen_random_uuid(),
  reviewer_id           uuid not null references public.job_hunter_engine_lab_collaborators(user_id),
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
  'exists (concealment is enforced by the application, not by this table).';

create index job_hunter_engine_lab_impressions_reviewer_day_idx
  on public.job_hunter_engine_lab_impressions (reviewer_id, shown_at);

alter table public.job_hunter_engine_lab_impressions enable row level security;

create policy reviewer_or_runner_select on public.job_hunter_engine_lab_impressions
  for select to authenticated
  using (
    coalesce((select auth.jwt() -> 'job_hunter_runner'), 'false'::jsonb) = 'true'::jsonb
    or (
      coalesce((select auth.jwt() -> 'engine_lab_reviewer'), 'false'::jsonb) = 'true'::jsonb
      and reviewer_id = (select auth.uid())
    )
  );
create policy reviewer_insert on public.job_hunter_engine_lab_impressions
  for insert to authenticated
  with check (
    coalesce((select auth.jwt() -> 'engine_lab_reviewer'), 'false'::jsonb) = 'true'::jsonb
    and reviewer_id = (select auth.uid())
  );
-- No update, no delete: an impression is immutable once written.

create table public.job_hunter_engine_lab_judgements (
  id                 uuid primary key default gen_random_uuid(),
  impression_id      uuid not null unique
    references public.job_hunter_engine_lab_impressions(id),
  reviewer_id        uuid not null references public.job_hunter_engine_lab_collaborators(user_id),
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

create policy reviewer_or_runner_select on public.job_hunter_engine_lab_judgements
  for select to authenticated
  using (
    coalesce((select auth.jwt() -> 'job_hunter_runner'), 'false'::jsonb) = 'true'::jsonb
    or (
      coalesce((select auth.jwt() -> 'engine_lab_reviewer'), 'false'::jsonb) = 'true'::jsonb
      and reviewer_id = (select auth.uid())
    )
  );
create policy reviewer_insert on public.job_hunter_engine_lab_judgements
  for insert to authenticated
  with check (
    coalesce((select auth.jwt() -> 'engine_lab_reviewer'), 'false'::jsonb) = 'true'::jsonb
    and reviewer_id = (select auth.uid())
  );
-- No update, no delete: re-judging is a new impression, never an edit to
-- this one's history.
