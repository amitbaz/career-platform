-- job_hunter_backfill_ats_triple_dupes (#249): the batched backfill that
-- collapses postings sharing a trustworthy ATS triple across more than one
-- source label, re-keys every trustworthy singleton/survivor onto the
-- fingerprint the fixed normalize.job_fingerprint now computes, and leaves
-- the #254 shape (a triple that is not trustworthy on both sides) alone.

begin;
create extension if not exists pgtap with schema extensions;
select no_plan();

create function pg_temp.posting(
  p_fingerprint text,
  p_source text,
  p_ats_provider text,
  p_ats_board text,
  p_ats_job_id text,
  p_source_job_id text default null,
  p_first_seen_at timestamptz default now())
returns uuid language sql as $$
  insert into public.job_hunter_postings
    (fingerprint, source, source_job_id, url, company, title, location,
     description, description_hash, content_confidence,
     ats_provider, ats_board, ats_job_id,
     first_seen_at, last_seen_at, created_at)
  values
    (p_fingerprint, p_source, p_source_job_id, '', '', '', '',
     '', encode(sha256(''::bytea), 'hex'), '',
     p_ats_provider, p_ats_board, p_ats_job_id,
     p_first_seen_at, p_first_seen_at, p_first_seen_at)
  returning id;
$$;

create function pg_temp.canonical_fingerprint(p_provider text, p_board text, p_job_id text)
returns text language sql as $$
  select encode(sha256(convert_to(
    'id:' || lower(p_provider) || ':' || lower(p_board) || ':' || p_job_id, 'UTF8')), 'hex');
$$;

-- Fixture ids are captured by a stable label here, rather than looked up by
-- their original fingerprint later: step 2 changes a row's fingerprint, so
-- any assertion below that ran a fresh `where fingerprint = '...'` lookup
-- after the backfill would find nothing for exactly the rows it means to
-- check.
create table pg_temp.ids (label text primary key, id uuid not null);

-- Two source labels, same trustworthy ATS job ------------------------------
--
-- source_job_id agrees with ats_job_id on both sides, exactly the shape
-- every known ATS adapter produces and company_watch.py's relabel preserves.

insert into pg_temp.ids values ('cross-1', pg_temp.posting(
  'jhbb-cross-1', 'ashby', 'ashby', 'bjak', 'cross-1',
  p_source_job_id => 'cross-1', p_first_seen_at => now() - interval '1 day'));
insert into pg_temp.ids values ('cross-2', pg_temp.posting(
  'jhbb-cross-2', 'watch:ashby', 'ashby', 'bjak', 'cross-1', p_source_job_id => 'cross-1'));

-- One source label, one trustworthy + one NOT (#254's shape) ---------------
--
-- Two genuinely different postings -- distinct fingerprints, distinct
-- source_job_id -- that happen to share an ats_job_id. Only the side whose
-- source_job_id actually agrees with it is trustworthy; the other must be
-- left exactly as it was, on both the merge and the re-key.

insert into pg_temp.ids values ('trust', pg_temp.posting(
  'jhbb-trust', 'greenhouse', 'greenhouse', 'alarmcom', 'trustworthy-job',
  p_source_job_id => 'trustworthy-job'));
insert into pg_temp.ids values ('untrust', pg_temp.posting(
  'jhbb-untrust', 'greenhouse', 'greenhouse', 'alarmcom', 'trustworthy-job',
  p_source_job_id => 'a-different-real-job'));

-- A closed posting sharing the cross-source triple --------------------------
--
-- Closed postings are not delivered and must not be pulled into a merge.

insert into pg_temp.ids values ('closed', pg_temp.posting(
  'jhbb-closed', 'lever', 'lever', 'acme', 'closed-1', p_source_job_id => 'closed-1'));
update public.job_hunter_postings set closed_at = now(), closed_reason = 'closure_phrase'
 where id = (select id from pg_temp.ids where label = 'closed');
insert into pg_temp.ids values ('closed-watch', pg_temp.posting(
  'jhbb-closed-watch', 'watch:lever', 'lever', 'acme', 'closed-1', p_source_job_id => 'closed-1'));

-- A singleton, never duplicated, complete-ATS posting -----------------------
--
-- Never shared a triple with anything, so step 1 never touches it -- but it
-- still carries an old-scheme fingerprint from before #249, and step 2 must
-- re-key it too, or the next crawl of it inserts a fresh duplicate.

insert into pg_temp.ids values ('singleton', pg_temp.posting(
  'jhbb-singleton-old-fingerprint', 'ashby', 'ashby', 'solo-co', 'solo-1', p_source_job_id => 'solo-1'));

-- A batch limit of zero bounds the call to doing nothing --------------------
--
-- Checked against the fixtures above, before the real backfill runs, so
-- this actually proves the limit bounds work rather than finding nothing
-- left to do regardless.

create temp table limited_result as
select * from public.job_hunter_backfill_ats_triple_dupes(0);

select is(
  (select groups_processed from limited_result),
  0,
  'a batch limit of zero merges nothing');

select is(
  (select fingerprints_rewritten from limited_result),
  0,
  'nor re-keys anything');

select isnt(
  (select public.job_hunter_resolve_posting((select id from pg_temp.ids where label = 'cross-1'))),
  (select public.job_hunter_resolve_posting((select id from pg_temp.ids where label = 'cross-2'))),
  'the cross-source pair is still two postings after a zero-limit call');

create temp table backfill_result as
select * from public.job_hunter_backfill_ats_triple_dupes();

select is(
  (select groups_processed from backfill_result),
  1,
  'exactly one ATS-triple group qualifies for merging: the cross-source pair');

select is(
  (select merged_pairs from backfill_result),
  1,
  'one merge collapses that pair');

select is(
  (select fingerprints_rewritten from backfill_result),
  4,
  'four rows are re-keyed: the merged survivor, the trustworthy greenhouse row '
  '(sole trustworthy claimant of its triple despite an untrustworthy sibling), '
  'the open lever row (sole OPEN claimant of its triple -- its closed sibling '
  'is filtered out before the count, not merged with), and the never-duplicated '
  'singleton; the merged-away duplicate, the untrustworthy greenhouse row, and '
  'the closed lever row itself are excluded');

-- The merge keeps the merged-away row -- see job_hunter_merge_postings'
-- header on why -- so the raw row count under this triple stays 2. What
-- must be one is where both now resolve to, and what that survivor's own
-- fingerprint column now is.
select is(
  (select public.job_hunter_resolve_posting((select id from pg_temp.ids where label = 'cross-1'))),
  (select public.job_hunter_resolve_posting((select id from pg_temp.ids where label = 'cross-2'))),
  'the two source labels now resolve to the same posting');

select is(
  (select p.fingerprint from public.job_hunter_postings p
    where p.id = (select public.job_hunter_resolve_posting(
                    (select id from pg_temp.ids where label = 'cross-1')))),
  pg_temp.canonical_fingerprint('ashby', 'bjak', 'cross-1'),
  'the survivor is re-keyed onto the fingerprint the fixed job_fingerprint now computes');

select is(
  (select p.fingerprint from public.job_hunter_postings p
    where p.id = (select id from pg_temp.ids where label = 'untrust')),
  'jhbb-untrust',
  'the untrustworthy #254-shaped row keeps its own old fingerprint, unmerged and unre-keyed');

select is(
  (select count(*)::int from public.job_hunter_postings
    where ats_provider = 'greenhouse' and ats_board = 'alarmcom' and ats_job_id = 'trustworthy-job'),
  2,
  'the trustworthy and untrustworthy greenhouse rows are not merged with each other');

select is(
  (select count(*)::int from public.job_hunter_postings
    where ats_provider = 'lever' and ats_board = 'acme' and ats_job_id = 'closed-1'),
  2,
  'a closed posting sharing the triple with an open one is not merged');

select is(
  (select fingerprint from public.job_hunter_postings
    where id = (select id from pg_temp.ids where label = 'closed')),
  'jhbb-closed',
  'nor is the closed side re-keyed');

select is(
  (select fingerprint from public.job_hunter_postings
    where id = (select id from pg_temp.ids where label = 'closed-watch')),
  pg_temp.canonical_fingerprint('lever', 'acme', 'closed-1'),
  'the open sibling IS re-keyed on its own -- it is the sole open, trustworthy '
  'claimant of its triple once the closed row is filtered out, so this is a '
  'legitimate never-duplicated-among-open-postings singleton, not a merge');

select is(
  (select fingerprint from public.job_hunter_postings
    where id = (select id from pg_temp.ids where label = 'singleton')),
  pg_temp.canonical_fingerprint('ashby', 'solo-co', 'solo-1'),
  'a never-duplicated singleton with a complete ATS triple is re-keyed too -- '
  'otherwise its next crawl would insert a fresh duplicate under the new scheme');

-- The trustworthy greenhouse row, sole trustworthy claimant of its triple
-- despite sharing it with an untrustworthy sibling, is re-keyed on its own.
select is(
  (select fingerprint from public.job_hunter_postings
    where id = (select id from pg_temp.ids where label = 'trust')),
  pg_temp.canonical_fingerprint('greenhouse', 'alarmcom', 'trustworthy-job'),
  'the trustworthy greenhouse row is re-keyed even though an untrustworthy sibling shares its triple');

-- Idempotent: nothing left to do on a second unlimited call ------------------

create temp table backfill_rerun as
select * from public.job_hunter_backfill_ats_triple_dupes();

select is(
  (select merged_pairs from backfill_rerun),
  0,
  're-running the backfill after it has already run merges nothing further');

select is(
  (select fingerprints_rewritten from backfill_rerun),
  0,
  'nor re-keys anything further -- every trustworthy row already carries its canonical fingerprint');

select * from finish();
rollback;
