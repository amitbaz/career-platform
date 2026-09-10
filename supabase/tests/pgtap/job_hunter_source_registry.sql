begin;
select plan(25);

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

-- The scheduler installs one entry per source ------------------------------

insert into public.job_hunter_sources (source_key, kind) values ('arbeitnow', 'crawl')
  on conflict (source_key) do nothing;
update public.job_hunter_sources set enabled = false where source_key = 'example_licensed';

select is(
  public.job_hunter_reschedule_sources(),
  (select count(*)::int from public.job_hunter_sources where enabled),
  'every enabled source is scheduled and no disabled one is');

select is(
  (select count(distinct jobname)::int from cron.job
    where jobname like 'job-hunter-enqueue-crawl-source-%'),
  (select count(*)::int from public.job_hunter_sources where enabled),
  'one distinct cron entry per enabled source, not one shared entry');

select is_empty(
  $$ select 1 from cron.job
      where jobname = 'job-hunter-enqueue-crawl-source-example-licensed' $$,
  'a disabled source is unscheduled rather than left firing');

select * from finish();
rollback;
