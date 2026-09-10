-- Posting freshness: re-check on a decaying schedule, close what is gone
-- (issue #186).
--
-- Before this, nothing learned whether a posting still existed. The corpus
-- only grew, and an advertisement its employer had taken down was scored and
-- delivered as though it were live. This migration gives a posting the state
-- the `recheck_freshness` stage needs -- when it was last checked, when it is
-- next due, the validators that make that check conditional, and whether it
-- has been found gone -- plus the one function the cron tick calls to put due
-- postings on the stage's queue.
--
-- Freshness is a property of the advertisement, identical for every user, so
-- it lives on job_hunter_postings rather than in a table of its own. Written
-- only by ingestion's privileged connection, like every other posting column
-- since #179.
--
-- Design: docs/superpowers/specs/2026-09-10-job-hunter-posting-freshness-design.md

-- Columns ------------------------------------------------------------------------

alter table public.job_hunter_postings
  add column closed_at timestamptz,
  add column closed_reason text,
  add column freshness_checked_at timestamptz,
  -- Six hours after discovery, not immediately: a posting a crawl has just
  -- listed is as fresh as it will ever be, and checking it at once would
  -- spend the first check on the one moment it tells us nothing. Existing
  -- postings take the same default when this runs, so the backlog becomes
  -- due together and drains at the tick's limit.
  add column freshness_next_check_at timestamptz not null
    default (now() + interval '6 hours'),
  add column freshness_etag text not null default '',
  add column freshness_last_modified text not null default '',
  -- A closed posting says why. An empty digest has to carry its reason
  -- (AGENTS.md rule 5), and "the employer took it down" and "we could not
  -- reach it" are different answers -- which is why the second is not a
  -- reason at all: failing to reach a page never closes a posting.
  add constraint job_hunter_postings_closed_reason_check check (
    (closed_at is null and closed_reason is null)
    or (closed_at is not null and closed_reason in (
          'http_404',           -- the posting's own page is gone
          'http_410',           -- the posting's own page says it is gone for good
          'closure_phrase',     -- the page is up and states the posting is closed
          'absent_from_board',  -- its ATS board no longer lists it
          'board_gone'          -- its ATS board itself is gone
        )));

comment on column public.job_hunter_postings.closed_at is
  'When a freshness re-check established the advertisement is gone (#186). '
  'Null while open. A closed posting is never delivered, but keeps its row, '
  'its text and its facets for anything that already references it. Its '
  'employer''s own ATS board listing it again reopens it; an aggregator does not.';
comment on column public.job_hunter_postings.freshness_next_check_at is
  'When recheck_freshness is next due for this posting. Set from '
  'job_hunter_freshness_interval after every completed check, and pushed a '
  'day out when the posting is enqueued, so a message that dead-letters is '
  'retried the next day rather than on every tick.';

-- What the enqueuer scans: open postings in due order.
create index job_hunter_postings_freshness_due_idx
  on public.job_hunter_postings (freshness_next_check_at)
  where closed_at is null;


-- job_hunter_freshness_interval ------------------------------------------------------
--
-- How long a posting waits before its next check, from its age alone: a
-- quarter of the time since it was first seen, never less than six hours and
-- never more than a week. A posting found this morning is checked this
-- afternoon; one found last month, weekly. New postings are the ones most
-- likely to be edited or pulled, and old ones the most numerous, so the
-- cost of freshness falls as the corpus ages rather than growing with it.
--
-- No operator input: the bounds are part of the definition, not a setting.

create or replace function public.job_hunter_freshness_interval(p_age interval)
returns interval
language sql
immutable
set search_path = ''
as $$
  select greatest(interval '6 hours', least(interval '7 days', p_age / 4));
$$;

comment on function public.job_hunter_freshness_interval(interval) is
  'The re-check interval for a posting of the given age: age / 4, clamped to '
  'between 6 hours and 7 days (#186).';


-- job_hunter_enqueue_due_freshness ------------------------------------------------------
--
-- What the cron tick runs. Sends one {posting_id} message per posting that is
-- due, open, checkable and not merged away, oldest-due first, up to p_limit.
--
-- * Checkable: it has a URL, or an ATS identity the stage can find it by on
--   its board. A posting with neither has nothing to fetch and is never sent.
-- * Not merged away: the survivor is the posting everybody reads, and the one
--   that gets checked.
-- * Not already waiting on the queue: this, not the lease, is what stops
--   duplicates. A consumer that falls behind leaves messages queued past their
--   lease, and without the anti-join each tick would add another.
-- * Leased: the posting's next check moves a day out as it is sent. The stage
--   sets the real next check when it completes; the lease only matters for a
--   message that never completes, which is then retried tomorrow rather than
--   on every tick.
--
-- Enqueues and does nothing else, as #183 requires of anything cron runs.

create or replace function public.job_hunter_enqueue_due_freshness(p_limit integer)
returns integer
language plpgsql
security invoker
set search_path = ''
as $$
declare
  v_messages jsonb[];
begin
  if p_limit is null or p_limit <= 0 then
    raise exception 'p_limit must be positive, got %', p_limit
      using errcode = '22023';
  end if;

  with due as (
    select p.id
      from public.job_hunter_postings p
     where p.closed_at is null
       and p.freshness_next_check_at <= now()
       and (p.url <> ''
            or p.canonical_url <> ''
            or (coalesce(p.ats_provider, '') <> ''
                and coalesce(p.ats_board, '') <> ''
                and coalesce(p.ats_job_id, '') <> ''))
       and not exists (
             select 1 from public.job_hunter_posting_merges m
              where m.duplicate_id = p.id)
       and not exists (
             select 1 from pgmq.q_job_hunter_recheck_freshness q
              where q.message->>'posting_id' = p.id::text)
     order by p.freshness_next_check_at, p.id
     limit p_limit
     for update of p skip locked
  ),
  leased as (
    update public.job_hunter_postings p
       set freshness_next_check_at = now() + interval '1 day'
      from due
     where p.id = due.id
    returning p.id
  )
  select coalesce(
           array_agg(jsonb_build_object('posting_id', l.id) order by p.freshness_next_check_at, l.id),
           '{}'::jsonb[])
    into v_messages
    from leased l
    join public.job_hunter_postings p on p.id = l.id;

  if cardinality(v_messages) > 0 then
    perform pgmq.send_batch('job_hunter_recheck_freshness', v_messages);
  end if;
  return cardinality(v_messages);
end;
$$;

comment on function public.job_hunter_enqueue_due_freshness(integer) is
  'Put up to p_limit due, open, checkable, surviving postings on the '
  'recheck_freshness queue, each at most once, and lease them a day out. Run '
  'by pg_cron; enqueues and performs no stage work (#183, #186).';

-- Ingestion's, like every other stage function: Supabase's default privileges
-- grant execute on a new public function to anon and authenticated by name,
-- so revoking from PUBLIC alone would leave both of them holding it.
revoke all on function public.job_hunter_enqueue_due_freshness(integer)
  from public, anon, authenticated, service_role;


-- job_hunter_pending_delivery_jobs ---------------------------------------------------
--
-- Re-created from 20260908160000 with one change: a job whose posting has
-- been closed is no longer pending delivery. Sending someone an advertisement
-- the employer has taken down is the failure this ticket exists to end, and
-- this is where the delivery retry path picks its jobs.
--
-- Otherwise it is the 20260908160000 definition, parameter name included:
-- PostgREST resolves the call by it.

create or replace function public.job_hunter_pending_delivery_jobs(p_score_floor int)
returns table (job_id uuid)
language sql
security invoker
set search_path = ''
as $$
  select j.id
    from public.job_hunter_jobs j
    join public.job_hunter_postings p on p.id = j.posting_id
    join lateral (
      select e.decision, e.total_score
        from public.job_hunter_evaluations e
       where e.job_id = j.id and e.user_id = j.user_id
       order by e.evaluated_at desc, e.created_at desc, e.id desc
       limit 1
    ) e on true
   where j.user_id = (select auth.uid())
     and p.closed_at is null
     and e.total_score >= p_score_floor
     and e.decision in ('possible_match', 'high_priority', 'package_match')
     and not exists (
           select 1
             from public.job_hunter_deliveries d
            where d.job_id = j.id
              and d.user_id = j.user_id
              and d.delivery_type = 'telegram_message'
         );
$$;


-- job_hunter_merge_posting_batch ----------------------------------------------------
--
-- Re-created from 20260909180000 with one change: the employer's own board
-- listing a closed posting again reopens it. A re-check closes a posting on
-- the evidence it has -- a 404, a missing board entry -- and the board that
-- publishes the advertisement listing it again is newer evidence the other
-- way. Without this, one bad afternoon on an employer's site would close a
-- live advertisement for good.
--
-- Only an `official_ats` listing reopens. An aggregator is routinely slower
-- to drop an advertisement than the board it copied it from; if its listing
-- reopened what the board had dropped, the posting would flip open on every
-- daily crawl, be delivered from that crawl, and be closed again by the next
-- re-check. A job genuinely re-advertised on an aggregator usually comes back
-- under a new id, which is a new posting anyway. job_hunter_upsert_posting,
-- which a Gmail alert also reaches, does not reopen at all.
--
-- The rest of the body is the 20260909180000 definition.

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
      last_seen_at       = greatest(p.last_seen_at, statement_timestamp()),
      -- Only the employer's own board listing it reopens a closed posting
      -- (#186). An aggregator echoing an advert the board already dropped
      -- must not, or the posting flips open every day and is delivered.
      closed_at          = case when c.content_confidence = 'official_ats'
                                then null else p.closed_at end,
      closed_reason      = case when c.content_confidence = 'official_ats'
                                then null else p.closed_reason end
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


-- The tick ----------------------------------------------------------------------------
--
-- Every 30 minutes. The limit bounds one transaction, not throughput: the
-- anti-join above means a limit reached today simply leaves the rest due for
-- the next tick, and nothing is ever sent twice.

select cron.schedule(
  'job-hunter-enqueue-recheck-freshness',
  '*/30 * * * *',
  'select public.job_hunter_enqueue_due_freshness(1000);'
);
