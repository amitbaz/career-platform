begin;
select plan(11);

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

select * from finish();
rollback;
