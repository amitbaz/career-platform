-- Share automatic company watches against the company entity (#204, parent #118).
--
-- `job_hunter_company_watch` holds facts about employers -- that a careers
-- page is at a given URL, that it is served by a given ATS, whether that
-- endpoint has been failing -- yet it carries `user_id` and four owner-scoped
-- RLS policies. Those facts are true for everyone and were discovered and
-- re-verified once per user. This is the #174/#203 argument applied to a
-- third table: the postings work moved the *result* of discovery to a
-- shared table; the ATS registry ticket moved the knowledge that *directs*
-- discovery; this moves the knowledge that a company's careers page even
-- exists.
--
-- What moves and what does not -----------------------------------------------
--
-- Shared here, on the new `job_hunter_company_watch_health`: careers_url,
-- ats_provider, ats_identifier, confidence, first_seen_at, last_verified_at,
-- last_successful_check_at, consecutive_failures, active, paused_until.
-- Endpoint health is a property of the endpoint, keyed on `company_id`
-- referencing #198's `job_hunter_companies` -- not on a normalized name of
-- its own, so this does not grow a second notion of "the same company".
--
-- Stays on `job_hunter_company_watch`, per-user: `promotion_source =
-- 'manual'`. A manual watch is a user asking for this company to be
-- watched -- intent, not knowledge -- so it is not merged into the shared
-- row at all. Only automatic promotions move; `sources/company_watch.py`
-- checks watches from both tables every run (`list_due_company_watches`
-- unions them), so a company that is both manually watched by one user and
-- automatically discovered by the engine is checked from both places. That
-- is accepted here as a rare, harmless duplicate rather than solved by
-- merging the two pools: merging them would mean a user's manual intent
-- could be silently upgraded or overridden by another user's automatic
-- discovery, which is a worse property than one redundant crawl.
--
-- The ranking a single upsert previously applied across manual and
-- automatic on one shared row -- ATS target beats generic URL beats
-- company-only, equal strength requires greater confidence to replace --
-- still applies, independently, inside each pool: a second manual upsert
-- for the same company can still upgrade the first, and a second automatic
-- promotion (this user's or another's) can still upgrade the shared row.
-- What no longer happens is a manual watch absorbing an automatic upgrade,
-- because there is no longer one row for the two to share.
--
-- Provenance stays advisory only -----------------------------------------
--
-- `discovered_from_job_id` carried `foreign key (discovered_from_job_id,
-- user_id) references job_hunter_jobs (id, user_id)` -- a composite key a
-- shared row cannot hold, because a shared row has no `user_id` to pair it
-- with. On the shared table it is kept as a bare, unreferenced uuid:
-- useful for a human reading which job first suggested this company, never
-- joined against by any query, and never enforced. Dropped outright from
-- `job_hunter_company_watch`, because after this migration that table only
-- ever holds manual rows, and a manual watch is never discovered from a
-- job -- `sync_manual_watch_seeds` has always called `upsert_company_watch`
-- with `discovered_from_job_id=None`.
--
-- Sharing ----------------------------------------------------------------
--
-- Nothing on `job_hunter_company_watch_health` is per-user, so it carries no
-- user_id. Reads are open to every authenticated user, exactly as
-- `job_hunter_companies` and `job_hunter_ats_boards` are. Unlike those two
-- when they first landed, this table adopts the #179 privileged-writer
-- pattern from the start rather than opening writes and narrowing them
-- later: `select` only, and insert/update/delete revoked outright from
-- every role a user can hold. #179 already established the pattern; a
-- table added after it should not repeat the transitional window.

create table public.job_hunter_company_watch_health (
  id uuid primary key default gen_random_uuid(),

  -- #198's company entity. One health row per company: two users (or one
  -- user, twice) promoting the same employer converge on this row rather
  -- than duplicating it.
  company_id uuid not null unique references public.job_hunter_companies(id),

  careers_url text not null default '',
  ats_provider text,
  ats_identifier text,
  -- The strength/confidence comparison `upsert_company_watch` already used
  -- to decide whether a new candidate endpoint replaces the stored one,
  -- applied here across every automatic promotion for this company.
  confidence double precision not null default 0,

  -- Advisory only -- see the note above. Never joined against.
  discovered_from_job_id uuid,

  first_seen_at timestamptz not null,
  last_verified_at timestamptz,
  last_successful_check_at timestamptz,
  consecutive_failures integer not null default 0,
  active boolean not null default true,
  paused_until timestamptz,

  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.job_hunter_company_watch_health enable row level security;

create policy select_authenticated on public.job_hunter_company_watch_health
  for select to authenticated using (true);

revoke insert, update, delete on table public.job_hunter_company_watch_health
  from anon, authenticated, service_role;

-- "Which active watches are due for a check" -- list_due_company_watches.
create index job_hunter_company_watch_health_due_idx
  on public.job_hunter_company_watch_health (active, paused_until);

comment on table public.job_hunter_company_watch_health is
  'Shared knowledge about a company''s careers-page endpoint and its health '
  '-- discovered once by any user''s automatic promotion and reused by '
  'every later run of every user. Readable by any authenticated user; '
  'writable only by the privileged ingestion role (#179 pattern, adopted '
  'from the start). Manual watches stay per-user on '
  'job_hunter_company_watch and are never merged in here (#204).';

comment on column public.job_hunter_company_watch_health.company_id is
  'References job_hunter_companies(id) -- #198''s company entity, keyed on '
  'job_hunter_normalize_company. Not a normalized name of its own: there is '
  'one notion of "the same company" in this schema.';

comment on column public.job_hunter_company_watch_health.discovered_from_job_id is
  'Advisory provenance only -- which job first suggested this company. '
  'Never enforced or joined against: a shared row has no user_id to pair '
  'it with in a composite foreign key into the per-user job_hunter_jobs.';


-- Backfill: promote existing automatic watches -----------------------------
--
-- Today's corpus belongs to one user (this deployment is single-user,
-- pre-launch), but this is written to be correct for more than one:
-- distinct users' automatic watches for the same employer collapse onto one
-- shared row, the strongest/most-confident endpoint across all of them
-- wins by the same ranking `upsert_company_watch` applied one write at a
-- time, and health is the most informative/most recent evidence across
-- every contributing row.

