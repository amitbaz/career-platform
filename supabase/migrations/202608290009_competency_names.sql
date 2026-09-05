alter table public.interview_questions
  add column competency_name text;

create or replace function public.create_conversation_session_with_plan(p_plan jsonb)
returns table(session_id uuid)
language plpgsql
security invoker
set search_path = public
as $$
declare
  v_user_id uuid := auth.uid();
  v_session_id uuid;
  v_count integer;
  v_min_sequence integer;
  v_max_sequence integer;
  v_distinct_sequences integer;
  v_categories text[];
begin
  if v_user_id is null then
    raise exception 'Authentication is required' using errcode = '42501';
  end if;
  if jsonb_typeof(p_plan) <> 'array' or jsonb_array_length(p_plan) <> 5 then
    raise exception 'Conversation plan must contain the exact five-question backbone' using errcode = '22023';
  end if;

  select count(*), min(sequence), max(sequence), count(distinct sequence), array_agg(category order by sequence)
  into v_count, v_min_sequence, v_max_sequence, v_distinct_sequences, v_categories
  from jsonb_to_recordset(p_plan) as question(
    sequence integer,
    category text,
    competency_id uuid,
    competency_name text,
    difficulty text,
    is_follow_up boolean,
    prompt text
  );

  if v_count <> 5
    or v_min_sequence <> 1
    or v_max_sequence <> 5
    or v_distinct_sequences <> 5
    or v_categories <> array['introduction', 'experience', 'technical', 'architecture', 'behavioral']::text[] then
    raise exception 'Conversation plan must preserve the exact five-question backbone' using errcode = '22023';
  end if;

  insert into public.interview_sessions (user_id, kind, status)
  values (v_user_id, 'conversation', 'active')
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
    coalesce(question.is_follow_up, false),
    trim(question.prompt),
    now()
  from jsonb_to_recordset(p_plan) as question(
    sequence integer,
    category text,
    competency_id uuid,
    competency_name text,
    difficulty text,
    is_follow_up boolean,
    prompt text
  );

  return query select v_session_id;
end;
$$;

revoke all on function public.create_conversation_session_with_plan(jsonb) from public;
grant execute on function public.create_conversation_session_with_plan(jsonb) to authenticated;

create or replace function public.create_conversation_session_with_blueprint(p_blueprint jsonb)
returns table(session_id uuid)
language plpgsql
security invoker
set search_path = public
as $$
declare
  v_user_id uuid := auth.uid();
  v_session_id uuid;
  v_count integer;
  v_min_sequence integer;
  v_max_sequence integer;
  v_distinct_sequences integer;
  v_categories text[];
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
  if jsonb_typeof(p_blueprint -> 'questions') <> 'array' or jsonb_array_length(p_blueprint -> 'questions') <> 5 then
    raise exception 'Interview blueprint must contain the exact five-question backbone' using errcode = '22023';
  end if;

  select count(*), min(sequence), max(sequence), count(distinct sequence), array_agg(category order by sequence)
  into v_count, v_min_sequence, v_max_sequence, v_distinct_sequences, v_categories
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
    follow_up_limit integer,
    source_confidence numeric
  );

  if v_count <> 5
    or v_min_sequence <> 1
    or v_max_sequence <> 5
    or v_distinct_sequences <> 5
    or v_categories <> array['introduction', 'experience', 'technical', 'architecture', 'behavioral']::text[] then
    raise exception 'Interview blueprint must preserve the exact five-question backbone' using errcode = '22023';
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
    blueprint_status,
    blueprint_fallback_reason,
    blueprint_max_follow_ups,
    blueprint_max_questions
  )
  values (v_user_id, 'conversation', 'active', v_status, v_reason, v_max_follow_ups, v_max_questions)
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
  p_follow_up jsonb
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

  if p_follow_up is not null then
    select count(*), count(*) filter (where is_follow_up)
    into v_total, v_follow_ups
    from public.interview_questions
    where public.interview_questions.session_id = v_question.session_id
      and user_id = v_user_id;

    if v_total >= 8 or v_follow_ups >= 3 or v_question.is_follow_up then
      raise exception 'Conversation follow-up limit reached' using errcode = '22023';
    end if;
    if length(trim(coalesce(p_follow_up ->> 'prompt', ''))) = 0 then
      raise exception 'A follow-up prompt is required' using errcode = '22023';
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
      asked_at
    ) values (
      v_user_id,
      v_question.session_id,
      v_question.sequence + 1,
      v_question.category,
      v_question.competency_id,
      nullif(trim(coalesce(p_follow_up ->> 'competencyName', p_follow_up ->> 'competency_name', '')), ''),
      v_question.difficulty,
      true,
      trim(p_follow_up ->> 'prompt'),
      now()
    );
  elsif p_next_question_id is not null then
    update public.interview_questions q
    set prompt = trim(p_next_prompt),
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

revoke all on function public.record_conversation_turn(uuid, text, numeric, jsonb, jsonb, jsonb, jsonb, jsonb, text, numeric, jsonb, jsonb, jsonb, jsonb, uuid, text, jsonb) from public;
grant execute on function public.record_conversation_turn(uuid, text, numeric, jsonb, jsonb, jsonb, jsonb, jsonb, text, numeric, jsonb, jsonb, jsonb, jsonb, uuid, text, jsonb) to authenticated;
