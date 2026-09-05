-- Make profile_evidence identity durable so later Career Brain tables can hold
-- long-lived foreign keys into it. Today `save_profile_bundle` deletes and
-- recreates all evidence on every profile save, so row ids are not stable.
--
-- This migration:
--   1. adds evidence_key/is_active/retired_at to profile_evidence;
--   2. backfills evidence_key for existing rows with a unique legacy key;
--   3. makes evidence_key not null and unique per (user_id, evidence_key);
--   4. replaces save_profile_bundle so evidence is reconciled by stable key
--      (retire what's no longer present, upsert what is) instead of deleted
--      and reinserted. Profile/source_documents/competency behavior is
--      unchanged from 202608290005_profile_evidence.sql.

alter table public.profile_evidence
  add column evidence_key text,
  add column is_active boolean not null default true,
  add column retired_at timestamptz;

update public.profile_evidence
set evidence_key = 'legacy:' || id::text
where evidence_key is null;

alter table public.profile_evidence
  alter column evidence_key set not null;

alter table public.profile_evidence
  add constraint profile_evidence_user_id_evidence_key_key unique (user_id, evidence_key);

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

  -- Evidence reconciliation instead of delete/reinsert: retire everything
  -- currently active, then upsert incoming evidence on (user_id, evidence_key)
  -- so a stable identity survives regeneration and reactivates on conflict.
  -- Evidence that is no longer present stays retired rather than being
  -- deleted, preserving provenance for anything that references it.
  update public.profile_evidence
  set is_active = false,
      retired_at = coalesce(retired_at, now()),
      updated_at = now()
  where user_id = v_user_id
    and is_active = true;

  for v_evidence in select value from jsonb_array_elements(coalesce(p_evidence, '[]'::jsonb))
  loop
    insert into public.profile_evidence (
      user_id,
      evidence_key,
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
      is_active,
      retired_at,
      updated_at
    ) values (
      v_user_id,
      trim(v_evidence ->> 'evidence_key'),
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
      true,
      null,
      now()
    )
    on conflict (user_id, evidence_key) do update
    set source_kind = excluded.source_kind,
        source_excerpt = excluded.source_excerpt,
        project_or_employer = excluded.project_or_employer,
        ownership = excluded.ownership,
        technologies = excluded.technologies,
        decision = excluded.decision,
        constraint_text = excluded.constraint_text,
        outcome = excluded.outcome,
        recency = excluded.recency,
        confidence = excluded.confidence,
        is_active = true,
        retired_at = null,
        updated_at = now();
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