-- 1. Ensure the company entity exists for every automatically-watched
--    employer. A watch can predate any posting from that employer, so this
--    cannot assume #198's extraction has already created the row.
insert into public.job_hunter_companies (identity, display_name)
select ranked.identity, ranked.display_name
from (
  select
    public.job_hunter_normalize_company(w.company_name) as identity,
    w.company_name as display_name,
    row_number() over (
      partition by public.job_hunter_normalize_company(w.company_name)
      order by w.company_name
    ) as rn
  from public.job_hunter_company_watch w
  where w.promotion_source = 'automatic'
) ranked
where ranked.rn = 1
  and ranked.identity <> ''
on conflict (identity) do nothing;

-- 2. Promote the endpoint and health, ranked the same way a live upsert
--    would have ranked repeated writes to one row.
with classified as (
  select
    public.job_hunter_normalize_company(w.company_name) as identity,
    w.careers_url, w.ats_provider, w.ats_identifier, w.confidence,
    w.discovered_from_job_id, w.first_seen_at, w.last_verified_at,
    w.last_successful_check_at, w.consecutive_failures, w.active,
    w.paused_until
  from public.job_hunter_company_watch w
  where w.promotion_source = 'automatic'
    and public.job_hunter_normalize_company(w.company_name) <> ''
),
best_endpoint as (
  select distinct on (identity)
    identity, careers_url, ats_provider, ats_identifier, confidence
  from classified
  order by identity,
    case when ats_provider is not null and ats_identifier is not null then 3
         when careers_url <> '' then 2
         else 1 end desc,
    confidence desc
),
health as (
  select
    identity,
    min(first_seen_at) as first_seen_at,
    max(last_verified_at) as last_verified_at,
    max(last_successful_check_at) as last_successful_check_at,
    max(consecutive_failures) as consecutive_failures,
    bool_and(active) as active,
    max(paused_until) as paused_until,
    (array_agg(discovered_from_job_id)
       filter (where discovered_from_job_id is not null))[1]
      as discovered_from_job_id
  from classified
  group by identity
)
insert into public.job_hunter_company_watch_health (
  company_id, careers_url, ats_provider, ats_identifier, confidence,
  discovered_from_job_id, first_seen_at, last_verified_at,
  last_successful_check_at, consecutive_failures, active, paused_until
)
select
  c.id, be.careers_url, be.ats_provider, be.ats_identifier, be.confidence,
  h.discovered_from_job_id, h.first_seen_at, h.last_verified_at,
  h.last_successful_check_at, h.consecutive_failures, h.active,
  h.paused_until
