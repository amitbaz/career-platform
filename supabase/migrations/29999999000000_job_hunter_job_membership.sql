-- A job row becomes per-user membership over a posting (issue #178, epic #118).
--
-- This is the last structural step of the sequence #174 opened. The postings
-- table exists (20260909100000), every reader of a posting-level fact reads it
-- from there (20260909150000), objective facets key on it (20260909110000),
-- and cross-identity merging is decided once for everyone on it
-- (20260909180000). What is left is the duplication itself: every job row
-- still carries its own copy of the advertisement, and every additional user
-- who discovers a posting still pays for a whole copy of it.
--
-- After this migration a job row says four things and no more: which user,
-- which posting, which market that user attributed it to, and where it sits in
-- that user's funnel -- plus when this user first and last saw it. Every column
-- that describes the advertisement comes off, uniqueness becomes one row per
-- user per posting, and the fingerprint's uniqueness lives on the posting,
-- which is the row the fingerprint actually names.
--
-- Three consequences follow, and each is a section below.
--
--   * Identity resolution moves with the columns. The job upsert resolved a
--     listing against the caller's own rows by canonical URL, ATS triple and
--     normalized company/title/location; those columns are now the posting's,
--     so the resolution runs against postings and its outcome is a posting
--     merge (#176's job) rather than a private job merge. This is what makes
--     the decision cost once for the platform rather than once per user.
--   * Merging inverts. `job_hunter_merge_jobs` used to decide two
--     advertisements were the same and drag the postings along; now merging
--     two postings is the decision, and collapsing the affected users' rows is
--     its consequence -- including for users who were not the caller. With one
--     row per user per posting, two of one user's rows can only be merged by
--     merging the postings behind them, so that is exactly what
--     `job_hunter_merge_jobs` now does.
--   * The dual write collapses. `job_hunter_upsert_job` writes the posting
--     once and the membership row once. There is no second copy to keep in
--     step, which is the whole point: an additional user costs one narrow row
--     per posting instead of the description, the identity columns and the
--     fetch metadata all over again.
--
-- What deliberately does NOT change: what a run delivers. The `Job` the
-- pipeline is handed is composed from the posting plus the membership row
-- (PostgresJobStore.job_from_row), which is where every one of these values
-- was already read from for everything except `url` -- and `url` moves here
-- because a job row no longer absorbs several postings. The postings do: a
-- merge folds the better link onto the survivor (20260909180000), so the
-- resolved URL that used to live only on the merged job row is now on the
-- posting every user reads.
--
-- One behaviour does change, stated here rather than found later. The narrow
-- `match_mode = 'fingerprint'` upsert used to overwrite its row's stored text
-- unconditionally. Its text is now the posting's, shared by every user, so it
-- goes through `job_hunter_upsert_posting`'s keep-the-richer rule like every
-- other write. A thin re-fetch can no longer blank a description for everyone,
-- which is the only defensible behaviour once the row is not private.

-- Identity moves to the posting ------------------------------------------------
--
-- The same four stored generated columns 202609070002 put on job_hunter_jobs,
-- for the same reason: under row-level security a qual calling a non-LEAKPROOF
-- normalizer cannot be pushed below the security barrier, so the expression
-- indexes are unreachable and the lookup degrades to a sequential scan. Plain
-- column equality pushes down; the normalizers stay the single definition of
-- what normalization means and the columns cache their output where the
-- planner can reach it.
--
-- The indexes have no user_id prefix, and that is the point of the table: a
-- posting is not anybody's, so an identity lookup is over the whole corpus and
-- resolves to the same posting whoever asks.

alter table public.job_hunter_postings
  add column normalized_identity text
    generated always as (
      public.job_hunter_normalize_text(company) || '|' ||
      public.job_hunter_normalize_text(title) || '|' ||
      public.job_hunter_normalize_text(location)
    ) stored,
  add column canonical_url_of_url text
    generated always as (public.job_hunter_canonicalize_url(url)) stored,
  add column normalized_company text
    generated always as (public.job_hunter_normalize_company(company)) stored,
  add column normalized_title text
    generated always as (public.job_hunter_normalize_tokens(title)) stored;

create index job_hunter_postings_normalized_identity_idx
  on public.job_hunter_postings (normalized_identity);
create index job_hunter_postings_canonical_of_url_idx
  on public.job_hunter_postings (canonical_url_of_url);
create index job_hunter_postings_normalized_company_title_idx
  on public.job_hunter_postings (normalized_company, normalized_title);
create index job_hunter_postings_canonical_url_idx
  on public.job_hunter_postings (canonical_url);
create index job_hunter_postings_ats_idx
  on public.job_hunter_postings (ats_provider, ats_board, ats_job_id);
create index job_hunter_postings_source_job_idx
  on public.job_hunter_postings (source, source_job_id);

-- job_hunter_find_posting_by_identity ------------------------------------------
--
-- 202609070002's job-level function with the user taken out. The ambiguity
-- rule is unchanged and is the part worth keeping: when two matched rows are
-- in locations that are not compatible with each other, the identity is not
-- confident enough to act on and nothing is returned. Answering with one of
-- them would merge two different jobs at the same employer.

create or replace function public.job_hunter_find_posting_by_identity(
  p_company text, p_title text, p_location text
) returns setof uuid
language plpgsql
stable
security invoker
set search_path = ''
as $$
declare
  v_company text := public.job_hunter_normalize_company(p_company);
  v_title text := public.job_hunter_normalize_tokens(p_title);
begin
  if v_company = '' or v_title = '' then
    return;
  end if;

  return query
  with matches as (
    select p.id as posting_id,
           p.location as posting_location,
           p.created_at as posting_created_at
      from public.job_hunter_postings p
     where p.normalized_company = v_company
       and p.normalized_title = v_title
       and public.job_hunter_locations_compatible(p_location, p.location)
  )
  select m.posting_id
    from matches m
   where not exists (
           select 1
             from matches l
             join matches r on l.posting_id < r.posting_id
            where not public.job_hunter_locations_compatible(
                        l.posting_location, r.posting_location)
         )
   order by m.posting_created_at, m.posting_id;
end $$;

comment on function public.job_hunter_find_posting_by_identity(text, text, text) is
  'Postings whose normalized company, title and compatible location identify '
  'one advertisement, or nothing when the matches disagree about location. '
  'The job-level form of this (202609070002) asked the same question of one '
  'user''s rows; asked of postings it is answered once for everyone.';

-- job_hunter_collapse_job_rows --------------------------------------------------
--
-- Two membership rows of one user, folded into one. This is
-- job_hunter_merge_jobs' plumbing with every column decision taken out of it,
-- because there are no columns left to decide: what survives is one row's
-- market attribution and status, the earlier first_seen_at, the later
-- last_seen_at, and every record that hung off either row.
--
-- It takes the user explicitly rather than reading auth.uid(), which is what
-- lets job_hunter_merge_postings call it for users who are not the caller --
-- the same requirement that made that function SECURITY DEFINER in #176.
-- Nothing outside this schema may call it: it is revoked from every role
-- below, so the only ways in are the posting merge and job_hunter_merge_jobs.
--
-- The survivor is this user's evidence-bearing row, as it always was: an
-- application event first, then any evaluation/material/delivery, then the
-- earlier first_seen_at, then the lower id. Status and market_id are the
-- survivor's and are not folded -- that is what the old merge did, and the two
-- values are answers about a user's funnel that cannot be combined.

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

  update public.job_hunter_company_watch w set discovered_from_job_id = v_survivor_id
   where w.discovered_from_job_id = v_duplicate_id and w.user_id = p_user_id;

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
  'otherwise duplicate, and job_hunter_merge_jobs.';

revoke all on function public.job_hunter_collapse_job_rows(uuid, uuid, uuid)
  from public, anon, authenticated, service_role;

-- Every job row gets a posting, and one row per user per posting --------------
--
-- Two data steps before the columns can come off, and both are written to be
-- no-ops on a database that has only ever been written through the RPC.
--
-- 1. A row without a posting. `posting_id` was nullable precisely because
--    direct inserts still existed (pgTAP fixtures,
--    scripts/migrate_sqlite_to_postgres.py), and such a row has no
--    advertisement to fall back on once its own columns are gone. Its
--    fingerprint is the posting key, so the posting is created from the row's
--    own columns -- which still describe the advertisement at this point in
--    the migration -- and the row is pointed at it.
--
-- 2. Two rows of one user on one posting. Legal until now (two fingerprints,
--    two rows, both merged onto one posting by #176) and illegal from here,
--    so they are collapsed with the same function the merge path uses rather
--    than by an ad-hoc delete that would strand evaluations and deliveries.

insert into public.job_hunter_postings
  (fingerprint, source, source_job_id, url, canonical_url, company, title,
   location, remote, description, description_hash, content_confidence,
   ats_provider, ats_board, ats_job_id, first_seen_at, last_seen_at)
select distinct on (j.fingerprint)
       j.fingerprint, j.source, j.source_job_id, j.url, j.canonical_url,
       j.company, j.title, j.location, j.remote, j.description,
       j.description_hash, j.content_confidence, j.ats_provider, j.ats_board,
       j.ats_job_id, j.first_seen_at, j.last_seen_at
  from public.job_hunter_jobs j
 where j.posting_id is null
 order by j.fingerprint, j.first_seen_at, j.id
on conflict (fingerprint) do nothing;

update public.job_hunter_jobs j
   set posting_id = public.job_hunter_resolve_posting(p.id)
  from public.job_hunter_postings p
 where j.posting_id is null
   and p.fingerprint = j.fingerprint;

do $$
declare
  v_group record;
  v_survivor uuid;
  v_index int;
begin
  for v_group in
    select user_id, posting_id, array_agg(id order by first_seen_at, id) as ids
      from public.job_hunter_jobs
     group by user_id, posting_id
    having count(*) > 1
  loop
    v_survivor := v_group.ids[1];
    for v_index in 2..array_length(v_group.ids, 1) loop
      v_survivor := public.job_hunter_collapse_job_rows(
        v_group.user_id, v_survivor, v_group.ids[v_index]);
    end loop;
  end loop;
end $$;

-- The reduction ---------------------------------------------------------------
--
-- The four generated columns go with the columns they are computed from; the
-- indexes over any dropped column go with it automatically, including the
-- `unique (user_id, fingerprint)` constraint, whose job is now done by
-- job_hunter_postings.fingerprint's own unique constraint.
--
-- `unique (id, user_id)` stays: eight tables carry a composite foreign key to
-- it, which is what makes a child row unable to name a job belonging to
-- somebody else.

alter table public.job_hunter_jobs
  drop column normalized_identity,
  drop column canonical_url_of_url,
  drop column normalized_company,
  drop column normalized_title,
  drop column fingerprint,
  drop column source,
  drop column source_job_id,
  drop column url,
  drop column canonical_url,
  drop column company,
  drop column title,
  drop column location,
  drop column remote,
  drop column description,
  drop column description_hash,
  drop column content_confidence,
  drop column ats_provider,
  drop column ats_board,
  drop column ats_job_id,
  alter column posting_id set not null,
  add constraint job_hunter_jobs_user_id_posting_id_key unique (user_id, posting_id);

comment on table public.job_hunter_jobs is
  'One user''s membership of one posting: which market they attributed it to, '
  'where it sits in their funnel, and when they first and last saw it. Every '
  'fact about the advertisement itself lives on job_hunter_postings, which is '
  'shared, so a second user interested in the same posting costs this row and '
  'nothing else (#178).';

comment on column public.job_hunter_jobs.posting_id is
  'The advertisement this row is a membership of. Not null, and unique per '
  'user: the fingerprint that used to be unique per user is now unique on the '
  'posting, where it names the advertisement rather than one user''s copy.';

-- job_hunter_jobs_posting_idx stays. `unique (user_id, posting_id)` cannot
-- replace it: user_id leads that index, and a posting merge -- the statement
-- that re-points every affected user's row -- scans by posting_id alone.

-- job_hunter_merge_postings ---------------------------------------------------
--
-- Re-created from 20260909180000 with one addition, which the new uniqueness
-- makes mandatory rather than optional: before the affected users' rows are
-- re-pointed at the survivor, any user who holds a row on *both* postings has
-- those two rows collapsed. Without it the re-point violates
-- `unique (user_id, posting_id)` and the merge fails -- and failing is not the
-- right answer, because two rows of one user over what turned out to be one
-- advertisement is exactly the duplicate this whole sequence exists to remove.
--
-- The collapse runs for every affected user, not only the caller, for the same
-- reason this function is SECURITY DEFINER at all: the merge is one decision
-- for everyone, so it cannot leave other users holding a pair of rows that the
-- constraint says cannot exist.
--
-- Everything else -- the survivor ladder, the column fold, the facet discard,
-- the flattened redirect -- is unchanged.

create or replace function public.job_hunter_merge_postings(p_survivor uuid, p_duplicate uuid)
returns uuid
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_left uuid;
  v_right uuid;
  v_survivor_id uuid;
  v_duplicate_id uuid;
  v_survivor public.job_hunter_postings%rowtype;
  v_duplicate public.job_hunter_postings%rowtype;
  v_survivor_has_ats boolean;
  v_duplicate_has_ats boolean;
  v_canonical_url text;
  v_url text;
  v_preferred text[];
  v_prefer_duplicate boolean;
  v_attempt int;
  v_resolved_left uuid;
  v_resolved_right uuid;
  v_pair record;
begin
  if p_survivor is null or p_duplicate is null then
    raise exception 'survivor and duplicate postings must both exist';
  end if;

  v_left := public.job_hunter_resolve_posting(p_survivor);
  v_right := public.job_hunter_resolve_posting(p_duplicate);
  if v_left = v_right then
    return v_left;
  end if;

  for v_attempt in 1..16 loop
    perform 1 from public.job_hunter_postings p
     where p.id in (v_left, v_right)
     order by p.id
       for update;

    v_resolved_left := public.job_hunter_resolve_posting(v_left);
    v_resolved_right := public.job_hunter_resolve_posting(v_right);
    exit when v_resolved_left = v_left and v_resolved_right = v_right;

    v_left := v_resolved_left;
    v_right := v_resolved_right;
    if v_left = v_right then
      return v_left;
    end if;
    if v_attempt = 16 then
      raise exception 'posting merge could not settle on unmerged postings: % and %',
        v_left, v_right;
    end if;
  end loop;

  if v_left = v_right then
    return v_left;
  end if;

  select p.id
    into v_survivor_id
    from public.job_hunter_postings p
   where p.id in (v_left, v_right)
   order by
     (coalesce(p.description, '') = '') asc,
     public.job_hunter_confidence_rank(coalesce(p.content_confidence, '')) asc,
     length(regexp_replace(coalesce(p.description, ''), '^\s+|\s+$', '', 'g')) desc,
     (coalesce(p.ats_provider, '') <> ''
      and coalesce(p.ats_board, '') <> ''
      and coalesce(p.ats_job_id, '') <> '') desc,
     p.first_seen_at asc,
     p.id asc
   limit 1;

  v_duplicate_id := case when v_survivor_id = v_left then v_right else v_left end;

  select * into v_survivor from public.job_hunter_postings p where p.id = v_survivor_id;
  select * into v_duplicate from public.job_hunter_postings p where p.id = v_duplicate_id;

  if v_survivor.id is null or v_duplicate.id is null then
    raise exception 'survivor and duplicate postings must both exist';
  end if;

  v_survivor_has_ats := coalesce(v_survivor.ats_provider, '') <> ''
                    and coalesce(v_survivor.ats_board, '') <> ''
                    and coalesce(v_survivor.ats_job_id, '') <> '';
  v_duplicate_has_ats := coalesce(v_duplicate.ats_provider, '') <> ''
                     and coalesce(v_duplicate.ats_board, '') <> ''
                     and coalesce(v_duplicate.ats_job_id, '') <> '';

  v_prefer_duplicate := v_duplicate_has_ats and not v_survivor_has_ats;

  v_canonical_url := case
    when v_prefer_duplicate
      then coalesce(nullif(v_duplicate.canonical_url, ''), v_survivor.canonical_url, '')
    else coalesce(nullif(v_survivor.canonical_url, ''), v_duplicate.canonical_url, '')
  end;
  v_url := case
    when v_prefer_duplicate
      then coalesce(nullif(v_duplicate.url, ''), v_survivor.url, '')
    else coalesce(nullif(v_survivor.url, ''), v_duplicate.url, '')
  end;
  if coalesce(v_canonical_url, '') <> '' and (v_survivor_has_ats or v_duplicate_has_ats) then
    v_url := v_canonical_url;
  end if;

  v_preferred := public.job_hunter_preferred_description(
    v_survivor.description, v_survivor.content_confidence,
    v_duplicate.description, v_duplicate.content_confidence);

  update public.job_hunter_postings p set
    source             = coalesce(nullif(p.source, ''), v_duplicate.source, ''),
    source_job_id      = coalesce(nullif(p.source_job_id, ''), v_duplicate.source_job_id),
    url                = v_url,
    canonical_url      = v_canonical_url,
    company            = coalesce(nullif(p.company, ''), nullif(v_duplicate.company, ''), ''),
    title              = coalesce(nullif(p.title, ''), nullif(v_duplicate.title, ''), ''),
    location           = coalesce(nullif(p.location, ''), nullif(v_duplicate.location, ''), ''),
    remote             = coalesce(p.remote, v_duplicate.remote),
    description        = v_preferred[1],
    description_hash   = encode(sha256(convert_to(v_preferred[1], 'UTF8')), 'hex'),
    content_confidence = v_preferred[2],
    ats_provider       = coalesce(nullif(p.ats_provider, ''), nullif(v_duplicate.ats_provider, '')),
    ats_board          = coalesce(nullif(p.ats_board, ''), nullif(v_duplicate.ats_board, '')),
    ats_job_id         = coalesce(nullif(p.ats_job_id, ''), nullif(v_duplicate.ats_job_id, '')),
    first_seen_at      = least(p.first_seen_at, v_duplicate.first_seen_at),
    last_seen_at       = greatest(p.last_seen_at, v_duplicate.last_seen_at)
  where p.id = v_survivor_id;

  -- One row per user per posting, so a user holding both is folded before
  -- anything is re-pointed. The collapse chooses that user's evidence-bearing
  -- row, which may be either of the two; whichever survives is re-pointed by
  -- the statement below if it is still on the duplicate.
  for v_pair in
    select dup.user_id, dup.id as duplicate_job_id, sur.id as survivor_job_id
      from public.job_hunter_jobs dup
      join public.job_hunter_jobs sur
        on sur.user_id = dup.user_id
       and sur.posting_id = v_survivor_id
     where dup.posting_id = v_duplicate_id
  loop
    perform public.job_hunter_collapse_job_rows(
      v_pair.user_id, v_pair.survivor_job_id, v_pair.duplicate_job_id);
  end loop;

  update public.job_hunter_jobs j
     set posting_id = v_survivor_id
   where j.posting_id = v_duplicate_id;

  delete from public.job_hunter_job_facets f
   where f.posting_id = v_duplicate_id;

  update public.job_hunter_posting_merges m
     set survivor_id = v_survivor_id,
         merged_at = now()
   where m.survivor_id = v_duplicate_id;

  insert into public.job_hunter_posting_merges (duplicate_id, survivor_id)
  values (v_duplicate_id, v_survivor_id)
  on conflict (duplicate_id) do update
    set survivor_id = excluded.survivor_id,
        merged_at = now();

  return v_survivor_id;
end $$;

comment on function public.job_hunter_merge_postings(uuid, uuid) is
  'Collapse two postings that are the same advertisement into one survivor, '
  'once for everyone: the survivor keeps the better description, every '
  'affected user''s membership row is re-pointed at it -- and where a user '
  'held a row on both, the two are collapsed first, since one row per user '
  'per posting is now a constraint (#178) -- the merged-away posting''s facets '
  'are discarded, and a redirect records where it went.';

revoke all on function public.job_hunter_merge_postings(uuid, uuid)
  from public, anon, authenticated, service_role;

-- job_hunter_merge_jobs -------------------------------------------------------
--
-- What is left of it once a job row has no columns of its own. Two of one
-- user's rows can now only differ by posting, so "these two rows are the same
-- advertisement" *is* "these two postings are the same advertisement": this
-- resolves the caller's two rows, merges the postings behind them, and returns
-- the row the user is left with. The collapse of their two rows happens inside
-- that merge, along with every other user's.
--
-- It stays SECURITY DEFINER and it stays the only path to a posting merge that
-- `authenticated` can reach, which is what confines the merge to postings the
-- caller already holds rows for. Both statements that read job rows carry an
-- explicit `user_id = v_uid` predicate -- auth.uid() is still the caller's id
-- inside a definer function -- so a caller naming another user's row gets the
-- same 'survivor and duplicate jobs must both exist' as before.

create or replace function public.job_hunter_merge_jobs(p_survivor uuid, p_duplicate uuid)
returns uuid
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_uid uuid := (select auth.uid());
  v_survivor_posting uuid;
  v_duplicate_posting uuid;
  v_posting_id uuid;
  v_job_id uuid;
begin
  if p_survivor = p_duplicate then
    return p_survivor;
  end if;

  select j.posting_id into v_survivor_posting
    from public.job_hunter_jobs j
   where j.id = p_survivor and j.user_id = v_uid;
  select j.posting_id into v_duplicate_posting
    from public.job_hunter_jobs j
   where j.id = p_duplicate and j.user_id = v_uid;

  if v_survivor_posting is null or v_duplicate_posting is null then
    raise exception 'survivor and duplicate jobs must both exist';
  end if;

  if v_survivor_posting = v_duplicate_posting then
    -- Unreachable while `unique (user_id, posting_id)` holds, and cheaper to
    -- answer than to reason about: two rows on one posting are two rows this
    -- user should never have had, so collapse them directly.
    return public.job_hunter_collapse_job_rows(v_uid, p_survivor, p_duplicate);
  end if;

  v_posting_id := public.job_hunter_merge_postings(v_survivor_posting, v_duplicate_posting);

  select j.id into v_job_id
    from public.job_hunter_jobs j
   where j.user_id = v_uid and j.posting_id = v_posting_id;

  return v_job_id;
end $$;

comment on function public.job_hunter_merge_jobs(uuid, uuid) is
  'Merge two of one user''s membership rows by merging the postings behind '
  'them, and return the row that user is left with. Since #178 a job row '
  'carries no fact of its own, so there is nothing else to merge: the '
  'posting merge folds the advertisements, collapses every affected user''s '
  'duplicate rows, and records the redirect.';

revoke all on function public.job_hunter_merge_jobs(uuid, uuid)
  from public, anon, service_role;
grant execute on function public.job_hunter_merge_jobs(uuid, uuid) to authenticated;

-- job_hunter_upsert_job -------------------------------------------------------
--
-- The dual write collapses here. What used to be "resolve this listing against
-- my own rows, merge the duplicates, then write the advertisement into my row
-- and into the posting" becomes "resolve this listing against the postings,
-- merge the duplicates there, then make sure I have a membership row".
--
-- Identity resolution is the same ladder against the same values, one table
-- over: canonical URL, then a complete ATS triple, then normalized
-- company/title/location, then the fingerprint -- which is the posting the
-- payload itself resolves to and therefore never absent. Every candidate is
-- put through job_hunter_resolve_posting first, so a merged-away posting is
-- never a merge target, and the survivor of all of them is the posting the
-- membership row points at.
--
-- SECURITY DEFINER, changed from invoker, and it buys exactly one thing: the
-- posting merge stays revoked from `authenticated`. The alternative was a
-- definer wrapper around job_hunter_merge_postings callable by anyone, which
-- would let a caller name any two postings and collapse them for everybody.
-- Here the pair being merged is decided by the identity ladder from a payload,
-- never named by the caller -- the one posting id a payload may carry
-- (`posting_id`, from a staged batch) says which advertisement this listing
-- is, not which two to collapse. That is the same exposure #176 left behind
-- and #179 closes by moving shared-table writes off PostgREST.
--
-- Every statement that touches a per-user table carries `user_id = v_uid`,
-- with v_uid from auth.uid(), which under a definer function is still the
-- caller's id and not the owner's. RLS on job_hunter_jobs and
-- job_hunter_job_sources is exactly `(select auth.uid()) = user_id`, so the
-- predicates express what the policies did and the change is
-- behaviour-preserving. Any statement added here later must carry that
-- predicate or be provably safe without it.
--
-- `description_changed` now answers a question about the advertisement rather
-- than about one user's copy: did this call change the posting's description?
-- A payload that arrives with its posting already resolved -- the staged batch
-- path (#182), where job_hunter_merge_posting_batch has already folded this
-- listing's text in -- reports false, because the change, if any, happened
-- there and not here. Nothing in the pipeline reads this value: re-evaluation
-- is decided by job_hunter_needs_evaluation, which compares the evaluation's
-- recorded hash against the posting's current one and is unaffected.

create or replace function public.job_hunter_upsert_job(p_job jsonb)
returns table (id uuid, is_new boolean, description_changed boolean)
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_uid uuid := (select auth.uid());
  v_fingerprint text := coalesce(p_job->>'fingerprint', '');
  v_raw_canonical text := coalesce(p_job->>'canonical_url', '');
  v_lookup_canonical text;
  v_ats_provider text := lower(coalesce(p_job->>'ats_provider', ''));
  v_match_mode text := coalesce(p_job->>'match_mode', 'logical');
  v_supplied_posting uuid;
  v_posting_id uuid;
  v_candidates uuid[] := '{}'::uuid[];
  v_candidate uuid;
  v_resolved uuid;
  v_previous_hash text;
  v_current_hash text;
  v_job_id uuid;
  v_is_new boolean;
  v_now timestamptz := clock_timestamp();
  v_source text := coalesce(p_job->>'source', '');
  v_source_job_id text := p_job->>'source_job_id';
  v_source_url text;
  v_identity_key text;
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

  -- The advertisement. A staged batch (#182) has usually resolved and folded
  -- it already and passes the id in; a payload without one resolves its own,
  -- in this same call, so a membership row can never exist without the
  -- advertisement it is a membership of.
  v_supplied_posting := nullif(coalesce(p_job->>'posting_id', ''), '')::uuid;
  if v_supplied_posting is null then
    select p.description_hash into v_previous_hash
      from public.job_hunter_postings p
     where p.id = public.job_hunter_resolve_posting(
             (select q.id from public.job_hunter_postings q
               where q.fingerprint = v_fingerprint));
    v_posting_id := public.job_hunter_upsert_posting(p_job);
  else
    v_posting_id := public.job_hunter_resolve_posting(v_supplied_posting);
  end if;

  -- match_mode = 'fingerprint': identity is the fingerprint and nothing else,
  -- so there are no candidates to gather and nothing is ever merged. It also
  -- records no discovery source -- upsert_job never called _record_job_source.
  -- Collapsing this into the logical mode would hand a caller
  -- duplicate-merging it did not ask for.
  if v_match_mode = 'logical' then
    -- Identity resolution, strongest evidence first, preserving order and
    -- dropping repeats exactly as _append_unique_id did -- over postings now,
    -- because that is where these columns live and because the answer is the
    -- same for every user who asks.
    if v_lookup_canonical <> '' then
      for v_candidate in
        select p.id from public.job_hunter_postings p
         where p.canonical_url = v_lookup_canonical
         order by p.created_at, p.id
      loop
        v_resolved := public.job_hunter_resolve_posting(v_candidate);
        if not (v_resolved = any(v_candidates)) then
          v_candidates := v_candidates || v_resolved;
        end if;
      end loop;
    end if;

    if v_ats_provider in ('ashby', 'greenhouse', 'lever')
       and coalesce(p_job->>'ats_board', '') <> ''
       and coalesce(p_job->>'ats_job_id', '') <> '' then
      for v_candidate in
        select p.id from public.job_hunter_postings p
         where p.ats_provider = v_ats_provider
           and p.ats_board = p_job->>'ats_board'
           and p.ats_job_id = p_job->>'ats_job_id'
         order by p.created_at, p.id
      loop
        v_resolved := public.job_hunter_resolve_posting(v_candidate);
        if not (v_resolved = any(v_candidates)) then
          v_candidates := v_candidates || v_resolved;
        end if;
      end loop;
    end if;

    for v_candidate in
      select f from public.job_hunter_find_posting_by_identity(
        coalesce(p_job->>'company', ''),
        coalesce(p_job->>'title', ''),
        coalesce(p_job->>'location', '')) f
    loop
      v_resolved := public.job_hunter_resolve_posting(v_candidate);
      if not (v_resolved = any(v_candidates)) then
        v_candidates := v_candidates || v_resolved;
      end if;
    end loop;

    -- Every candidate collapses into the posting this payload resolved to.
    -- job_hunter_merge_postings chooses the survivor from the two rows rather
    -- than from argument order, so which of them the payload arrived as does
    -- not decide the outcome.
    foreach v_candidate in array v_candidates loop
      if v_candidate <> v_posting_id then
        v_posting_id := public.job_hunter_merge_postings(v_posting_id, v_candidate);
      end if;
    end loop;
  end if;

  select p.description_hash into v_current_hash
    from public.job_hunter_postings p where p.id = v_posting_id;

  -- The membership row. One statement for the row that does not exist yet and
  -- one for the row that does, both keyed on the pair this table is now unique
  -- on. Nothing else about the row is rewritten on a re-sighting: market_id
  -- and status are this user's answers and are set by their own writers
  -- (job_hunter_set_job_markets, set_job_status).
  insert into public.job_hunter_jobs as ins
    (user_id, posting_id, market_id, status, first_seen_at, last_seen_at, created_at)
  values (v_uid, v_posting_id, '', 'new', v_now, v_now, v_now)
  on conflict (user_id, posting_id) do nothing
  returning ins.id into v_job_id;

  if v_job_id is not null then
    v_is_new := true;
  else
    update public.job_hunter_jobs j set last_seen_at = v_now
     where j.user_id = v_uid and j.posting_id = v_posting_id
    returning j.id into v_job_id;
    v_is_new := false;
  end if;

  if v_job_id is null then
    raise exception 'membership row for posting % could not be written', v_posting_id;
  end if;

  if v_match_mode = 'logical' then
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
  end if;

  return query select
    v_job_id,
    v_is_new,
    v_previous_hash is not null and v_previous_hash is distinct from v_current_hash;
end
$$;

comment on function public.job_hunter_upsert_job(jsonb) is
  'Write the posting once and the caller''s membership of it once (#178). '
  'Identity is resolved against job_hunter_postings -- canonical URL, ATS '
  'triple, normalized company/title/location, fingerprint -- and every '
  'duplicate found is merged there, so the decision is made once for everyone '
  'rather than once per user. A payload carrying a posting_id keeps it rather '
  'than resolving the posting again, which is how a staged batch (#182) pays '
  'for the whole batch once. Timestamps come from clock_timestamp(), not '
  'now(), so multiple calls inside one transaction (job_hunter_upsert_jobs) '
  'still get strictly-ordered first_seen_at values matching input order.';

revoke all on function public.job_hunter_upsert_job(jsonb)
  from public, anon, service_role;
grant execute on function public.job_hunter_upsert_job(jsonb) to authenticated;

-- The readers -----------------------------------------------------------------
--
-- 20260909150000 read these facts from the posting and coalesced onto the job
-- row's copy, because a row written outside the RPC might have had no posting.
-- posting_id is not null now, so the fallback is dead code and the join is
-- inner.

create or replace function public.job_hunter_needs_evaluation(p_job_ids uuid[])
returns table (job_id uuid, needs boolean)
language sql
security invoker
set search_path = ''
as $$
  select
    j.id,
    case
      when e.evaluated_at is null then true
      when e.status = 'failed' then true
      -- Deliberately asymmetric: the posting side is coalesced, the
      -- evaluation side is not. PostgresJobStore.needs_evaluation compares
      -- `evaluation[...] != (facts.get(...) or "")`, so a null recorded at
      -- evaluation time counts as changed against an empty stored value.
      when e.description_hash_at_eval
           is distinct from coalesce(p.description_hash, '') then true
      when e.content_confidence_at_eval
           is distinct from coalesce(p.content_confidence, '') then true
      else false
    end
  from public.job_hunter_jobs j
  join public.job_hunter_postings p on p.id = j.posting_id
  left join lateral (
    select ev.status, ev.evaluated_at,
           ev.description_hash_at_eval, ev.content_confidence_at_eval
      from public.job_hunter_evaluations ev
     where ev.job_id = j.id
     order by ev.evaluated_at desc, ev.created_at desc, ev.id desc
     limit 1
  ) e on true
  where j.id = any(p_job_ids);
$$;

comment on function public.job_hunter_needs_evaluation(uuid[]) is
  'Bulk form of PostgresJobStore.needs_evaluation. The description hash and '
  'content confidence are the posting''s, which is the text the pipeline '
  'evaluates. An id the caller cannot read returns no row, and the caller '
  'treats a missing id as needing evaluation.';

-- job_hunter_eligible_inbound_jobs --------------------------------------------
--
-- The three match predicates move to the posting with the columns. #177 kept
-- them on the job row on the argument that a merged job row had seen every
-- posting behind it while posting_id named only one; that is no longer true in
-- either direction. A job row has seen nothing of its own, and the postings
-- themselves are merged now (#176), so the survivor carries the resolved
-- canonical URL and the folded identity columns that the merged job row used
-- to be the only holder of. Matching against it therefore matches everything
-- it used to and nothing more.

create or replace function public.job_hunter_eligible_inbound_jobs()
returns setof jsonb
language sql
security invoker
set search_path = ''
as $$
  with cand as materialized (
    select c.*,
           'gmail:' || c.source_platform as match_source,
           public.job_hunter_canonicalize_url(c.url) as match_canonical_url,
           public.job_hunter_normalize_text(c.company) || '|' ||
           public.job_hunter_normalize_text(c.title) || '|' ||
           public.job_hunter_normalize_text(c.location) as match_identity
      from public.job_hunter_inbound_job_candidates c
     where c.user_id = (select auth.uid())
       and c.last_seen_at >= now() - interval '14 days'
  )
  select to_jsonb(cand) - 'match_source' - 'match_canonical_url' - 'match_identity'
    from cand
   where not exists (
           select 1 from public.job_hunter_jobs j
            join public.job_hunter_postings p on p.id = j.posting_id
            where j.user_id = (select auth.uid())
              and p.source = cand.match_source
              and p.source_job_id = cand.source_candidate_key
              and public.job_hunter_gmail_candidate_complete(
                    j.id, j.status, p.description_hash, p.content_confidence)
         )
     and not exists (
           select 1 from public.job_hunter_jobs j
            join public.job_hunter_postings p on p.id = j.posting_id
            where j.user_id = (select auth.uid())
              and cand.url <> '' and p.url <> ''
              and p.canonical_url_of_url = cand.match_canonical_url
              and public.job_hunter_gmail_candidate_complete(
                    j.id, j.status, p.description_hash, p.content_confidence)
         )
     and not exists (
           select 1 from public.job_hunter_jobs j
            join public.job_hunter_postings p on p.id = j.posting_id
            where j.user_id = (select auth.uid())
              and cand.match_identity <> '||'
              and p.normalized_identity = cand.match_identity
              and public.job_hunter_gmail_candidate_complete(
                    j.id, j.status, p.description_hash, p.content_confidence)
         )
   order by cand.created_at, cand.id;
$$;

comment on function public.job_hunter_eligible_inbound_jobs() is
  'Gmail candidates whose logical job has not reached a terminal state or a '
  'current successful evaluation. Both halves of that -- which of the user''s '
  'rows the candidate matches, and whether the work done on it is current -- '
  'are decided from the posting the row is a membership of (#178).';

-- job_hunter_find_job_by_identity ---------------------------------------------
--
-- The store's identity lookup still answers with a job id, because that is
-- what its callers hold; the matching happens on postings, and the caller's
-- membership rows are what turn the answer back into their own ids. The
-- ambiguity rule lives in job_hunter_find_posting_by_identity now, so a
-- location disagreement returns nothing here too.

create or replace function public.job_hunter_find_job_by_identity(
  p_company text, p_title text, p_location text
) returns setof uuid
language sql
stable
security invoker
set search_path = ''
as $$
  select j.id
    from public.job_hunter_find_posting_by_identity(p_company, p_title, p_location) as f(posting_id)
    join public.job_hunter_jobs j
      on j.posting_id = f.posting_id
     and j.user_id = (select auth.uid())
   order by j.created_at, j.id;
$$;

comment on function public.job_hunter_find_job_by_identity(text, text, text) is
  'The caller''s membership rows for the postings a normalized company, title '
  'and compatible location identify. The identity itself is a fact about the '
  'advertisement and is matched on job_hunter_postings (#178).';

analyze public.job_hunter_jobs;
analyze public.job_hunter_postings;
