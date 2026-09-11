-- Variant groups: fold an employer's location fanout of one position into
-- one match, without merging anything (issue #61).
--
-- An employer that advertises one position in many locations publishes one
-- ATS job per location -- `ashby:bjakcareer`'s `iOS Developer - AI Finance
-- Agent` alone is 23 advertisements, one per location. Each is a distinct
-- posting with its own fingerprint, apply URL and membership row, and each
-- competed for the stack and for subjective-scoring quota as an independent
-- job. A variant group is the fact that a set of postings on one ATS board
-- advertise the same position in different places. It is a fact about the
-- world, decided once at ingestion, not about any user.
--
-- This is NOT job_hunter_posting_merges (20260909180000). A merge collapses
-- two postings that are the same advertisement into one survivor and
-- discards the loser's facets -- for two renderings of one page. A variant
-- group leaves every posting exactly as it is: its own row, its own apply
-- URL, its own location, its own facets. Grouping only links postings
-- together so matching can fold them into one result; nothing is merged,
-- redirected or discarded. See CONTEXT.md for the glossary entry.

-- The column ----------------------------------------------------------------
--
-- Nullable and self-referencing by value, not by a foreign key: a group's
-- identity is the id of whichever posting founded it (the first one grouped,
-- deterministically), and every later variant that joins the group copies
-- that same value onto its own row. A posting with no ATS board is left
-- null forever -- #61 groups only ATS-board postings, and null is "not
-- applicable", not "not yet decided".

alter table public.job_hunter_postings
  add column variant_group_id uuid;

comment on column public.job_hunter_postings.variant_group_id is
  'The id of the posting that founded this row''s variant group (#61), or '
  'itself if it founded its own. Null for a posting with no ATS board, which '
  'this ticket does not group. Never a foreign key: the founder is not a '
  'privileged row, just whichever posting the grouping walk reached first.';

create index job_hunter_postings_variant_group_idx
  on public.job_hunter_postings (variant_group_id);

-- What the grouping walk searches: open candidates to compare a new posting
-- against, scoped to postings that already belong to a group.
create index job_hunter_postings_variant_candidates_idx
  on public.job_hunter_postings (ats_provider, ats_board, normalized_title)
  where variant_group_id is not null;

-- job_hunter_word_set_jaccard ------------------------------------------------
--
-- Word-set Jaccard similarity of two texts: |intersection| / |union| of
-- their lowercased word sets, ignoring order and repetition. Measured on
-- ashby:bjakcareer's live corpus (issue #61): location variants of one role
-- score 0.80-1.00 against each other, while the KIRA/BJAK `Lead Software
-- Engineer` pair -- two different positions sharing a title -- tops out at
-- 0.696. This is the whole reason exact-hash grouping under-groups (location
-- text differs) and title-alone grouping over-groups (KIRA and BJAK share
-- a title).

create or replace function public.job_hunter_word_set_jaccard(p_left text, p_right text)
returns numeric
language sql
immutable
security invoker
set search_path = ''
as $$
  with l(word) as (
    select distinct lower(m[1])
      from regexp_matches(coalesce(p_left, ''), '[a-zA-Z0-9]+', 'g') as m
  ),
  r(word) as (
    select distinct lower(m[1])
      from regexp_matches(coalesce(p_right, ''), '[a-zA-Z0-9]+', 'g') as m
  ),
  u as (select word from l union select word from r),
  i as (select word from l intersect select word from r)
  select case when (select count(*) from u) = 0 then 0::numeric
              else (select count(*) from i)::numeric / (select count(*) from u)
         end;
$$;

comment on function public.job_hunter_word_set_jaccard(text, text) is
  'Word-set Jaccard similarity of two texts, 0 to 1. Two empty texts score 0, '
  'not 1 -- there is nothing to agree they are the same about (#61).';

-- job_hunter_assign_variant_groups -------------------------------------------
--
-- For each given posting that has no group yet: if it carries an ATS board,
-- compare its description against every already-grouped posting sharing its
-- (ats_provider, ats_board, normalized_title), by word-set Jaccard, and join
-- whichever group scores highest -- if that highest score clears the
-- threshold. Otherwise it founds a new group of its own. A posting with no
-- ATS board is left ungrouped.
--
-- Processed in first_seen_at, id order and one posting at a time (not as one
-- set-based statement): each UPDATE is visible to the SELECTs that follow it
-- within this same call, which is what lets several ungrouped variants of
-- one position, staged in the same crawl batch, fold onto each other without
-- a second pass -- the second variant sees the first's group, the third sees
-- both, and so on. This is also what keeps KIRA and BJAK apart despite
-- sharing a title: the candidate search is over every posting sharing the
-- key, and the *best* (max) score wins, so a KIRA variant arriving after
-- several BJAK variants exist still compares against all of them and still
-- scores below threshold against every one.
--
-- Idempotent: only ever touches a posting whose variant_group_id is still
-- null, so calling this again with the same ids, or with ids already
-- grouped, changes nothing and reports nothing.

create or replace function public.job_hunter_assign_variant_groups(p_posting_ids uuid[])
returns table (posting_id uuid, group_id uuid, joined_existing boolean)
language plpgsql
security invoker
set search_path = ''
as $$
declare
  v_id uuid;
  v_provider text;
  v_board text;
  v_title text;
  v_description text;
  v_best_group uuid;
  v_best_score numeric;
  -- Strictly above the highest KIRA/BJAK cross-role pair measured on
  -- ashby:bjakcareer (0.696) and strictly below the lowest true
  -- location-variant pair in the same corpus's 0.80-0.90 bucket. Not
  -- operator-tunable: a constant in code, justified by the measured
  -- distribution in issue #61.
  v_threshold constant numeric := 0.75;
begin
  for v_id, v_provider, v_board, v_title, v_description in
    select p.id, coalesce(p.ats_provider, ''), coalesce(p.ats_board, ''),
           p.normalized_title, p.description
      from public.job_hunter_postings p
     where p.id = any(p_posting_ids)
       and p.variant_group_id is null
     order by p.first_seen_at, p.id
  loop
    if v_provider = '' or v_board = '' then
      posting_id := v_id;
      group_id := null;
      joined_existing := false;
      return next;
      continue;
    end if;

    -- Serialize per (provider, board, title): without this, two concurrent
    -- batches ingesting variants of the same position at once each see no
    -- existing group under read committed and each found their own -- a
    -- split this function's own "only touch variant_group_id is null" rule
    -- then never repairs, because both rows already have a group. Held for
    -- the rest of this transaction, which is one resolve_persist batch.
    perform pg_advisory_xact_lock(
      hashtextextended(v_provider || ':' || v_board || ':' || v_title, 0));

    v_best_group := null;
    v_best_score := null;

    select c.variant_group_id,
           public.job_hunter_word_set_jaccard(v_description, c.description)
      into v_best_group, v_best_score
      from public.job_hunter_postings c
     where c.ats_provider = v_provider
       and c.ats_board = v_board
       and c.normalized_title = v_title
       and c.id <> v_id
       and c.variant_group_id is not null
     order by public.job_hunter_word_set_jaccard(v_description, c.description) desc,
              c.first_seen_at, c.id
     limit 1;

    if v_best_group is not null and v_best_score >= v_threshold then
      update public.job_hunter_postings set variant_group_id = v_best_group where id = v_id;
      posting_id := v_id;
      group_id := v_best_group;
      joined_existing := true;
    else
      update public.job_hunter_postings set variant_group_id = v_id where id = v_id;
      posting_id := v_id;
      group_id := v_id;
      joined_existing := false;
    end if;
    return next;
  end loop;
  return;
end;
$$;

comment on function public.job_hunter_assign_variant_groups(uuid[]) is
  'Assign each given, not-yet-grouped posting to a variant group by ATS '
  'board, normalized title and description similarity (#61). Idempotent: a '
  'posting that already has a group is left untouched and reported as '
  'nothing. Called from resolve_persist after every merged batch, over the '
  'privileged ingestion connection.';

-- Ingestion's own, like job_hunter_merge_posting_batch: Supabase's default
-- privileges grant execute on a new public function to anon and
-- authenticated by name, so revoking from PUBLIC alone would leave both
-- holding it.
revoke all on function public.job_hunter_assign_variant_groups(uuid[])
  from public, anon, authenticated, service_role;
revoke all on function public.job_hunter_word_set_jaccard(text, text)
  from public, anon, authenticated, service_role;

-- job_hunter_backfill_variant_groups ------------------------------------------
--
-- Groups every existing posting job_hunter_assign_variant_groups has not
-- yet reached, one batch at a time -- but all batches in one call, so one
-- transaction. At corpus scale use scripts/backfill_variant_groups.py, which
-- commits per batch. Idempotent and safe to re-run: a
-- completed backfill finds nothing left ungrouped and reports zero both
-- ways, which is also how a caller confirms it is done.

create or replace function public.job_hunter_backfill_variant_groups(p_batch_size integer default 500)
returns table (groups_formed integer, postings_grouped integer)
language plpgsql
security invoker
set search_path = ''
as $$
declare
  v_ids uuid[];
  v_groups_formed integer := 0;
  v_postings_grouped integer := 0;
  v_result record;
begin
  if p_batch_size is null or p_batch_size <= 0 then
    raise exception 'p_batch_size must be positive, got %', p_batch_size
      using errcode = '22023';
  end if;

  loop
    select array_agg(s.id) into v_ids
      from (
        select p.id
          from public.job_hunter_postings p
         where p.variant_group_id is null
           and coalesce(p.ats_provider, '') <> ''
           and coalesce(p.ats_board, '') <> ''
         order by p.first_seen_at, p.id
         limit p_batch_size
      ) s;
    exit when v_ids is null or cardinality(v_ids) = 0;

    for v_result in select * from public.job_hunter_assign_variant_groups(v_ids)
    loop
      if v_result.group_id is not null then
        v_postings_grouped := v_postings_grouped + 1;
        if not v_result.joined_existing then
          v_groups_formed := v_groups_formed + 1;
        end if;
      end if;
    end loop;
  end loop;

  groups_formed := v_groups_formed;
  postings_grouped := v_postings_grouped;
  return next;
end;
$$;

comment on function public.job_hunter_backfill_variant_groups(integer) is
  'Group every existing open-or-closed posting job_hunter_assign_variant_groups '
  'has not yet reached, p_batch_size postings at a time. Returns how many '
  'groups it formed and how many postings it grouped; both zero means the '
  'backfill is complete (#61).';

revoke all on function public.job_hunter_backfill_variant_groups(integer)
  from public, anon, authenticated, service_role;

-- The corpus backfill is NOT run here ----------------------------------------
--
-- This migration used to call job_hunter_backfill_variant_groups(500) in a DO
-- block. On the live corpus (23,744 postings with an ATS board, ~222k
-- word-set comparisons) that ran past the deploy's ~2 minute statement
-- timeout and rolled the whole migration back, blocking every migration
-- after it. p_batch_size does not help: batches inside one statement are
-- still one transaction and one statement-timeout window.
--
-- Run apps/job-hunter/scripts/backfill_variant_groups.py right after this
-- deploys instead. It commits one batch per transaction. Until it runs, the
-- existing corpus stays ungrouped and a newly crawled variant founds a fresh
-- group instead of joining its old siblings.