from health h
join best_endpoint be on be.identity = h.identity
join public.job_hunter_companies c on c.identity = h.identity
on conflict (company_id) do nothing;

-- 3. The migrated rows now live on the shared table; remove them from the
--    per-user one so the two pools do not double-report the same watch.
delete from public.job_hunter_company_watch where promotion_source = 'automatic';


-- job_hunter_collapse_job_rows loses its company_watch re-pointing ----------
--
-- 20260909210000 gave this function a step that re-pointed a merged-away
-- job's `job_hunter_company_watch.discovered_from_job_id` at the surviving
-- job, so an automatic watch's provenance kept naming a job that still
-- exists. That column is dropped from this table below -- automatic watches
-- do not live here any more, and a manual watch never carried one -- so the
-- step has nothing left to do and is removed rather than left to reference a
-- column that no longer exists. The rest of the function is copied
-- unchanged; recreating it here (rather than leaving 20260909210000 to
-- reference a column this migration is about to drop) is what keeps `alter
-- table ... drop column` below from failing on a dependent function body.
create or replace function public.job_hunter_collapse_job_rows(
  p_user_id uuid, p_left uuid, p_right uuid
) returns uuid
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_survivor_id uuid;
  v_duplicate_id uuid;
  v_survivor public.job_hunter_jobs%rowtype;
  v_duplicate public.job_hunter_jobs%rowtype;
