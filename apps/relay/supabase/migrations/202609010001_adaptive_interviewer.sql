alter table public.interview_sessions
  add column round_id text not null default 'tech-lead',
  add column mode text not null default 'real'
    check (mode in ('coach', 'real')),
  -- Release 3 must not derive confident observations from a degraded session.
  add column degraded boolean not null default false;

-- opportunity_id already exists (202608300006_session_career_context.sql).

alter table public.interview_questions
  add column asked_intent jsonb,
  add column assistance jsonb not null default '[]'::jsonb,
  add column non_answer boolean not null default false;

-- A question's text no longer exists until the interviewer authors it.
alter table public.interview_questions
  alter column prompt drop not null;

create or replace function public.create_conversation_session_with_blueprint(p_blueprint jsonb)
returns table(session_id uuid)
language plpgsql
security invoker
set search_path = public
as $$
declare
  v_user_id uuid := auth.uid();
  v_session_id uuid;
  v_round_id text;
  v_mode text;
  v_status text;
  v_reason text;
  v_max_follow_ups integer;
  v_max_questions integer;
begin
  if v_user_id is null then
    raise exception 'Authentication is required' using errcode = '42501';
  end if;
  if jsonb_typeof(p_blueprint) <> 'object' then
    raise exception 'Interview blueprint payload must be an object' using errcode = '22023';
  end if;
  if jsonb_typeof(p_blueprint -> 'targets') <> 'array' then
    raise exception 'Interview blueprint must contain a targets array' using errcode = '22023';
  end if;

  v_round_id := coalesce(nullif(trim(p_blueprint ->> 'roundId'), ''), 'tech-lead');
  v_mode := coalesce(nullif(trim(p_blueprint ->> 'mode'), ''), 'real');
  if v_mode not in ('coach', 'real') then
    raise exception 'Interview blueprint mode is invalid' using errcode = '22023';
  end if;

  v_status := coalesce(nullif(trim(p_blueprint ->> 'status'), ''), 'grounded');
  if v_status not in ('grounded', 'limited-grounding') then
    raise exception 'Interview blueprint status is invalid' using errcode = '22023';
  end if;
  v_reason := nullif(trim(coalesce(p_blueprint ->> 'fallback_reason', '')), '');
  v_max_follow_ups := greatest(0, least(3, coalesce((p_blueprint ->> 'max_follow_ups')::integer, 3)));
  v_max_questions := greatest(5, least(8, coalesce((p_blueprint ->> 'max_questions')::integer, 8)));

  insert into public.interview_sessions (
    user_id,
    kind,
    status,
    round_id,
    mode,
    blueprint_status,
    blueprint_fallback_reason,
    blueprint_max_follow_ups,
    blueprint_max_questions
  )
  values (v_user_id, 'conversation', 'active', v_round_id, v_mode, v_status, v_reason, v_max_follow_ups, v_max_questions)
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
    source_confidence
  )
  select
    v_user_id,
    v_session_id,
    target.sequence,
    target.category,
    target.competency_id,
    nullif(trim(target.competency_name), ''),
    target.difficulty,
    false,
    null,
    trim(target.objective),
    coalesce(target.evidence_ids, '[]'::jsonb),
    coalesce(target.expected_signals, '[]'::jsonb),
    coalesce(target.missing_signal_prompts, '[]'::jsonb),
    coalesce(target.rubric_criteria, '[]'::jsonb),
    greatest(0, least(3, coalesce(target.follow_up_limit, 0))),
    target.source_confidence
  from jsonb_to_recordset(p_blueprint -> 'targets') as target(
    sequence integer,
    category text,
    competency_id uuid,
    competency_name text,
    difficulty text,
    objective text,
    evidence_ids jsonb,
    expected_signals jsonb,
    missing_signal_prompts jsonb,
    rubric_criteria jsonb,
    follow_up_limit integer,
    source_confidence numeric
  );

  return query select v_session_id;
end;
$$;

revoke all on function public.create_conversation_session_with_blueprint(jsonb) from public;
grant execute on function public.create_conversation_session_with_blueprint(jsonb) to authenticated;

create or replace function public.record_conversation_turn(
  p_question_id uuid,
  p_answer text,
  p_score numeric,
  p_dimensions jsonb,
  p_strengths jsonb,
  p_needs_work jsonb,
  p_missing_points jsonb,
  p_better_structure jsonb,
  p_improved_answer text,
  p_relevance numeric,
  p_supported_claims jsonb,
  p_expected_signals_present jsonb,
  p_unsupported_claims jsonb,
  p_dimension_reasons jsonb,
  p_next_question_id uuid,
  p_next_prompt text,
  p_follow_up jsonb,
  p_asked_intent jsonb,
  p_assistance jsonb,
  p_non_answer boolean,
  p_degraded boolean
)
returns table(session_id uuid)
language plpgsql
security invoker
set search_path = public
as $$
declare
  v_user_id uuid := auth.uid();
  v_question public.interview_questions%rowtype;
  v_shift public.interview_questions%rowtype;
  v_total integer;
  v_follow_ups integer;
  v_parent_follow_ups integer;
  v_session_max_follow_ups integer;
  v_session_max_questions integer;
  v_follow_up_limit integer;
