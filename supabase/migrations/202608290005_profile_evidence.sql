alter table public.profiles
  add column profile_ready boolean not null default false,
  add column profile_missing jsonb not null default '[]'::jsonb;

create table public.profile_evidence (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  source_kind text check (source_kind in ('cv', 'cover_letter', 'summary')),
  source_excerpt text not null,
  project_or_employer text,
  ownership text,
  technologies jsonb not null default '[]'::jsonb,
  decision text,
  constraint_text text,
  outcome text,
  recency text,
  confidence numeric not null default 0 check (confidence between 0 and 1),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (id, user_id)
);

create index profile_evidence_user_created_idx on public.profile_evidence (user_id, created_at desc);

alter table public.profile_evidence enable row level security;

create policy select_own on public.profile_evidence for select using (auth.uid() = user_id);
create policy insert_own on public.profile_evidence for insert with check (auth.uid() = user_id);
create policy update_own on public.profile_evidence for update using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy delete_own on public.profile_evidence for delete using (auth.uid() = user_id);

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
  p_evidence jsonb,
  p_profile_ready boolean,
  p_profile_missing jsonb,
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
  v_evidence jsonb;
begin
  if v_user_id is null then
    raise exception 'Authentication is required' using errcode = '42501';
  end if;
  if p_scope is null or jsonb_typeof(p_scope) <> 'array' or jsonb_array_length(p_scope) = 0 then
    raise exception 'Competency scope is required' using errcode = '22023';
  end if;

  insert into public.profiles (
    user_id, role, seniority, summary, narrative, expertise, characteristics, profile_ready, profile_missing, updated_at
  ) values (
    v_user_id, p_role, p_seniority, p_summary, p_narrative,
    coalesce(p_expertise, '[]'::jsonb), coalesce(p_characteristics, '[]'::jsonb),
    coalesce(p_profile_ready, false), coalesce(p_profile_missing, '[]'::jsonb), now()
  )
  on conflict (user_id) do update
  set role = excluded.role,
      seniority = excluded.seniority,
      summary = excluded.summary,
      narrative = excluded.narrative,
      expertise = excluded.expertise,
      characteristics = excluded.characteristics,
      profile_ready = excluded.profile_ready,
      profile_missing = excluded.profile_missing,
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

  delete from public.profile_evidence
  where user_id = v_user_id;

  for v_evidence in select value from jsonb_array_elements(coalesce(p_evidence, '[]'::jsonb))
  loop
    insert into public.profile_evidence (
      user_id,
      source_kind,
      source_excerpt,
      project_or_employer,
      ownership,
      technologies,
      decision,
      constraint_text,
      outcome,
      recency,
      confidence,
      updated_at
    ) values (
      v_user_id,
      nullif(trim(v_evidence ->> 'source_kind'), ''),
      trim(coalesce(v_evidence ->> 'source_excerpt', '')),
      nullif(trim(v_evidence ->> 'project_or_employer'), ''),
      nullif(trim(v_evidence ->> 'ownership'), ''),
      coalesce(v_evidence -> 'technologies', '[]'::jsonb),
      nullif(trim(v_evidence ->> 'decision'), ''),
      nullif(trim(v_evidence ->> 'constraint'), ''),
      nullif(trim(v_evidence ->> 'outcome'), ''),
      nullif(trim(v_evidence ->> 'recency'), ''),
      greatest(0::numeric, least(1::numeric, coalesce((v_evidence ->> 'confidence')::numeric, 0))),
      now()
    );
  end loop;

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

revoke all on function public.save_profile_bundle(text, text, text, text, jsonb, jsonb, text, text, text, text, jsonb, boolean, jsonb, jsonb) from public;
grant execute on function public.save_profile_bundle(text, text, text, text, jsonb, jsonb, text, text, text, text, jsonb, boolean, jsonb, jsonb) to authenticated;
