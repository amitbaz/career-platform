-- Issue #287 (ADR-0002 step B): delete Gmail intake and Telegram navigation
-- from the engine, along with the tables and functions that existed only to
-- serve them.
--
-- Gmail is deleted rather than parked (owner, 2026-09-12), until its place in
-- the product is worked out. The deleting pull request records its own
-- commit in #80, which owns outcome tracking, so this is recoverable from
-- history when Gmail is designed back in.
--
-- `job_hunter_application_events` and `job_hunter_review_deliveries` look
-- like a pair, but only one of them is Gmail-specific. `application_events`
-- stays: it is threaded through the job-merge-redirect machinery (migrations
-- 202609060002 through 20260910145000) as a general "history that must
-- survive a merge" signal, on par with evaluations, materials and
-- deliveries -- `postgres_store.py`'s `save_application_event`,
-- `list_application_events` and `current_application_state` keep it alive
-- with no Gmail dependency left (`current_application_state` now derives its
-- answer locally instead of importing `gmail_matching`, which this issue
-- deletes). `job_hunter_review_deliveries` has no such life outside Gmail's
-- Telegram review-card flow -- it exists in no merge-redirect function and
-- nothing else writes or reads it -- so it goes.
--
-- `job_hunter_telegram_navigation_sessions` is not in the issue's named list
-- of Gmail tables (it never was one), but `navigation_repository.py` and
-- `telegram_navigation.py`, its only reason to exist, are deleted in this
-- same pull request, and nothing else touches it. Left in place it would be
-- exactly the kind of unowned table the Gmail tables are dropped to avoid
-- (step E's ownership tests would fail on it too), so it is dropped here for
-- the same reason.
--
-- Drop order: functions before the tables they read, since
-- `job_hunter_gmail_candidate_complete` reads `job_hunter_evaluations` (kept)
-- rather than a dropped table and would otherwise survive as a broken
-- function. No kept table carries a foreign key *to* any of the five dropped
-- tables, so nothing survives pointing at a gap; the one reference across the
-- boundary runs the other way (`job_hunter_review_deliveries.event_id` ->
-- the kept `job_hunter_application_events`), and dropping the child of a kept
-- parent is always safe. Among themselves the five reference nothing, so
-- their relative order is unconstrained.

drop function if exists public.job_hunter_pending_review_events(double precision);
drop function if exists public.job_hunter_eligible_inbound_jobs();
drop function if exists public.job_hunter_gmail_candidate_complete(uuid, text, text, text);

drop table if exists public.job_hunter_review_deliveries;
drop table if exists public.job_hunter_telegram_navigation_sessions;
drop table if exists public.job_hunter_inbound_job_candidates;
drop table if exists public.job_hunter_gmail_messages;
drop table if exists public.job_hunter_gmail_sync_state;
