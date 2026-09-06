-- Three dead overloads of `record_conversation_turn` are live in production.
--
-- Every migration before 202609040001 widened the function with `create or
-- replace` alone. Postgres keys functions by argument types, so changing the
-- signature never replaced anything -- it created another overload and left
-- the previous one behind. 202609040001 was the first to drop its predecessor
-- explicitly, and its comment claims "exactly one signature exists at any
-- instant". That is not true of the database: the 9-, 12-, and 17-argument
-- versions from the migrations before it are all still there.
--
-- Nothing calls them. The app sends all 22 named arguments, and PostgREST
-- resolves an overload by matching parameter names against the payload, so
-- only the current signature can match. No other function references them and
-- none has a dependent object.
--
-- They are dropped rather than left alone because the next widening is where
-- they become dangerous: a new signature that happens to collide by name with
-- one of these would make PostgREST's choice ambiguous, and the failure would
-- surface as a confusing runtime error rather than a migration conflict.
--
-- The 22-argument signature is deliberately not touched.

-- 202608290002_complete_adaptive_interview_loop.sql
drop function if exists public.record_conversation_turn(
  uuid, text, numeric, jsonb, jsonb, jsonb, uuid, text, jsonb
);

-- 202608290003_richer_feedback.sql
drop function if exists public.record_conversation_turn(
  uuid, text, numeric, jsonb, jsonb, jsonb, jsonb, jsonb, text, uuid, text, jsonb
);

-- Introduced by 202608290007_grounded_evaluations.sql; redefined in place by
-- 202608290009 and 202608290010, which kept this same signature.
drop function if exists public.record_conversation_turn(
  uuid, text, numeric, jsonb, jsonb, jsonb, jsonb, jsonb, text, numeric, jsonb,
  jsonb, jsonb, jsonb, uuid, text, jsonb
);
