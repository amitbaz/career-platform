-- Job Hunter store operations that PostgREST cannot express in one HTTP
-- call, translated from apps/job-hunter/src/job_hunter/store.py.
--
-- SECURITY MODEL. Every function here is `security invoker` with
-- `set search_path = ''` and fully qualified `public.` table names. Row
-- level security therefore still applies inside the function body: the
-- caller's own JWT decides which rows it can see, exactly as if it had run
-- the statements itself. A `security definer` function would run as its
-- owner, see every user's rows, and undo the isolation #86/#87 built.
-- Nothing below references service_role or disables RLS. On top of RLS,
-- every statement also filters on `user_id = (select auth.uid())` so a
-- later loosening of a policy still cannot make one user's call read
-- another user's rows.
--
-- SQLITE -> POSTGRES TRANSLATION NOTES.
--   * "Latest evaluation" used to mean MAX(id) over an INTEGER
--     AUTOINCREMENT column. Ids are random uuids now, so latest means
--     newest evaluated_at with a deterministic tie-break on created_at
--     then id. Never an arbitrary row.
--   * SQLite's two-argument MIN/MAX are scalar; in Postgres those names
--     are aggregates. They become LEAST / GREATEST.
--   * Timestamps were ISO text in SQLite and are timestamptz here, so
--     they are compared as timestamps, never as strings.
--   * SQLite LIKE is case-insensitive. Nothing translated below actually
--     used LIKE -- the case-insensitivity the Python relied on lived in
--     its own normalizers, which are reproduced as the helpers below.
--
-- HELPERS. job_hunter_normalize_text / _normalize_tokens /
-- _normalize_company / _locations_compatible / _canonicalize_url /
-- _confidence_rank are direct ports of the pure Python functions in
-- normalize.py, job_identity.py and content_confidence.py. They are
-- immutable and touch no tables, so they carry no isolation risk, but they
-- are still `security invoker` with a pinned search_path for consistency.

-- Helpers ---------------------------------------------------------------------

-- normalize.py:normalize_text -- re.sub(r"\s+", " ", text.lower().strip())
create or replace function public.job_hunter_normalize_text(p_value text)
returns text
language sql
immutable
security invoker
set search_path = ''
as $$
  select regexp_replace(
           regexp_replace(lower(coalesce(p_value, '')), '^\s+|\s+$', '', 'g'),
           '\s+', ' ', 'g');
$$;

-- job_identity.py:_tokens joined by spaces -- also normalize_job_title and
-- normalize_location, which are that exact expression.
create or replace function public.job_hunter_normalize_tokens(p_value text)
returns text
language sql
immutable
security invoker
set search_path = ''
as $$
  select coalesce(
    (select string_agg(t.tok[1], ' ' order by t.ord)
       from regexp_matches(lower(coalesce(p_value, '')), '[a-z0-9]+', 'g')
            with ordinality as t(tok, ord)),
    '');
$$;

-- job_identity.py:normalize_company_name -- tokens with trailing safe legal
-- suffixes removed, repeatedly ("Acme Inc Ltd" -> "acme").
create or replace function public.job_hunter_normalize_company(p_value text)
returns text
language plpgsql
immutable
security invoker
set search_path = ''
as $$
declare
  v_tokens text[];
  v_safe constant text[] := array[
    'gmbh', 'ag', 'ltd', 'limited', 'inc', 'incorporated',
    'llc', 'corp', 'corporation'];
begin
  select coalesce(array_agg(t.tok[1] order by t.ord), '{}'::text[])
    into v_tokens
    from regexp_matches(lower(coalesce(p_value, '')), '[a-z0-9]+', 'g')
         with ordinality as t(tok, ord);

  while array_length(v_tokens, 1) is not null
    and v_tokens[array_length(v_tokens, 1)] = any(v_safe)
  loop
    v_tokens := v_tokens[1:array_length(v_tokens, 1) - 1];
  end loop;

  return coalesce(array_to_string(v_tokens, ' '), '');
end $$;

-- job_identity.py:locations_compatible -- equal, either side empty, or one
-- contained in the other as a run of whole words. Tokens are alphanumeric
-- and space-joined, so wrapping both in spaces makes substring containment
-- exactly the contiguous-whole-word containment _contains_phrase computes.
create or replace function public.job_hunter_locations_compatible(p_left text, p_right text)
returns boolean
language sql
immutable
security invoker
set search_path = ''
as $$
  with n as (
    select public.job_hunter_normalize_tokens(p_left) as l,
           public.job_hunter_normalize_tokens(p_right) as r
  )
  select case
           when n.l = '' or n.r = '' then true
           when n.l = n.r then true
           else position(' ' || n.r || ' ' in ' ' || n.l || ' ') > 0
             or position(' ' || n.l || ' ' in ' ' || n.r || ' ') > 0
         end
    from n;
