-- Career Brain Release 2: transactional plan-driven session starts.
--
-- Generic conversations keep the EXACT five-question backbone enforced by
-- `create_conversation_session_with_plan`/`create_conversation_session_with_blueprint`
-- (both untouched here). Planned practice sessions are a SEPARATE contract:
-- 1-5 base questions, contiguous sequences from 1, no follow-ups among the
-- base questions -- see `assertPracticeConversationBlueprint` in
-- `src/lib/repositories/interviews.ts`.
--
-- `blueprint_max_questions` is widened from its original 5-8 range to 1-8 so
-- a practice session with fewer than five base questions can still record a
-- sane follow-up ceiling (see `interview_sessions_blueprint_max_questions_check`
-- from migration 202608290006). No other constraint is touched.
--
-- Two new RPCs start a planned practice session atomically -- session
-- creation and the owning plan's `ready` -> `started` transition happen in
-- one transaction, so a crash between the two steps can never leave a
-- session without its plan (or a plan marked started with no session):
--
-- - `create_planned_conversation_session_with_blueprint` persists a 1-5
--   question blueprint the same way the generic RPC persists its five,
--   reusing the identical persisted-question column set.
-- - `start_hands_on_practice_session` stores a caller-supplied exercise
--   object unchanged on a `hands-on` session (mirroring the plain-insert
--   shape of `createHandsOnSession`, but as an RPC since it must also move
--   plan status in the same transaction).
--
-- Both RPCs share the same career-context invariant introduced by migration
-- 202608300006: when an opportunity is supplied, it must be linked to the
-- plan via `practice_plan_opportunities`, and if the plan has a `primary`
-- link, the supplied opportunity must match it. Both lock the owned plan row
-- `for update` and require it to be `ready`, so a plan can only be started
-- once and concurrent start attempts serialize instead of racing.

alter table public.interview_sessions
  drop constraint if exists interview_sessions_blueprint_max_questions_check;

alter table public.interview_sessions
  add constraint interview_sessions_blueprint_max_questions_check
  check (blueprint_max_questions between 1 and 8);

create or replace function public.create_planned_conversation_session_with_blueprint(
  p_blueprint jsonb,
  p_practice_plan_id uuid,
  p_opportunity_id uuid default null
)
returns table(session_id uuid)
language plpgsql
security invoker
set search_path = public
as $$
declare
  v_user_id uuid := auth.uid();
  v_session_id uuid;
  v_plan public.practice_plans%rowtype;
  v_primary_opportunity_id uuid;
  v_count integer;
  v_min_sequence integer;
  v_max_sequence integer;
  v_distinct_sequences integer;
  v_status text;
  v_reason text;
  v_max_follow_ups integer;
  v_max_questions integer;
begin
  if v_user_id is null then
    raise exception 'Authentication is required' using errcode = '42501';
  end if;
  if jsonb_typeof(p_blueprint) <> 'object' then
    raise exception 'Practice blueprint payload must be an object' using errcode = '22023';
  end if;
  if jsonb_typeof(p_blueprint -> 'questions') <> 'array'
    or jsonb_array_length(p_blueprint -> 'questions') < 1
    or jsonb_array_length(p_blueprint -> 'questions') > 5 then
    raise exception 'Practice blueprint must contain between one and five base questions' using errcode = '22023';
  end if;

  select count(*), min(sequence), max(sequence), count(distinct sequence)
  into v_count, v_min_sequence, v_max_sequence, v_distinct_sequences
  from jsonb_to_recordset(p_blueprint -> 'questions') as question(
    sequence integer,
    category text,
    competency_id uuid,
    competency_name text,
    difficulty text,
    prompt text,
    objective text,
    evidence_ids jsonb,
    expected_signals jsonb,
    missing_signal_prompts jsonb,
    rubric_criteria jsonb,
    follow_up_limit integer,
    source_confidence numeric
  );

  if v_count < 1 or v_count > 5
    or v_min_sequence <> 1
    or v_max_sequence <> v_count
    or v_distinct_sequences <> v_count then
    raise exception 'Practice blueprint questions must be contiguous base questions starting at one' using errcode = '22023';
  end if;

  v_status := coalesce(nullif(trim(p_blueprint ->> 'status'), ''), 'grounded');
  if v_status not in ('grounded', 'limited-grounding') then
    raise exception 'Practice blueprint status is invalid' using errcode = '22023';
  end if;
  v_reason := nullif(trim(coalesce(p_blueprint ->> 'fallback_reason', '')), '');
  v_max_follow_ups := greatest(0, least(3, coalesce((p_blueprint ->> 'max_follow_ups')::integer, 3)));
  -- Never let the session-wide question ceiling fall below the base
  -- question count actually being persisted, mirroring the generic
  -- function's `greatest(5, ...)` clamp against its fixed five-question
  -- backbone.
  v_max_questions := greatest(v_count, least(8, coalesce((p_blueprint ->> 'max_questions')::integer, 8)));

  -- Lock the owned plan for the duration of the transaction so a second
  -- concurrent start attempt blocks here instead of racing the status
  -- check below.
  select * into v_plan from public.practice_plans
  where id = p_practice_plan_id and user_id = v_user_id
  for update;

  if not found then
    raise exception 'Owned practice plan was not found' using errcode = 'P0002';
  end if;
  if v_plan.status <> 'ready' then
    raise exception 'Practice plan is not ready to start' using errcode = '22023';
  end if;

  if p_opportunity_id is not null then
    if not exists (
      select 1 from public.opportunities
      where id = p_opportunity_id and user_id = v_user_id
    ) then
      raise exception 'Owned opportunity was not found' using errcode = 'P0002';
    end if;

    if not exists (
      select 1 from public.practice_plan_opportunities
      where practice_plan_id = p_practice_plan_id
        and opportunity_id = p_opportunity_id
        and user_id = v_user_id
    ) then
      raise exception 'The practice plan and opportunity do not match' using errcode = '22023';
    end if;

    select opportunity_id into v_primary_opportunity_id
    from public.practice_plan_opportunities
    where practice_plan_id = p_practice_plan_id
      and user_id = v_user_id
      and relevance = 'primary';

    if v_primary_opportunity_id is not null and v_primary_opportunity_id <> p_opportunity_id then
      raise exception 'The practice plan and opportunity do not match' using errcode = '22023';
    end if;
  end if;

  insert into public.interview_sessions (
    user_id,
    kind,
    status,
    blueprint_status,
    blueprint_fallback_reason,
    blueprint_max_follow_ups,
    blueprint_max_questions,
    practice_plan_id,
    opportunity_id
  )
  values (
    v_user_id, 'conversation', 'active', v_status, v_reason, v_max_follow_ups, v_max_questions,
    p_practice_plan_id, p_opportunity_id
  )
  returning id into v_session_id;

  insert into public.interview_questions (
    user_id,
    session_id,
    sequence,
    category,
    competency_id,
    competency_name,
    difficulty,
    is_follow_up,
    prompt,
    objective,
    evidence_ids,
    expected_signals,
    missing_signal_prompts,
    rubric_criteria,
    follow_up_limit,
    source_confidence,
    asked_at
  )
  select
    v_user_id,
    v_session_id,
    question.sequence,
    question.category,
    question.competency_id,
    nullif(trim(question.competency_name), ''),
    question.difficulty,
    false,
    trim(question.prompt),
    trim(question.objective),
    coalesce(question.evidence_ids, '[]'::jsonb),
    coalesce(question.expected_signals, '[]'::jsonb),
    coalesce(question.missing_signal_prompts, '[]'::jsonb),
    coalesce(question.rubric_criteria, '[]'::jsonb),
    greatest(0, least(3, coalesce(question.follow_up_limit, 0))),
    question.source_confidence,
    now()
  from jsonb_to_recordset(p_blueprint -> 'questions') as question(
    sequence integer,
    category text,
    competency_id uuid,
    competency_name text,
    difficulty text,
    prompt text,
    objective text,
    evidence_ids jsonb,
    expected_signals jsonb,
    missing_signal_prompts jsonb,
    rubric_criteria jsonb,
    follow_up_limit integer,
    source_confidence numeric
  );

  update public.practice_plans
  set status = 'started', updated_at = now()
  where id = p_practice_plan_id and user_id = v_user_id;

  return query select v_session_id;
