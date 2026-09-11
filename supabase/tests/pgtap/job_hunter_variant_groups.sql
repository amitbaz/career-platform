-- Variant groups: location fanout of one position folds to one match
-- (issue #61).
--
-- Modeled on the measured ashby:bjakcareer corpus from the issue: a 23-way
-- location fanout of one role must collapse to one group and one
-- job_hunter_match_jobs row carrying all 23 locations, while a same-titled
-- but textually different role at the same employer (the KIRA/BJAK case)
-- must stay apart.
begin;
create extension if not exists pgtap with schema extensions;
select plan(17);

create function pg_temp.authenticate_as(p_user uuid) returns void
language plpgsql as $$
begin
  execute 'reset role';
  perform set_config('request.jwt.claims',
    json_build_object('sub', p_user, 'role', 'authenticated')::text, true);
  execute 'set local role authenticated';
end $$;

create function pg_temp.become_postgres() returns void
language plpgsql as $$
begin
  execute 'reset role';
  perform set_config('request.jwt.claims', '', true);
end $$;

select pg_temp.become_postgres();

-- Signatures ------------------------------------------------------------------

select has_function('public', 'job_hunter_assign_variant_groups', array['uuid[]'],
  'job_hunter_assign_variant_groups exists');
select has_function('public', 'job_hunter_word_set_jaccard', array['text', 'text'],
  'job_hunter_word_set_jaccard exists');

-- job_hunter_word_set_jaccard --------------------------------------------------

select is(
  public.job_hunter_word_set_jaccard('kubernetes postgres', 'kubernetes postgres'),
  1::numeric,
  'word_set_jaccard: identical texts score 1'
);
select is(
  public.job_hunter_word_set_jaccard('kubernetes postgres', 'marketing sales'),
  0::numeric,
  'word_set_jaccard: disjoint texts score 0'
);

-- Fixtures: a 23-location fanout of one KIRA role, and a differently-worded
-- BJAK role sharing the same title on the same board (#61's KIRA/BJAK case).

insert into public.job_hunter_postings
  (id, fingerprint, source, ats_provider, ats_board, company, title, location,
   description, description_hash, content_confidence, first_seen_at, last_seen_at)
select
  ('e1000000-0000-0000-0000-' || lpad(i::text, 12, '0'))::uuid,
  'variant-fp-kira-' || i,
  'ashby', 'ashby', 'bjakcareer',
  'KIRA', 'Lead Software Engineer', 'City ' || i,
  'about kira we build fintech infra location loc' || i,
  '', 'official_ats',
  '2026-01-01T00:00:00Z'::timestamptz + (i || ' seconds')::interval,
  '2026-01-01T00:00:00Z'::timestamptz + (i || ' seconds')::interval
from generate_series(1, 23) as i;

insert into public.job_hunter_postings
  (id, fingerprint, source, ats_provider, ats_board, company, title, location,
   description, description_hash, content_confidence, first_seen_at, last_seen_at)
select
  ('e2000000-0000-0000-0000-' || lpad(i::text, 12, '0'))::uuid,
  'variant-fp-bjak-' || i,
  'ashby', 'ashby', 'bjakcareer',
  'BJAK', 'Lead Software Engineer', 'City B' || i,
  'the role bjak builds payments platform location loc' || i,
  '', 'official_ats',
  '2026-01-01T00:00:00Z'::timestamptz + (i || ' seconds')::interval,
  '2026-01-01T00:00:00Z'::timestamptz + (i || ' seconds')::interval
from generate_series(1, 3) as i;

select * from public.job_hunter_assign_variant_groups(
  array(select id from public.job_hunter_postings where fingerprint like 'variant-fp-kira-%')
);
select * from public.job_hunter_assign_variant_groups(
  array(select id from public.job_hunter_postings where fingerprint like 'variant-fp-bjak-%')
);

select is(
  (select count(distinct variant_group_id) from public.job_hunter_postings
    where fingerprint like 'variant-fp-kira-%'),
  1::bigint,
  'assign_variant_groups: 23 location variants of one KIRA role collapse into one group'
);

select is(
  (select count(distinct variant_group_id) from public.job_hunter_postings
    where fingerprint like 'variant-fp-bjak-%'),
  1::bigint,
  'assign_variant_groups: the BJAK location variants collapse into their own one group'
);

select isnt(
  (select variant_group_id from public.job_hunter_postings where fingerprint = 'variant-fp-kira-1'),
  (select variant_group_id from public.job_hunter_postings where fingerprint = 'variant-fp-bjak-1'),
  'assign_variant_groups: KIRA and BJAK, sharing a title on one board but not their text, stay in separate groups'
);

select is(
  (select count(*) from public.job_hunter_assign_variant_groups(
     array(select id from public.job_hunter_postings where fingerprint like 'variant-fp-kira-%')
   )),
  0::bigint,
  'assign_variant_groups: re-running on already-grouped postings changes and reports nothing'
);

-- job_hunter_backfill_variant_groups --------------------------------------------

select variant_group_id as kira_group_id from public.job_hunter_postings
 where fingerprint = 'variant-fp-kira-1' \gset

insert into public.job_hunter_postings
  (id, fingerprint, source, ats_provider, ats_board, company, title, location,
   description, description_hash, content_confidence, first_seen_at, last_seen_at)
values (
  'e1000000-0000-0000-0000-000000000024'::uuid, 'variant-fp-kira-24',
  'ashby', 'ashby', 'bjakcareer', 'KIRA', 'Lead Software Engineer', 'City 24',
  'about kira we build fintech infra location loc24', '', 'official_ats',
  '2026-01-02T00:00:00Z', '2026-01-02T00:00:00Z'
);

select groups_formed, postings_grouped from public.job_hunter_backfill_variant_groups() \gset backfill_

select is(:backfill_postings_grouped, 1,
  'backfill_variant_groups: groups the one newly-ungrouped KIRA variant');
select is(:backfill_groups_formed, 0,
  'backfill_variant_groups: it joins the existing KIRA group rather than founding a new one');
select is(
  (select variant_group_id from public.job_hunter_postings where fingerprint = 'variant-fp-kira-24'),
  :'kira_group_id'::uuid,
  'backfill_variant_groups: the new variant joined KIRA''s group'
);

select groups_formed, postings_grouped from public.job_hunter_backfill_variant_groups() \gset backfill2_

select is(:backfill2_groups_formed, 0,
  'backfill_variant_groups: a second, completed run forms no new groups');
select is(:backfill2_postings_grouped, 0,
  'backfill_variant_groups: a second, completed run groups nothing -- idempotent');

-- job_hunter_match_jobs folds the group --------------------------------------

insert into auth.users (id, email, instance_id, aud, role, raw_app_meta_data, raw_user_meta_data, created_at, updated_at)
values
  ('cccccccc-2222-0000-0000-000000000001', 'variant-a@test.local', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', '{}', '{}', now(), now())
on conflict (id) do nothing;

select pg_temp.authenticate_as('cccccccc-2222-0000-0000-000000000001'::uuid);

insert into public.job_hunter_search_profiles
  (user_id, timezone, scheduled_hour, max_jobs_per_run, source_minimum_per_run,
   source_max_share, salary_floor_eur, max_search_queries_per_run,
   max_canonical_resolutions_per_run, max_learned_ats_boards_per_run)
values
  ('cccccccc-2222-0000-0000-000000000001', 'Europe/Berlin', 9, 35, 0, 0.5, 90000, 30, 80, 75);

insert into public.job_hunter_jobs (id, user_id, posting_id, market_id, first_seen_at, last_seen_at)
select
  ('f1000000-0000-0000-0000-' || lpad(i::text, 12, '0'))::uuid,
  'cccccccc-2222-0000-0000-000000000001',
  ('e1000000-0000-0000-0000-' || lpad(i::text, 12, '0'))::uuid,
  '', now(), now()
from generate_series(1, 23) as i;

select is(
  (select count(*) from public.job_hunter_match_jobs()
    where posting_id in (select id from public.job_hunter_postings where fingerprint like 'variant-fp-kira-%')),
  1::bigint,
  'match_jobs: 23 membership rows on one variant group fold into a single result'
);

select is(
  (select array_length(locations, 1) from public.job_hunter_match_jobs()
    where posting_id in (select id from public.job_hunter_postings where fingerprint like 'variant-fp-kira-%')),
  23,
  'match_jobs: the single result carries every open location in the group'
);

select pg_temp.become_postgres();
update public.job_hunter_postings set closed_at = now(), closed_reason = 'http_404'
 where fingerprint = 'variant-fp-kira-1';
select pg_temp.authenticate_as('cccccccc-2222-0000-0000-000000000001'::uuid);

select is(
  (select array_length(locations, 1) from public.job_hunter_match_jobs()
    where posting_id in (select id from public.job_hunter_postings where fingerprint like 'variant-fp-kira-%')),
  22,
  'match_jobs: a closed variant drops out of the group''s open locations (#61, #186)'
);

select pg_temp.become_postgres();
update public.job_hunter_postings set closed_at = now(), closed_reason = 'http_404'
 where fingerprint like 'variant-fp-kira-%';
select pg_temp.authenticate_as('cccccccc-2222-0000-0000-000000000001'::uuid);

select is(
  (select count(*) from public.job_hunter_match_jobs()
    where posting_id in (select id from public.job_hunter_postings where fingerprint like 'variant-fp-kira-%')),
  0::bigint,
  'match_jobs: a group whose every variant is closed produces no row at all'
);

select * from finish();
rollback;
