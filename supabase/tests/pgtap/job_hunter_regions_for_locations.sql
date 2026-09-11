-- job_hunter_regions_for_locations (issue #243): the alias-to-region half of
-- hiring_scope.py, ported to SQL for comparing a user's configured market
-- locations against a posting's stated hiring_regions. This file is what the
-- design doc's Tests section promised and the PR that introduced the
-- function shipped without: a hundred hand-entered aliases with no direct
-- coverage is exactly the shape AGENTS.md rule 6 warns about -- a typo in
-- one of them fails silently and permanently.
begin;
create extension if not exists pgtap with schema extensions;
select plan(10);

select has_function('public', 'job_hunter_regions_for_locations', array['text[]'],
  'job_hunter_regions_for_locations exists');

-- One representative alias per region --------------------------------------

select is(
  public.job_hunter_regions_for_locations(array['Berlin']),
  array['europe'],
  'a European city resolves to europe'
);

select is(
  public.job_hunter_regions_for_locations(array['San Francisco']),
  array['north_america'],
  'a North American city resolves to north_america'
);

select is(
  public.job_hunter_regions_for_locations(array['Tel Aviv']),
  array['middle_east'],
  'a Middle Eastern city resolves to middle_east'
);

select is(
  public.job_hunter_regions_for_locations(array['Singapore']),
  array['asia_pacific'],
  'an Asia-Pacific city resolves to asia_pacific'
);

-- Acronyms are configuration, not prose: matched case-insensitively, with no
-- bare-pronoun guard (hiring_scope.regions_for_locations's own documented
-- reading -- these are typed place names, never the English pronoun "us").

select is(
  public.job_hunter_regions_for_locations(array['uk']),
  array['europe'],
  'a lower-case acronym still resolves -- configured locations are typed place names, not prose'
);

-- A location naming aliases from two different regions resolves both, and a
-- location the alias table does not recognise resolves to no region rather
-- than blocking or erroring -- fails open on missing evidence either way.

select is(
  public.job_hunter_regions_for_locations(array['Berlin, remote from Tel Aviv']),
  array['europe', 'middle_east'],
  'one location naming two regions'' aliases resolves both'
);

select is(
  public.job_hunter_regions_for_locations(array['Atlantis']),
  '{}'::text[],
  'an unrecognised location resolves to no region -- fails open, never a fabricated blocker'
);

-- Empty input, documented and pinned rather than left to be rediscovered:
-- `p_locations` is joined with a space before matching, so an alias can form
-- *across* two separate configured locations that individually mean
-- nothing. Contrived for real market configuration (nobody configures "New"
-- and "York" as two locations), and it still fails toward resolving a
-- region rather than inventing a blocker, so this is intended behaviour to
-- record, not a defect to fix.

select is(
  public.job_hunter_regions_for_locations(array['New', 'York']),
  array['north_america'],
  'two separately-configured locations can form an alias together once joined -- intended, not a defect (see file header)'
);

select is(
  public.job_hunter_regions_for_locations(null),
  '{}'::text[],
  'null input resolves to no region rather than erroring'
);

select * from finish();
rollback;
