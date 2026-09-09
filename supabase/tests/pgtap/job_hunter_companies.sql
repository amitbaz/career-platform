-- The employer as an entity of its own (issue #198).
--
-- Three acceptance criteria are properties of this schema rather than of the
-- pipeline, so they are proved here.
--
-- 1. **Filterable.** A future dashboard browses the corpus by industry,
--    business model and company stage. That has to be an index scan, and the
--    values have to come from a controlled vocabulary or the filter is
--    meaningless -- "B2B SaaS" and "b2b_saas" would be two answers.
--
-- 2. **Shared.** One row per employer, readable by every authenticated user,
--    so two users hiring-hunting the same company cause one extraction
--    between them. Like job_hunter_postings and job_hunter_job_facets, this
--    table is deliberately excluded from job_hunter_isolation.sql: cross-user
--    isolation is what it must NOT have.
--
-- 3. **Refreshed on time, not on a posting's hash.** The absence of a
--    description-hash column is asserted, because attaching company facts to
--    the posting invalidation mechanism would re-derive stable facts at
--    posting cadence -- the exact cost this table exists to avoid.

begin;
create extension if not exists pgtap with schema extensions;
select no_plan();

-- Seed users ----------------------------------------------------------------

insert into auth.users (id, email, instance_id, aud, role, raw_app_meta_data, raw_user_meta_data, created_at, updated_at)
values
  ('cccccccc-0000-0000-0000-000000000005', 'companies-a@test.local', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', '{}', '{}', now(), now()),
  ('cccccccc-0000-0000-0000-000000000006', 'companies-b@test.local', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', '{}', '{}', now(), now())
on conflict (id) do nothing;

create function pg_temp.authenticate_as(p_user uuid) returns void
language plpgsql as $$
begin
  execute 'reset role';
  perform set_config('request.jwt.claims',
    json_build_object('sub', p_user, 'role', 'authenticated')::text, true);
  execute 'set local role authenticated';
end $$;

-- Shape ---------------------------------------------------------------------

select has_table('public', 'job_hunter_companies', 'the employer has a table of its own');

select columns_are('public', 'job_hunter_companies', array[
  'id',
  'identity',
  'display_name',
  'industry',
  'business_model',
  'stage',
  'size_band',
  'headquarters_region',
  'source_supplied',
  'model',
  'extracted_at',
  'created_at',
  'updated_at'
], 'the company columns are exactly the objective facts CONTEXT.md names');

-- Named separately from columns_are so the reason stays readable when
-- someone later wonders where the owner went.
select hasnt_column('public', 'job_hunter_companies', 'user_id',
                    'a company has no owner: it is the same employer to everyone');

-- Refresh is time-based. A description hash here would tie an employer's
-- attributes to one advertisement's text, which is what this ticket
-- deliberately does not do.
select hasnt_column('public', 'job_hunter_companies', 'description_hash_at_extraction',
                    'company facts are refreshed on a long interval, not by a posting''s hash');
select hasnt_column('public', 'job_hunter_companies', 'posting_id',
                    'a company is not attached to any one advertisement');

select col_is_unique('public', 'job_hunter_companies', array['identity'],
                     'one row per employer, whoever wrote it');
select col_not_null('public', 'job_hunter_companies', 'identity',
                    'a company row without an identity would be facts about nobody');

-- Filterability -------------------------------------------------------------

select has_index('public', 'job_hunter_companies',
                 'job_hunter_companies_industry_idx', 'industry is filterable');
select has_index('public', 'job_hunter_companies',
                 'job_hunter_companies_business_model_idx', 'business model is filterable');
select has_index('public', 'job_hunter_companies',
                 'job_hunter_companies_stage_idx', 'company stage is filterable');
select has_index('public', 'job_hunter_companies',
                 'job_hunter_companies_size_band_idx', 'company size is filterable');
select has_index('public', 'job_hunter_companies',
                 'job_hunter_companies_headquarters_region_idx',
                 'headquarters region is filterable');
select has_index('public', 'job_hunter_companies',
                 'job_hunter_companies_extracted_at_idx',
                 'the refresh scan -- "read before X" -- is an index scan');

-- The value domains ---------------------------------------------------------

select col_has_check('public', 'job_hunter_companies', 'industry',
                     'industry is a checked domain, not free text');
select col_has_check('public', 'job_hunter_companies', 'business_model',
                     'business model is a checked domain, not free text');
select col_has_check('public', 'job_hunter_companies', 'stage',
                     'stage is a checked domain, not free text');
select col_has_check('public', 'job_hunter_companies', 'size_band',
                     'size band is a checked domain, not free text');
select col_has_check('public', 'job_hunter_companies', 'headquarters_region',
                     'headquarters region is a checked domain, not free text');

-- Access --------------------------------------------------------------------

select is(
  (select relrowsecurity from pg_class where oid = 'public.job_hunter_companies'::regclass),
  true,
  'row level security is on');

select is(
  (select array_agg(polname::text order by polname)
     from pg_policy where polrelid = 'public.job_hunter_companies'::regclass),
  array['insert_authenticated', 'select_authenticated', 'update_authenticated'],
  'read and write are open to authenticated users; nobody may delete a shared company row');

-- Behaviour -----------------------------------------------------------------

-- The identity is the suffix-stripping normalization, so "Acme Payments Ltd"
-- and "Acme Payments" are one employer. This is the ticket's user story 14:
-- the system must have one notion of "the same company".
select is(
  public.job_hunter_normalize_company('Acme Payments Ltd'),
  public.job_hunter_normalize_company('Acme Payments'),
  'a trailing legal suffix does not make a second employer');

-- User A reads the company once.
select pg_temp.authenticate_as('cccccccc-0000-0000-0000-000000000005');

select lives_ok(
  $$ insert into public.job_hunter_companies
       (identity, display_name, industry, business_model, stage, size_band,
        headquarters_region, model, extracted_at)
     values (public.job_hunter_normalize_company('Acme Payments Ltd'),
             'Acme Payments Ltd', 'fintech', 'b2b_saas', 'series_a', '51_200',
             'europe', 'gemini-test', now()) $$,
  'the user who read the company may store what it says');

-- An out-of-vocabulary value must not reach the table by any path, or the
-- filters above stop meaning anything.
select throws_ok(
  $$ update public.job_hunter_companies
        set business_model = 'vibes'
      where identity = public.job_hunter_normalize_company('Acme Payments') $$,
  '23514',
  null,
  'a value outside the vocabulary is refused by the database, not just by the extractor');

-- "unknown" is a member of every vocabulary: a company nobody has
-- established anything about has to be representable, because a missing fact
-- is a first-class value here and never a negative one.
select lives_ok(
  $$ update public.job_hunter_companies
        set industry = 'unknown', stage = 'unknown'
      where identity = public.job_hunter_normalize_company('Acme Payments') $$,
  'unknown is a value every dimension may carry');

-- User B, who never read it, gets the answer anyway. This is the whole
-- issue: their run costs no provider call for this employer.
select pg_temp.authenticate_as('cccccccc-0000-0000-0000-000000000006');

select is(
  (select business_model from public.job_hunter_companies
    where identity = public.job_hunter_normalize_company('Acme Payments Ltd')),
  'b2b_saas',
  'a second user reads the company facts the first user paid for');

select lives_ok(
  $$ update public.job_hunter_companies
        set stage = 'series_b', extracted_at = now()
      where identity = public.job_hunter_normalize_company('Acme Payments') $$,
  'a second user may refresh the facts once the interval has passed');

select is(
  (select count(*)::int from public.job_hunter_companies
    where identity = public.job_hunter_normalize_company('Acme Payments')),
  1,
  'a refresh replaces the answer rather than appending a second one');

-- Nobody may take a shared row away from the others.
select lives_ok(
  $$ delete from public.job_hunter_companies
      where identity = public.job_hunter_normalize_company('Acme Payments') $$,
  'a delete is refused silently by row-level security rather than erroring');

select is(
  (select count(*)::int from public.job_hunter_companies
    where identity = public.job_hunter_normalize_company('Acme Payments')),
  1,
  'and the row is still there: no user may delete another user''s reading');

-- The search profile's company preferences ----------------------------------
--
-- Preferences are per-user and stay per-user: the facts above are shared, the
-- opinion about them is not.

select has_column('public', 'job_hunter_search_profiles', 'preferred_industries',
                  'a user can say which industries they want');
select has_column('public', 'job_hunter_search_profiles', 'excluded_industries',
                  'a user can say which industries they will not work in');
select has_column('public', 'job_hunter_search_profiles', 'preferred_business_models',
                  'a user can say which business models they want');
select has_column('public', 'job_hunter_search_profiles', 'excluded_business_models',
                  'a user can say which business models they will not work for');
select has_column('public', 'job_hunter_search_profiles', 'preferred_company_stages',
                  'a user can say which company stages they want');
select has_column('public', 'job_hunter_search_profiles', 'preferred_company_sizes',
                  'a user can say which company sizes they want');

-- Stating nothing must leave a user exactly where they were, which is why
-- every one of these defaults to the empty array rather than to a value.
select col_default_is('public', 'job_hunter_search_profiles', 'excluded_industries',
                      '{}', 'no opinion is the default, and it is not "match nothing"');

select * from finish();
rollback;
