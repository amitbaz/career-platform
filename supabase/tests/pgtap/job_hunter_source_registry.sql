begin;
select plan(36);

-- Shared knowledge: readable by any authenticated user, written by none ----

select has_table('public', 'job_hunter_sources', 'the source registry exists');

insert into public.job_hunter_sources (source_key, kind, display_credit)
values
  ('remotive', 'crawl', '{}'::jsonb),
  ('example_licensed', 'licensed', jsonb_build_object(
     'required', true,
     'text', 'Jobs by Example',
     'link_text', 'Jobs',
     'link_url', 'https://example.test/',
     'badge_url', 'https://example.test/logo.png',
     'badge_min_px', jsonb_build_array(116, 23)));

select ok(
  has_table_privilege('authenticated', 'public.job_hunter_sources', 'select'),
  'an authenticated user may read the registry');
select is_empty(
  $$ select 1 where has_table_privilege(
       'authenticated', 'public.job_hunter_sources', 'insert, update, delete') $$,
  'but may not write it');
select is_empty(
  $$ select 1 where has_table_privilege(
       'anon', 'public.job_hunter_sources', 'insert, update, delete') $$,
  'nor may anon');
select is_empty(
  $$ select 1 where has_table_privilege(
       'service_role', 'public.job_hunter_sources', 'insert, update, delete') $$,
  'nor the unused service role');

-- "source" does not imply "crawl" ------------------------------------------

select throws_ok(
  $$ insert into public.job_hunter_sources (source_key, kind)
     values ('bad_kind', 'scrape') $$,
  '23514',
  null,
  'kind is constrained to the two source kinds');
select is(
  (select kind from public.job_hunter_sources where source_key = 'example_licensed'),
  'licensed',
  'a licensed API is a first-class source kind, not a variant of a crawl');

-- The obligation resolves with no user in the path -------------------------

insert into public.job_hunter_postings (fingerprint, source, first_seen_at, last_seen_at)
values
  ('18400000-fingerprint-licensed', 'example_licensed', now(), now()),
  ('18400000-fingerprint-scraped', 'remotive', now(), now());

select is(
  public.job_hunter_posting_display_credit(
    (select id from public.job_hunter_postings
      where fingerprint = '18400000-fingerprint-licensed')) ->> 'text',
  'Jobs by Example',
  'a posting resolves its source''s required credit');
select is(
  public.job_hunter_posting_display_credit(
    (select id from public.job_hunter_postings
      where fingerprint = '18400000-fingerprint-licensed')) ->> 'link_url',
  'https://example.test/',
  'including the link a surface that cannot render the badge still owes');
select ok(
  public.job_hunter_posting_display_credit(
    (select id from public.job_hunter_postings
      where fingerprint = '18400000-fingerprint-scraped')) is null,
  'a source declaring no obligation resolves to nothing rather than an empty object');
select ok(
  public.job_hunter_posting_display_credit(gen_random_uuid()) is null,
  'an unknown posting resolves to nothing rather than raising');

-- Shared machinery: nobody reads the engine's own telemetry ----------------

select has_table('public', 'job_hunter_source_crawls', 'the crawl ledger exists');
select has_table('public', 'job_hunter_source_cursors', 'the cursor store exists');

select is_empty(
  $$ select 1 where has_table_privilege(
       'authenticated', 'public.job_hunter_source_crawls',
       'select, insert, update, delete') $$,
  'a user cannot reach the crawl ledger at all');
select is_empty(
  $$ select 1 where has_table_privilege(
       'authenticated', 'public.job_hunter_source_cursors',
       'select, insert, update, delete') $$,
  'nor the cursor store');

-- An empty result has to carry its reason ----------------------------------

select throws_ok(
  $$ insert into public.job_hunter_source_crawls (source_key, started_at, outcome)
     values ('remotive', now(), 'nothing_today') $$,
  '23514',
  null,
  'a crawl outcome must be one of the four that distinguish why it was empty');

select lives_ok(
  $$ insert into public.job_hunter_source_crawls
       (source_key, started_at, finished_at, outcome, fetched, new_to_corpus)
     values ('remotive', now(), now(), 'not_modified', 0, 0) $$,
  'an unchanged board records not_modified rather than an empty fetch');

-- The search allowance is a property of the key, not of a person -----------

select has_table('public', 'job_hunter_platform_search_usage',
  'the platform search ledger exists');
select hasnt_table('public', 'job_hunter_search_api_usage',
  'and the per-user table it replaces is gone');

select col_is_unique(
  'public', 'job_hunter_platform_search_usage', array['provider', 'occurred_at'],
  'the platform ledger converges a retried write on (provider, occurred_at)');

select is_empty(
  $$ select 1 from pg_policy
      where polrelid = 'public.job_hunter_platform_search_usage'::regclass
        and polcmd = 'd' $$,
  'a ledger that can be rewritten is not a ledger: no delete policy');

select is(
  (select count(*)::int from pg_policy
    where polrelid = 'public.job_hunter_platform_search_usage'::regclass),
  3,
  'select, insert and update only, all on the runner claim');

-- The scheduler installs one entry per crawl target, not per registry row --
--
-- job_hunter_sources is keyed by the coarse posting source; build_source
-- answers only to the fine crawl key. The scheduler must loop over
-- job_hunter_crawl_targets -- keyed by that fine string -- or an ATS
-- adapter's cron payload carries a key no adapter recognises (issue #184).

select has_table('public', 'job_hunter_crawl_targets',
  'the crawl target registry exists');

select is_empty(
  $$ select 1 where has_table_privilege(
       'authenticated', 'public.job_hunter_crawl_targets',
       'select, insert, update, delete') $$,
  'a user cannot reach the crawl target registry at all');
