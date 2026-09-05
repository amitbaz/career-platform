-- Task 9's create_conversation_session_with_blueprint persists a coverage
-- target's rubric material onto its question row but drops `required`.
-- The director (src/lib/interview-director.ts) needs `required` to prioritize
-- an unasked required target over deepening an open one when turns run
-- short (spec 9.3 rule 4) -- and that decision runs on every turn against a
-- session freshly reloaded from these rows, so `required` must survive the
-- round trip. Existing rows backfill to `true`, matching this release's
-- coverage targets, which are always required unless explicitly optional.
alter table public.interview_questions
  add column required boolean not null default true;

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
    source_confidence,
    required
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
    target.source_confidence,
    coalesce(target.required, true)
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
    source_confidence numeric,
    required boolean
  );

  return query select v_session_id;
end;
$$;

revoke all on function public.create_conversation_session_with_blueprint(jsonb) from public;
grant execute on function public.create_conversation_session_with_blueprint(jsonb) to authenticated;
