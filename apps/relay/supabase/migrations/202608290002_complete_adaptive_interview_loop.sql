alter table public.competencies
  add column is_active boolean not null default true;

alter table public.hands_on_checkpoints
  add column interviewer_prompt text;

create table public.session_evaluations (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  session_id uuid not null,
  competency_id uuid,
  competency_name text not null,
  overall_score numeric not null check (overall_score between 0 and 10),
  dimensions jsonb not null default '{}'::jsonb,
  strengths jsonb not null default '[]'::jsonb,
  weaknesses jsonb not null default '[]'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  foreign key (session_id, user_id) references public.interview_sessions (id, user_id) on delete cascade,
  foreign key (competency_id, user_id) references public.competencies (id, user_id) on delete set null (competency_id)
);

create unique index session_evaluations_session_competency_key
  on public.session_evaluations (session_id, lower(competency_name));
create index session_evaluations_user_created_idx
  on public.session_evaluations (user_id, created_at desc);

alter table public.session_evaluations enable row level security;

create policy select_own on public.session_evaluations for select using (auth.uid() = user_id);
create policy insert_own on public.session_evaluations for insert with check (auth.uid() = user_id);
create policy update_own on public.session_evaluations for update using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy delete_own on public.session_evaluations for delete using (auth.uid() = user_id);

create or replace function public.apply_owned_competency_evidence(
  p_competency_id uuid,
  p_score numeric,
  p_strengths jsonb,
  p_needs_work jsonb
)
returns void
language plpgsql
security invoker
set search_path = public
as $$
declare
  v_user_id uuid := auth.uid();
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

  select * into v_competency
  from public.competencies
  where id = p_competency_id
    and user_id = v_user_id
  for update;

  if not found then
    return;
  end if;

  v_count := greatest(0, coalesce(v_competency.question_count, 0)) + 1;
  v_average := greatest(0::numeric, least(10::numeric,
    ((greatest(0, coalesce(v_competency.question_count, 0))
      * greatest(0::numeric, least(10::numeric, coalesce(v_competency.average_score, 0)))) + v_score) / v_count
  ));

  with evidence_values as (
    select value, ordinality as position
    from jsonb_array_elements_text(
      coalesce(v_competency.strengths, '[]'::jsonb) || coalesce(p_strengths, '[]'::jsonb)
    ) with ordinality
  ), latest as (
    select value, max(position) as position
    from evidence_values
    where length(value) > 0
    group by value
    order by max(position) desc
    limit 5
  )
  select coalesce(jsonb_agg(value order by position), '[]'::jsonb)
  into v_strengths
  from latest;

  with evidence_values as (
    select value, ordinality as position
    from jsonb_array_elements_text(
      coalesce(v_competency.weaknesses, '[]'::jsonb) || coalesce(p_needs_work, '[]'::jsonb)
    ) with ordinality
  ), latest as (
    select value, max(position) as position
    from evidence_values
    where length(value) > 0
    group by value
    order by max(position) desc
    limit 5
  )
  select coalesce(jsonb_agg(value order by position), '[]'::jsonb)
  into v_weaknesses
  from latest;

  update public.competencies
  set question_count = v_count,
      average_score = v_average,
      recent_score = v_score,
      estimated_level = case
        when v_average < 5.5 then 'intermediate'
        when v_average < 7.5 then 'senior'
        else 'advanced'
      end,
      confidence = case when v_count < 3 then 0.25 when v_count < 6 then 0.6 else 0.9 end,
      last_practiced_at = now(),
      strengths = v_strengths,
      weaknesses = v_weaknesses,
      updated_at = now()
  where id = p_competency_id
    and user_id = v_user_id;
end;
$$;

revoke all on function public.apply_owned_competency_evidence(uuid, numeric, jsonb, jsonb) from public;
grant execute on function public.apply_owned_competency_evidence(uuid, numeric, jsonb, jsonb) to authenticated;

