-- The metered search reservation is one atomic, serialised operation.
--
-- `SearchUsageLedger.try_record` used to count the ledger and then insert,
-- over two PostgREST round trips, and relied on a GitHub Actions concurrency
-- group to guarantee there was only ever one writer. #189 deleted the workflow
-- that carried that guarantee, so the guarantee now has to live here.
--
-- The property that matters is not "the function refuses over the cap" -- a
-- read-then-write does that too, right up until two callers overlap. It is
-- that the check and the insert are serialised, and the advisory lock is the
-- only thing providing that: under READ COMMITTED, two transactions counting
-- the same window do not see each other's uncommitted rows, so folding the
-- pair into one function without the lock would leave the race exactly where
-- it was while looking fixed. Hence the pg_locks assertion below: it fails if
-- someone removes the lock and keeps the function.

begin;
create extension if not exists pgtap with schema extensions;
select no_plan();

insert into auth.users
  (id, email, instance_id, aud, role, raw_app_meta_data, raw_user_meta_data, created_at, updated_at)
values
  ('dddddddd-0000-0000-0000-000000000011', 'search-runner@test.local',
   '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', '{}', '{}', now(), now())
on conflict (id) do nothing;

create function pg_temp.authenticate_as(p_user uuid, p_runner boolean default false)
returns void language plpgsql as $$
begin
  execute 'reset role';
  perform set_config(
    'request.jwt.claims',
    jsonb_build_object(
      'sub', p_user,
      'role', 'authenticated',
      'job_hunter_runner', p_runner
    )::text,
    true
  );
  execute 'set local role authenticated';
end $$;

create function pg_temp.become_postgres() returns void language plpgsql as $$
begin
  execute 'reset role';
  perform set_config('request.jwt.claims', '', true);
end $$;

-- Every reservation below lands in 2031-03, a window no other test in the
-- suite writes into, so a row counted here is one this file made. The ledger
-- has no user_id (#184), so nothing else marks a row as ours.

select has_function(
  'public', 'job_hunter_reserve_search_request',
  array['text', 'timestamp with time zone', 'integer', 'integer'],
  'the reservation is a database function, not a client-side read-then-write');

-- Serialisation ------------------------------------------------------------
--
-- The lock is transaction-scoped, so inside this test transaction it is still
-- held after the call returns and is visible in pg_locks. A second session
-- calling for the same provider blocks here; one calling for another provider
-- does not.

select ok(
  public.job_hunter_reserve_search_request(
    'pgtap_brave', '2031-03-01T00:00:00Z'::timestamptz, 2, 2),
  'the first reservation under both caps is granted');

select is(
  (select count(*)::int from pg_locks
    where locktype = 'advisory'
      and pid = pg_backend_pid()
      and classid = hashtext('job_hunter_platform_search_usage')::oid
      and objid = hashtext('pgtap_brave')::oid),
  1,
  'it holds a per-provider advisory lock, which is what serialises the count against the insert');

select is(
  (select count(*)::int from pg_locks
    where locktype = 'advisory'
      and pid = pg_backend_pid()
      and classid = hashtext('job_hunter_platform_search_usage')::oid
      and objid = hashtext('pgtap_adzuna')::oid),
  0,
  'and does not hold another provider''s, so two providers do not queue behind each other');

-- Caps ----------------------------------------------------------------------

select ok(
  public.job_hunter_reserve_search_request(
    'pgtap_brave', '2031-03-01T00:00:00.000001Z'::timestamptz, 2, 2),
  'the second fills the cap');
select ok(
  not public.job_hunter_reserve_search_request(
    'pgtap_brave', '2031-03-01T00:00:00.000002Z'::timestamptz, 2, 2),
  'the third is refused by the monthly cap');

select is(
  (select count(*)::int from public.job_hunter_platform_search_usage
    where provider = 'pgtap_brave'),
  2,
  'a refused reservation writes nothing');

-- The daily cap binds separately from the monthly one: a caller with monthly
-- room left but today's share spent must still be refused, which is what
-- spreads a monthly allowance across the month instead of burning it on day
-- one.
select ok(
  not public.job_hunter_reserve_search_request(
    'pgtap_daily', '2031-03-01T00:00:00Z'::timestamptz, 100, 0),
  'a zero daily share refuses even with the month wide open');
select ok(
  public.job_hunter_reserve_search_request(
    'pgtap_daily', '2031-03-01T00:00:00Z'::timestamptz, 100, 1),
  'and one day''s share is spendable');
select ok(
  not public.job_hunter_reserve_search_request(
    'pgtap_daily', '2031-03-01T00:00:00.000001Z'::timestamptz, 100, 1),
  'a second call the same day is refused while the month still has 99 left');
select ok(
  public.job_hunter_reserve_search_request(
    'pgtap_daily', '2031-03-02T00:00:00Z'::timestamptz, 100, 1),
  'the next day gets its own share');

-- Windows are UTC, not the session's zone, and PostgREST does not pin
-- TimeZone. The three rows above sit just after the UTC month boundary, which
-- in Los Angeles is still February: a boundary computed in the session's zone
-- would count an empty February window and hand out a fourth call against a
-- cap of two.
set local timezone to 'America/Los_Angeles';
select ok(
  not public.job_hunter_reserve_search_request(
    'pgtap_brave', '2031-03-01T00:00:00.000003Z'::timestamptz, 2, 2),
  'the month window does not move when the session timezone does');
reset timezone;

-- Retry ---------------------------------------------------------------------
--
-- The unique key exists so a retried write converges rather than double-counting
-- one call. A retry must therefore get the same answer it got the first time,
-- without spending a second slot.
select ok(
  public.job_hunter_reserve_search_request(
    'pgtap_retry', '2031-03-01T00:00:00Z'::timestamptz, 1, 1),
  'a reservation is granted');
select ok(
  public.job_hunter_reserve_search_request(
    'pgtap_retry', '2031-03-01T00:00:00Z'::timestamptz, 1, 1),
  'and repeating it for the same instant is the same reservation, not a second one');
select is(
  (select count(*)::int from public.job_hunter_platform_search_usage
    where provider = 'pgtap_retry'),
  1,
  'which leaves exactly one row against the cap');

-- Refusals that cost nothing -------------------------------------------------

select ok(
  not public.job_hunter_reserve_search_request(
    'pgtap_unbudgeted', '2031-03-01T00:00:00Z'::timestamptz, 0, 5),
  'no monthly budget means no reservation');
select throws_ok(
  $$ select public.job_hunter_reserve_search_request(
       null, '2031-03-01T00:00:00Z'::timestamptz, 1, 1) $$,
  '22023',
  null,
  'a missing provider is a caller error, not a silent refusal');

-- Access ---------------------------------------------------------------------
--
-- `security invoker`, so the ledger's runner-claim policies still decide. A
-- signed-in user who is not a runner must not be able to spend the platform
-- key's allowance through this function.

select pg_temp.authenticate_as('dddddddd-0000-0000-0000-000000000011', true);
select ok(
  public.job_hunter_reserve_search_request(
    'pgtap_runner', '2031-03-01T00:00:00Z'::timestamptz, 1, 1),
  'a runner may reserve');

select pg_temp.authenticate_as('dddddddd-0000-0000-0000-000000000011', false);
select throws_ok(
  $$ select public.job_hunter_reserve_search_request(
       'pgtap_user', '2031-03-01T00:00:00Z'::timestamptz, 1, 1) $$,
  '42501',
  null,
  'a signed-in user cannot spend the platform key through it');

select pg_temp.become_postgres();
select * from finish();
rollback;
