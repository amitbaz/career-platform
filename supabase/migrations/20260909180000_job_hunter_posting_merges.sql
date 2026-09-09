-- Cross-identity merging moves to the posting (issue #176, epic #118).
--
-- Deduplication by identity key is already done and is not what this is.
-- `job_hunter_merge_posting_batch` (20260909130000) resolves a staged batch
-- against existing postings *by fingerprint*, so a posting one user
-- discovered is found again when another user's run sees the same
-- fingerprint. What that cannot do is merge two *distinct* postings: the
-- fingerprint is source-scoped, so the same advertisement seen on an
-- employer's ATS board and on an aggregator hashes twice and produces two
-- postings.
--
-- Collapsing those was `job_hunter_merge_jobs` (20260909100000), which is
-- per-user throughout -- it reads `auth.uid()`, filters `j.user_id = v_uid`,
-- and records the redirect in `job_hunter_job_merges`, a table carrying a
-- `user_id` and four owner-scoped policies. Every user therefore repeated
-- the same cross-source decision, and two users could reach different
-- conclusions about the same pair of records.
--
-- This migration moves that decision onto the posting, where it is made
-- once and recorded once:
--
--   * `job_hunter_posting_merges` records where a merged-away posting went,
--     for everyone rather than per user;
--   * `job_hunter_resolve_posting` turns a stale posting id into the
--     survivor, in one lookup -- redirects are flattened when they are
--     written, so there is never a chain to walk;
--   * `job_hunter_merge_postings` performs the merge: it folds the
--     duplicate's columns onto the survivor under the existing
--     `job_hunter_preferred_description` rule, re-points *every* affected
--     user's job row, and discards the duplicate's facets;
--   * `job_hunter_upsert_posting` follows a redirect, so a later crawl of
--     the merged-away source improves the survivor instead of the row it
--     was merged out of;
--   * `job_hunter_merge_posting_batch` resolves the mapping it returns
--     through the same redirect, so the job upsert stamps the survivor;
--   * `job_hunter_merge_jobs` stops deciding. When its two job rows point
--     at different postings it delegates to `job_hunter_merge_postings` and
--     takes the surviving posting's text, so it is the per-user consequence
--     of one global decision rather than a second, competing authority.
--     It is left in place for #178 to remove along with the rest of the
--     duplicated job-row machinery.
--
-- What is deliberately NOT here, because #178 owns it: `url` and the
-- identity columns stay on the job row. #177 left them there because they
-- ask whether a user already holds a row covering this advertisement -- a
-- question about coverage, not about what the advertisement says -- and a
-- job row accumulates evidence from every posting merged into it while
-- `posting_id` names one. This migration is what makes that revisitable,
-- not what revisits it.
--
-- Facets: #125's rule, restated at the posting level. A merged-away
-- posting's facets are discarded rather than stamped onto the survivor,
-- because stamping them would pin one posting's facts to another posting's
-- text as permanently current, with no path back to re-extraction. The
-- survivor is extracted from its own text on a later run, through the
-- description-hash comparison that already governs currency.

-- The redirect ----------------------------------------------------------------
--
-- `job_hunter_job_merges` with the `user_id` taken out, which is the whole
-- point: one record of where a posting went, read by everyone.
--
-- Unlike the job-level table, this one keeps a foreign key on
-- `duplicate_id`. `job_hunter_merge_jobs` deletes the duplicate row, so its
-- redirect names a row that no longer exists; a posting merge does not.
-- Keeping the merged-away posting is what stops the next crawl of that
-- source resurrecting it: the fingerprint stays claimed, so the listing
-- resolves to a row that already knows where it went instead of inserting
-- a fresh competing posting and forcing the merge decision to be made again
-- every day. Nothing reads a merged-away posting's own columns; they are
-- left as they were, as the record of what that source said.

create table public.job_hunter_posting_merges (
  id uuid primary key default gen_random_uuid(),
  duplicate_id uuid not null unique
    references public.job_hunter_postings(id) on delete cascade,
  survivor_id uuid not null
    references public.job_hunter_postings(id) on delete cascade,
  merged_at timestamptz not null default now(),
  created_at timestamptz not null default now()
);

comment on table public.job_hunter_posting_merges is
  'Where a merged-away posting went. One record for everyone, not one per '
  'user: a caller holding a stale posting id resolves to the survivor '
  'through job_hunter_resolve_posting, and redirects are flattened when '
  'written so one lookup is always enough.';

-- Flattening a chain (see job_hunter_merge_postings) looks rows up by
-- survivor.
create index job_hunter_posting_merges_survivor_idx
  on public.job_hunter_posting_merges (survivor_id);

-- Reads are open to every authenticated user, exactly as job_hunter_postings
-- and job_hunter_job_facets are and for the same reason: a merge decided
-- once for everyone is useless if only its author can see it.
--
-- There are deliberately no write policies at all. Every write here happens
-- inside job_hunter_merge_postings, which is security definer and therefore
-- writes as the owner; a redirect written any other way would name a merge
-- that never re-pointed anything. This is tighter than the two shared
-- tables above, whose open writes #179 narrows.
alter table public.job_hunter_posting_merges enable row level security;
create policy select_authenticated on public.job_hunter_posting_merges
  for select to authenticated using (true);

-- Resolution --------------------------------------------------------------------

create or replace function public.job_hunter_resolve_posting(p_posting_id uuid)
returns uuid
language sql
stable
security invoker
set search_path = ''
as $$
  select coalesce(
    (select m.survivor_id
       from public.job_hunter_posting_merges m
      where m.duplicate_id = p_posting_id),
    p_posting_id);
$$;

comment on function public.job_hunter_resolve_posting(uuid) is
  'The posting a possibly merged-away posting id resolves to, or the id '
  'itself when it was never merged. One lookup: redirects are flattened '
  'when they are written, so there is no chain to walk.';

-- The merge ----------------------------------------------------------------------
--
-- SECURITY DEFINER, which this schema otherwise uses exactly once
-- (`job_hunter_get_provider_credentials`). It is the first acceptance
-- criterion of #176 that makes it necessary: merging two postings must
-- re-point *every* affected user's job row, not only the merging user's,
-- and row-level security on job_hunter_jobs scopes an invoker to its own
-- rows -- under which the update would silently touch nothing and leave
-- other users pointing at a posting nobody maintains.
--
-- The hazard that buys is real and is recorded here rather than discovered
-- later: any authenticated user may call this, and a call naming two
-- unrelated postings collapses them for everybody. It is the same trust
-- posture job_hunter_postings already has -- any authenticated user may
-- rewrite any posting's description today -- and #179, which narrows the
-- writes on the shared tables to the platform identity, is where both stop
-- being true. Treat this function as part of that ticket's blocking scope.
--
-- The survivor is chosen from the postings themselves, never from argument
-- order, so two callers who name the pair the other way round converge. It
-- is 20260909100000's backfill ladder -- "one row wins whole" -- with the
-- ATS identity added as a tie-break:
--
--   1. the posting whose description wins: an empty side never wins, then
--      the more trustworthy tier, then the longer text. The survivor is the
--      row the surviving text came from, so its source, source_job_id and
--      url still describe the rendering the description was read from
--      rather than some other source's.
--   2. then a complete ATS identity. The employer's own board is the better
--      identity for the same reason job_hunter_merge_jobs already prefers
--      it when it picks a URL: the aggregator's rendering is a copy of it.
--   3. then the earliest first_seen_at, then the lower id -- the job-level
--      tie-break, so the result does not depend on physical order.
--
-- Per-user evidence (an application, an evaluation) deliberately does not
-- enter the choice the way it does at the job level. A posting is not one
-- user's, so "which of these two rows has history" has as many answers as
-- there are users, and picking by one user's would make the outcome depend
-- on who merged first.

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
begin
  if p_survivor is null or p_duplicate is null then
    raise exception 'survivor and duplicate postings must both exist';
  end if;

  -- Resolving both sides first is what makes merging into an
  -- already-merged posting land on the posting that still exists, and what
  -- makes a repeated merge of the same pair a no-op rather than a second
  -- redirect.
  v_left := public.job_hunter_resolve_posting(p_survivor);
  v_right := public.job_hunter_resolve_posting(p_duplicate);
  if v_left = v_right then
    return v_left;
  end if;

  -- Lock both rows before choosing, in id order so two concurrent merges of
  -- overlapping pairs queue rather than deadlock. Without it, two sessions
  -- merging the same pair both read the pre-merge rows, both fold, and the
  -- second one folds a duplicate that has already been merged away -- the
  -- redirect survives that, but the survivor's description does not
  -- necessarily.
  --
  -- Locking once is not enough. A pair can be merged away while this call
  -- waits for the lock, and re-resolving then names a row this call does not
  -- hold -- which a third session is free to merge away in turn, leaving a
  -- redirect that points at a redirect. That is the one thing
  -- job_hunter_resolve_posting's single lookup rules out, so loop until the
  -- rows held are the rows resolved to. Each pass either settles or follows
  -- a committed merge, and merges strictly reduce the number of live
  -- postings, so the loop terminates; the bound is a guard against a cycle
  -- that flattening should already make impossible, not an expected path.
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

  -- job_hunter_merge_jobs' URL rule, at the posting level, including the
  -- asymmetry that rule turns on: when only the merged-away side carries an
  -- ATS identity, its links win outright rather than merely backfilling.
  --
  -- Dropping that would make the survivor lie. The survivor is chosen by
  -- description first, so a thin ATS listing scraped from a board index
  -- loses to a fat aggregator rendering -- and the column fold below then
  -- backfills the survivor's empty ats_provider/ats_board/ats_job_id from
  -- the ATS side. A posting claiming a Greenhouse identity while linking
  -- only to the aggregator is the one combination that must not come out of
  -- here: the ATS link is the one the employer will still honour, and the
  -- ATS identity is what tells the rest of the system to trust it.
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

  -- The survivor was chosen by this same ladder, so this returns the
  -- survivor's own text today. It is applied anyway rather than assumed:
  -- the two must agree, and stating it keeps a later change to the survivor
  -- rule -- one made for identity reasons rather than text ones -- from
  -- silently leaving the worse description on the row everyone reads.
  v_preferred := public.job_hunter_preferred_description(
    v_survivor.description, v_survivor.content_confidence,
    v_duplicate.description, v_duplicate.content_confidence);

  -- Identity columns keep what is already there and only backfill what is
  -- empty, as every other posting write does. first_seen_at is when anyone
  -- first saw the advertisement, which is now the earlier of the two.
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

  -- Every affected user's job row, which is the criterion this function
  -- exists for. No user_id filter, and none possible: the caller is one
  -- user and the rows belong to all of them.
  update public.job_hunter_jobs j
     set posting_id = v_survivor_id
   where j.posting_id = v_duplicate_id;

  -- Discarded, not carried over. See the header.
  delete from public.job_hunter_job_facets f
   where f.posting_id = v_duplicate_id;

  -- Redirects that already pointed at the duplicate are re-pointed first,
  -- so every redirect names a posting that is still a survivor and a reader
  -- never has to walk a chain.
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
  'once for everyone: the survivor keeps the better description by '
  'job_hunter_preferred_description, every affected user''s job row is '
  're-pointed at it, the merged-away posting''s facets are discarded, and a '
  'redirect records where it went. The survivor is chosen from the postings '
  '(ATS identity, then age, then id), so the two argument orders converge.';

-- Callable by the owner only. Supabase's default privileges grant execute on
-- a new public function to anon and authenticated by name, so revoking from
-- PUBLIC alone leaves both of them holding it.
--
-- The one path in is job_hunter_merge_jobs, which is security definer and
-- therefore executes this as the owner. That is what confines the merge to
-- pairs of postings the caller already holds job rows for, instead of any
-- two postings they can name. See the note on job_hunter_merge_jobs below
-- for what that does and does not close.
revoke all on function public.job_hunter_merge_postings(uuid, uuid)
  from public, anon, authenticated, service_role;

-- job_hunter_upsert_posting ------------------------------------------------------
--
-- Re-created from 20260909100000 with one addition: the row the fingerprint
-- resolves to is put through the redirect before anything is written to it.
--
-- Without that, the merge is undone by the next run. The merged-away
-- posting keeps its fingerprint, so the aggregator listing that produced it
-- keeps resolving to it; folding this run's text into that row would leave
-- the survivor -- the row every job actually points at -- never seeing the
-- improvement, and would leave a better description sitting on a row nobody
-- reads. Everything else below is unchanged.

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
  v_own_id uuid;
  v_target_id uuid;
  v_redirected boolean;
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
    -- A row this statement just created cannot have been merged away.
    return v_id;
  end if;

  -- The redirect, applied here rather than at the end: a merged-away
  -- posting is a record of what one source said, and this run's fetch of
  -- that source belongs to the advertisement it was merged into (#176).
  select r.id, public.job_hunter_resolve_posting(r.id)
    into v_own_id, v_target_id
    from public.job_hunter_postings r
   where r.fingerprint = v_fingerprint;

  v_redirected := v_own_id is distinct from v_target_id;

  select * into v_row from public.job_hunter_postings p
   where p.id = v_target_id;
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
  --
  -- The exception has its own exception, and it is the redirect above that
  -- creates it: "a resolved canonical URL is an improvement" holds within
  -- one source, not across a merge. url and canonical_url are the only two
  -- columns here that overwrite rather than backfill, so a fetch of a
  -- merged-away aggregator listing would otherwise replace the survivor's
  -- employer link with the aggregator's -- and store_mapping reads
  -- canonical_url off the posting, so that reaches the user. When this write
  -- has been redirected onto a survivor, both columns backfill like
  -- everything else and the merge's own choice of link stands.
  update public.job_hunter_postings p set
    source             = coalesce(nullif(p.source, ''), coalesce(p_posting->>'source', '')),
    source_job_id      = coalesce(nullif(p.source_job_id, ''), p_posting->>'source_job_id'),
    url                = case
                           when v_redirected
                             then coalesce(nullif(p.url, ''), nullif(v_raw_canonical, ''),
                                           nullif(coalesce(p_posting->>'url', ''), ''), '')
                           else coalesce(nullif(v_raw_canonical, ''), nullif(p.url, ''),
                                         nullif(coalesce(p_posting->>'url', ''), ''), '')
                         end,
    canonical_url      = case
                           when v_redirected
                             then coalesce(nullif(p.canonical_url, ''), nullif(v_canonical, ''), '')
                           else coalesce(nullif(v_canonical, ''), p.canonical_url)
                         end,
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
  'its fingerprint, and return its id. A fingerprint whose posting has been '
  'merged away resolves to the survivor, so the fetch improves the '
  'advertisement everything reads rather than the row it was merged out of. '
  'The better description wins by content confidence, so a second user '
  'fetching the same advertisement from a weaker source cannot degrade it.';

-- job_hunter_merge_jobs ----------------------------------------------------------
--
-- Re-created from 20260909100000 with the cross-posting decision taken out
-- of it.
--
-- What it used to do was decide, per user and privately, that two
-- advertisements were the same and which of their two descriptions was
-- better. Both halves now belong to the posting: when the two job rows point
-- at different postings this delegates to job_hunter_merge_postings, which
-- records the decision once for everyone and re-points every other user's
-- rows, and the surviving job row then takes the surviving posting's text
-- rather than repeating the comparison against a different pair of inputs.
--
-- Where both rows already point at one posting -- which is what the previous
-- paragraph leaves behind on the next run -- there is nothing to decide and
-- the posting's text is taken directly. Only when neither row has a posting
-- at all (a job row written without going through the upsert) does the
-- ladder still run here, through job_hunter_preferred_description rather
-- than a fourth open-coded copy of it.
--
-- The rest of the function is per-user plumbing and is unchanged: the
-- survivor is still chosen by this user's evidence, the attached records are
-- still reassigned, and the job-level redirect is still written. #178
-- removes all of it along with the duplicated job-row machinery; what
-- matters for #176 is that it no longer decides anything a second time.
--
-- SECURITY DEFINER, changed from invoker here, and this is a narrowing
-- rather than a widening. It is what lets job_hunter_merge_postings be
-- revoked from `authenticated` entirely: the posting merge is then reachable
-- only through this function, which acts on two job rows the caller already
-- owns, instead of on any two posting ids an authenticated user can name.
--
-- Removing RLS as a backstop is only safe because this function never
-- depended on it. Every statement in the body carries its own
-- `user_id = v_uid` predicate, with `v_uid := (select auth.uid())` -- which
-- reads the request's JWT claim and is therefore still the *caller's* id
-- under a definer function, not the owner's. The single statement that does
-- not is the read of job_hunter_postings for the surviving description, and
-- that table is shared by design: every authenticated user may already read
-- every row of it. RLS on all eight per-user tables this touches
-- (job_hunter_jobs, job_sources, evaluations, materials, deliveries,
-- application_events, company_watch, job_merges) is exactly
-- `(select auth.uid()) = user_id` and nothing more, so the explicit
-- predicates and the policies express the same restriction and the change
-- is behaviour-preserving. A caller naming another user's job row gets the
-- same `survivor and duplicate jobs must both exist` as before -- from the
-- predicate now rather than from the policy. Any statement added to this
-- body later must carry that predicate or be provably safe without it.
--
-- What this does NOT close, stated plainly because the revoke above could
-- be read as closing it: a user who wants two arbitrary postings collapsed
-- can still get there by upserting two job rows of their own that resolve to
-- those postings -- fingerprints are readable, so both are nameable -- and
-- merging those. The direct primitive is gone and every posting merge is now
-- a consequence of a per-user merge, which is the intended semantic, but
-- shared-table writes remain reachable from `authenticated`. Closing that
-- means taking the merge path off PostgREST and onto the privileged
-- connection, which is #179's shape and needs #178 to move the trigger off
-- the per-user path first.

create or replace function public.job_hunter_merge_jobs(p_survivor uuid, p_duplicate uuid)
returns uuid
language plpgsql
security definer
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
  v_preferred text[];
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

  -- url and the identity columns stay on the job row and are still merged
  -- here. #177 recorded why: they answer whether this user already holds a
  -- row covering the advertisement, and a job row that has absorbed several
  -- postings is the only row that has seen all of them. Moving them is
  -- #178's question, and #176 is what makes it askable.
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

  -- The decision, and where it now lives. Merging the postings re-points
  -- every user's rows -- including the two being merged here -- and returns
  -- the posting that survived; its text is then what the surviving job row
  -- carries.
  if v_survivor.posting_id is not null
     and v_duplicate.posting_id is not null
     and v_survivor.posting_id <> v_duplicate.posting_id then
    v_posting_id := public.job_hunter_merge_postings(v_survivor.posting_id, v_duplicate.posting_id);
  else
    v_posting_id := coalesce(v_survivor.posting_id, v_duplicate.posting_id);
  end if;

  if v_posting_id is not null then
    select p.description, p.content_confidence
      into v_description, v_confidence
      from public.job_hunter_postings p
     where p.id = v_posting_id;
  end if;

  if coalesce(v_description, '') = '' then
    -- No shared answer to take, so fall back to the same ladder, stated once
    -- in job_hunter_preferred_description (20260909130000).
    --
    -- Two cases reach this, and the emptiness test rather than a null test
    -- is what catches the second. Neither row has a posting at all -- a job
    -- row written without going through the upsert. Or the posting exists
    -- and its description is '': job_hunter_postings.description is `not
    -- null default ''`, so a posting first written from a payload that
    -- carried no text says nothing, while the job rows pointing at it can
    -- since have acquired text from a later fetch that left the pointer
    -- where it was. Taking the posting's answer there would blank a
    -- description the user's rows actually hold, which is precisely what the
    -- ladder's first rung -- an empty side never wins -- exists to prevent.
    v_preferred := public.job_hunter_preferred_description(
      v_survivor.description, v_survivor.content_confidence,
      v_duplicate.description, v_duplicate.content_confidence);
    v_description := v_preferred[1];
    v_confidence := v_preferred[2];
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
    -- The survivor points at the posting its description came from, which
    -- is now the posting the merge above produced.
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

comment on function public.job_hunter_merge_jobs(uuid, uuid) is
  'Collapse two of one user''s job rows into a survivor and reassign every '
  'record attached to the duplicate. The decision that the two are the same '
  'advertisement is no longer made here: when the rows point at different '
  'postings this delegates to job_hunter_merge_postings, which records it '
  'once for everyone, and the survivor takes that posting''s description. '
  '#178 removes this along with the rest of the duplicated job-row '
  'machinery.';

-- The same revoke discipline as the two functions above, which a definer
-- function needs more than an invoker one: Supabase's default privileges
-- grant execute on a new public function to anon by name, and a definer
-- function reachable unauthenticated is worth closing even when -- as here
-- -- auth.uid() is null for such a caller and every statement in the body
-- then matches nothing. authenticated keeps it: PostgREST is how a run
-- reaches job_hunter_upsert_job, which reaches this.
revoke all on function public.job_hunter_merge_jobs(uuid, uuid)
  from public, anon, service_role;
grant execute on function public.job_hunter_merge_jobs(uuid, uuid) to authenticated;

-- job_hunter_merge_posting_batch --------------------------------------------------
--
-- Re-created from 20260909130000 so that a merged-away fingerprint's
-- listings fold onto the survivor rather than onto the row they were merged
-- out of.
--
-- Resolving only the mapping returned to the caller is not enough, and this
-- is worth spelling out because it looks like it would be. A crawl reaches
-- this function, not job_hunter_upsert_posting: discovery always stages a
-- batch when it holds the direct connection, and job_hunter_upsert_job skips
-- the single-listing path entirely for a payload that already names its
-- posting. So if the fold stayed keyed on the staged fingerprint, every
-- later crawl of the merged-away source would write its text and its
-- last_seen_at to a row nobody reads: the survivor's description could never
-- improve from that source again, and its last_seen_at would stop being
-- refreshed by it while the advertisement was still being listed. An
-- advertisement that is still up would look progressively staler the more
-- sources carried it.
--
-- The resolution happens in `normalized` instead, one column wide: each
-- staged row also carries the fingerprint of the posting it resolves to, and
-- the fold groups on that. Two staged fingerprints then collapse into one
-- another exactly as two listings of a single fingerprint always have, which
-- also disposes of the reason not to do this -- there is no `UPDATE ... FROM`
-- with two source rows for one target, because the grouping merged them
-- before the update ran. The returned mapping is rebuilt per staged
-- fingerprint, since that is the key the caller maps its jobs back by.
--
-- Before any merge exists, target_fingerprint equals fingerprint for every
-- row and every statement below behaves exactly as 20260909130000's did.

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
      -- The fingerprint whose posting this listing is actually about (#176).
      -- For everything that has never been merged -- which is every listing
      -- until a merge happens -- this is s.fingerprint and the statement
      -- below is byte-for-byte the one from 20260909130000. For a listing
      -- whose posting has been merged away it is the survivor's fingerprint,
      -- which is what makes the fold land on the row everybody reads.
      coalesce(
        (select p2.fingerprint
           from public.job_hunter_postings p2
          where p2.id = public.job_hunter_resolve_posting(
                          (select p1.id from public.job_hunter_postings p1
                            where p1.fingerprint = s.fingerprint))),
        s.fingerprint)                                     as target_fingerprint,
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
    select distinct on (n.target_fingerprint)
           n.target_fingerprint as fingerprint, n.description, n.content_confidence
      from normalized n
     where n.description <> ''
     order by n.target_fingerprint,
              public.job_hunter_confidence_rank(n.content_confidence) asc,
              length(regexp_replace(n.description, '^\s+|\s+$', '', 'g')) desc,
              n.ordinal asc
  ),
  -- Grouped by the target rather than the staged fingerprint, so the two
  -- renderings of a merged advertisement -- the aggregator's and the
  -- employer's -- fold into one another exactly as two listings of one
  -- fingerprint always have. Before any merge exists the two are the same
  -- column and this groups identically.
  collapsed as (
    select
      n.target_fingerprint as fingerprint,
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
    left join best_description bd on bd.fingerprint = n.target_fingerprint
    group by n.target_fingerprint
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
  ),
  -- One row per fingerprint the caller staged, which is what the caller maps
  -- its jobs back by. `collapsed` is keyed by target, and two staged
  -- fingerprints can share one after a merge, so the mapping is rebuilt from
  -- the staged side rather than read off the fold.
  staged as (
    select distinct n.fingerprint, n.target_fingerprint
      from normalized n
  )
  select
    s.fingerprint,
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
    --
    -- Resolved through the posting redirect (#176), so a fingerprint whose
    -- posting has been merged away hands the caller the survivor and the job
    -- rows it stamps point at the advertisement everything else reads.
    public.job_hunter_resolve_posting(
      coalesce(i.id, u.id,
               (select p.id from public.job_hunter_postings p
                 where p.fingerprint = s.target_fingerprint)))  as posting_id,
    (i.id is not null)                                          as is_new
  from staged s
  left join inserted i on i.fingerprint = s.target_fingerprint
  left join updated u on u.fingerprint = s.target_fingerprint
$$;

comment on function public.job_hunter_merge_posting_batch(uuid) is
  'Resolve, deduplicate, insert and update a whole staged crawl batch of '
  'postings in one statement, then clear the batch from staging. Returns one '
  'row per distinct fingerprint in the batch with the posting it resolved to '
  '-- the survivor, where that posting has since been merged away (#176) -- '
  'and whether that posting was genuinely new. Merging a batch that is '
  'already cleared returns no rows and writes nothing.';

-- Supabase's default privileges grant execute on a new public function to
-- anon and authenticated by name, so revoking from PUBLIC alone leaves both
-- of them holding it. The merge is ingestion's, and ingestion connects as
-- the owner.
revoke all on function public.job_hunter_merge_posting_batch(uuid)
  from public, anon, authenticated, service_role;
