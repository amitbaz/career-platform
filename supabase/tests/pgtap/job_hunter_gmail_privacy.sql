-- Job Hunter reads the user's Gmail inbox but must only ever persist
-- metadata and derived judgements about a message or extraction -- never
-- the raw email body, and never a raw LLM prompt/response that could carry
-- quoted email text. This file pins the exact column list of every table
-- that could be tempted to grow such a column. Any future column added to
-- one of these tables fails this suite until a human consciously adds it
-- to the list below and, in doing so, re-affirms the privacy decision.
--
-- Column lists are taken from the CREATE TABLE statements in
-- supabase/migrations/202609060002_job_hunter_discovery_state.sql (no
-- later migration alters these tables' columns).

begin;
select plan(4);

select columns_are('public', 'job_hunter_gmail_messages', array[
  'id',
  'user_id',
  'message_id',
  'thread_id',
  'sender',
  'subject',
  'occurred_at',
  'classification',
  'confidence',
  'rationale',
  'processed_at',
  'created_at'
], 'job_hunter_gmail_messages carries no body/email_body column');

select columns_are('public', 'job_hunter_inbound_job_candidates', array[
  'id',
  'user_id',
  'origin',
  'source_message_id',
  'source_candidate_key',
  'source_platform',
  'source_job_id',
  'url',
  'company',
  'title',
  'location',
  'remote',
  'description',
  'last_seen_at',
  'created_at'
], 'job_hunter_inbound_job_candidates carries no body/email_body column');

select columns_are('public', 'job_hunter_application_events', array[
  'id',
  'user_id',
  'job_id',
  'event_type',
  'occurred_at',
  'source',
  'source_message_id',
  'source_thread_id',
  'confidence',
  'company',
  'role_title',
  'rationale',
  'created_at'
], 'job_hunter_application_events carries no body/email_body column');

select columns_are('public', 'job_hunter_ai_usage', array[
  'id',
  'user_id',
  'provider',
  'occurred_at',
  'run_id',
  'model',
  'purpose',
  'status',
  'estimated_input_tokens',
  'prompt_tokens',
  'output_tokens',
  'thinking_tokens',
  'cached_tokens',
  'total_tokens',
  'http_status',
  'error_code',
  'created_at'
], 'job_hunter_ai_usage carries no prompt/response column');

select * from finish();
rollback;
