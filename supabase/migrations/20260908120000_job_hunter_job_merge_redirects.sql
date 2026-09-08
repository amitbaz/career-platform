-- Job merge redirects (#145) ------------------------------------------------------------
--
-- `job_hunter_merge_jobs` deletes the duplicate job row, so any id a caller
-- is still holding for it becomes a dangling reference. Discovery merges
-- while it is building the run's shortlist, so a job selected early in
-- `collect_candidates` can be merged away by a later canonical resolution in
-- the same run; writing that job's evaluation then fails the
-- `(job_id, user_id)` foreign key with SQLSTATE 23503 and, before this
-- change, aborted the whole daily run.
--
-- Nothing recorded where a deleted job went: application events and company
-- watches are reassigned, but a caller holding the id had no way to ask.
-- This table is that record. It is written inside the merge transaction, so
-- a redirect exists for exactly the merges that actually happened, and it is
-- durable rather than run-scoped: a redirect a later run (or the Telegram
-- callback path, which does not consult it yet) needs is still there.

create table public.job_hunter_job_merges (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  -- Deliberately no foreign key: this id names a row that has just been
  -- deleted, which is the entire point of the record.
  duplicate_id uuid not null,
  survivor_id uuid not null,
  merged_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  unique (user_id, duplicate_id),
  -- The survivor does still exist, and if it is ever deleted outright the
  -- redirect is meaningless, so let it go with the row it points at.
  foreign key (survivor_id, user_id)
    references public.job_hunter_jobs (id, user_id) on delete cascade
);

-- Repointing a chain (see the function below) looks rows up by survivor.
create index job_hunter_job_merges_survivor_idx
  on public.job_hunter_job_merges (user_id, survivor_id);

alter table public.job_hunter_job_merges enable row level security;
create policy select_own on public.job_hunter_job_merges
  for select to authenticated using ((select auth.uid()) = user_id);
create policy insert_own on public.job_hunter_job_merges
  for insert to authenticated with check ((select auth.uid()) = user_id);
create policy update_own on public.job_hunter_job_merges
  for update to authenticated using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);
create policy delete_own on public.job_hunter_job_merges
  for delete to authenticated using ((select auth.uid()) = user_id);

-- merge_jobs, re-created --------------------------------------------------------------
--
-- Unchanged from 202609060004 apart from the two statements that record the
-- redirect immediately before the duplicate row is deleted.
create or replace function public.job_hunter_merge_jobs(p_survivor uuid, p_duplicate uuid)
returns uuid
language plpgsql
security invoker
set search_path = ''
as $$
declare
  v_uid uuid := (select auth.uid());
  v_survivor_id uuid;
  v_duplicate_id uuid;
  v_survivor public.job_hunter_jobs%rowtype;
  v_duplicate public.job_hunter_jobs%rowtype;
  v_survivor_has_ats boolean;
  v_duplicate_has_ats boolean;
  v_prefer_duplicate boolean;
  v_canonical_url text;
  v_url text;
  v_description text;
  v_confidence text;
