-- Objective facets move onto the posting (issue #175, epic #118).
--
-- Extraction reads an advertisement once and records what it says. The
-- prompt cannot see who is asking (#126), which is exactly what makes the
-- answer shareable -- but until now the storage was not: facets keyed on
-- the per-user job row and were scoped by user_id under row-level security,
-- so two users who discovered the same advertisement each spent a provider
-- call on it and neither could read the other's answer. That is the
-- per-user cost AGENTS.md rule 2 -- "cost scales with jobs, not with
-- users" -- forbids.
--
-- #174 gave the advertisement a row of its own. This migration moves the
-- facets onto it: the table keys on job_hunter_postings, drops user_id and
-- job_id, and opens its reads to every authenticated user. One posting, one
-- set of facets, whoever asks.
--
-- Invalidation stays where #125 put it -- description_hash_at_extraction
-- compared against a description hash the row carries -- but the hash it is
-- compared against is now the posting's rather than each user's job row's.
-- Because the posting holds one description, that comparison has a single
-- answer, and two users can no longer invalidate each other's extraction in
-- turn: a materially edited posting is re-read once, for everyone.
--
-- The extraction itself still reads the text on the *job* row it was
-- dispatched for, and stamps the posting's hash on the result. Those two
-- can disagree, and not only for an instant: the job upsert takes whatever
-- description this run fetched, while the posting keeps the better one by
-- content confidence, so facets read from a job's text can be stamped
-- current for a posting's different text and stay that way until the
-- posting's own text changes again. Stamping the posting's hash is
-- nevertheless what makes re-extraction happen once rather than once per
-- user, which is this issue's acceptance criterion; #177 closes the gap by
-- moving the read itself onto the posting.

-- The pointer -----------------------------------------------------------------
--
-- Cascading from the posting, as the row previously cascaded from the job:
-- facets describe an advertisement and have no meaning once it is gone.

alter table public.job_hunter_job_facets
  add column posting_id uuid references public.job_hunter_postings(id) on delete cascade;

-- Migration of the rows that exist ---------------------------------------------
--
-- Every facet row moves to its job row's posting. #174 backfilled a posting
-- onto every job row that existed, so the only rows this cannot place are
-- those whose job was written without going through job_hunter_upsert_job.
-- They have no advertisement to hang off and no way to acquire one, so they
-- go; the posting is re-read on a later run if a job row ever points at it.

update public.job_hunter_job_facets f
   set posting_id = j.posting_id
  from public.job_hunter_jobs j
 where j.id = f.job_id;

delete from public.job_hunter_job_facets where posting_id is null;

-- Where two users had each paid for the same posting, one answer survives:
-- the most recently extracted, with the id as a tie-break so the result does
-- not depend on physical order. The rows say the same thing -- the prompt
-- could not see who asked -- so which one survives decides nothing beyond
-- which description hash is carried forward, and the newest was read at the
-- text closest to what the posting holds now.

delete from public.job_hunter_job_facets f
 using public.job_hunter_job_facets newer
 where newer.posting_id = f.posting_id
   and (newer.extracted_at, newer.id) > (f.extracted_at, f.id);

-- The new key ------------------------------------------------------------------
--
-- The four per-user policies are dropped first because each one reads
-- user_id, and a column cannot go while a policy depends on it. The
-- replacements are created at the end of this file, once the table has its
-- new shape.

drop policy select_own on public.job_hunter_job_facets;
drop policy insert_own on public.job_hunter_job_facets;
drop policy update_own on public.job_hunter_job_facets;
drop policy delete_own on public.job_hunter_job_facets;

-- Dropping user_id and job_id takes with them the constraints and indexes
-- built on them: the unique (job_id), the composite foreign key into
-- job_hunter_jobs, and the three user-leading btree indexes recreated below.

alter table public.job_hunter_job_facets
  drop column user_id,
  drop column job_id,
  alter column posting_id set not null,
  add constraint job_hunter_job_facets_posting_key unique (posting_id);

-- The same four filters the acceptance criteria of #125 name, without the
-- user_id that used to lead each one: a facet is no longer filtered inside
-- one user's rows, because it is not one user's row.
create index job_hunter_job_facets_remote_policy_idx
  on public.job_hunter_job_facets (remote_policy);
create index job_hunter_job_facets_seniority_idx
  on public.job_hunter_job_facets (seniority);
create index job_hunter_job_facets_compensation_idx
  on public.job_hunter_job_facets (compensation_max);
-- job_hunter_job_facets_hiring_regions_idx (GIN) never named user_id and is
-- left exactly as it was.

comment on table public.job_hunter_job_facets is
  'Objective facets read once per posting and reused by every later run, by '
  'every user: stated requirements and depth, disclosed compensation, '
  'hiring-eligible regions, remote and relocation policy, seniority and '
  'stack. Keyed on job_hunter_postings, and invalidated through that '
  'posting''s description_hash.';

comment on column public.job_hunter_job_facets.posting_id is
  'The advertisement these facts were read from. One row per posting: '
  're-extraction after a description change replaces the answer rather than '
  'appending a second one.';

comment on column public.job_hunter_job_facets.description_hash_at_extraction is
  'The posting description these facets were read at. Compared against '
  'job_hunter_postings.description_hash to decide whether they are current, '
  'the same mechanism job_hunter_evaluations.description_hash_at_eval uses '
  'per user.';

-- Access ------------------------------------------------------------------------
--
-- Reads open to every authenticated user, exactly as job_hunter_postings
-- does and for the same reason: the work of reading a posting is done once
-- for everyone, so everyone must be able to read the result. Writes stay as
-- open as they are on the posting itself, and there is deliberately no
-- delete policy -- a facet row is shared, so no single user may remove one
-- out from under the others. Narrowing the writes on both tables to the
-- platform identity is #179.

create policy select_authenticated on public.job_hunter_job_facets
  for select to authenticated using (true);
create policy insert_authenticated on public.job_hunter_job_facets
  for insert to authenticated with check (true);
create policy update_authenticated on public.job_hunter_job_facets
  for update to authenticated using (true) with check (true);
