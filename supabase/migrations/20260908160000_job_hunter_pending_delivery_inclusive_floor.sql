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
-- inverted here. Moving the `>=` into SQL leaves one encoding.
--
-- DEPLOYMENT COMPATIBILITY. This is `create or replace` with the
-- parameter name unchanged, so the function keeps working for whichever
-- version of the application is live while the other is being deployed:
--
--   * old code + new function: passes 60, meaning ">= 60" where it meant
--     "> 60". One point more lenient for the length of the deploy, on a
--     retry path that only re-sends already-evaluated jobs. Self-correcting.
--   * new code + old function: passes `match_score_floor`, meaning
--     "> floor" where it means ">= floor". One point stricter, same window.
--
-- Renaming the parameter to `p_match_score_floor` would read better, and
-- an earlier draft of this migration did that. It cannot be done safely:
-- PostgREST resolves `rpc` by parameter name, so a rename breaks whichever
-- side is not yet deployed, and `pipeline.py` calls this outside the
-- per-job `try/except` -- a 404 there aborts the run after the Gemini
-- spend and before the digest sends. A cosmetic name is not worth a lost
-- daily digest. Postgres also cannot overload on parameter name alone
-- (both signatures are `(int)`: "function ... already exists with same
-- argument types"), so there is no expand/contract path that keeps the
-- name either.
--
-- Security model is unchanged from 202609060004: `security invoker`,
-- `set search_path = ''`, fully qualified `public.` names, and an explicit
-- `user_id = (select auth.uid())` filter on top of RLS. The body below is
-- 202609060004's, with `>` changed to `>=` on one line.

create or replace function public.job_hunter_pending_delivery_jobs(p_score_floor int)
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
     and e.total_score >= p_score_floor
     and e.decision in ('possible_match', 'high_priority', 'package_match')
     and not exists (
           select 1
             from public.job_hunter_deliveries d
            where d.job_id = j.id
              and d.user_id = j.user_id
              and d.delivery_type = 'telegram_message'
         );
$$;
