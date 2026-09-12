-- Make the external-search reservation atomic again.
--
-- `SearchUsageLedger.try_record` reserved one metered search call by counting
-- `job_hunter_platform_search_usage` and then inserting a row -- two PostgREST
-- round trips with nothing holding the slot between them. The SQLite ledger it
-- was ported from held `BEGIN IMMEDIATE` across that pair, so two overlapping
-- callers could never both observe the same under-the-cap slot and both insert.
-- The port had no PostgREST equivalent and leaned instead on the GitHub Actions
-- `concurrency: group: job-hunter-state` guard, which serialised every writer
-- that touched this ledger.
--
-- #189 retired that workflow. `crawl-source` is now a Render cron running every
-- fifteen minutes (render.yaml), and Render does not promise one invocation
-- finishes before the next starts: a drain that outlives its slot overlaps
-- itself, and the owner running the CLI locally while the cron fires overlaps
-- it too. Either puts two callers back on the read-then-write pair, and each
-- racer can overshoot the provider's monthly cap by one call.
--
-- Folding the pair into one function is necessary but not sufficient. Under
-- READ COMMITTED neither transaction sees the other's uncommitted insert, so
-- both would still count themselves under the cap and both would write. The
-- advisory lock is the part that actually serialises them. It is
-- transaction-scoped, so the per-request transaction PostgREST opens releases
-- it with no unlock path to leak, and it is keyed by provider so a future
-- second provider does not queue behind Brave.
--
-- Window arithmetic is done on the naive UTC value rather than on the
-- `timestamptz` directly: adding `interval '1 month'` to a `timestamptz` is
-- evaluated in the session's `TimeZone`, which PostgREST does not pin, so a
-- session on a DST-observing zone would shift a boundary by an hour twice a
-- year. `at time zone 'UTC'` both ways keeps the boundaries exactly where
-- `search_budget.py` computes them.
create or replace function public.job_hunter_reserve_search_request(
  p_provider text,
  p_occurred_at timestamptz,
  p_monthly_limit integer,
  p_daily_limit integer
)
returns boolean
language plpgsql
security invoker
set search_path = ''
as $$
declare
  v_month_start timestamp;
  v_day_start timestamp;
  v_used_month integer;
  v_used_day integer;
begin
  if p_provider is null or p_occurred_at is null then
    raise exception 'provider and occurred_at are required'
      using errcode = '22023';
  end if;
  if coalesce(p_monthly_limit, 0) <= 0 or coalesce(p_daily_limit, 0) <= 0 then
    return false;
  end if;

  -- Everything below this line is serialised per provider.
  perform pg_advisory_xact_lock(
    hashtext('job_hunter_platform_search_usage'),
    hashtext(p_provider)
  );

  -- Answer a retry before counting. The `(provider, occurred_at)` unique key
  -- exists so a retried write converges instead of double-counting one call,
  -- and `SupabaseClient.rpc` retries transient failures -- so a first attempt
  -- that committed and then lost its response must get the same `true` back,
  -- not a refusal because the row it already wrote filled the last slot.
  if exists (
    select 1
      from public.job_hunter_platform_search_usage u
     where u.provider = p_provider
       and u.occurred_at = p_occurred_at
  ) then
    return true;
  end if;

  v_month_start := date_trunc('month', p_occurred_at at time zone 'UTC');
  v_day_start := date_trunc('day', p_occurred_at at time zone 'UTC');

  select count(*)
    into v_used_month
    from public.job_hunter_platform_search_usage u
   where u.provider = p_provider
     and u.occurred_at >= v_month_start at time zone 'UTC'
     and u.occurred_at < (v_month_start + interval '1 month') at time zone 'UTC';

  select count(*)
    into v_used_day
    from public.job_hunter_platform_search_usage u
   where u.provider = p_provider
     and u.occurred_at >= v_day_start at time zone 'UTC'
     and u.occurred_at < (v_day_start + interval '1 day') at time zone 'UTC';

  if v_used_month >= p_monthly_limit or v_used_day >= p_daily_limit then
    return false;
  end if;

  -- `do nothing` is unreachable in practice -- the existence check above ran
  -- under the same lock -- and is kept only so that a conflict arriving from
  -- some path that bypasses this function cannot turn a reservation into an
  -- exception.
  insert into public.job_hunter_platform_search_usage (provider, occurred_at)
  values (p_provider, p_occurred_at)
  on conflict (provider, occurred_at) do nothing;

  return true;
end;
$$;

comment on function public.job_hunter_reserve_search_request(
  text, timestamptz, integer, integer
) is
  'Reserve one metered external-search call, or refuse it. The count and the '
  'insert are one transaction behind a per-provider advisory lock, so '
  'overlapping crawls cannot both spend the last slot under the cap.';

-- The runner calls this through PostgREST; `security invoker` means the
-- ledger's own runner-claim policies still decide whether the count sees
-- anything and whether the insert is allowed.
grant execute on function public.job_hunter_reserve_search_request(
  text, timestamptz, integer, integer
) to authenticated;
