begin;
select plan(6);

select has_index(
  'public', 'job_hunter_evaluations', 'job_hunter_evaluations_user_job_evaluated_key',
  'evaluations has a user-scoped natural key'
);
select has_index(
  'public', 'job_hunter_materials', 'job_hunter_materials_user_job_generated_key',
  'materials has a user-scoped natural key'
);
select has_index(
  'public', 'job_hunter_deliveries', 'job_hunter_deliveries_user_job_type_at_key',
  'deliveries has a user-scoped natural key'
);
select has_index(
  'public', 'job_hunter_ai_usage', 'job_hunter_ai_usage_user_run_model_purpose_at_key',
  'ai_usage has a user-scoped natural key'
);
select has_index(
  'public', 'job_hunter_search_api_usage', 'job_hunter_search_api_usage_user_provider_at_key',
  'search_api_usage has a user-scoped natural key'
);

-- run_id is nullable; nulls do not collide in a unique index, so the
-- constraint must be built on a coalesced expression to actually bite.
select col_not_null(
  'public', 'job_hunter_ai_usage', 'run_id',
  'ai_usage.run_id is NOT NULL so the natural key cannot be defeated by nulls'
);

select * from finish();
rollback;
