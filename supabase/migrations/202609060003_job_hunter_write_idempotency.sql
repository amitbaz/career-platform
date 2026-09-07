-- Five job_hunter tables were created without a unique key. The shared
-- HttpClient retries POST on 5xx, so a transient error against any of them
-- silently writes the row twice. Give each a natural key so the retry
-- conflicts instead of duplicating, and so #70's writes can be upserts.

-- run_id is the only discriminator separating two identical AI calls in one
-- run from the same call retried. The daily workflow always passes
-- GEMINI_RUN_ID; backfill anything historical to a sentinel before the
-- NOT NULL lands.
update public.job_hunter_ai_usage set run_id = 'unknown' where run_id is null;
alter table public.job_hunter_ai_usage alter column run_id set not null;

alter table public.job_hunter_evaluations
  add constraint job_hunter_evaluations_user_job_evaluated_key
  unique (user_id, job_id, evaluated_at);

alter table public.job_hunter_materials
  add constraint job_hunter_materials_user_job_generated_key
  unique (user_id, job_id, generated_at);

alter table public.job_hunter_deliveries
  add constraint job_hunter_deliveries_user_job_type_at_key
  unique (user_id, job_id, delivery_type, delivered_at);

alter table public.job_hunter_ai_usage
  add constraint job_hunter_ai_usage_user_run_model_purpose_at_key
  unique (user_id, run_id, model, purpose, occurred_at);

alter table public.job_hunter_search_api_usage
  add constraint job_hunter_search_api_usage_user_provider_at_key
  unique (user_id, provider, occurred_at);
