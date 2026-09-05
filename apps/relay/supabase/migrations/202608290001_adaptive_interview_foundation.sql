create table public.profiles (
  user_id uuid primary key references auth.users(id) on delete cascade,
  role text,
  seniority text,
  summary text,
  narrative text,
  expertise jsonb not null default '[]'::jsonb,
  characteristics jsonb not null default '[]'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table public.source_documents (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  kind text not null check (kind in ('cv', 'cover_letter')),
  file_name text,
  content text,
  storage_path text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table public.competencies (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  name text not null,
  normalized_name text generated always as (lower(name)) stored,
  relevance numeric not null default 0 check (relevance between 0 and 1),
  expected_level text not null check (expected_level in ('foundational', 'intermediate', 'senior', 'advanced')),
  estimated_level text check (estimated_level in ('foundational', 'intermediate', 'senior', 'advanced')),
  confidence numeric check (confidence between 0 and 1),
  last_practiced_at timestamptz,
  question_count integer not null default 0 check (question_count >= 0),
  average_score numeric,
  recent_score numeric,
  strengths jsonb not null default '[]'::jsonb,
  weaknesses jsonb not null default '[]'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (id, user_id),
  unique (user_id, normalized_name)
);

create table public.interview_sessions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  kind text not null,
  status text not null default 'active',
  started_at timestamptz not null default now(),
  completed_at timestamptz,
  exercise jsonb not null default '{}'::jsonb,
  result_summary jsonb not null default '{}'::jsonb,
  overall_score numeric,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (id, user_id)
);

create table public.interview_questions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  session_id uuid not null,
  sequence integer not null check (sequence > 0),
  category text not null,
  competency_id uuid,
  difficulty text not null check (difficulty in ('foundational', 'intermediate', 'senior', 'advanced')),
  is_follow_up boolean not null default false,
  prompt text not null,
  answer text,
  asked_at timestamptz,
  answered_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (id, user_id),
  foreign key (session_id, user_id) references public.interview_sessions (id, user_id) on delete cascade,
  foreign key (competency_id, user_id) references public.competencies (id, user_id) on delete set null (competency_id)
);

create table public.question_evaluations (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  question_id uuid not null,
  overall_score numeric not null,
  dimensions jsonb not null default '{}'::jsonb,
  strengths jsonb not null default '[]'::jsonb,
  weaknesses jsonb not null default '[]'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  foreign key (question_id, user_id) references public.interview_questions (id, user_id) on delete cascade
);

create table public.hands_on_checkpoints (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  session_id uuid not null,
  code text not null,
  note text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  foreign key (session_id, user_id) references public.interview_sessions (id, user_id) on delete cascade
);

create index interview_sessions_user_created_idx on public.interview_sessions (user_id, created_at desc);
create unique index interview_questions_session_sequence_key on public.interview_questions (session_id, sequence);
create unique index question_evaluations_question_key on public.question_evaluations (question_id);
create index hands_on_checkpoints_session_created_idx on public.hands_on_checkpoints (session_id, created_at desc);

alter table public.profiles enable row level security;
alter table public.source_documents enable row level security;
alter table public.competencies enable row level security;
alter table public.interview_sessions enable row level security;
alter table public.interview_questions enable row level security;
alter table public.question_evaluations enable row level security;
alter table public.hands_on_checkpoints enable row level security;

create policy select_own on public.profiles for select using (auth.uid() = user_id);
create policy insert_own on public.profiles for insert with check (auth.uid() = user_id);
create policy update_own on public.profiles for update using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy delete_own on public.profiles for delete using (auth.uid() = user_id);

create policy select_own on public.source_documents for select using (auth.uid() = user_id);
create policy insert_own on public.source_documents for insert with check (auth.uid() = user_id);
create policy update_own on public.source_documents for update using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy delete_own on public.source_documents for delete using (auth.uid() = user_id);

create policy select_own on public.competencies for select using (auth.uid() = user_id);
create policy insert_own on public.competencies for insert with check (auth.uid() = user_id);
create policy update_own on public.competencies for update using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy delete_own on public.competencies for delete using (auth.uid() = user_id);

create policy select_own on public.interview_sessions for select using (auth.uid() = user_id);
create policy insert_own on public.interview_sessions for insert with check (auth.uid() = user_id);
create policy update_own on public.interview_sessions for update using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy delete_own on public.interview_sessions for delete using (auth.uid() = user_id);

