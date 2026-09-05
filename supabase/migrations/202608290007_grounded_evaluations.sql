alter table public.question_evaluations
  add column relevance numeric check (relevance is null or relevance between 0 and 10),
  add column supported_claims jsonb not null default '[]'::jsonb,
  add column expected_signals_present jsonb not null default '[]'::jsonb,
  add column unsupported_claims jsonb not null default '[]'::jsonb,
  add column dimension_reasons jsonb not null default '{}'::jsonb;

alter table public.session_evaluations
  add column relevance numeric check (relevance is null or relevance between 0 and 10),
  add column supported_claims jsonb not null default '[]'::jsonb,
  add column expected_signals_present jsonb not null default '[]'::jsonb,
  add column unsupported_claims jsonb not null default '[]'::jsonb,
  add column dimension_reasons jsonb not null default '{}'::jsonb;

create or replace function public.record_interview_evidence(
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
  p_dimension_reasons jsonb
)
returns table(question_id uuid, session_id uuid)
language plpgsql
security invoker
set search_path = public
as $$
declare
  v_user_id uuid := auth.uid();
  v_question public.interview_questions%rowtype;
  v_session public.interview_sessions%rowtype;
  v_competency public.competencies%rowtype;
  v_score numeric := greatest(0::numeric, least(10::numeric, coalesce(p_score, 0)));
  v_count integer;
  v_average numeric;
  v_strengths jsonb;
  v_weaknesses jsonb;
begin
  if v_user_id is null then
    raise exception 'Authentication is required' using errcode = '42501';
  end if;

  select q.* into v_question
  from public.interview_questions q
  join public.interview_sessions s on s.id = q.session_id and s.user_id = q.user_id
  where q.id = p_question_id
    and q.user_id = v_user_id
    and s.status = 'active'
  for update of q;

  if not found then
    raise exception 'Active owned question was not found' using errcode = 'P0002';
  end if;
  if v_question.answer is not null then
    raise exception 'Question already has evidence' using errcode = '23505';
  end if;

  select * into v_session from public.interview_sessions
  where id = v_question.session_id and user_id = v_user_id
  for update;

  update public.interview_questions
  set answer = p_answer, answered_at = now(), updated_at = now()
  where id = v_question.id and user_id = v_user_id;

  insert into public.question_evaluations (
    user_id,
    question_id,
    overall_score,
    dimensions,
    strengths,
    weaknesses,
    missing_points,
    better_structure,
    improved_answer,
    relevance,
    supported_claims,
    expected_signals_present,
    unsupported_claims,
    dimension_reasons,
    updated_at
  ) values (
    v_user_id,
    v_question.id,
    v_score,
    coalesce(p_dimensions, '{}'::jsonb),
    coalesce(p_strengths, '[]'::jsonb),
    coalesce(p_needs_work, '[]'::jsonb),
    coalesce(p_missing_points, '[]'::jsonb),
    coalesce(p_better_structure, '[]'::jsonb),
    trim(coalesce(p_improved_answer, '')),
    p_relevance,
    coalesce(p_supported_claims, '[]'::jsonb),
    coalesce(p_expected_signals_present, '[]'::jsonb),
    coalesce(p_unsupported_claims, '[]'::jsonb),
    coalesce(p_dimension_reasons, '{}'::jsonb),
    now()
  );

  if v_question.competency_id is not null then
    select * into v_competency from public.competencies
    where id = v_question.competency_id and user_id = v_user_id
    for update;

    if found then
      v_count := greatest(0, coalesce(v_competency.question_count, 0)) + 1;
      v_average := greatest(0::numeric, least(10::numeric,
        ((greatest(0, coalesce(v_competency.question_count, 0)) * greatest(0::numeric, least(10::numeric, coalesce(v_competency.average_score, 0)))) + v_score) / v_count
      ));

      with values as (
        select value, ordinality as position
        from jsonb_array_elements_text(coalesce(v_competency.strengths, '[]'::jsonb) || coalesce(p_strengths, '[]'::jsonb)) with ordinality
      ), latest as (
        select value, max(position) as position from values where length(value) > 0 group by value order by max(position) desc limit 5
      ) select coalesce(jsonb_agg(value order by position), '[]'::jsonb) into v_strengths from latest;

      with values as (
        select value, ordinality as position
        from jsonb_array_elements_text(coalesce(v_competency.weaknesses, '[]'::jsonb) || coalesce(p_needs_work, '[]'::jsonb)) with ordinality
      ), latest as (
        select value, max(position) as position from values where length(value) > 0 group by value order by max(position) desc limit 5
      ) select coalesce(jsonb_agg(value order by position), '[]'::jsonb) into v_weaknesses from latest;

      update public.competencies
      set question_count = v_count,
          average_score = v_average,
          recent_score = v_score,
          estimated_level = case when v_average < 5.5 then 'intermediate' when v_average < 7.5 then 'senior' else 'advanced' end,
          confidence = case when v_count < 3 then 0.25 when v_count < 6 then 0.6 else 0.9 end,
          last_practiced_at = now(),
          strengths = v_strengths,
          weaknesses = v_weaknesses,
          updated_at = now()
      where id = v_competency.id and user_id = v_user_id;
    end if;
  end if;

  return query select v_question.id, v_session.id;