begin
  if p_left = p_right then
    return p_left;
  end if;

  select j.id
    into v_survivor_id
    from public.job_hunter_jobs j
   where j.user_id = p_user_id
     and j.id in (p_left, p_right)
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

  v_duplicate_id := case when v_survivor_id = p_left then p_right else p_left end;

  select * into v_survivor from public.job_hunter_jobs j
   where j.id = v_survivor_id and j.user_id = p_user_id;
  select * into v_duplicate from public.job_hunter_jobs j
   where j.id = v_duplicate_id and j.user_id = p_user_id;

  if v_survivor.id is null or v_duplicate.id is null then
    raise exception 'survivor and duplicate jobs must both exist';
  end if;

  update public.job_hunter_jobs j set
    first_seen_at = least(v_survivor.first_seen_at, v_duplicate.first_seen_at),
    last_seen_at  = greatest(v_survivor.last_seen_at, v_duplicate.last_seen_at)
  where j.id = v_survivor_id and j.user_id = p_user_id;

  -- Union the provenance rows, then reassign every attached record, exactly
  -- as the job merge always did. The delete-before-update on the three tables
  -- with natural keys (#87) is still needed: a duplicate row that would
  -- collide with an identical survivor row is dropped rather than raising,
  -- and both rows describe the same event at the same instant.
  insert into public.job_hunter_job_sources as tgt
    (user_id, job_id, source, source_job_id, source_url, identity_key,
     first_seen_at, last_seen_at)
  select p_user_id, v_survivor_id, src.source, src.source_job_id, src.source_url,
         src.identity_key, src.first_seen_at, src.last_seen_at
    from public.job_hunter_job_sources src
   where src.job_id = v_duplicate_id and src.user_id = p_user_id
   order by src.created_at, src.id
  on conflict (job_id, identity_key) do update set
    first_seen_at = least(tgt.first_seen_at, excluded.first_seen_at),
    last_seen_at  = greatest(tgt.last_seen_at, excluded.last_seen_at);

  delete from public.job_hunter_job_sources s
   where s.job_id = v_duplicate_id and s.user_id = p_user_id;

  delete from public.job_hunter_evaluations d
   where d.job_id = v_duplicate_id and d.user_id = p_user_id
     and exists (select 1 from public.job_hunter_evaluations s
                  where s.job_id = v_survivor_id and s.user_id = p_user_id
                    and s.evaluated_at = d.evaluated_at);
  update public.job_hunter_evaluations e set job_id = v_survivor_id
   where e.job_id = v_duplicate_id and e.user_id = p_user_id;

  delete from public.job_hunter_materials d
   where d.job_id = v_duplicate_id and d.user_id = p_user_id
     and exists (select 1 from public.job_hunter_materials s
                  where s.job_id = v_survivor_id and s.user_id = p_user_id
                    and s.generated_at = d.generated_at);
  update public.job_hunter_materials m set job_id = v_survivor_id
   where m.job_id = v_duplicate_id and m.user_id = p_user_id;

  delete from public.job_hunter_deliveries d
   where d.job_id = v_duplicate_id and d.user_id = p_user_id
     and exists (select 1 from public.job_hunter_deliveries s
                  where s.job_id = v_survivor_id and s.user_id = p_user_id
                    and s.delivery_type = d.delivery_type
                    and s.delivered_at = d.delivered_at);
  update public.job_hunter_deliveries dl set job_id = v_survivor_id
   where dl.job_id = v_duplicate_id and dl.user_id = p_user_id;

  update public.job_hunter_application_events a set job_id = v_survivor_id
   where a.job_id = v_duplicate_id and a.user_id = p_user_id;

  -- Rows that already pointed at the duplicate are re-pointed first, so every
  -- redirect names a job that still exists and a reader never walks a chain.
  update public.job_hunter_job_merges m
     set survivor_id = v_survivor_id,
         merged_at = now()
   where m.survivor_id = v_duplicate_id and m.user_id = p_user_id;

  insert into public.job_hunter_job_merges (user_id, duplicate_id, survivor_id)
  values (p_user_id, v_duplicate_id, v_survivor_id)
  on conflict (user_id, duplicate_id) do update
    set survivor_id = excluded.survivor_id,
        merged_at = now();

  delete from public.job_hunter_jobs j
   where j.id = v_duplicate_id and j.user_id = p_user_id;

  return v_survivor_id;
end $$;

comment on function public.job_hunter_collapse_job_rows(uuid, uuid, uuid) is
  'Fold two of one user''s membership rows into one and reassign every record '
  'attached to the loser. Internal to this schema: the two callers are '
  'job_hunter_merge_postings, which collapses the rows a posting merge would '
  'otherwise duplicate, and job_hunter_merge_jobs. Since #204 this no longer '
  're-points job_hunter_company_watch.discovered_from_job_id -- that column '
  'is dropped from the table below, because automatic watches carrying '
  'provenance moved off it entirely.';

revoke all on function public.job_hunter_collapse_job_rows(uuid, uuid, uuid)
  from public, anon, authenticated, service_role;


-- Contract the per-user table to what stays per-user -----------------------
--
-- `promotion_source` is dropped rather than kept and constrained to
-- 'manual': every remaining row already is one, going forward every write
-- to this table is one (`sync_manual_watch_seeds` is the only writer left),
-- and a column that can only ever hold one value documents nothing a
-- comment on the table cannot say better.
alter table public.job_hunter_company_watch
  drop column promotion_source,
  drop column discovered_from_job_id;

comment on table public.job_hunter_company_watch is
  'A user''s own manual company watch -- intent, not knowledge. Endpoint '
  'identity and health for a company discovered automatically live on the '
  'shared job_hunter_company_watch_health instead (#204); this table is '
  'manual watches only, one per (user, normalized_company_name), unaffected '
  'by anything another user or the engine''s own discovery does.';
