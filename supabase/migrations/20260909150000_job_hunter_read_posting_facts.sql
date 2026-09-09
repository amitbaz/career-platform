-- Read posting-level facts from the posting (issue #177, epic #118).
--
-- 20260909100000 created job_hunter_postings and pointed every job row at
-- one, but moved no reader onto it. This moves the readers. Two functions
-- consult facts about the advertisement rather than about the user's
-- relationship to it, and both now take them from the posting:
--
--   * job_hunter_needs_evaluation, which decides re-evaluation from the
--     description hash and the content confidence;
--   * job_hunter_eligible_inbound_jobs, which decides whether a Gmail
--     candidate still needs materializing -- specifically the part of that
--     decision that asks whether the work already done on the matched job
--     is still current.
--
-- The ticket asked for the inbound query's identity and URL matching to move
-- onto the posting too, and it deliberately does not. Those predicates ask
-- "does this user already hold a row that covers this advertisement", which
-- is a question about coverage, not about what the advertisement says. A job
-- row accumulates evidence from every posting merged into it -- its `url` is
-- the canonical one resolved across all of them and its identity columns are
-- backfilled from each -- while posting_id names one of those postings.
-- Matching on the posting therefore loses matches the job row still makes: a
-- candidate carrying the employer's link would stop matching a merged row
-- whose posting_id happens to name the aggregator's posting, and the
-- candidate would be re-materialized on every run for fourteen days. This is
-- the same line PostgresJobStore.job_from_row draws when it keeps `url` on
-- the membership row.
--
-- The reason this holds today is that merging across fingerprints is still
-- a per-user operation. job_hunter_merge_jobs collapses two of one user's
-- job rows for the same advertisement discovered under two different
-- fingerprints, and nothing does the equivalent for the postings behind
-- them -- 20260909130000 dedups records that *share* a fingerprint, which
-- is a different operation despite the name. So a job row is the only row
-- that has seen every posting for its advertisement. #176 is where identity
-- resolution and merging move to the posting level; once a posting is one
-- advertisement across sources, this split is worth revisiting, and these
-- predicates can move with it.
--
-- The duplicated columns stay on job_hunter_jobs and still carry the same
-- values, so this migration is reversible on its own and CI is green
-- whichever order the batches of #118 land in.
--
-- job_hunter_pending_delivery_jobs is deliberately untouched: it reads ids,
-- evaluations and deliveries and no posting-level fact at all. The
-- pending-delivery path's read of the advertisement is PostgresJobStore.get_job
-- (pipeline.py:_requeue_pending_delivery), which now embeds the posting.

-- Re-evaluation --------------------------------------------------------------
--
-- The body is 202609070003's with one change: the description hash and the
-- content confidence come from the posting the job row points at. That is
-- the text the pipeline is handed -- PostgresJobStore.get_job composes its
-- `Job` from the posting -- so "the evaluation is still current" now means
-- the same thing here as it does to the code that acts on the answer.
--
-- The join is LEFT and the posting side is coalesced onto the job row's
-- own copy: posting_id is nullable while direct inserts (pgTAP fixtures,
-- scripts/migrate_sqlite_to_postgres.py) still exist, and a row without a
-- posting must keep answering exactly as it did. The join is on the
-- primary key, so it costs an index lookup per job and the round trips per
-- run do not change.

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
      -- Reproduced, not corrected: correcting it here would quietly change
      -- which jobs get re-evaluated.
      when e.description_hash_at_eval
           is distinct from coalesce(p.description_hash, j.description_hash, '') then true
      when e.content_confidence_at_eval
           is distinct from coalesce(p.content_confidence, j.content_confidence, '') then true
      else false
    end
  from public.job_hunter_jobs j
  left join public.job_hunter_postings p on p.id = j.posting_id
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
  'Bulk form of PostgresJobStore.needs_evaluation: two requests per job '
  'become one request per id array. The description hash and content '
  'confidence are the posting''s, which is the text the pipeline evaluates '
  '(#177). An id the caller cannot read returns no row, and the caller '
  'treats a missing id as needing evaluation -- which is what the per-job '
  'method does with a job whose evaluations it cannot see.';

-- Inbound candidates ---------------------------------------------------------
--
-- The body is 20260907104935's with one change: every completeness check
-- is made against the posting's description hash and content confidence
-- rather than the job row's copies, so "this candidate's job has a current
-- evaluation" means current against the text the pipeline evaluated.
--
-- The three match predicates are untouched, for the reason in the header:
-- they ask which of the user's rows covers the advertisement, and the job
-- row is the better answer to that than any one of the postings it stands
-- for.
--
-- Same LEFT join and coalesce as job_hunter_needs_evaluation, and for the
-- same reason: a job row without a posting must keep answering exactly as
-- it did. It is a primary-key join per matched row, so no index on
-- job_hunter_jobs is given up and the plan 202609070002 measured is
-- unchanged.

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
            left join public.job_hunter_postings p on p.id = j.posting_id
            where j.user_id = (select auth.uid())
              and j.source = cand.match_source
              and j.source_job_id = cand.source_candidate_key
              and public.job_hunter_gmail_candidate_complete(
                    j.id, j.status,
                    coalesce(p.description_hash, j.description_hash),
                    coalesce(p.content_confidence, j.content_confidence))
         )
     and not exists (
           select 1 from public.job_hunter_jobs j
            left join public.job_hunter_postings p on p.id = j.posting_id
            where j.user_id = (select auth.uid())
              and cand.url <> '' and j.url <> ''
              and j.canonical_url_of_url = cand.match_canonical_url
              and public.job_hunter_gmail_candidate_complete(
                    j.id, j.status,
                    coalesce(p.description_hash, j.description_hash),
                    coalesce(p.content_confidence, j.content_confidence))
         )
     and not exists (
           select 1 from public.job_hunter_jobs j
            left join public.job_hunter_postings p on p.id = j.posting_id
            where j.user_id = (select auth.uid())
              and cand.match_identity <> '||'
              and j.normalized_identity = cand.match_identity
              and public.job_hunter_gmail_candidate_complete(
                    j.id, j.status,
                    coalesce(p.description_hash, j.description_hash),
                    coalesce(p.content_confidence, j.content_confidence))
         )
   order by cand.created_at, cand.id;
