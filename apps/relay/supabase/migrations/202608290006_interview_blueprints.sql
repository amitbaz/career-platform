alter table public.interview_sessions
  add column blueprint_status text not null default 'grounded'
    check (blueprint_status in ('grounded', 'limited-grounding')),
  add column blueprint_fallback_reason text,
  add column blueprint_max_follow_ups integer not null default 3
    check (blueprint_max_follow_ups between 0 and 3),
  add column blueprint_max_questions integer not null default 8
    check (blueprint_max_questions between 5 and 8);

alter table public.interview_questions
  add column objective text,
  add column evidence_ids jsonb not null default '[]'::jsonb,
  add column expected_signals jsonb not null default '[]'::jsonb,
  add column missing_signal_prompts jsonb not null default '[]'::jsonb,
  add column follow_up_limit integer not null default 0
    check (follow_up_limit between 0 and 3),
  add column source_confidence numeric
    check (source_confidence is null or source_confidence between 0 and 1);

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
