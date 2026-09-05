-- Career Brain Release 1: link interview sessions to their career context.
--
-- `interview_sessions` receives two nullable columns:
--
-- - `practice_plan_id` answers "why this practice existed and what success
--   meant";
-- - `opportunity_id` identifies "the primary real job/interview being
--   prepared for", when there is one.
--
-- Both are nullable so every existing interview session remains valid
-- without a backfill guess -- no historical session is assigned a plan or
-- opportunity automatically.
--
-- Both use same-user composite foreign keys into the Task 2/5 parent
-- tables, which already carry `unique (id, user_id)`:
--
--   (practice_plan_id, user_id) -> practice_plans(id, user_id)
--   (opportunity_id, user_id)   -> opportunities(id, user_id)
--
-- `interview_sessions.user_id` is `not null`, so a plain composite
-- `on delete set null` (which would try to null every referenced column,
-- including `user_id`) would fail at runtime. The PostgreSQL 15+
-- column-list form -- `on delete set null (practice_plan_id)` /
-- `(opportunity_id)` -- nulls only the Career Brain reference on delete
-- and leaves session ownership intact.
--
-- If a session has BOTH a practice_plan_id and an opportunity_id, normal
-- domain code (`linkSessionCareerContext` in
-- `src/lib/repositories/interviews.ts`) must ensure the opportunity is
-- associated with the plan via `practice_plan_opportunities`, and -- when
-- the plan has a `primary` opportunity -- that the session's
-- opportunity_id matches it. This is deliberately not a cross-table SQL
-- constraint; see the design doc section 10 for the rationale.

alter table public.interview_sessions
  add column practice_plan_id uuid,
  add column opportunity_id uuid,
  add foreign key (practice_plan_id, user_id)
    references public.practice_plans (id, user_id) on delete set null (practice_plan_id),
  add foreign key (opportunity_id, user_id)
    references public.opportunities (id, user_id) on delete set null (opportunity_id);

-- Cheap lookups for Release 2 queries such as "sessions for this
-- opportunity, newest first" and "sessions for this plan".
create index interview_sessions_user_opportunity_idx
  on public.interview_sessions (user_id, opportunity_id, created_at desc);
create index interview_sessions_user_plan_idx
  on public.interview_sessions (user_id, practice_plan_id);