begin
  if v_user_id is null then
    raise exception 'Authentication is required' using errcode = '42501';
  end if;

  select q.* into v_question
  from public.interview_questions q
  join public.interview_sessions s on s.id = q.session_id and s.user_id = q.user_id
  where q.id = p_question_id
    and q.user_id = v_user_id
    and s.kind = 'conversation'
    and s.status = 'active'
  for update of q;

  if not found then
    raise exception 'Active owned question was not found' using errcode = 'P0002';
  end if;

  select
    coalesce(s.blueprint_max_follow_ups, 3),
    coalesce(s.blueprint_max_questions, 8)
  into v_session_max_follow_ups, v_session_max_questions
  from public.interview_sessions s
  where s.id = v_question.session_id
    and s.user_id = v_user_id;

  -- Once degraded, a session stays degraded.
  update public.interview_sessions
  set degraded = degraded or coalesce(p_degraded, false),
      updated_at = now()
  where id = v_question.session_id
    and user_id = v_user_id;

  if not p_non_answer then
    perform * from public.record_interview_evidence(
      p_question_id,
      p_answer,
      p_score,
      p_dimensions,
      p_strengths,
      p_needs_work,
      p_missing_points,
      p_better_structure,
      p_improved_answer,
      p_relevance,
      p_supported_claims,
      p_expected_signals_present,
      p_unsupported_claims,
      p_dimension_reasons
    );
  end if;

  -- Mark the answered question with the intent it was asked under and any
  -- assistance the interviewer granted before scoring it.
  update public.interview_questions
  set non_answer = p_non_answer,
      assistance = coalesce(p_assistance, '[]'::jsonb),
      updated_at = now()
  where id = p_question_id
    and user_id = v_user_id;

  if p_follow_up is not null then
    select count(*), count(*) filter (where is_follow_up)
    into v_total, v_follow_ups
    from public.interview_questions
    where public.interview_questions.session_id = v_question.session_id
      and user_id = v_user_id;

    select count(*)
    into v_parent_follow_ups
    from public.interview_questions
    where public.interview_questions.session_id = v_question.session_id
      and user_id = v_user_id
      and parent_question_id = v_question.id;

    v_follow_up_limit := greatest(0, least(3, coalesce(v_question.follow_up_limit, 0)));
    if v_total >= v_session_max_questions
      or v_follow_ups >= v_session_max_follow_ups
      or v_parent_follow_ups >= v_follow_up_limit
      or v_question.is_follow_up then
      raise exception 'Conversation follow-up limit reached' using errcode = '22023';
    end if;

    for v_shift in
      select *
      from public.interview_questions
      where public.interview_questions.session_id = v_question.session_id
        and user_id = v_user_id
        and sequence > v_question.sequence
      order by sequence desc
      for update
    loop
      update public.interview_questions
      set sequence = v_shift.sequence + 1,
          updated_at = now()
      where id = v_shift.id
        and user_id = v_user_id;
    end loop;

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
      parent_question_id,
      asked_intent,
      assistance,
      non_answer,
      asked_at
    ) values (
      v_user_id,
      v_question.session_id,
      v_question.sequence + 1,
      v_question.category,
      v_question.competency_id,
      nullif(trim(coalesce(p_follow_up ->> 'competencyName', p_follow_up ->> 'competency_name', v_question.competency_name, '')), ''),
      v_question.difficulty,
      true,
      trim(p_follow_up ->> 'prompt'),
      nullif(trim(coalesce(p_follow_up ->> 'objective', v_question.objective, '')), ''),
      coalesce(p_follow_up -> 'evidenceIds', p_follow_up -> 'evidence_ids', v_question.evidence_ids, '[]'::jsonb),
      coalesce(p_follow_up -> 'expectedSignals', p_follow_up -> 'expected_signals', v_question.expected_signals, '[]'::jsonb),
      coalesce(p_follow_up -> 'missingSignalPrompts', p_follow_up -> 'missing_signal_prompts', v_question.missing_signal_prompts, '[]'::jsonb),
      coalesce(p_follow_up -> 'rubricCriteria', p_follow_up -> 'rubric_criteria', v_question.rubric_criteria, '[]'::jsonb),
      greatest(0, least(3, coalesce((p_follow_up ->> 'followUpLimit')::integer, (p_follow_up ->> 'follow_up_limit')::integer, v_question.follow_up_limit, 0))),
      coalesce((p_follow_up ->> 'sourceConfidence')::numeric, (p_follow_up ->> 'source_confidence')::numeric, v_question.source_confidence),
      v_question.id,
      p_asked_intent,
      '[]'::jsonb,
      false,
      now()
    );
  elsif p_next_question_id is not null then
    update public.interview_questions q
    set prompt = trim(p_next_prompt),
        asked_intent = p_asked_intent,
        asked_at = now(),
        updated_at = now()
    where q.id = p_next_question_id
      and q.session_id = v_question.session_id
      and q.user_id = v_user_id
      and q.answer is null;

    if not found then
      raise exception 'Owned next question was not found' using errcode = 'P0002';
    end if;
  end if;

  return query select v_question.session_id;
end;
$$;

revoke all on function public.record_conversation_turn(uuid, text, numeric, jsonb, jsonb, jsonb, jsonb, jsonb, text, numeric, jsonb, jsonb, jsonb, jsonb, uuid, text, jsonb, jsonb, jsonb, boolean, boolean) from public;
grant execute on function public.record_conversation_turn(uuid, text, numeric, jsonb, jsonb, jsonb, jsonb, jsonb, text, numeric, jsonb, jsonb, jsonb, jsonb, uuid, text, jsonb, jsonb, jsonb, boolean, boolean) to authenticated;