$$;

comment on function public.job_hunter_eligible_inbound_jobs() is
  'Gmail candidates whose logical job has not reached a terminal state or a '
  'current successful evaluation. Whether that evaluation is current is '
  'decided from the posting''s description hash and content confidence '
  '(#177); which of the user''s rows the candidate matches is decided from '
  'the job row, which covers every posting merged into it.';

-- The pointer has to name the posting the row's description came from -------
--
-- Reading the description from the posting is only the same description the
-- store returns today if the job row and the posting it points at agree.
-- They can disagree, and not rarely: the fingerprint is source-scoped, so
-- the same advertisement on an aggregator and on the employer's ATS is two
-- postings. When the second payload resolves onto the first payload's job
-- row -- by canonical URL, by ATS triple or by normalized identity -- the
-- logical update keeps whichever description is better, but 20260909100000
-- left `posting_id = coalesce(v_row.posting_id, v_posting_id)`: the row
-- took the ATS text and went on pointing at the aggregator's posting.
-- get_job would then hand the pipeline the weaker description, and the
-- facets extracted per posting would describe text nobody evaluated.
--
-- job_hunter_merge_jobs already avoids this when it merges two rows: the
-- survivor keeps the posting whose description it kept. The body below is
-- 20260909130000's -- the newest one, which also accepts a posting_id the
-- caller already resolved (#182) -- with that same rule applied to the
-- update that merges nothing. Nothing else in it changes.

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
      -- The pointer follows the description (#177). A job row can stand for
      -- several postings -- the fingerprint is source-scoped, so the same
      -- advertisement on an aggregator and on the employer's ATS hashes
      -- twice -- and the update above keeps whichever description is
      -- better, which may be the incoming payload's. Leaving the pointer
      -- where it was would leave the row saying one thing and the posting
      -- everything now reads it from saying another. This is the rule
      -- job_hunter_merge_jobs already applies when it merges two rows;
      -- applied here it holds for an update that merges nothing. It cannot
      -- flap between runs: it moves only when the description moves.
      posting_id         = case
                             when v_description is distinct from v_row.description
                               then v_posting_id
                             else coalesce(v_row.posting_id, v_posting_id)
                           end
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
  'one call, leaving the row pointing at the posting its description came '
  'from (#177). A payload carrying a posting_id keeps it rather than resolving '
  'the posting again, which is how a staged batch merge (#182) pays for the '
  'whole batch once instead of once per listing. Timestamps come from '
  'clock_timestamp(), not now(), so multiple calls inside one transaction '
  '(job_hunter_upsert_jobs, 202609070003) still get strictly-ordered '
  'first_seen_at/created_at values matching input order -- now() is constant '
  'for the whole transaction and would tie every row in a batch, letting the '
  'merge-survivor tiebreak fall through to id order instead of preserving '
  'discovery order.';
