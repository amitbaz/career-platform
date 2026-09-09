begin;
select plan(13);

-- Existence assertions: verify constraint names exist
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

-- Uniqueness assertions: verify constraints enforce uniqueness
select col_is_unique(
  'public', 'job_hunter_evaluations', array['user_id', 'job_id', 'evaluated_at'],
  'evaluations enforces unique (user_id, job_id, evaluated_at)'
);
select col_is_unique(
  'public', 'job_hunter_materials', array['user_id', 'job_id', 'generated_at'],
  'materials enforces unique (user_id, job_id, generated_at)'
);
select col_is_unique(
  'public', 'job_hunter_deliveries', array['user_id', 'job_id', 'delivery_type', 'delivered_at'],
  'deliveries enforces unique (user_id, job_id, delivery_type, delivered_at)'
);
select col_is_unique(
  'public', 'job_hunter_ai_usage', array['user_id', 'run_id', 'model', 'purpose', 'occurred_at'],
  'ai_usage enforces unique (user_id, run_id, model, purpose, occurred_at)'
);
select col_is_unique(
  'public', 'job_hunter_search_api_usage', array['user_id', 'provider', 'occurred_at'],
  'search_api_usage enforces unique (user_id, provider, occurred_at)'
);

-- Seed test user and helper functions for behavioral tests
insert into auth.users (id, email, instance_id, aud, role, raw_app_meta_data, raw_user_meta_data, created_at, updated_at)
values ('cccccccc-0000-0000-0000-000000000003', 'test-idempotency@test.local', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', '{}', '{}', now(), now())
on conflict (id) do nothing;

create function pg_temp.authenticate_as(p_user uuid) returns void
language plpgsql as $$
begin
  execute 'reset role';
  perform set_config('request.jwt.claims',
    json_build_object('sub', p_user, 'role', 'authenticated')::text, true);
  execute 'set local role authenticated';
end $$;

-- Create a test job for use in duplicate tests. A job row is a membership of
-- a posting since #178, so the advertisement -- which is what the fingerprint
-- names -- is inserted first.
insert into public.job_hunter_postings (id, fingerprint, first_seen_at, last_seen_at)
values ('eeeeeeee-0000-0000-0000-000000000005', 'test-fingerprint', now(), now())
on conflict (id) do nothing;

insert into public.job_hunter_jobs (id, user_id, posting_id, first_seen_at, last_seen_at)
values ('dddddddd-0000-0000-0000-000000000004', 'cccccccc-0000-0000-0000-000000000003',
        'eeeeeeee-0000-0000-0000-000000000005', now(), now())
on conflict (id) do nothing;

-- Behavioral test: job_hunter_evaluations rejects duplicate
select pg_temp.authenticate_as('cccccccc-0000-0000-0000-000000000003');
insert into public.job_hunter_evaluations (user_id, job_id, evaluated_at)
values ('cccccccc-0000-0000-0000-000000000003', 'dddddddd-0000-0000-0000-000000000004', now());

select throws_ok(
  format($$
    insert into public.job_hunter_evaluations (user_id, job_id, evaluated_at)
    values ('%s', '%s', (select evaluated_at from public.job_hunter_evaluations
                         where user_id = '%s' and job_id = '%s' limit 1))
  $$, 'cccccccc-0000-0000-0000-000000000003', 'dddddddd-0000-0000-0000-000000000004', 'cccccccc-0000-0000-0000-000000000003', 'dddddddd-0000-0000-0000-000000000004'),
  '23505', null,
  'job_hunter_evaluations rejects duplicate on (user_id, job_id, evaluated_at)'
);

-- Behavioral test: job_hunter_ai_usage rejects duplicate
insert into public.job_hunter_ai_usage (user_id, run_id, model, purpose, occurred_at, status)
values ('cccccccc-0000-0000-0000-000000000003', 'test-run-id', 'gemini-test', 'job_evaluation', now(), 'success');

select throws_ok(
  format($$
    insert into public.job_hunter_ai_usage (user_id, run_id, model, purpose, occurred_at, status)
    values ('%s', '%s', '%s', '%s', (select occurred_at from public.job_hunter_ai_usage
                                      where user_id = '%s' and run_id = '%s' limit 1), 'success')
  $$, 'cccccccc-0000-0000-0000-000000000003', 'test-run-id', 'gemini-test', 'job_evaluation', 'cccccccc-0000-0000-0000-000000000003', 'test-run-id'),
  '23505', null,
  'job_hunter_ai_usage rejects duplicate on (user_id, run_id, model, purpose, occurred_at)'
);

select * from finish();
rollback;