$$;

-- normalize.py:canonicalize_url -- drop the fragment, drop tracking params,
-- drop blank-valued params (parse_qsl's keep_blank_values=False default),
-- and sort what is left.
--
-- Fidelity note: Python's parse_qsl/urlencode round trip also percent-
-- decodes and re-encodes each pair; this does not. Both sides of every
-- comparison in this file run through THIS function, so the two stay
-- self-consistent -- a divergence would only show for two raw URLs that
-- differ solely in percent-encoding. Sorting uses the C collation so it
-- matches Python's bytewise tuple sort rather than the database locale.
create or replace function public.job_hunter_canonicalize_url(p_url text)
returns text
language plpgsql
immutable
security invoker
set search_path = ''
as $$
declare
  v_url text := coalesce(p_url, '');
  v_base text;
  v_query text := '';
  v_pos int;
  v_pair text;
  v_key text;
  v_val text;
  v_kept text[] := '{}'::text[];
  v_tracking constant text[] := array[
    'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content',
    'gh_src', 'lever-source', 'source', 'ref', 'fbclid', 'gclid'];
begin
  v_pos := position('#' in v_url);
  if v_pos > 0 then
    v_url := left(v_url, v_pos - 1);
  end if;

  v_pos := position('?' in v_url);
  if v_pos > 0 then
    v_base := left(v_url, v_pos - 1);
    v_query := substr(v_url, v_pos + 1);
  else
    v_base := v_url;
  end if;

  if v_query <> '' then
    foreach v_pair in array string_to_array(v_query, '&') loop
      if v_pair = '' then
        continue;
      end if;
      v_pos := position('=' in v_pair);
      if v_pos > 0 then
        v_key := left(v_pair, v_pos - 1);
        v_val := substr(v_pair, v_pos + 1);
      else
        v_key := v_pair;
        v_val := '';
      end if;
      if v_val <> '' and not (v_key = any(v_tracking)) then
        v_kept := v_kept || (v_key || '=' || v_val);
      end if;
    end loop;

    select coalesce(string_agg(p.pair, '&' order by p.pair collate "C"), '')
      into v_query
      from unnest(v_kept) as p(pair);
  end if;

  if v_query = '' then
    return v_base;
  end if;
  return v_base || '?' || v_query;
end $$;

-- content_confidence.py:tier_rank -- lower is more trustworthy, an
-- unrecognized or empty tier ranks worst of all.
create or replace function public.job_hunter_confidence_rank(p_tier text)
returns int
language sql
immutable
security invoker
set search_path = ''
as $$
  select coalesce(
    array_position(
      array['official_ats', 'canonical_employer_page', 'source_detail_page',
            'aggregator_text', 'partial_unknown'],
      p_tier),
    6);
$$;

-- 1. find_job_by_identity ---------------------------------------------------------
-- store.py:1180-1222 (find_job_by_identity + _find_job_ids_by_identity).
--
-- Returns EVERY normalized identity match, which is _find_job_ids_by_identity.
-- The narrower public find_job_by_identity ("a job id only when exactly one
-- row matches") is `select ... limit 2` at the call site and taking the row
-- only when the count is 1 -- derivable from this, whereas the plural set
-- is not derivable from the singular.
--
-- The Python scans every job and filters in memory; this pushes the same
-- predicates into SQL. The pairwise guard is preserved: if any two matches
-- disagree on location, the whole match set is discarded rather than
-- guessing which one is right.
create or replace function public.job_hunter_find_job_by_identity(
  p_company text, p_title text, p_location text)
returns setof uuid
language plpgsql
security invoker
set search_path = ''
as $$
declare
  v_uid uuid := (select auth.uid());
  v_company text := public.job_hunter_normalize_company(p_company);
  v_title text := public.job_hunter_normalize_tokens(p_title);
begin
  if v_company = '' or v_title = '' then
    return;
  end if;

  return query
  with matches as (
    select j.id as job_id, j.location as job_location, j.created_at as job_created_at
      from public.job_hunter_jobs j
     where j.user_id = v_uid
       and public.job_hunter_normalize_company(j.company) = v_company
       and public.job_hunter_normalize_tokens(j.title) = v_title
       and public.job_hunter_locations_compatible(p_location, j.location)
  )
  select m.job_id
    from matches m
   where not exists (
           select 1
             from matches l
             join matches r on l.job_id < r.job_id
            where not public.job_hunter_locations_compatible(l.job_location, r.job_location)
         )
   order by m.job_created_at, m.job_id;
end $$;

-- 2. pending_delivery_jobs ---------------------------------------------------------
-- store.py:2126-2141.
--
-- The Python joined evaluations on `e.id = (SELECT MAX(id) ...)`, an inner
-- join, so a job with no evaluation was never a candidate; the lateral join
-- below keeps that. Its post-filter reduces to: latest total_score strictly
-- above the floor, latest decision in the three deliverable values, and no
-- telegram_message delivery yet. _DELIVERABLE_SCORE_FLOOR becomes the
-- parameter so the threshold stays in application code.
create or replace function public.job_hunter_pending_delivery_jobs(p_score_floor int)
returns table (job_id uuid)
language sql
security invoker
set search_path = ''
as $$
  select j.id
    from public.job_hunter_jobs j
    join lateral (
      select e.decision, e.total_score
        from public.job_hunter_evaluations e
       where e.job_id = j.id and e.user_id = j.user_id
       order by e.evaluated_at desc, e.created_at desc, e.id desc
       limit 1
    ) e on true
   where j.user_id = (select auth.uid())
     and e.total_score > p_score_floor
     and e.decision in ('possible_match', 'high_priority', 'package_match')
     and not exists (
           select 1
             from public.job_hunter_deliveries d
            where d.job_id = j.id
              and d.user_id = j.user_id
              and d.delivery_type = 'telegram_message'
         );
$$;

-- 3. pending_review_events -----------------------------------------------------------
-- store.py:1907-1932.
--
-- `SELECT e.*, m.subject` becomes the event row as jsonb with `subject`
-- merged in. AUTO_CONFIDENCE_THRESHOLD becomes the parameter. The join and
-- anti-join are both re-scoped on user_id so a future policy change cannot
-- let an event join another user's gmail message. The `ORDER BY e.occurred_at,
-- e.id` tie-break was insertion order under AUTOINCREMENT, so it becomes
-- created_at then id.
create or replace function public.job_hunter_pending_review_events(
  p_confidence_threshold double precision)
returns setof jsonb
language sql
security invoker
set search_path = ''
as $$
  select to_jsonb(e) || jsonb_build_object('subject', m.subject)
    from public.job_hunter_application_events e
    join public.job_hunter_gmail_messages m
      on m.message_id = e.source_message_id
     and m.user_id = e.user_id
    left join public.job_hunter_review_deliveries d
      on d.event_id = e.id
     and d.user_id = e.user_id
   where e.user_id = (select auth.uid())
     and d.event_id is null
     and (
           e.event_type = 'REVIEW_NEEDED'
           or (
             e.event_type in ('RECRUITER_CONTACT', 'APPLIED', 'INTERVIEW',
                              'TECHNICAL', 'OFFER', 'REJECTED')
             and (e.job_id is null or e.confidence < p_confidence_threshold)
           )
         )
   order by e.occurred_at, e.created_at, e.id;
$$;

-- 4. unmaterialized_inbound_jobs ------------------------------------------------------
-- store.py:1805-1822 (list_unmaterialized_inbound_jobs +
-- _matches_materialized_job).
--
-- The Python loaded every candidate and every job and did an O(n*m) match in
-- memory. The three match rules are unchanged: the gmail source tuple, equal
-- canonical URLs when both sides have one, or an identical normalized
-- company|title|location triple that is not entirely empty.
create or replace function public.job_hunter_unmaterialized_inbound_jobs()
returns setof jsonb
language sql
security invoker
set search_path = ''
as $$
  select to_jsonb(c)
    from public.job_hunter_inbound_job_candidates c
   where c.user_id = (select auth.uid())
     and not exists (
           select 1
             from public.job_hunter_jobs j
            where j.user_id = c.user_id
              and (
                    (j.source = 'gmail:' || c.source_platform
                     and j.source_job_id = c.source_candidate_key)
                 or (c.url <> '' and j.url <> ''
                     and public.job_hunter_canonicalize_url(c.url)
                       = public.job_hunter_canonicalize_url(j.url))
                 or (
                      public.job_hunter_normalize_text(c.company) || '|' ||
                      public.job_hunter_normalize_text(c.title) || '|' ||
                      public.job_hunter_normalize_text(c.location) <> '||'
                      and public.job_hunter_normalize_text(c.company) || '|' ||
                          public.job_hunter_normalize_text(c.title) || '|' ||
                          public.job_hunter_normalize_text(c.location)
                        = public.job_hunter_normalize_text(j.company) || '|' ||
                          public.job_hunter_normalize_text(j.title) || '|' ||
                          public.job_hunter_normalize_text(j.location)
                    )
                  )
         )
   order by c.created_at, c.id;
$$;

-- 5. merge_jobs -----------------------------------------------------------------------
-- store.py:906-1090 (merge_jobs, _merge_jobs, _job_survivor_sort_key,
-- _has_complete_ats_identity, _better_description).
--
-- The survivor is chosen, not assumed: the job with application-event
-- history wins, then the one with any other history, then the earlier
-- first_seen_at, then the lower id. Under SQLite that last tie-break meant
-- "the older row"; with uuids it is arbitrary but still deterministic, and
-- it can only fire when the three real signals are identical.
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
    description_hash   = encode(extensions.digest(v_description, 'sha256'), 'hex'),
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

  -- job_sources and pending_ai_work cascade from the job row, which matches
  -- the SQLite behaviour: the Python never reassigned pending AI work
  -- either, it let the foreign key drop it.
  delete from public.job_hunter_jobs j
   where j.id = v_duplicate_id and j.user_id = v_uid;

  return v_survivor_id;
end $$;

-- 6. upsert_job -------------------------------------------------------------------------
-- store.py:649-743 (upsert_job) and 744-905 (upsert_logical_job,
-- _insert_logical_job, _update_logical_job, _record_job_source).
--
-- upsert_logical_job is the path the app actually uses (discovery.py:328,
-- 345, 491); upsert_job is its single-fingerprint special case, and its
-- column list is the one written here. So this function implements the
-- logical upsert and subsumes both.
--
-- p_job is the Job record as jsonb. Recognized keys, all optional except
-- `fingerprint`:
--   fingerprint      required. Computed by Python (job_fingerprint), NOT
--                    recomputed here -- reproducing that sha256 over a
--                    Python-canonicalized URL in SQL would risk two
--                    engines disagreeing about identity.
--   source, source_job_id, url, canonical_url, company, title, location,
--   remote (boolean), description, content_confidence,
--   ats_provider, ats_board, ats_job_id,
--   original_url     preferred over `url` when recording the discovery source.
-- description_hash IS computed here, via extensions.digest, which produces
-- exactly hashlib.sha256(text.encode("utf-8")).hexdigest().
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
  v_previous_hash text;
  v_current_hash text;
  v_is_new boolean;
  v_changed boolean;
  v_now timestamptz := now();
  v_row public.job_hunter_jobs%rowtype;
  v_description text;
  v_confidence text;
  v_persisted_url text;
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

  v_lookup_canonical := case when v_raw_canonical <> ''
                             then public.job_hunter_canonicalize_url(v_raw_canonical)
                             else '' end;

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
      description_hash   = encode(extensions.digest(v_description, 'sha256'), 'hex'),
      canonical_url      = coalesce(nullif(v_lookup_canonical, ''), v_row.canonical_url),
      ats_provider       = coalesce(nullif(coalesce(p_job->>'ats_provider', ''), ''), v_row.ats_provider),
      ats_board          = coalesce(nullif(coalesce(p_job->>'ats_board', ''), ''), v_row.ats_board),
      ats_job_id         = coalesce(nullif(coalesce(p_job->>'ats_job_id', ''), ''), v_row.ats_job_id),
      content_confidence = v_confidence,
      last_seen_at       = v_now
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
       first_seen_at, last_seen_at, status)
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
      encode(extensions.digest(coalesce(p_job->>'description', ''), 'sha256'), 'hex'),
      public.job_hunter_canonicalize_url(
        coalesce(nullif(v_raw_canonical, ''), coalesce(p_job->>'url', ''), '')),
      p_job->>'ats_provider',
      p_job->>'ats_board',
      p_job->>'ats_job_id',
      coalesce(p_job->>'content_confidence', ''),
      v_now,
      v_now,
      'new')
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
end $$;
