-- Career Brain Release 1: the canonical opportunity lifecycle.
--
-- `opportunities` is one durable record covering the full lifecycle from a
-- saved/shortlisted role through its outcome -- both roles a user is only
-- considering and roles they have actually applied to or interviewed for.
-- It is deliberately not called `applications`.
--
-- `opportunity_events` is its append-only history: normal application code
-- may select/insert but must never update or delete an event directly, so
-- it gets no update/delete RLS policy. History changes only alongside the
-- summary row, through the two narrow RPCs below, so the two can never
-- disagree. Parent deletion cascades the history away.
--
-- Both tables carry a composite id/user_id uniqueness constraint so later
-- tables (observation evidence, practice plan links, interview sessions)
-- can hold ownership-preserving composite foreign keys into them.

create table public.opportunities (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  company text not null,
  role text not null,
  status text not null default 'considering'
    check (status in ('considering', 'applied', 'interviewing', 'offer', 'rejected', 'withdrawn', 'closed')),
  location text,
  remote boolean,
  job_url text,
  job_description text,
  source_label text,
  source_system text,
  source_external_id text,
  match_score numeric check (match_score is null or match_score between 0 and 100),
  strengths jsonb not null default '[]'::jsonb,
  gaps jsonb not null default '[]'::jsonb,
  notes text,
  applied_at timestamptz,
  next_interview_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (id, user_id)
);

-- job_url is deliberately not unique: employers and aggregators reuse/change
-- URLs. Only the (source_system, source_external_id) pair is a stable
-- source-owned identity, and only when both are present.
create unique index opportunities_user_source_identity_key
  on public.opportunities (user_id, source_system, source_external_id)
  where source_system is not null and source_external_id is not null;

create index opportunities_user_status_idx on public.opportunities (user_id, status);
create index opportunities_user_next_interview_idx on public.opportunities (user_id, next_interview_at);
create index opportunities_user_updated_idx on public.opportunities (user_id, updated_at desc);

create table public.opportunity_events (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  opportunity_id uuid not null,
  event_type text not null
    check (event_type in ('created', 'status_changed', 'interview_scheduled', 'interview_completed', 'note', 'source_updated')),
  from_status text check (from_status is null or from_status in ('considering', 'applied', 'interviewing', 'offer', 'rejected', 'withdrawn', 'closed')),
  to_status text check (to_status is null or to_status in ('considering', 'applied', 'interviewing', 'offer', 'rejected', 'withdrawn', 'closed')),
  occurred_at timestamptz not null default now(),
  note text,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  unique (id, user_id),
  foreign key (opportunity_id, user_id) references public.opportunities (id, user_id) on delete cascade
);

create index opportunity_events_opportunity_occurred_idx on public.opportunity_events (opportunity_id, occurred_at desc);
create index opportunity_events_user_occurred_idx on public.opportunity_events (user_id, occurred_at desc);

alter table public.opportunities enable row level security;
alter table public.opportunity_events enable row level security;

create policy select_own on public.opportunities for select using (auth.uid() = user_id);
create policy insert_own on public.opportunities for insert with check (auth.uid() = user_id);
create policy update_own on public.opportunities for update using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy delete_own on public.opportunities for delete using (auth.uid() = user_id);

-- opportunity_events is append-oriented: own-row select/insert only. No
-- update/delete policy exists, so normal authenticated clients cannot mutate
-- history directly; it is only ever removed via the parent's delete cascade.
create policy select_own on public.opportunity_events for select using (auth.uid() = user_id);
create policy insert_own on public.opportunity_events for insert with check (auth.uid() = user_id);

-- Inserts and records the opportunity's `created` history event in one
-- transaction. Status is always `considering` -- callers cannot choose a
-- different starting status, so the summary row and its first event can
-- never disagree.
create or replace function public.create_opportunity(
  p_company text,
  p_role text,
  p_location text,
  p_remote boolean,
  p_job_url text,
  p_job_description text,
  p_source_label text,
  p_source_system text,
  p_source_external_id text,
  p_match_score numeric,
  p_strengths jsonb,
  p_gaps jsonb,
  p_notes text
)
returns table(opportunity_id uuid)
language plpgsql
security invoker
set search_path = public
as $$
declare
  v_user_id uuid := auth.uid();
  v_opportunity_id uuid;
begin
  if v_user_id is null then
    raise exception 'Authentication is required' using errcode = '42501';
  end if;
  if p_company is null or length(trim(p_company)) = 0 then
    raise exception 'Company is required' using errcode = '22023';
  end if;
  if p_role is null or length(trim(p_role)) = 0 then
    raise exception 'Role is required' using errcode = '22023';
  end if;

  insert into public.opportunities (
    user_id, company, role, status, location, remote, job_url, job_description,
    source_label, source_system, source_external_id, match_score, strengths, gaps, notes
  ) values (
    v_user_id, p_company, p_role, 'considering', p_location, p_remote, p_job_url, p_job_description,
    p_source_label, p_source_system, p_source_external_id, p_match_score,
    coalesce(p_strengths, '[]'::jsonb), coalesce(p_gaps, '[]'::jsonb), p_notes
  )
  returning id into v_opportunity_id;

  insert into public.opportunity_events (
    user_id, opportunity_id, event_type, from_status, to_status, occurred_at, note, metadata
  ) values (
    v_user_id, v_opportunity_id, 'created', null, 'considering', now(), null, '{}'::jsonb
  );

  return query select v_opportunity_id;