begin
  if p_survivor = p_duplicate then
    return p_survivor;
  end if;

  select j.id
    into v_survivor_id
    from public.job_hunter_jobs j
   where j.user_id = v_uid
     and j.id in (p_survivor, p_duplicate)
   order by
     (exists (select 1 from public.job_hunter_application_events a
               where a.job_id = j.id and a.user_id = j.user_id)) desc,
     (exists (select 1 from public.job_hunter_evaluations e
               where e.job_id = j.id and e.user_id = j.user_id)
      or exists (select 1 from public.job_hunter_materials m
                  where m.job_id = j.id and m.user_id = j.user_id)
      or exists (select 1 from public.job_hunter_deliveries d
                  where d.job_id = j.id and d.user_id = j.user_id)) desc,
     j.first_seen_at asc,
     j.id asc
   limit 1;

  v_duplicate_id := case when v_survivor_id = p_survivor then p_duplicate else p_survivor end;

  select * into v_survivor from public.job_hunter_jobs j
   where j.id = v_survivor_id and j.user_id = v_uid;
  select * into v_duplicate from public.job_hunter_jobs j
   where j.id = v_duplicate_id and j.user_id = v_uid;

  if v_survivor.id is null or v_duplicate.id is null then
    raise exception 'survivor and duplicate jobs must both exist';
  end if;

  v_survivor_has_ats := coalesce(v_survivor.ats_provider, '') <> ''
                    and coalesce(v_survivor.ats_board, '') <> ''
                    and coalesce(v_survivor.ats_job_id, '') <> '';
  v_duplicate_has_ats := coalesce(v_duplicate.ats_provider, '') <> ''
                     and coalesce(v_duplicate.ats_board, '') <> ''
                     and coalesce(v_duplicate.ats_job_id, '') <> '';
  v_prefer_duplicate := v_duplicate_has_ats and not v_survivor_has_ats;

  v_canonical_url := case
    when v_prefer_duplicate then v_duplicate.canonical_url
    else coalesce(nullif(v_survivor.canonical_url, ''), v_duplicate.canonical_url)
  end;
  v_url := case
    when v_prefer_duplicate then v_duplicate.url
    else coalesce(nullif(v_survivor.url, ''), v_duplicate.url)
  end;
  if coalesce(v_canonical_url, '') <> '' and (v_survivor_has_ats or v_duplicate_has_ats) then
    v_url := v_canonical_url;
  end if;

  -- _better_description: an empty side never wins; otherwise the more
  -- trustworthy tier wins, and only on a tie does the longer text win.
  if coalesce(v_duplicate.description, '') = '' then
    v_description := v_survivor.description;
    v_confidence := v_survivor.content_confidence;
  elsif coalesce(v_survivor.description, '') = '' then
    v_description := v_duplicate.description;
    v_confidence := v_duplicate.content_confidence;
  elsif public.job_hunter_confidence_rank(v_duplicate.content_confidence)
      < public.job_hunter_confidence_rank(v_survivor.content_confidence) then
    v_description := v_duplicate.description;
    v_confidence := v_duplicate.content_confidence;
  elsif public.job_hunter_confidence_rank(v_duplicate.content_confidence)
      > public.job_hunter_confidence_rank(v_survivor.content_confidence) then
    v_description := v_survivor.description;
    v_confidence := v_survivor.content_confidence;
  elsif length(regexp_replace(v_duplicate.description, '^\s+|\s+$', '', 'g'))
      > length(regexp_replace(v_survivor.description, '^\s+|\s+$', '', 'g')) then
    v_description := v_duplicate.description;
    v_confidence := v_duplicate.content_confidence;
  else
    v_description := v_survivor.description;
    v_confidence := v_survivor.content_confidence;
  end if;

  update public.job_hunter_jobs j set
    source             = coalesce(nullif(v_survivor.source, ''), v_duplicate.source),
    source_job_id      = coalesce(nullif(v_survivor.source_job_id, ''), v_duplicate.source_job_id),
    url                = v_url,
    company            = coalesce(nullif(v_survivor.company, ''), v_duplicate.company),
    title              = coalesce(nullif(v_survivor.title, ''), v_duplicate.title),
    location           = coalesce(nullif(v_survivor.location, ''), v_duplicate.location),
    remote             = coalesce(v_survivor.remote, v_duplicate.remote),
    description        = v_description,
    description_hash   = encode(sha256(convert_to(v_description, 'UTF8')), 'hex'),
    canonical_url      = v_canonical_url,
    ats_provider       = case when v_prefer_duplicate then v_duplicate.ats_provider
                              else coalesce(nullif(v_survivor.ats_provider, ''), v_duplicate.ats_provider) end,
    ats_board          = case when v_prefer_duplicate then v_duplicate.ats_board
                              else coalesce(nullif(v_survivor.ats_board, ''), v_duplicate.ats_board) end,
    ats_job_id         = case when v_prefer_duplicate then v_duplicate.ats_job_id
                              else coalesce(nullif(v_survivor.ats_job_id, ''), v_duplicate.ats_job_id) end,
    content_confidence = v_confidence,
    first_seen_at      = least(v_survivor.first_seen_at, v_duplicate.first_seen_at),
    last_seen_at       = greatest(v_survivor.last_seen_at, v_duplicate.last_seen_at)
  where j.id = v_survivor_id and j.user_id = v_uid;

  -- Union the provenance rows. SQLite's scalar MIN/MAX in the upsert
  -- become LEAST/GREATEST; the two-column unique key is unchanged.
  insert into public.job_hunter_job_sources as tgt
    (user_id, job_id, source, source_job_id, source_url, identity_key,
     first_seen_at, last_seen_at)
  select v_uid, v_survivor_id, src.source, src.source_job_id, src.source_url,
         src.identity_key, src.first_seen_at, src.last_seen_at
    from public.job_hunter_job_sources src
   where src.job_id = v_duplicate_id and src.user_id = v_uid
   order by src.created_at, src.id
  on conflict (job_id, identity_key) do update set
    first_seen_at = least(tgt.first_seen_at, excluded.first_seen_at),
    last_seen_at  = greatest(tgt.last_seen_at, excluded.last_seen_at);

  delete from public.job_hunter_job_sources s
   where s.job_id = v_duplicate_id and s.user_id = v_uid;

  -- SQLite had no natural key on these three tables, so reassigning job_id
  -- could never conflict. #87's unique constraints mean it can, so a
  -- duplicate row that would collide with an identical survivor row is
  -- dropped instead of raising. Both rows describe the same event at the
  -- same instant, so nothing is lost.
  delete from public.job_hunter_evaluations d
   where d.job_id = v_duplicate_id and d.user_id = v_uid
     and exists (select 1 from public.job_hunter_evaluations s
                  where s.job_id = v_survivor_id and s.user_id = v_uid
                    and s.evaluated_at = d.evaluated_at);
  update public.job_hunter_evaluations e set job_id = v_survivor_id
   where e.job_id = v_duplicate_id and e.user_id = v_uid;

  delete from public.job_hunter_materials d
   where d.job_id = v_duplicate_id and d.user_id = v_uid
     and exists (select 1 from public.job_hunter_materials s
                  where s.job_id = v_survivor_id and s.user_id = v_uid
                    and s.generated_at = d.generated_at);
  update public.job_hunter_materials m set job_id = v_survivor_id
   where m.job_id = v_duplicate_id and m.user_id = v_uid;

  delete from public.job_hunter_deliveries d
   where d.job_id = v_duplicate_id and d.user_id = v_uid
     and exists (select 1 from public.job_hunter_deliveries s
                  where s.job_id = v_survivor_id and s.user_id = v_uid
                    and s.delivery_type = d.delivery_type
                    and s.delivered_at = d.delivered_at);
  update public.job_hunter_deliveries dl set job_id = v_survivor_id
   where dl.job_id = v_duplicate_id and dl.user_id = v_uid;

  update public.job_hunter_application_events a set job_id = v_survivor_id
   where a.job_id = v_duplicate_id and a.user_id = v_uid;

  update public.job_hunter_company_watch w set discovered_from_job_id = v_survivor_id
   where w.discovered_from_job_id = v_duplicate_id and w.user_id = v_uid;

  -- Record where the duplicate went before its row disappears. Rows that
  -- already pointed at the duplicate are repointed first, so every redirect
  -- names a job that still exists and a reader never has to walk a chain.
  update public.job_hunter_job_merges m
     set survivor_id = v_survivor_id,
         merged_at = now()
   where m.survivor_id = v_duplicate_id and m.user_id = v_uid;

  insert into public.job_hunter_job_merges (user_id, duplicate_id, survivor_id)
  values (v_uid, v_duplicate_id, v_survivor_id)
  on conflict (user_id, duplicate_id) do update
    set survivor_id = excluded.survivor_id,
        merged_at = now();

  -- job_sources and pending_ai_work cascade from the job row, which matches
  -- the SQLite behaviour: the Python never reassigned pending AI work
  -- either, it let the foreign key drop it.
  delete from public.job_hunter_jobs j
   where j.id = v_duplicate_id and j.user_id = v_uid;

  return v_survivor_id;
end $$;
