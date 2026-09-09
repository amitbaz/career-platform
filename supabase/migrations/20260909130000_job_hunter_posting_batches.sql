-- Persist a crawl batch with one set-based merge (issue #182, epic #181).
--
-- Until now a crawl persisted its listings one at a time. `job_hunter_upsert_jobs`
-- (202609070003) already put a chunk of them in one round trip, but the work
-- inside is still a plpgsql loop calling `job_hunter_upsert_job` per element,
-- and each of those calls `job_hunter_upsert_posting` -- an INSERT, a SELECT
-- and an UPDATE against `job_hunter_postings` for every listing in the batch,
-- however many of them describe the same advertisement.
--
-- Run 34289288702 measured what that costs: 1170.8s of canonical resolution
-- for 1,221 jobs against only 81 network attempts, 213.0s of unique
-- persistence and 172.2s of raw persistence. The engine was waiting on its
-- own database, one row at a time.
--
-- This migration adds the other shape. A crawl bulk-loads its listings into
-- `job_hunter_posting_staging` under a batch identifier -- over a direct,
-- pooled Postgres connection held by ingestion, which is what makes COPY
-- available at all -- and then calls `job_hunter_merge_posting_batch` once.
-- That one statement normalizes the batch, collapses duplicates inside it,
-- resolves each listing against existing postings by fingerprint, inserts
-- what is new, updates what changed, reports which rows were genuinely new,
-- and clears the batch from staging.
--
-- The identity rules do not change. A posting is still keyed by the
-- fingerprint `normalize.py:job_fingerprint` computes (source + source job
-- id, else canonical URL, else company/title/location) and the field-by-field
-- resolution below is `job_hunter_upsert_posting`'s, restated over a set: the
-- better description wins by content confidence, identity columns keep what
-- they have and only backfill what is empty, a resolved canonical URL
-- replaces whatever URL the posting was first seen under, and `first_seen_at`
-- never moves. What changes is that the rules are applied once over a batch
-- instead of once per listing over the network.
--
-- Nothing here is per-user. Postings are the shared corpus, so the privileged
-- connection touches only tables with no user dimension; `job_hunter_jobs`
-- and every other per-user surface keep PostgREST and row-level security
-- exactly as they are. That asymmetry is deliberate -- see #181.

-- The staging area ------------------------------------------------------------
--
-- Deliberately not a queue and not durable state: a row lives here only
-- between the COPY that wrote it and the merge that consumes it. The columns
-- are the posting-shaped subset of the payload `job_hunter_upsert_job`
-- already receives, so a caller stages exactly what it would otherwise have
-- sent one row at a time.
--
-- `ordinal` is the listing's position in the batch, and it is not decoration:
-- the per-listing rules it replaces are order-dependent (first non-empty
-- company wins, last resolved canonical URL wins), so collapsing a batch
-- without knowing input order would not reproduce them.

create table public.job_hunter_posting_staging (
  batch_id uuid not null,
  ordinal int not null,
  fingerprint text not null,
  source text,
  source_job_id text,
  url text,
  canonical_url text,
  company text,
  title text,
  location text,
  remote boolean,
  description text,
  content_confidence text,
  ats_provider text,
  ats_board text,
  ats_job_id text,
  primary key (batch_id, ordinal)
);

comment on table public.job_hunter_posting_staging is
  'Scratch space for one crawl batch of listings, keyed by batch identifier '
  'and input position. Written by COPY over ingestion''s privileged '
  'connection and emptied by job_hunter_merge_posting_batch; a row outliving '
  'its merge means the merge did not run.';

-- No user can reach this table, by either route. Row-level security with no
-- policy denies everything to a non-owner, and the grants are revoked as
-- well so the denial does not depend on a policy nobody wrote later being
-- absent. Ingestion connects as the owner, which is the only writer --
-- service_role is revoked with the rest because nothing in this application
-- uses that key, and a table only ingestion writes should not become
-- reachable the day something does.
alter table public.job_hunter_posting_staging enable row level security;
revoke all on table public.job_hunter_posting_staging
  from anon, authenticated, service_role;

-- The description rule, stated once ---------------------------------------------
--
-- `job_hunter_upsert_posting` and `job_hunter_merge_jobs` both decide which of
-- two descriptions to keep with the same ladder, each spelling it out inline:
-- an empty side never wins, then the more trustworthy tier wins, and only on
-- a tie does the longer text win, with the stored side keeping it on a full
-- tie. The set-based merge below needs that answer three times in one UPDATE
-- (the text, its hash and its tier), and an UPDATE ... FROM cannot put the
-- target row through a LATERAL, so here the ladder is a function rather than
-- a repeated CASE.
--
-- The other two are deliberately NOT re-created to call it. Re-creating a
-- 300-line function to no behavioural end is a diff a reviewer has to read
-- for nothing; the next change that touches one of them is when it should
-- adopt this.
--
-- Returns `{description, content_confidence}` -- the tier always travels with
-- the text it belongs to, which is the property the ladder exists to keep.

create or replace function public.job_hunter_preferred_description(
  p_stored text,
  p_stored_confidence text,
  p_incoming text,
  p_incoming_confidence text)
returns text[]
language sql
immutable
security invoker
set search_path = ''
as $$
  select case
    when coalesce(p_incoming, '') = ''
      then array[coalesce(p_stored, ''), coalesce(p_stored_confidence, '')]
    when coalesce(p_stored, '') = ''
      then array[p_incoming, coalesce(p_incoming_confidence, '')]
    when public.job_hunter_confidence_rank(coalesce(p_incoming_confidence, ''))
       < public.job_hunter_confidence_rank(coalesce(p_stored_confidence, ''))
      then array[p_incoming, coalesce(p_incoming_confidence, '')]
    when public.job_hunter_confidence_rank(coalesce(p_incoming_confidence, ''))
       > public.job_hunter_confidence_rank(coalesce(p_stored_confidence, ''))
      then array[coalesce(p_stored, ''), coalesce(p_stored_confidence, '')]
    when length(regexp_replace(p_incoming, '^\s+|\s+$', '', 'g'))
       > length(regexp_replace(p_stored, '^\s+|\s+$', '', 'g'))
      then array[p_incoming, coalesce(p_incoming_confidence, '')]
    else array[coalesce(p_stored, ''), coalesce(p_stored_confidence, '')]
  end;
$$;

comment on function public.job_hunter_preferred_description(text, text, text, text) is
  'Which of two descriptions a posting keeps, and the content-confidence tier '
  'that belongs to it, as {description, content_confidence}. The ladder is '
  'the one job_hunter_upsert_posting applies: an empty side never wins, then '
  'the better tier, then the longer text, and the stored side keeps a full '
  'tie.';

-- The merge --------------------------------------------------------------------
--
-- One statement. The data-modifying CTEs all see the same snapshot, so
-- `normalized` still reads the batch that `cleared` deletes, and `inserted`
-- and `updated` touch disjoint rows -- a fingerprint either already had a
-- posting in that snapshot (updated) or did not (inserted).
--
-- Every expression below has a line in `job_hunter_upsert_posting` it is
-- reproducing. Where a rule is order-dependent, the aggregate says which end
-- of the batch wins and why:
--
--   * `source`, `company`, `title`, `location`, `ats_*`: the update keeps a
--     non-empty stored value and only backfills an empty one, so folding a
--     batch left to right keeps the FIRST non-empty value.
--   * `url`/`canonical_url`: an incoming resolved canonical URL overwrites
--     what is stored, because resolving one is an improvement on whatever URL
--     the posting was first seen under -- so the LAST non-empty one wins, and
--     a batch with no canonical URL at all falls back to the first non-empty
--     plain URL.
--   * `description`: the better text wins, never the later one. An empty
--     side never wins, then the more trustworthy tier wins, and only on a tie
--     does the longer text win -- with the earlier listing keeping it on a
--     full tie, because the fold compares each incoming row against the
--     accumulator and keeps the accumulator unless the incoming row is
--     strictly better.
--   * `remote`: the update coalesces onto the stored value, so the first
--     non-null wins.
--
-- `source_job_id` and the `ats_*` columns are nullable, and the two differ in
-- what an all-empty batch leaves behind: `source_job_id`'s update coalesces
-- the incoming value raw (`coalesce(nullif(p.source_job_id, ''), incoming)`),
-- so the last listing's raw value survives, while the `ats_*` updates
-- `nullif` the incoming value too, so a batch of more than one listing that
-- says nothing about ATS identity leaves null. Reproduced rather than
-- tidied: tidying either one would change which postings a re-run produces.

create or replace function public.job_hunter_merge_posting_batch(p_batch_id uuid)
returns table (fingerprint text, posting_id uuid, is_new boolean)
language sql
security invoker
set search_path = ''
as $$
  with normalized as (
    select
      s.ordinal,
      s.fingerprint                                        as fingerprint,
      coalesce(s.source, '')                               as source,
      s.source_job_id                                      as source_job_id,
      coalesce(s.url, '')                                  as url,
      coalesce(s.canonical_url, '')                        as raw_canonical,
      -- job_hunter_upsert_posting: an explicit canonical_url is taken as
      -- given, and only a bare URL is put through the canonicalizer.
      coalesce(nullif(coalesce(s.canonical_url, ''), ''),
               public.job_hunter_canonicalize_url(coalesce(s.url, ''))) as canonical_url,
      coalesce(s.company, '')                              as company,
      coalesce(s.title, '')                                as title,
      coalesce(s.location, '')                             as location,
      s.remote                                             as remote,
      coalesce(s.description, '')                          as description,
      coalesce(s.content_confidence, '')                   as content_confidence,
      s.ats_provider                                       as ats_provider,
      s.ats_board                                          as ats_board,
      s.ats_job_id                                         as ats_job_id
    from public.job_hunter_posting_staging s
    where s.batch_id = p_batch_id
      and coalesce(s.fingerprint, '') <> ''
  ),
  -- The description the fold keeps, with the confidence tier that belongs to
  -- it: best tier first, then longest trimmed text, then earliest listing.
  best_description as (
    select distinct on (n.fingerprint)
           n.fingerprint, n.description, n.content_confidence
      from normalized n
     where n.description <> ''
     order by n.fingerprint,
              public.job_hunter_confidence_rank(n.content_confidence) asc,
              length(regexp_replace(n.description, '^\s+|\s+$', '', 'g')) desc,
              n.ordinal asc
  ),
  collapsed as (
    select
      n.fingerprint,
      count(*)                                                              as listings,
      coalesce((array_remove(array_agg(nullif(n.source, '') order by n.ordinal), null))[1], '') as source,
      -- First non-empty, else the last listing's raw value (which is what the
      -- coalesce chain leaves when every listing is silent).
      coalesce(
        (array_remove(array_agg(nullif(coalesce(n.source_job_id, ''), '') order by n.ordinal), null))[1],
        (array_agg(n.source_job_id order by n.ordinal desc))[1])            as source_job_id,
      (array_remove(array_agg(nullif(n.raw_canonical, '') order by n.ordinal desc), null))[1] as last_canonical_source,
      (array_remove(array_agg(nullif(n.url, '') order by n.ordinal), null))[1]                as first_url,
      (array_remove(array_agg(nullif(n.canonical_url, '') order by n.ordinal desc), null))[1] as last_canonical,
      coalesce((array_remove(array_agg(nullif(n.company, '') order by n.ordinal), null))[1], '')  as company,
      coalesce((array_remove(array_agg(nullif(n.title, '') order by n.ordinal), null))[1], '')    as title,
      coalesce((array_remove(array_agg(nullif(n.location, '') order by n.ordinal), null))[1], '') as location,
      (array_remove(array_agg(n.remote order by n.ordinal), null))[1]       as remote,
      coalesce(max(bd.description), '')                                     as description,
      -- No listing carried text, so the fold never left the first listing's
      -- tier -- an empty description keeps the confidence it was stored with.
      coalesce(max(bd.content_confidence),
               (array_agg(n.content_confidence order by n.ordinal))[1], '') as content_confidence,
      -- One listing inserts its raw value; two or more fold through
      -- nullif(), which leaves null when nobody said anything.
      case when count(*) = 1 then (array_agg(n.ats_provider order by n.ordinal))[1]
           else (array_remove(array_agg(nullif(coalesce(n.ats_provider, ''), '') order by n.ordinal), null))[1]
      end                                                                   as ats_provider,
      case when count(*) = 1 then (array_agg(n.ats_board order by n.ordinal))[1]
           else (array_remove(array_agg(nullif(coalesce(n.ats_board, ''), '') order by n.ordinal), null))[1]
      end                                                                   as ats_board,
      case when count(*) = 1 then (array_agg(n.ats_job_id order by n.ordinal))[1]
           else (array_remove(array_agg(nullif(coalesce(n.ats_job_id, ''), '') order by n.ordinal), null))[1]
      end                                                                   as ats_job_id
    from normalized n
    left join best_description bd on bd.fingerprint = n.fingerprint
    group by n.fingerprint
  ),
  inserted as (
    insert into public.job_hunter_postings as ins
      (fingerprint, source, source_job_id, url, canonical_url, company, title,
       location, remote, description, description_hash, content_confidence,
       ats_provider, ats_board, ats_job_id, first_seen_at, last_seen_at, created_at)
    select
      c.fingerprint,
      c.source,
      c.source_job_id,
      coalesce(c.last_canonical_source, c.first_url, ''),
      coalesce(c.last_canonical, ''),
      c.company,
      c.title,
      c.location,
      c.remote,
      c.description,
      encode(sha256(convert_to(c.description, 'UTF8')), 'hex'),
      c.content_confidence,
      c.ats_provider,
      c.ats_board,
      c.ats_job_id,
      -- statement_timestamp(), not clock_timestamp(): the whole batch was
      -- seen at one moment, and one moment is what a posting's three
      -- timestamps should agree on. job_hunter_upsert_job needs
      -- clock_timestamp() for the opposite reason -- its per-element loop
      -- relies on strictly increasing values to keep discovery order -- but
      -- nothing here is ordered by time, and a fingerprint is unique.
      statement_timestamp(), statement_timestamp(), statement_timestamp()
    from collapsed c
    on conflict (fingerprint) do nothing
    returning ins.id, ins.fingerprint
  ),
  updated as (
    update public.job_hunter_postings p set
      source             = coalesce(nullif(p.source, ''), c.source),
      source_job_id      = coalesce(nullif(p.source_job_id, ''), c.source_job_id),
      url                = coalesce(c.last_canonical_source, nullif(p.url, ''), c.first_url, ''),
      canonical_url      = coalesce(c.last_canonical, p.canonical_url),
      company            = coalesce(nullif(p.company, ''), nullif(c.company, ''), ''),
      title              = coalesce(nullif(p.title, ''), nullif(c.title, ''), ''),
      location           = coalesce(nullif(p.location, ''), nullif(c.location, ''), ''),
      remote             = coalesce(p.remote, c.remote),
      description        = (public.job_hunter_preferred_description(
                              p.description, p.content_confidence,
                              c.description, c.content_confidence))[1],
      description_hash   = encode(sha256(convert_to(
                              (public.job_hunter_preferred_description(
                                 p.description, p.content_confidence,
                                 c.description, c.content_confidence))[1], 'UTF8')), 'hex'),
      content_confidence = (public.job_hunter_preferred_description(
                              p.description, p.content_confidence,
                              c.description, c.content_confidence))[2],
      ats_provider       = coalesce(nullif(p.ats_provider, ''), nullif(c.ats_provider, '')),
      ats_board          = coalesce(nullif(p.ats_board, ''), nullif(c.ats_board, '')),
      ats_job_id         = coalesce(nullif(p.ats_job_id, ''), nullif(c.ats_job_id, '')),
      last_seen_at       = greatest(p.last_seen_at, statement_timestamp())
    from collapsed c
    where p.fingerprint = c.fingerprint
    returning p.id, p.fingerprint
  ),
  cleared as (
    delete from public.job_hunter_posting_staging s
     where s.batch_id = p_batch_id
    returning s.ordinal
  )
  select
    c.fingerprint,
    -- Both arms are empty only for a fingerprint another connection inserted
    -- after this statement took its snapshot: `on conflict do nothing` sees
    -- that row through the unique index and skips it, while the update never
    -- saw it at all. The read-back arm cannot recover it either -- it runs
    -- under this statement's own snapshot, which is exactly the snapshot the
    -- row is invisible in -- so posting_id is NULL for such a row, and NULL
    -- is the caller's signal: merge_posting_batch leaves that fingerprint out
    -- of the mapping it returns, and the job upsert resolves its own posting
    -- the way it did before #182. The arm is kept because it does recover the
    -- one case it can, a row this transaction had already written.
    coalesce(i.id, u.id,
             (select p.id from public.job_hunter_postings p
               where p.fingerprint = c.fingerprint)) as posting_id,
    (i.id is not null)                               as is_new
  from collapsed c
  left join inserted i on i.fingerprint = c.fingerprint
  left join updated u on u.fingerprint = c.fingerprint