end;
$$;

revoke all on function public.create_opportunity(text, text, text, boolean, text, text, text, text, text, numeric, jsonb, jsonb, text) from public;
grant execute on function public.create_opportunity(text, text, text, boolean, text, text, text, text, text, numeric, jsonb, jsonb, text) to authenticated;

-- Atomically moves an owned opportunity to `p_to_status` and appends the
-- one `status_changed` event that summarizes the change, so the current
-- status and its history are always updated together.
create or replace function public.transition_opportunity(
  p_opportunity_id uuid,
  p_to_status text,
  p_occurred_at timestamptz,
  p_note text,
  p_metadata jsonb
)
returns table(opportunity_id uuid)
language plpgsql
security invoker
set search_path = public
as $$
declare
  v_user_id uuid := auth.uid();
  v_opportunity public.opportunities%rowtype;
begin
  if v_user_id is null then
    raise exception 'Authentication is required' using errcode = '42501';
  end if;
  if p_to_status is null
    or p_to_status not in ('considering', 'applied', 'interviewing', 'offer', 'rejected', 'withdrawn', 'closed') then
    raise exception 'Invalid opportunity status' using errcode = '22023';
  end if;

  select * into v_opportunity
  from public.opportunities
  where id = p_opportunity_id and user_id = v_user_id
  for update;

  if not found then
    raise exception 'Owned opportunity was not found' using errcode = 'P0002';
  end if;

  update public.opportunities
  set status = p_to_status,
      applied_at = case
        when p_to_status = 'applied' then coalesce(applied_at, p_occurred_at, now())
        else applied_at
      end,
      updated_at = now()
  where id = p_opportunity_id and user_id = v_user_id;

  insert into public.opportunity_events (
    user_id, opportunity_id, event_type, from_status, to_status, occurred_at, note, metadata
  ) values (
    v_user_id, p_opportunity_id, 'status_changed', v_opportunity.status, p_to_status,
    coalesce(p_occurred_at, now()), p_note, coalesce(p_metadata, '{}'::jsonb)
  );

  return query select p_opportunity_id;
end;
$$;

revoke all on function public.transition_opportunity(uuid, text, timestamptz, text, jsonb) from public;
grant execute on function public.transition_opportunity(uuid, text, timestamptz, text, jsonb) to authenticated;

-- Atomically sets `next_interview_at`, moves a pre-interview opportunity
-- (`considering` or `applied`) into `interviewing`, and appends one
-- `interview_scheduled` event. It deliberately never moves `offer`,
-- `rejected`, `withdrawn`, or `closed` back to `interviewing`.
create or replace function public.schedule_opportunity_interview(
  p_opportunity_id uuid,
  p_interview_at timestamptz,
  p_note text,
  p_metadata jsonb
)
returns table(opportunity_id uuid)
language plpgsql
security invoker
set search_path = public
as $$
declare
  v_user_id uuid := auth.uid();
  v_opportunity public.opportunities%rowtype;
  v_new_status text;
begin
  if v_user_id is null then
    raise exception 'Authentication is required' using errcode = '42501';
  end if;
  if p_interview_at is null then
    raise exception 'Interview time is required' using errcode = '22023';
  end if;

  select * into v_opportunity
  from public.opportunities
  where id = p_opportunity_id and user_id = v_user_id
  for update;

  if not found then
    raise exception 'Owned opportunity was not found' using errcode = 'P0002';
  end if;

  v_new_status := case
    when v_opportunity.status in ('considering', 'applied') then 'interviewing'
    else v_opportunity.status
  end;

  update public.opportunities
  set next_interview_at = p_interview_at,
      status = v_new_status,
      updated_at = now()
  where id = p_opportunity_id and user_id = v_user_id;

  insert into public.opportunity_events (
    user_id, opportunity_id, event_type, from_status, to_status, occurred_at, note, metadata
  ) values (
    v_user_id, p_opportunity_id, 'interview_scheduled', v_opportunity.status, v_new_status,
    now(), p_note, coalesce(p_metadata, '{}'::jsonb) || jsonb_build_object('interview_at', p_interview_at)
  );

  return query select p_opportunity_id;
end;
$$;

revoke all on function public.schedule_opportunity_interview(uuid, timestamptz, text, jsonb) from public;
grant execute on function public.schedule_opportunity_interview(uuid, timestamptz, text, jsonb) to authenticated;
