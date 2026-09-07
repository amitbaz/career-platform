-- job_hunter_upsert_jobs (202609070003) wraps job_hunter_upsert_job in a
-- loop inside a single transaction, and promises "the second must merge
-- into the first exactly as two sequential single-job calls would." That
-- promise broke: job_hunter_upsert_job stamped first_seen_at/created_at
-- with `now()`, which is the *transaction* start time in Postgres -- stable
-- across every iteration of one batch call. Two jobs created in the same
-- batch that later canonicalize to the same identity therefore tie on
-- first_seen_at, and the merge-survivor tiebreak (first_seen_at asc, id
-- asc) falls through to `id asc`, which has no relationship to input
-- order. Two real, separate HTTP requests never had this problem: each
-- request is its own transaction, so first_seen_at naturally advanced
-- between them and the earlier job always survived.
--
-- clock_timestamp() advances on every call within a transaction (unlike
-- now()), so ordering jobs within one batch now behaves like ordering jobs
-- created by separate sequential requests: the first-processed job keeps
-- the lower first_seen_at and wins ties. created_at is set explicitly here
-- for the same reason -- its column default was also `now()`.
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
       first_seen_at, last_seen_at, created_at, status)
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
      v_now, v_now, v_now, 'new')
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
      last_seen_at       = v_now
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
       first_seen_at, last_seen_at, created_at, status)
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
end
$$;

comment on function public.job_hunter_upsert_job(jsonb) is
  'Upsert one logical job. Timestamps come from clock_timestamp(), not '
  'now(), so multiple calls inside one transaction (job_hunter_upsert_jobs, '
  '202609070003) still get strictly-ordered first_seen_at/created_at values '
  'matching input order -- now() is constant for the whole transaction and '
  'would tie every row in a batch, letting the merge-survivor tiebreak fall '
  'through to id order instead of preserving discovery order.';