end;
$$;

revoke all on function public.create_planned_conversation_session_with_blueprint(jsonb, uuid, uuid) from public;
grant execute on function public.create_planned_conversation_session_with_blueprint(jsonb, uuid, uuid) to authenticated;

create or replace function public.start_hands_on_practice_session(
  p_practice_plan_id uuid,
  p_opportunity_id uuid,
  p_exercise jsonb
)
returns table(session_id uuid)
language plpgsql
security invoker
set search_path = public
as $$
declare
  v_user_id uuid := auth.uid();
  v_session_id uuid;
  v_plan public.practice_plans%rowtype;
  v_primary_opportunity_id uuid;
begin
  if v_user_id is null then
    raise exception 'Authentication is required' using errcode = '42501';
  end if;
  if jsonb_typeof(p_exercise) <> 'object' then
    raise exception 'Hands-on exercise payload must be an object' using errcode = '22023';
  end if;

  select * into v_plan from public.practice_plans
  where id = p_practice_plan_id and user_id = v_user_id
  for update;

  if not found then
    raise exception 'Owned practice plan was not found' using errcode = 'P0002';
  end if;
  if v_plan.status <> 'ready' then
    raise exception 'Practice plan is not ready to start' using errcode = '22023';
  end if;

  if p_opportunity_id is not null then
    if not exists (
      select 1 from public.opportunities
      where id = p_opportunity_id and user_id = v_user_id
    ) then
      raise exception 'Owned opportunity was not found' using errcode = 'P0002';
    end if;

    if not exists (
      select 1 from public.practice_plan_opportunities
      where practice_plan_id = p_practice_plan_id
        and opportunity_id = p_opportunity_id
        and user_id = v_user_id
    ) then
      raise exception 'The practice plan and opportunity do not match' using errcode = '22023';
    end if;

    select opportunity_id into v_primary_opportunity_id
    from public.practice_plan_opportunities
    where practice_plan_id = p_practice_plan_id
      and user_id = v_user_id
      and relevance = 'primary';

    if v_primary_opportunity_id is not null and v_primary_opportunity_id <> p_opportunity_id then
      raise exception 'The practice plan and opportunity do not match' using errcode = '22023';
    end if;
  end if;

  insert into public.interview_sessions (
    user_id, kind, status, exercise, practice_plan_id, opportunity_id
  )
  values (v_user_id, 'hands-on', 'active', p_exercise, p_practice_plan_id, p_opportunity_id)
  returning id into v_session_id;

  update public.practice_plans
  set status = 'started', updated_at = now()
  where id = p_practice_plan_id and user_id = v_user_id;

  return query select v_session_id;
end;
$$;

revoke all on function public.start_hands_on_practice_session(uuid, uuid, jsonb) from public;
grant execute on function public.start_hands_on_practice_session(uuid, uuid, jsonb) to authenticated;
