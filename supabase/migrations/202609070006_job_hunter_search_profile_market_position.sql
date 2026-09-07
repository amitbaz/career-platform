-- 202609070006_job_hunter_search_profile_market_position.sql
-- job_hunter_search_profile_markets has no ordinal column. save_search_profile
-- deletes all markets and re-inserts them in one batch; every row in that
-- batch gets an identical created_at, so ordering by created_at.asc (what
-- get_search_profile did) produced an unpredictable permutation (tiebreak on
-- random gen_random_uuid()). Market declared order is load-bearing --
-- ranking.py's market_priority_bonus, discovery_queries.py's
-- allocate_market_query_slots, and market_policy.py's attribute_market all
-- rely on it. This column lets the application record and read back the
-- caller's declared order explicitly instead of depending on insert timing.

alter table public.job_hunter_search_profile_markets
  add column position integer not null default 0;