select is_empty(
  $$ select 1 where has_table_privilege(
       'anon', 'public.job_hunter_crawl_targets',
       'select, insert, update, delete') $$,
  'nor anon');
select is_empty(
  $$ select 1 where has_table_privilege(
       'service_role', 'public.job_hunter_crawl_targets',
       'select, insert, update, delete') $$,
  'nor the unused service role');

insert into public.job_hunter_crawl_targets (crawl_key) values
  ('remotive'), ('arbeitnow'), ('greenhouse:acme')
  on conflict (crawl_key) do nothing;
update public.job_hunter_crawl_targets set enabled = false
  where crawl_key = 'arbeitnow';

select is(
  public.job_hunter_reschedule_sources(),
  (select count(*)::int from public.job_hunter_crawl_targets where enabled),
  'every enabled crawl target is scheduled and no disabled one is');

select is(
  (select count(distinct jobname)::int from cron.job
    where jobname like 'job-hunter-enqueue-crawl-source-%'),
  (select count(*)::int from public.job_hunter_crawl_targets where enabled),
  'one distinct cron entry per enabled crawl target, not one shared entry');

select is_empty(
  $$ select 1 from cron.job
      where jobname = 'job-hunter-enqueue-crawl-source-arbeitnow-'
        || left(encode(sha256(convert_to('arbeitnow', 'UTF8')), 'hex'), 16) $$,
  'a disabled crawl target is unscheduled rather than left firing');

-- The regression: a fine key, not a coarse one, on the cron payload --------
--
-- Before the fix this fails: the scheduler read job_hunter_sources, which
-- has no row keyed 'greenhouse:acme', so this fine key was never scheduled
-- and its payload never carried the string build_source() actually needs.
select ok(
  exists(
    select 1 from cron.job
     where jobname like 'job-hunter-enqueue-crawl-source-%'
       and command like '%"crawl_key": "greenhouse:acme"%'),
  'a fine crawl key reaches the cron payload verbatim, not the coarse source');

-- job_hunter_sources and job_hunter_crawl_targets are allowed to disagree --
--
-- Deliberately not foreign-keyed: a posting source nothing has crawled yet,
-- and a crawl target that yielded nothing the corpus did not already have,
-- are both ordinary states. Nobody should "fix" this with a link.
select ok(
  exists(
    select 1 from public.job_hunter_sources s
     where not exists (
       select 1 from public.job_hunter_crawl_targets t
        where t.crawl_key = s.source_key
     )),
  'a posting source with no matching crawl target is a legitimate state');
select ok(
  exists(
    select 1 from public.job_hunter_crawl_targets t
     where not exists (
       select 1 from public.job_hunter_sources s
        where s.source_key = t.crawl_key
     )),
  'a crawl target with no matching posting source is a legitimate state too');

-- The band responds to measured yield -------------------------------------
--
-- This is acceptance criterion 4 and until now nothing asserted it: the
-- suite proved one cron entry per enabled target and that a disabled target
-- is unscheduled, neither of which says a productive source is visited more
-- often than a barren one. The policy that actually runs is the PL/pgSQL
-- below; `source_schedule.py` carries the cron *rendering* and is
-- cross-checked against it from the Python side, but the band selection
-- lives only here.
--
-- `job_hunter_reschedule_sources` reads the last six crawls per target and
-- lands on band `3 + demotions - promotions`, so six productive crawls put a
-- source at the floor (15 minutes, `*/15`) and six barren ones at the
-- ceiling (weekly, a fixed weekday literal).

insert into public.job_hunter_crawl_targets (crawl_key) values
  ('band-productive'), ('band-barren')
  on conflict (crawl_key) do nothing;

insert into public.job_hunter_source_crawls
  (source_key, outcome, fetched, new_to_corpus, changed, started_at)
select 'band-productive', 'fetched', 10, 5, 5, now() - (n || ' hours')::interval
  from generate_series(1, 6) n;

insert into public.job_hunter_source_crawls
  (source_key, outcome, fetched, new_to_corpus, changed, started_at)
select 'band-barren', 'fetched', 10, 0, 0, now() - (n || ' hours')::interval
  from generate_series(1, 6) n;

select public.job_hunter_reschedule_sources();

select is(
  (select split_part(schedule, ' ', 1) ~ '/15$' from cron.job
    where command like '%"crawl_key": "band-productive"%'),
  true,
  'six productive crawls put a source on the fastest band');

select is(
  (select split_part(schedule, ' ', 5) from cron.job
    where command like '%"crawl_key": "band-barren"%'),
  '4',
  'six barren crawls put a source on the slowest, weekly band');

-- A rate-limited source backs off even though it returned rows, because the
-- constraint is the source's tolerance rather than its productivity.
insert into public.job_hunter_crawl_targets (crawl_key) values ('band-throttled')
  on conflict (crawl_key) do nothing;
insert into public.job_hunter_source_crawls
  (source_key, outcome, fetched, new_to_corpus, changed, started_at)
select 'band-throttled', 'rate_limited', 10, 5, 5, now() - (n || ' hours')::interval
  from generate_series(1, 6) n;

select public.job_hunter_reschedule_sources();

select is(
  (select split_part(schedule, ' ', 5) from cron.job
    where command like '%"crawl_key": "band-throttled"%'),
  '4',
  'a rate-limited source backs off however much it returned');

select isnt(
  (select schedule from cron.job
    where command like '%"crawl_key": "band-productive"%'),
  (select schedule from cron.job
    where command like '%"crawl_key": "band-barren"%'),
  'yield changes the cadence: the two bands are not the same schedule');

select * from finish();
rollback;
