-- 20260908090000_job_hunter_search_profile_delivery_policy.sql
-- Delivery policy on the search profile (issue #117): how many job offers a day
-- the digest may deliver, and the match score below which an offer is withheld.
-- The reasoning behind the allowed values lives with the fields themselves, in
-- apps/job-hunter/src/job_hunter/search_profile.py; these checks mirror that
-- validation rather than replacing it, as the market policy checks already do.
--
-- Both columns carry defaults, so profiles saved before this migration keep
-- working without a data migration: an existing row reads back as 10 and 80.

alter table public.job_hunter_search_profiles
  add column daily_offer_limit integer not null default 10,
  add column match_score_floor integer not null default 80;

alter table public.job_hunter_search_profiles
  add constraint job_hunter_search_profiles_daily_offer_limit_check
    check (daily_offer_limit in (5, 10, 20)),
  add constraint job_hunter_search_profiles_match_score_floor_check
    check (match_score_floor between 50 and 95);