create policy select_own on public.interview_questions for select using (auth.uid() = user_id);
create policy insert_own on public.interview_questions for insert with check (auth.uid() = user_id);
create policy update_own on public.interview_questions for update using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy delete_own on public.interview_questions for delete using (auth.uid() = user_id);

create policy select_own on public.question_evaluations for select using (auth.uid() = user_id);
create policy insert_own on public.question_evaluations for insert with check (auth.uid() = user_id);
create policy update_own on public.question_evaluations for update using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy delete_own on public.question_evaluations for delete using (auth.uid() = user_id);

create policy select_own on public.hands_on_checkpoints for select using (auth.uid() = user_id);
create policy insert_own on public.hands_on_checkpoints for insert with check (auth.uid() = user_id);
create policy update_own on public.hands_on_checkpoints for update using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy delete_own on public.hands_on_checkpoints for delete using (auth.uid() = user_id);

insert into storage.buckets (id, name, public)
values ('career-documents', 'career-documents', false);

create policy career_documents_select_own on storage.objects for select
using (bucket_id = 'career-documents' and (storage.foldername(name))[1] = auth.uid()::text);
create policy career_documents_insert_own on storage.objects for insert
with check (bucket_id = 'career-documents' and (storage.foldername(name))[1] = auth.uid()::text);
create policy career_documents_update_own on storage.objects for update
using (bucket_id = 'career-documents' and (storage.foldername(name))[1] = auth.uid()::text)
with check (bucket_id = 'career-documents' and (storage.foldername(name))[1] = auth.uid()::text);
create policy career_documents_delete_own on storage.objects for delete
using (bucket_id = 'career-documents' and (storage.foldername(name))[1] = auth.uid()::text);

create or replace function public.record_interview_evidence(
  p_question_id uuid,
  p_answer text,
  p_score numeric,
  p_dimensions jsonb,
  p_strengths jsonb,
  p_needs_work jsonb
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
    user_id, question_id, overall_score, dimensions, strengths, weaknesses, updated_at
  ) values (
    v_user_id, v_question.id, v_score, coalesce(p_dimensions, '{}'::jsonb),
    coalesce(p_strengths, '[]'::jsonb), coalesce(p_needs_work, '[]'::jsonb), now()
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

grant execute on function public.record_interview_evidence(uuid, text, numeric, jsonb, jsonb, jsonb) to authenticated;

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
  v_has_follow_up boolean;
begin
  if v_user_id is null then
    raise exception 'Authentication is required' using errcode = '42501';
  end if;
  if jsonb_typeof(p_plan) <> 'array' or jsonb_array_length(p_plan) <> 5 then
    raise exception 'Conversation plan must contain the five-question backbone' using errcode = '22023';
  end if;

  select count(*), min(sequence), max(sequence), count(distinct sequence),
    array_agg(category order by sequence), bool_or(is_follow_up)
  into v_count, v_min_sequence, v_max_sequence, v_distinct_sequences, v_categories, v_has_follow_up
  from jsonb_to_recordset(p_plan) as plan(
    sequence integer,
    category text,
    competency_id uuid,
    difficulty text,
    is_follow_up boolean,
    prompt text
  );

  if v_count <> 5
    or v_min_sequence <> 1
    or v_max_sequence <> 5
    or v_distinct_sequences <> 5
    or v_has_follow_up
    or v_categories <> array['introduction', 'experience', 'technical', 'architecture', 'behavioral']::text[] then
    raise exception 'Conversation plan must contain the exact five-question backbone' using errcode = '22023';
  end if;

  insert into public.interview_sessions (user_id, kind, status)
  values (v_user_id, 'conversation', 'active')
  returning id into v_session_id;

  insert into public.interview_questions (
    user_id, session_id, sequence, category, competency_id, difficulty, is_follow_up, prompt, asked_at
  )
  select v_user_id, v_session_id, plan.sequence, plan.category, plan.competency_id,
    plan.difficulty, plan.is_follow_up, plan.prompt, now()
  from jsonb_to_recordset(p_plan) as plan(
    sequence integer,
    category text,
    competency_id uuid,
    difficulty text,
    is_follow_up boolean,
    prompt text
  );

  return query select v_session_id;
end;
$$;

grant execute on function public.create_conversation_session_with_plan(jsonb) to authenticated;