$$;

comment on function public.job_hunter_merge_posting_batch(uuid) is
  'Resolve, deduplicate, insert and update a whole staged crawl batch of '
  'postings in one statement, then clear the batch from staging. Returns one '
  'row per distinct fingerprint in the batch with the posting it resolved to '
  'and whether that posting was genuinely new. Merging a batch that is '
  'already cleared returns no rows and writes nothing.';

-- Supabase's default privileges grant execute on a new public function to
-- anon and authenticated by name, so revoking from PUBLIC alone leaves both
-- of them holding it. The merge is ingestion's, and ingestion connects as
-- the owner.
revoke all on function public.job_hunter_merge_posting_batch(uuid)
  from public, anon, authenticated, service_role;

-- job_hunter_upsert_job --------------------------------------------------------
--
-- Re-created from 20260909100000 with one addition: a payload that already
-- names its posting keeps it instead of resolving one again.
--
-- That is what makes the batch merge a saving rather than extra work. A crawl
-- now stages its listings, merges them once, and then sends the same jobs
-- through `job_hunter_upsert_jobs` carrying the posting id the merge resolved
-- -- so the per-element loop no longer pays an INSERT, a SELECT and an UPDATE
-- against `job_hunter_postings` for every listing, including the many that
-- describe an advertisement another listing in the same batch already
-- described.
--
-- A payload without `posting_id` behaves exactly as before and resolves its
-- own posting, so every caller that has no direct Postgres connection --
-- `upsert_logical_job` in the canonical resolution tail, the Gmail paths, a
-- deployment with no `SUPABASE_DB_URL` at all -- is unaffected. Everything
-- else below is unchanged.

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

  -- The posting this payload describes. A batch merge
  -- (job_hunter_merge_posting_batch, #182) has usually resolved it already
  -- and passes the id in; a payload without one resolves its own, in this
  -- same call, so a job row can never exist without the advertisement it is
  -- a copy of.
  v_posting_id := nullif(coalesce(p_job->>'posting_id', ''), '')::uuid;
  if v_posting_id is null then
    v_posting_id := public.job_hunter_upsert_posting(p_job);
  end if;

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
  'one call. A payload carrying a posting_id keeps it rather than resolving '
  'the posting again, which is how a staged batch merge (#182) pays for the '
  'whole batch once instead of once per listing. Timestamps come from '
  'clock_timestamp(), not now(), so multiple calls inside one transaction '
  '(job_hunter_upsert_jobs, 202609070003) still get strictly-ordered '
  'first_seen_at/created_at values matching input order -- now() is constant '
  'for the whole transaction and would tie every row in a batch, letting the '
  'merge-survivor tiebreak fall through to id order instead of preserving '
  'discovery order.';