end;
$$;

revoke all on function public.record_interview_evidence(uuid, text, numeric, jsonb, jsonb, jsonb, jsonb, jsonb, text, numeric, jsonb, jsonb, jsonb, jsonb) from public;
grant execute on function public.record_interview_evidence(uuid, text, numeric, jsonb, jsonb, jsonb, jsonb, jsonb, text, numeric, jsonb, jsonb, jsonb, jsonb) to authenticated;

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

create or replace function public.complete_hands_on_session(
  p_session_id uuid,
  p_overall_score numeric,
  p_summary text,
  p_evaluations jsonb
)
returns table(session_id uuid)
language plpgsql
security invoker
set search_path = public
as $$
declare
  v_user_id uuid := auth.uid();
  v_session public.interview_sessions%rowtype;
  v_evaluation jsonb;
  v_competency_id uuid;
  v_competency_name text;
  v_score numeric;
begin
  if v_user_id is null then
    raise exception 'Authentication is required' using errcode = '42501';
  end if;
  if p_evaluations is null or jsonb_typeof(p_evaluations) <> 'array' or jsonb_array_length(p_evaluations) = 0 then
    raise exception 'Hands-on evaluations are required' using errcode = '22023';
  end if;

  select * into v_session
  from public.interview_sessions
  where id = p_session_id
    and user_id = v_user_id
    and kind = 'hands-on'
    and status = 'active'
  for update;

  if not found then
    raise exception 'Active owned hands-on session was not found' using errcode = 'P0002';
  end if;

  for v_evaluation in select value from jsonb_array_elements(p_evaluations)
  loop
    v_competency_name := coalesce(nullif(trim(v_evaluation ->> 'competency'), ''), 'Communication');
    v_score := greatest(0::numeric, least(10::numeric, coalesce((v_evaluation ->> 'score')::numeric, 0)));

    select id into v_competency_id
    from public.competencies
    where user_id = v_user_id
      and is_active
      and normalized_name = lower(v_competency_name)
    limit 1;

    insert into public.session_evaluations (
      user_id,
      session_id,
      competency_id,
      competency_name,
      overall_score,
      dimensions,
      strengths,
      weaknesses,
      relevance,
      supported_claims,
      expected_signals_present,
      unsupported_claims,
      dimension_reasons
    ) values (
      v_user_id,
      p_session_id,
      v_competency_id,
      v_competency_name,
      v_score,
      coalesce(v_evaluation -> 'dimensions', '{}'::jsonb),
      coalesce(v_evaluation -> 'strengths', '[]'::jsonb),
      coalesce(v_evaluation -> 'needs_work', '[]'::jsonb),
      (v_evaluation ->> 'relevance')::numeric,
      coalesce(v_evaluation -> 'supported_claims', '[]'::jsonb),
      coalesce(v_evaluation -> 'expected_signals_present', '[]'::jsonb),
      coalesce(v_evaluation -> 'unsupported_claims', '[]'::jsonb),
      coalesce(v_evaluation -> 'dimension_reasons', '{}'::jsonb)
    );

    if v_competency_id is not null then
      perform public.apply_owned_competency_evidence(
        v_competency_id,
        v_score,
        coalesce(v_evaluation -> 'strengths', '[]'::jsonb),
        coalesce(v_evaluation -> 'needs_work', '[]'::jsonb)
      );
    end if;
  end loop;

  update public.interview_sessions
  set status = 'complete',
      completed_at = now(),
      overall_score = greatest(0::numeric, least(10::numeric, coalesce(p_overall_score, 0))),
      result_summary = jsonb_build_object('summary', coalesce(p_summary, '')),
      updated_at = now()
  where id = p_session_id
    and user_id = v_user_id;

  return query select v_session.id;
end;
$$;

revoke all on function public.complete_hands_on_session(uuid, numeric, text, jsonb) from public;
grant execute on function public.complete_hands_on_session(uuid, numeric, text, jsonb) to authenticated;
