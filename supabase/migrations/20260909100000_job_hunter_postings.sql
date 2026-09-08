-- One row per posting, shared by everyone who discovers it (issue #174,
-- epic #118).
--
-- A posting is one job advertisement in the world. Until now it had no row
-- of its own: every user who discovered it got a private copy in
-- job_hunter_jobs carrying the description, the identity columns and the
-- fetch metadata that are identical for all of them. That duplication is
-- why objective extraction (job_hunter_job_facets) is paid once per user
-- rather than once per posting, which AGENTS.md rule 2 -- "cost scales with
-- jobs, not with users" -- says it must not be.
--
-- This migration adds the destination and starts filling it. It moves no
-- reader onto it: every column job_hunter_jobs has today stays exactly
-- where it is, the job upsert keeps writing all of them, and nothing reads
-- a posting yet. The delivered outcome of a run is therefore unchanged.
--
-- Identity is the fingerprint job_hunter_upsert_job already receives
-- (normalize.py:job_fingerprint -- source + source job id, else canonical
-- URL, else company/title/location). That key names the advertisement and
-- never the user who found it, so two users who discover the same
-- advertisement the same way resolve to the same posting row -- which is
-- what pays for extraction once instead of once per user.
--
-- It is not the whole of "one row per advertisement", and #174 does not
-- claim it is. The key is source-scoped: the same job seen on RemoteOK and
-- on the employer's Greenhouse board hashes differently, so it is two
-- postings until something resolves them together. Per-user job rows
-- already collapse that case (_dedupe, then job_hunter_merge_jobs), and
-- teaching postings the same trick is a separate piece of work -- the
-- candidate is a cross-source resolution key, tracked on epic #118, and it
-- needs a way to retire the surplus rows, which no policy here grants.
--
-- posting_id is nullable on purpose. Every row written through
-- job_hunter_upsert_job carries one, and the backfill at the bottom fills
-- in every row that predates this migration, so no real job row lacks one;
-- but direct inserts (pgTAP fixtures, scripts/migrate_sqlite_to_postgres.py)
-- still exist, and the last ticket in this sequence is where the column is
-- tightened, once every writer is known to be on the RPC.

-- The postings table ---------------------------------------------------------
--
-- No user_id, and no market_id or status: those are the two things a job
-- row says that the advertisement itself does not -- which markets a user
-- matched it to, and where it sits in that user's funnel. They stay on
-- job_hunter_jobs.

create table public.job_hunter_postings (
  id uuid primary key default gen_random_uuid(),
  fingerprint text not null unique,
  source text not null default '',
  source_job_id text,
  url text not null default '',
  canonical_url text not null default '',
  company text not null default '',
  title text not null default '',
  location text not null default '',
  remote boolean,
  description text not null default '',
  description_hash text not null default '',
  content_confidence text not null default '',
  ats_provider text,
  ats_board text,
  ats_job_id text,
  first_seen_at timestamptz not null,
  last_seen_at timestamptz not null,
  created_at timestamptz not null default now()
);

comment on table public.job_hunter_postings is
  'One row per job advertisement, shared by every user who discovers it. '
  'Keyed by the fingerprint job_hunter_upsert_job computes, which is '
  'user-independent, so two users resolve to the same row.';

-- Reads are open to every authenticated user: a posting is not private to
-- whoever discovered it, and the whole point of the table is that the work
-- done on one posting is done once for everyone. There is deliberately no
-- delete policy: a posting is shared, so no single user may remove one out
-- from under the others.
--
-- Writes stay as open as they are on the per-user tables for now, and this
-- is the first table in the schema where that means one user writing a row
-- another user reads. The hazard is concrete, not theoretical: identity
-- columns are only backfilled when empty, so a row inserted first with a
-- made-up company or title keeps them against every later real discovery,
-- and nobody can delete it. Narrowing this is the last ticket in the
-- sequence and is a blocking item on it, not a nicety -- one writer (the
-- runner claim, as job_hunter_platform_ai_usage already does) or a check
-- that a writer has a job row for the fingerprint.
alter table public.job_hunter_postings enable row level security;
create policy select_authenticated on public.job_hunter_postings
  for select to authenticated using (true);
create policy insert_authenticated on public.job_hunter_postings
  for insert to authenticated with check (true);
create policy update_authenticated on public.job_hunter_postings
  for update to authenticated using (true) with check (true);

-- The pointer ----------------------------------------------------------------

alter table public.job_hunter_jobs
  add column posting_id uuid references public.job_hunter_postings(id);

comment on column public.job_hunter_jobs.posting_id is
  'The shared advertisement this private copy is of. Written by '
  'job_hunter_upsert_job; nothing reads it yet (#174).';

-- Postings are the parent here, so the index that matters is the one that
-- answers "which job rows are copies of this posting" -- the direction the
-- rest of this sequence reads in, and the direction a delete of a posting
-- would have to scan.
create index job_hunter_jobs_posting_idx
  on public.job_hunter_jobs (posting_id);

-- Upsert ---------------------------------------------------------------------
--
-- Takes the same payload as job_hunter_upsert_job and reads only the keys
-- that describe the advertisement, so the caller passes p_job straight
-- through and the two rows cannot disagree about what was fetched.

create or replace function public.job_hunter_upsert_posting(p_posting jsonb)
returns uuid
language plpgsql
security invoker
set search_path = ''
as $$
declare
  v_fingerprint text := coalesce(p_posting->>'fingerprint', '');
  v_now timestamptz := clock_timestamp();
  v_id uuid;
  v_row public.job_hunter_postings%rowtype;
  v_incoming_description text := coalesce(p_posting->>'description', '');
  v_incoming_confidence text := coalesce(p_posting->>'content_confidence', '');
  v_raw_canonical text := coalesce(p_posting->>'canonical_url', '');
  v_canonical text;
  v_description text;
  v_confidence text;
begin
  if v_fingerprint = '' then
    raise exception 'p_posting must carry a non-empty fingerprint';
  end if;

  v_canonical := coalesce(nullif(v_raw_canonical, ''),
                          public.job_hunter_canonicalize_url(coalesce(p_posting->>'url', '')));

  -- INSERT OR IGNORE + read-back, the same race-free shape
  -- job_hunter_upsert_job uses on (user_id, fingerprint).
  insert into public.job_hunter_postings as ins
    (fingerprint, source, source_job_id, url, canonical_url, company, title,
     location, remote, description, description_hash, content_confidence,
     ats_provider, ats_board, ats_job_id, first_seen_at, last_seen_at, created_at)
  values (
    v_fingerprint,
    coalesce(p_posting->>'source', ''),
    p_posting->>'source_job_id',
    coalesce(nullif(v_raw_canonical, ''), coalesce(p_posting->>'url', ''), ''),
    v_canonical,
    coalesce(p_posting->>'company', ''),
    coalesce(p_posting->>'title', ''),
    coalesce(p_posting->>'location', ''),
    (p_posting->>'remote')::boolean,
    v_incoming_description,
    encode(sha256(convert_to(v_incoming_description, 'UTF8')), 'hex'),
    v_incoming_confidence,
    p_posting->>'ats_provider',
    p_posting->>'ats_board',
    p_posting->>'ats_job_id',
    v_now, v_now, v_now)
  on conflict (fingerprint) do nothing
  returning ins.id into v_id;

  if v_id is not null then
    return v_id;
  end if;

  select * into v_row from public.job_hunter_postings p
   where p.fingerprint = v_fingerprint;
  if not found then
    -- The insert conflicted, so a row with this fingerprint exists, and
    -- reads here are unfiltered. Missing it means something took it away
    -- between the two statements. Raising keeps the caller from writing a
    -- null posting_id and calling it a job with a posting.
    raise exception 'posting vanished between insert and read-back: %', v_fingerprint;
  end if;

  -- The posting keeps the better text. This is _better_description from
  -- job_hunter_merge_jobs, applied across users rather than across two of
  -- one user's rows: an empty side never wins, then the more trustworthy
  -- tier wins, and only on a tie does the longer text win. A
  -- lower-confidence fetch therefore cannot overwrite a better one however
  -- much longer it is, and the hash and the tier follow the text they
  -- belong to.
  if v_incoming_description = '' then
    v_description := v_row.description;
    v_confidence := v_row.content_confidence;
  elsif coalesce(v_row.description, '') = '' then
    v_description := v_incoming_description;
    v_confidence := v_incoming_confidence;
  elsif public.job_hunter_confidence_rank(v_incoming_confidence)
      < public.job_hunter_confidence_rank(v_row.content_confidence) then
    v_description := v_incoming_description;
    v_confidence := v_incoming_confidence;
  elsif public.job_hunter_confidence_rank(v_incoming_confidence)
      > public.job_hunter_confidence_rank(v_row.content_confidence) then
    v_description := v_row.description;
    v_confidence := v_row.content_confidence;
  elsif length(regexp_replace(v_incoming_description, '^\s+|\s+$', '', 'g'))
      > length(regexp_replace(v_row.description, '^\s+|\s+$', '', 'g')) then
    v_description := v_incoming_description;
    v_confidence := v_incoming_confidence;
  else
    v_description := v_row.description;
    v_confidence := v_row.content_confidence;
  end if;

  -- Identity columns keep what is already there and only backfill what is
  -- empty, as the logical job update does; a resolved canonical URL is the
  -- exception, because resolving one is an improvement on whatever URL the
  -- posting was first seen under. first_seen_at is when anyone first saw
  -- the advertisement and never moves.
  update public.job_hunter_postings p set
    source             = coalesce(nullif(p.source, ''), coalesce(p_posting->>'source', '')),
    source_job_id      = coalesce(nullif(p.source_job_id, ''), p_posting->>'source_job_id'),
    url                = coalesce(nullif(v_raw_canonical, ''), nullif(p.url, ''),
                                  nullif(coalesce(p_posting->>'url', ''), ''), ''),
    canonical_url      = coalesce(nullif(v_canonical, ''), p.canonical_url),
    company            = coalesce(nullif(p.company, ''), nullif(coalesce(p_posting->>'company', ''), ''), ''),
    title              = coalesce(nullif(p.title, ''), nullif(coalesce(p_posting->>'title', ''), ''), ''),
    location           = coalesce(nullif(p.location, ''), nullif(coalesce(p_posting->>'location', ''), ''), ''),
    remote             = coalesce(p.remote, (p_posting->>'remote')::boolean),
    description        = v_description,
    description_hash   = encode(sha256(convert_to(v_description, 'UTF8')), 'hex'),
    content_confidence = v_confidence,
    ats_provider       = coalesce(nullif(p.ats_provider, ''), nullif(coalesce(p_posting->>'ats_provider', ''), '')),
    ats_board          = coalesce(nullif(p.ats_board, ''), nullif(coalesce(p_posting->>'ats_board', ''), '')),
    ats_job_id         = coalesce(nullif(p.ats_job_id, ''), nullif(coalesce(p_posting->>'ats_job_id', ''), '')),
    last_seen_at       = greatest(p.last_seen_at, v_now)
  where p.id = v_row.id;

  return v_row.id;
end
$$;

comment on function public.job_hunter_upsert_posting(jsonb) is
  'Create or update the shared posting a job payload describes, keyed by '
  'its fingerprint, and return its id. The better description wins by '
  'content confidence, so a second user fetching the same advertisement '
  'from a weaker source cannot degrade it.';

-- job_hunter_upsert_job ------------------------------------------------------
--
-- Re-created from 202609070004 with one addition, in both match modes: the
-- posting is written and the job row points at it, in the same call. A job
-- row that already carries a posting_id keeps it -- the logical mode
-- resolves several fingerprints onto one job row, so re-pointing it at
-- whichever fingerprint arrived last would make the pointer flap between
-- runs. Everything else below is unchanged.

create or replace function public.job_hunter_upsert_job(p_job jsonb)
returns table (id uuid, is_new boolean, description_changed boolean)
language plpgsql
security invoker
set search_path = ''
as $$
declare
  v_uid uuid := (select auth.uid());
  v_fingerprint text := coalesce(p_job->>'fingerprint', '');
  v_raw_canonical text := coalesce(p_job->>'canonical_url', '');
  v_lookup_canonical text;
  v_ats_provider text := lower(coalesce(p_job->>'ats_provider', ''));
  v_candidates uuid[] := '{}'::uuid[];
  v_candidate uuid;
  v_job_id uuid;
  v_posting_id uuid;
  v_previous_hash text;
  v_current_hash text;
  v_is_new boolean;
  v_changed boolean;
  v_now timestamptz := clock_timestamp();
  v_row public.job_hunter_jobs%rowtype;
  v_description text;
  v_confidence text;
  v_persisted_url text;
  v_source text := coalesce(p_job->>'source', '');
  v_source_job_id text := p_job->>'source_job_id';
  v_source_url text;
  v_identity_key text;
  v_match_mode text := coalesce(p_job->>'match_mode', 'logical');
  v_desc_hash text := encode(sha256(convert_to(coalesce(p_job->>'description', ''), 'UTF8')), 'hex');
begin
  if v_uid is null then
    raise exception 'job_hunter_upsert_job requires an authenticated user';
  end if;
  if v_fingerprint = '' then
    raise exception 'p_job must carry a non-empty fingerprint';
  end if;
  if v_match_mode not in ('logical', 'fingerprint') then
    raise exception 'p_job.match_mode must be ''logical'' or ''fingerprint'', got %', v_match_mode;
  end if;

  v_lookup_canonical := case when v_raw_canonical <> ''
                             then public.job_hunter_canonicalize_url(v_raw_canonical)
                             else '' end;

  -- The posting this payload describes, created or updated. Same call, so a
  -- job row can never exist without the advertisement it is a copy of.
  v_posting_id := public.job_hunter_upsert_posting(p_job);

  -- ------------------------------------------------------------------
  -- match_mode = 'fingerprint' -- store.py:649-743 (upsert_job)
  --
  -- The narrow upsert: identity is the fingerprint and nothing else, so
  -- there are no candidates to gather and nothing is ever merged. It also
  -- overwrites the stored fields unconditionally rather than keeping the
  -- richer one, and it does NOT record a discovery source -- upsert_job
  -- never called _record_job_source. Collapsing this into the logical mode
  -- would hand a caller duplicate-merging it did not ask for.
  -- ------------------------------------------------------------------
  if v_match_mode = 'fingerprint' then
    -- INSERT OR IGNORE + read-back, as one race-free statement.
    insert into public.job_hunter_jobs as ins
      (user_id, fingerprint, source, source_job_id, url, company, title,
       location, remote, description, description_hash, canonical_url,
       ats_provider, ats_board, ats_job_id, content_confidence,
       first_seen_at, last_seen_at, created_at, status, posting_id)
    values (
      v_uid, v_fingerprint, v_source, v_source_job_id,
      coalesce(p_job->>'url', ''),
      coalesce(p_job->>'company', ''),
      coalesce(p_job->>'title', ''),
      coalesce(p_job->>'location', ''),
      (p_job->>'remote')::boolean,
      coalesce(p_job->>'description', ''),
      v_desc_hash,
      coalesce(nullif(v_raw_canonical, ''),
               public.job_hunter_canonicalize_url(coalesce(p_job->>'url', ''))),
      p_job->>'ats_provider', p_job->>'ats_board', p_job->>'ats_job_id',
      coalesce(p_job->>'content_confidence', ''),
      v_now, v_now, v_now, 'new', v_posting_id)
    on conflict (user_id, fingerprint) do nothing
    returning ins.id into v_job_id;

    if v_job_id is not null then
      return query select v_job_id, true, false;
      return;
    end if;

    select * into v_row from public.job_hunter_jobs j
     where j.user_id = v_uid and j.fingerprint = v_fingerprint;

    update public.job_hunter_jobs j set
      url                = coalesce(p_job->>'url', ''),
      company            = coalesce(p_job->>'company', ''),
      title              = coalesce(p_job->>'title', ''),
      location           = coalesce(p_job->>'location', ''),
      remote             = (p_job->>'remote')::boolean,
      description        = coalesce(p_job->>'description', ''),
      description_hash   = v_desc_hash,
      -- COALESCE(NULLIF(?, ''), canonical_url) in the original
      canonical_url      = coalesce(nullif(coalesce(nullif(v_raw_canonical, ''),
                                     public.job_hunter_canonicalize_url(coalesce(p_job->>'url', ''))), ''),
                                    v_row.canonical_url),
      -- COALESCE(?, ats_*): an absent key keeps the stored value, an
      -- explicit empty string overwrites it. Not NULLIF.
      ats_provider       = coalesce(p_job->>'ats_provider', v_row.ats_provider),
      ats_board          = coalesce(p_job->>'ats_board', v_row.ats_board),
      ats_job_id         = coalesce(p_job->>'ats_job_id', v_row.ats_job_id),
      content_confidence = coalesce(p_job->>'content_confidence', ''),
      last_seen_at       = v_now,
      posting_id         = coalesce(v_row.posting_id, v_posting_id)
    where j.id = v_row.id and j.user_id = v_uid;

    return query select v_row.id, false, (v_row.description_hash is distinct from v_desc_hash);
    return;
  end if;

  -- ------------------------------------------------------------------
  -- match_mode = 'logical' (default) -- store.py:744-905
  -- ------------------------------------------------------------------

  -- Identity resolution, strongest evidence first, preserving order and
  -- dropping repeats exactly as _append_unique_id does.
  if v_lookup_canonical <> '' then
    for v_candidate in
      select j.id from public.job_hunter_jobs j
       where j.user_id = v_uid and j.canonical_url = v_lookup_canonical
       order by j.created_at, j.id
    loop
      if not (v_candidate = any(v_candidates)) then
        v_candidates := v_candidates || v_candidate;
      end if;
    end loop;
  end if;

  if v_ats_provider in ('ashby', 'greenhouse', 'lever')
     and coalesce(p_job->>'ats_board', '') <> ''
     and coalesce(p_job->>'ats_job_id', '') <> '' then
    for v_candidate in
      select j.id from public.job_hunter_jobs j
       where j.user_id = v_uid
         and j.ats_provider = v_ats_provider
         and j.ats_board = p_job->>'ats_board'
         and j.ats_job_id = p_job->>'ats_job_id'
       order by j.created_at, j.id
    loop
      if not (v_candidate = any(v_candidates)) then
        v_candidates := v_candidates || v_candidate;
      end if;
    end loop;
  end if;

  for v_candidate in
    select f from public.job_hunter_find_job_by_identity(
      coalesce(p_job->>'company', ''),
      coalesce(p_job->>'title', ''),
      coalesce(p_job->>'location', '')) f
  loop
    if not (v_candidate = any(v_candidates)) then
      v_candidates := v_candidates || v_candidate;
    end if;
  end loop;

  -- _find_single_job_id on fingerprint. unique (user_id, fingerprint) makes
  -- "exactly one match" automatic.
  select j.id into v_candidate
    from public.job_hunter_jobs j
   where j.user_id = v_uid and j.fingerprint = v_fingerprint;
  if v_candidate is not null and not (v_candidate = any(v_candidates)) then
    v_candidates := v_candidates || v_candidate;
  end if;

  if array_length(v_candidates, 1) is not null then
    -- min(candidate_ids, key=_job_survivor_sort_key)
    select j.id into v_job_id
      from unnest(v_candidates) as c(candidate_id)
      join public.job_hunter_jobs j on j.id = c.candidate_id and j.user_id = v_uid
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

    select j.description_hash into v_previous_hash
      from public.job_hunter_jobs j where j.id = v_job_id and j.user_id = v_uid;

    foreach v_candidate in array v_candidates loop
      if v_candidate <> v_job_id then
        v_job_id := public.job_hunter_merge_jobs(v_job_id, v_candidate);
      end if;
    end loop;

    -- _update_logical_job: the stored row keeps its richer fields; only an
    -- empty one is backfilled. A resolved canonical URL becomes the usable
    -- job URL. first_seen_at, source, source_job_id and status are not
    -- touched, matching the Python UPDATE's column list.
    select * into v_row from public.job_hunter_jobs j
     where j.id = v_job_id and j.user_id = v_uid;
    if v_row.id is null then
      raise exception 'job does not exist: %', v_job_id;
    end if;

    if coalesce(p_job->>'description', '') = '' then
      v_description := v_row.description;
      v_confidence := v_row.content_confidence;
    elsif coalesce(v_row.description, '') = '' then
      v_description := p_job->>'description';
      v_confidence := coalesce(p_job->>'content_confidence', '');
    elsif public.job_hunter_confidence_rank(coalesce(p_job->>'content_confidence', ''))
        < public.job_hunter_confidence_rank(v_row.content_confidence) then
      v_description := p_job->>'description';
      v_confidence := coalesce(p_job->>'content_confidence', '');
    elsif public.job_hunter_confidence_rank(coalesce(p_job->>'content_confidence', ''))
        > public.job_hunter_confidence_rank(v_row.content_confidence) then
      v_description := v_row.description;
      v_confidence := v_row.content_confidence;
    elsif length(regexp_replace(p_job->>'description', '^\s+|\s+$', '', 'g'))
        > length(regexp_replace(v_row.description, '^\s+|\s+$', '', 'g')) then
      v_description := p_job->>'description';
      v_confidence := coalesce(p_job->>'content_confidence', '');
    else
      v_description := v_row.description;
      v_confidence := v_row.content_confidence;
    end if;

    v_persisted_url := coalesce(nullif(v_raw_canonical, ''),
                                nullif(v_row.url, ''),
                                nullif(coalesce(p_job->>'url', ''), ''),
                                '');

    update public.job_hunter_jobs j set
      url                = v_persisted_url,
      company            = coalesce(nullif(v_row.company, ''), nullif(coalesce(p_job->>'company', ''), ''), ''),
      title              = coalesce(nullif(v_row.title, ''), nullif(coalesce(p_job->>'title', ''), ''), ''),
      location           = coalesce(nullif(v_row.location, ''), nullif(coalesce(p_job->>'location', ''), ''), ''),
      remote             = coalesce(v_row.remote, (p_job->>'remote')::boolean),
      description        = v_description,
      description_hash   = encode(sha256(convert_to(v_description, 'UTF8')), 'hex'),
      canonical_url      = coalesce(nullif(v_lookup_canonical, ''), v_row.canonical_url),
      ats_provider       = coalesce(nullif(coalesce(p_job->>'ats_provider', ''), ''), v_row.ats_provider),
      ats_board          = coalesce(nullif(coalesce(p_job->>'ats_board', ''), ''), v_row.ats_board),
      ats_job_id         = coalesce(nullif(coalesce(p_job->>'ats_job_id', ''), ''), v_row.ats_job_id),
      content_confidence = v_confidence,
      last_seen_at       = v_now,
      posting_id         = coalesce(v_row.posting_id, v_posting_id)
    where j.id = v_job_id and j.user_id = v_uid;

    select j.description_hash into v_current_hash
      from public.job_hunter_jobs j where j.id = v_job_id and j.user_id = v_uid;
    v_changed := v_previous_hash is distinct from v_current_hash;
    v_is_new := false;
  else
    -- _insert_logical_job
    insert into public.job_hunter_jobs as ins
      (user_id, fingerprint, source, source_job_id, url, company, title,
       location, remote, description, description_hash, canonical_url,
       ats_provider, ats_board, ats_job_id, content_confidence,
       first_seen_at, last_seen_at, created_at, status, posting_id)
    values (
      v_uid,
      v_fingerprint,
      v_source,
      v_source_job_id,
      coalesce(nullif(v_raw_canonical, ''), coalesce(p_job->>'url', ''), ''),
      coalesce(p_job->>'company', ''),
      coalesce(p_job->>'title', ''),
      coalesce(p_job->>'location', ''),
      (p_job->>'remote')::boolean,
      coalesce(p_job->>'description', ''),
      v_desc_hash,
      public.job_hunter_canonicalize_url(
        coalesce(nullif(v_raw_canonical, ''), coalesce(p_job->>'url', ''), '')),
      p_job->>'ats_provider',
      p_job->>'ats_board',
      p_job->>'ats_job_id',
      coalesce(p_job->>'content_confidence', ''),
      v_now,
      v_now,
      v_now,
      'new',
      v_posting_id)
    returning ins.id into v_job_id;

    v_changed := false;
    v_is_new := true;
  end if;

  -- _record_job_source
  v_source_url := coalesce(nullif(coalesce(p_job->>'original_url', ''), ''),
                           coalesce(p_job->>'url', ''), '');
  v_identity_key := case
    when coalesce(v_source_job_id, '') <> ''
      then 'id:' || v_source || ':' || v_source_job_id
    else 'url:' || public.job_hunter_canonicalize_url(v_source_url)
  end;

  insert into public.job_hunter_job_sources
    (user_id, job_id, source, source_job_id, source_url, identity_key,
     first_seen_at, last_seen_at)
  values (v_uid, v_job_id, v_source, v_source_job_id, v_source_url,
          v_identity_key, v_now, v_now)
  on conflict (job_id, identity_key) do update set last_seen_at = excluded.last_seen_at;

  return query select v_job_id, v_is_new, v_changed;
end
$$;

comment on function public.job_hunter_upsert_job(jsonb) is
  'Upsert one logical job and the shared posting it is a copy of (#174), in '
  'one call. Timestamps come from clock_timestamp(), not now(), so multiple '
  'calls inside one transaction (job_hunter_upsert_jobs, 202609070003) still '
  'get strictly-ordered first_seen_at/created_at values matching input order '
  '-- now() is constant for the whole transaction and would tie every row in '
  'a batch, letting the merge-survivor tiebreak fall through to id order '
  'instead of preserving discovery order.';

-- job_hunter_merge_jobs ------------------------------------------------------
--
-- Re-created from 20260908120000 with one addition: the survivor carries a
-- posting_id, chosen by the same comparison that chooses its description.
-- Two of one user's job rows can be copies of two different postings (the
-- fingerprint is source-scoped, so the same advertisement seen on an
-- aggregator and on the employer's ATS hashes twice), and merging them
-- deletes one row and its pointer. Everything else below is unchanged.

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
  v_posting_id uuid;
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
    v_posting_id := v_survivor.posting_id;
  elsif coalesce(v_survivor.description, '') = '' then
    v_description := v_duplicate.description;
    v_confidence := v_duplicate.content_confidence;
    v_posting_id := v_duplicate.posting_id;
  elsif public.job_hunter_confidence_rank(v_duplicate.content_confidence)
      < public.job_hunter_confidence_rank(v_survivor.content_confidence) then
    v_description := v_duplicate.description;
    v_confidence := v_duplicate.content_confidence;
    v_posting_id := v_duplicate.posting_id;
  elsif public.job_hunter_confidence_rank(v_duplicate.content_confidence)
      > public.job_hunter_confidence_rank(v_survivor.content_confidence) then
    v_description := v_survivor.description;
    v_confidence := v_survivor.content_confidence;
    v_posting_id := v_survivor.posting_id;
  elsif length(regexp_replace(v_duplicate.description, '^\s+|\s+$', '', 'g'))
      > length(regexp_replace(v_survivor.description, '^\s+|\s+$', '', 'g')) then
    v_description := v_duplicate.description;
    v_confidence := v_duplicate.content_confidence;
    v_posting_id := v_duplicate.posting_id;
  else
    v_description := v_survivor.description;
    v_confidence := v_survivor.content_confidence;
    v_posting_id := v_survivor.posting_id;
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
    last_seen_at       = greatest(v_survivor.last_seen_at, v_duplicate.last_seen_at),
    -- The survivor points at the posting whose text it kept (#174). The
    -- duplicate's row is about to disappear and takes its pointer with it,
    -- so without this a merge could leave the job pointing at the weaker
    -- side's posting while carrying the stronger side's description.
    posting_id         = coalesce(v_posting_id, v_survivor.posting_id, v_duplicate.posting_id)
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

-- Backfill -------------------------------------------------------------------
--
-- Every job row that already exists becomes a posting, resolved by its
-- fingerprint. There is one user's corpus today, so the only conflict that
-- can arise is the same user holding two rows with the same fingerprint,
-- which the (user_id, fingerprint) unique constraint already forbids.
--
-- Where a second user's rows do collide, one row wins whole: the posting
-- takes its description by the confidence rule below and its identity
-- columns from that same row. The upsert instead merges column by column,
-- so a losing row's company or ATS board is not carried over here the way a
-- later upsert would carry it -- the next time either user re-sees the
-- posting, the upsert backfills whatever this left empty. The description,
-- which is the column worth getting right on day one, is decided the same
-- way in both.

with best as (
  select distinct on (j.fingerprint)
         j.fingerprint, j.source, j.source_job_id, j.url, j.canonical_url,
         j.company, j.title, j.location, j.remote, j.description,
         j.description_hash, j.content_confidence,
         j.ats_provider, j.ats_board, j.ats_job_id
    from public.job_hunter_jobs j
   order by j.fingerprint,
            -- the description the posting keeps: best tier, then longest,
            -- then the earliest row, so the result does not depend on
            -- physical order.
            (j.description = '') asc,
            public.job_hunter_confidence_rank(j.content_confidence) asc,
            length(regexp_replace(j.description, '^\s+|\s+$', '', 'g')) desc,
            j.first_seen_at asc,
            j.id asc
),
seen as (
  select j.fingerprint,
         min(j.first_seen_at) as first_seen_at,
         max(j.last_seen_at) as last_seen_at
    from public.job_hunter_jobs j
   group by j.fingerprint
)
insert into public.job_hunter_postings
  (fingerprint, source, source_job_id, url, canonical_url, company, title,
   location, remote, description, description_hash, content_confidence,
   ats_provider, ats_board, ats_job_id, first_seen_at, last_seen_at)
select b.fingerprint, b.source, b.source_job_id, b.url, b.canonical_url,
       b.company, b.title, b.location, b.remote, b.description,
       b.description_hash, b.content_confidence,
       b.ats_provider, b.ats_board, b.ats_job_id,
       s.first_seen_at, s.last_seen_at
  from best b
  join seen s on s.fingerprint = b.fingerprint
on conflict (fingerprint) do nothing;

update public.job_hunter_jobs j
   set posting_id = p.id
  from public.job_hunter_postings p
 where p.fingerprint = j.fingerprint
   and j.posting_id is null;
