-- Make the pending-delivery score floor inclusive.
--
-- `job_hunter_pending_delivery_jobs` (202609060004) was written when the
-- delivery floor was a constant the store owned: `_DELIVERABLE_SCORE_FLOOR
-- = 60`, meaning "strictly above 60". Issue #116 replaced that constant
-- with the search profile's `match_score_floor`, which is inclusive -- a
-- job scoring exactly the floor is delivered.
--
-- Bridging the two in Python meant passing `match_score_floor - 1` and
-- explaining the off-by-one in a docstring, which left the same rule
-- encoded three times: twice as `score < floor` in pipeline.py and once
-- inverted here. Moving the `>=` into SQL leaves one encoding, and the
-- parameter now means what its name says at both ends of the call.
--
-- The parameter is renamed, so this drops and recreates rather than
-- `create or replace` (Postgres refuses to rename an input parameter in
-- place). The rename is deliberate: a caller still passing the old
-- exclusive value fails loudly on the unknown argument name instead of
-- silently withholding every job that scores exactly the floor.
--
-- Security model is unchanged from 202609060004: `security invoker`,
-- `set search_path = ''`, fully qualified `public.` names, and an explicit
-- `user_id = (select auth.uid())` filter on top of RLS.

drop function if exists public.job_hunter_pending_delivery_jobs(int);

create function public.job_hunter_pending_delivery_jobs(p_match_score_floor int)
returns table (job_id uuid)
language sql
security invoker
set search_path = ''
as $$
  select j.id
    from public.job_hunter_jobs j
    join lateral (
      select e.decision, e.total_score
        from public.job_hunter_evaluations e
       where e.job_id = j.id and e.user_id = j.user_id
       order by e.evaluated_at desc, e.created_at desc, e.id desc
       limit 1
    ) e on true
   where j.user_id = (select auth.uid())
     and e.total_score >= p_match_score_floor
     and e.decision in ('possible_match', 'high_priority', 'package_match')
     and not exists (
           select 1
             from public.job_hunter_deliveries d
            where d.job_id = j.id
              and d.user_id = j.user_id
              and d.delivery_type = 'telegram_message'
         );
$$;