create or replace function public.record_conversation_turn(
  p_question_id uuid,
  p_answer text,
  p_score numeric,
  p_dimensions jsonb,
  p_strengths jsonb,
  p_needs_work jsonb,
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
    p_needs_work
  );

  if p_follow_up is not null then
    select count(*), count(*) filter (where is_follow_up)
    into v_total, v_follow_ups
    from public.interview_questions
    where session_id = v_question.session_id
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
      where session_id = v_question.session_id
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

revoke all on function public.record_conversation_turn(uuid, text, numeric, jsonb, jsonb, jsonb, uuid, text, jsonb) from public;
grant execute on function public.record_conversation_turn(uuid, text, numeric, jsonb, jsonb, jsonb, uuid, text, jsonb) to authenticated;

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
      weaknesses
    ) values (
      v_user_id,
      p_session_id,
      v_competency_id,
      v_competency_name,
      v_score,
      coalesce(v_evaluation -> 'dimensions', '{}'::jsonb),
      coalesce(v_evaluation -> 'strengths', '[]'::jsonb),
      coalesce(v_evaluation -> 'needs_work', '[]'::jsonb)
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

create or replace function public.save_profile_bundle(
  p_role text,
  p_seniority text,
  p_summary text,
  p_narrative text,
  p_expertise jsonb,
  p_characteristics jsonb,
  p_cv_text text,
  p_cv_file_name text,
  p_cover_letter_text text,
  p_cover_letter_file_name text,
  p_scope jsonb
)
returns void
language plpgsql
security invoker
set search_path = public
as $$
declare
  v_user_id uuid := auth.uid();
  v_scope jsonb;
begin
  if v_user_id is null then
    raise exception 'Authentication is required' using errcode = '42501';
  end if;
  if p_scope is null or jsonb_typeof(p_scope) <> 'array' or jsonb_array_length(p_scope) = 0 then
    raise exception 'Competency scope is required' using errcode = '22023';
  end if;

  insert into public.profiles (
    user_id, role, seniority, summary, narrative, expertise, characteristics, updated_at
  ) values (
    v_user_id, p_role, p_seniority, p_summary, p_narrative,
    coalesce(p_expertise, '[]'::jsonb), coalesce(p_characteristics, '[]'::jsonb), now()
  )
  on conflict (user_id) do update
  set role = excluded.role,
      seniority = excluded.seniority,
      summary = excluded.summary,
      narrative = excluded.narrative,
      expertise = excluded.expertise,
      characteristics = excluded.characteristics,
      updated_at = now();

  delete from public.source_documents
  where user_id = v_user_id;

  if length(trim(coalesce(p_cv_text, ''))) > 0 then
    insert into public.source_documents (user_id, kind, file_name, content)
    values (v_user_id, 'cv', p_cv_file_name, p_cv_text);
  end if;
  if length(trim(coalesce(p_cover_letter_text, ''))) > 0 then
    insert into public.source_documents (user_id, kind, file_name, content)
    values (v_user_id, 'cover_letter', p_cover_letter_file_name, p_cover_letter_text);
  end if;

  update public.competencies
  set is_active = false,
      updated_at = now()
  where user_id = v_user_id;

  for v_scope in select value from jsonb_array_elements(p_scope)
  loop
    insert into public.competencies (
      user_id, name, relevance, expected_level, is_active, updated_at
    ) values (
      v_user_id,
      trim(v_scope ->> 'name'),
      greatest(0::numeric, least(1::numeric, coalesce((v_scope ->> 'relevance')::numeric, 0))),
      v_scope ->> 'expected_level',
      true,
      now()
    )
    on conflict (user_id, normalized_name) do update
    set name = excluded.name,
        relevance = excluded.relevance,
        expected_level = excluded.expected_level,
        is_active = true,
        updated_at = now();
  end loop;
end;
$$;

revoke all on function public.save_profile_bundle(text, text, text, text, jsonb, jsonb, text, text, text, text, jsonb) from public;
grant execute on function public.save_profile_bundle(text, text, text, text, jsonb, jsonb, text, text, text, text, jsonb) to authenticated;
