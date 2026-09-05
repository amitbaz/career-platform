-- Runtime verification of 202608310001_planned_practice_sessions.sql.
--
-- Ran green against a local Supabase stack on 2026-09-01, before the
-- migration was pushed to the hosted project. Reproduce with:
--
--   supabase start && supabase db reset
--   psql "postgresql://postgres:postgres@127.0.0.1:54322/postgres" \
--     -v ON_ERROR_STOP=1 -f supabase/tests/202608310001_planned_practice_sessions.verify.sql
--
-- Run against a DISPOSABLE database (a local `supabase start` stack, or a
-- throwaway project). It creates two auth users and seeds rows for them.
-- Every check RAISEs on failure, so a clean run that prints only NOTICE
-- lines and ends with "ALL CHECKS PASSED" means the migration holds.
--
-- Nothing is left behind: the whole script runs in one transaction that
-- ends in ROLLBACK.

\set ON_ERROR_STOP on

begin;

do $verify$
declare
  v_owner uuid := gen_random_uuid();
  v_other uuid := gen_random_uuid();
  v_plan uuid;
  v_plan_b uuid;
  v_plan_c uuid;
  v_plan_hands_on uuid;
  v_other_plan uuid;
  v_opportunity uuid;
  v_second_opportunity uuid;
  v_other_opportunity uuid;
begin
  -- Seed two users. auth.users has many defaulted columns; id and email are enough.
  insert into auth.users (id, instance_id, aud, role, email, encrypted_password, created_at, updated_at)
  values
    (v_owner, '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 'owner@example.test', '', now(), now()),
    (v_other, '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', 'other@example.test', '', now(), now());

  insert into public.opportunities (user_id, company, role, status)
  values (v_owner, 'Northwind', 'Staff Engineer', 'interviewing') returning id into v_opportunity;
  insert into public.opportunities (user_id, company, role, status)
  values (v_owner, 'Contoso', 'Staff Engineer', 'applied') returning id into v_second_opportunity;
  insert into public.opportunities (user_id, company, role, status)
  values (v_other, 'Fabrikam', 'Staff Engineer', 'applied') returning id into v_other_opportunity;

  insert into public.practice_plans (user_id, status, primary_focus, format)
  values (v_owner, 'ready', 'Trade-off narration', 'targeted_drill') returning id into v_plan;
  insert into public.practice_plans (user_id, status, primary_focus, format)
  values (v_owner, 'ready', 'One question', 'self_presentation') returning id into v_plan_b;
  insert into public.practice_plans (user_id, status, primary_focus, format)
  values (v_owner, 'ready', 'Five questions', 'full_simulation') returning id into v_plan_c;
  insert into public.practice_plans (user_id, status, primary_focus, format)
  values (v_owner, 'ready', 'Hands on', 'hands_on') returning id into v_plan_hands_on;
  insert into public.practice_plans (user_id, status, primary_focus, format)
  values (v_other, 'ready', 'Not yours', 'targeted_drill') returning id into v_other_plan;

  insert into public.practice_plan_opportunities (user_id, practice_plan_id, opportunity_id, relevance)
  values (v_owner, v_plan, v_opportunity, 'primary');
  insert into public.practice_plan_opportunities (user_id, practice_plan_id, opportunity_id, relevance)
  values (v_owner, v_plan_hands_on, v_opportunity, 'primary');

  raise notice 'seeded owner=% other=%', v_owner, v_other;
end
$verify$;

-- Everything below runs AS THE OWNER, through the same role and claims path
-- PostgREST uses, so `auth.uid()`, RLS, and the `authenticated` grants are all
-- exercised rather than bypassed.

create or replace function pg_temp.blueprint(p_questions integer, p_status text default 'grounded')
returns jsonb language sql as $$
  select jsonb_build_object(
    'status', p_status,
    'fallback_reason', null,
    'max_follow_ups', 3,
    'max_questions', 8,
    'questions', coalesce(
      (select jsonb_agg(jsonb_build_object(
        'sequence', sequence,
        'category', 'experience',
        'competency_id', null,
        'competency_name', 'Architecture',
        'difficulty', 'senior',
        'prompt', 'Prompt ' || sequence,
        'objective', 'Objective ' || sequence,
        'evidence_ids', '["evidence-1"]'::jsonb,
        'expected_signals', '["ownership"]'::jsonb,
        'missing_signal_prompts', '["What was the constraint?"]'::jsonb,
        'rubric_criteria', '["States the trade-off"]'::jsonb,
        'follow_up_limit', 1,
        'source_confidence', 0.8
      ) order by sequence)
       from generate_series(1, p_questions) as sequence),
      '[]'::jsonb)
  );
$$;

do $verify$
declare
  v_owner uuid;
  v_other uuid;
  v_plan uuid;
  v_plan_b uuid;
  v_plan_c uuid;
  v_plan_hands_on uuid;
  v_other_plan uuid;
  v_opportunity uuid;
  v_second_opportunity uuid;
  v_other_opportunity uuid;
  v_session uuid;
  v_status text;
  v_count integer;
begin
  select id into v_owner from auth.users where email = 'owner@example.test';
  select id into v_other from auth.users where email = 'other@example.test';
  select id into v_opportunity from public.opportunities where user_id = v_owner and company = 'Northwind';
  select id into v_second_opportunity from public.opportunities where user_id = v_owner and company = 'Contoso';
  select id into v_other_opportunity from public.opportunities where user_id = v_other;
  select id into v_plan from public.practice_plans where user_id = v_owner and format = 'targeted_drill';
  select id into v_plan_b from public.practice_plans where user_id = v_owner and format = 'self_presentation';
  select id into v_plan_c from public.practice_plans where user_id = v_owner and format = 'full_simulation';
  select id into v_plan_hands_on from public.practice_plans where user_id = v_owner and format = 'hands_on';
  select id into v_other_plan from public.practice_plans where user_id = v_other;

  -- Impersonate the owner exactly as PostgREST does.
  perform set_config('request.jwt.claims', json_build_object('sub', v_owner, 'role', 'authenticated')::text, true);
  execute 'set local role authenticated';

  ---------------------------------------------------------------- CHECK 1
  -- The OLD generic RPC still refuses anything but its five-question backbone.
  begin
    perform public.create_conversation_session_with_blueprint(pg_temp.blueprint(3));
    raise exception 'CHECK 1 FAILED: the generic RPC accepted a three-question blueprint';
  exception when others then
    if sqlerrm like 'CHECK 1 FAILED%' then raise; end if;
    raise notice 'CHECK 1 ok: generic RPC still rejects 3 questions (% / %)', sqlstate, sqlerrm;
  end;

  ---------------------------------------------------------------- CHECK 2
  -- The NEW RPC accepts 1, 3, and 5 base questions.
  select session_id into v_session
  from public.create_planned_conversation_session_with_blueprint(pg_temp.blueprint(1), v_plan_b, null);
  if v_session is null then raise exception 'CHECK 2 FAILED: 1 question was rejected'; end if;

  select session_id into v_session
  from public.create_planned_conversation_session_with_blueprint(pg_temp.blueprint(5), v_plan_c, null);
  if v_session is null then raise exception 'CHECK 2 FAILED: 5 questions were rejected'; end if;

  select session_id into v_session
  from public.create_planned_conversation_session_with_blueprint(pg_temp.blueprint(3), v_plan, v_opportunity);
  if v_session is null then raise exception 'CHECK 2 FAILED: 3 questions were rejected'; end if;
  raise notice 'CHECK 2 ok: 1, 3, and 5 base questions all accepted';

  ---------------------------------------------------------------- CHECK 3
  -- The payload key names the JS repository sends actually land in columns.
  -- This is the one gap static review could not close: a rename on either
  -- side would silently null these out rather than error.
  select count(*) into v_count
  from public.interview_questions
  where session_id = v_session
    and prompt is not null and objective is not null
    and competency_name = 'Architecture'
    and difficulty = 'senior'
    and jsonb_array_length(evidence_ids) = 1
    and jsonb_array_length(expected_signals) = 1
    and jsonb_array_length(missing_signal_prompts) = 1
    and jsonb_array_length(rubric_criteria) = 1
    and follow_up_limit = 1
    and source_confidence = 0.8;
  if v_count <> 3 then
    raise exception 'CHECK 3 FAILED: only % of 3 questions hydrated every p_blueprint field -- a payload key does not match the SQL field list', v_count;
  end if;
  raise notice 'CHECK 3 ok: every p_blueprint question key maps to a persisted column';

  ---------------------------------------------------------------- CHECK 4
  -- The start is atomic: session context set AND the plan flipped to started.
  select status into v_status from public.practice_plans where id = v_plan;
  if v_status <> 'started' then raise exception 'CHECK 4 FAILED: plan status is %, expected started', v_status; end if;

  select count(*) into v_count from public.interview_sessions
  where id = v_session and practice_plan_id = v_plan and opportunity_id = v_opportunity
    and kind = 'conversation' and status = 'active';
  if v_count <> 1 then raise exception 'CHECK 4 FAILED: the session did not carry its plan and opportunity context'; end if;
  raise notice 'CHECK 4 ok: session context and plan status move together';

  ---------------------------------------------------------------- CHECK 5
  -- A second start of the same plan fails (the plan is no longer `ready`).
  begin
    perform public.create_planned_conversation_session_with_blueprint(pg_temp.blueprint(3), v_plan, v_opportunity);
    raise exception 'CHECK 5 FAILED: the same plan started twice';
  exception when others then
    if sqlerrm like 'CHECK 5 FAILED%' then raise; end if;
    raise notice 'CHECK 5 ok: a second start is refused (% / %)', sqlstate, sqlerrm;
  end;

  ---------------------------------------------------------------- CHECK 6
  -- 0 and 6 base questions are both refused.
  begin
    perform public.create_planned_conversation_session_with_blueprint(pg_temp.blueprint(0), v_plan_b, null);
    raise exception 'CHECK 6 FAILED: an empty blueprint was accepted';
  exception when others then
    if sqlerrm like 'CHECK 6 FAILED%' then raise; end if;
    raise notice 'CHECK 6a ok: 0 questions refused (%)', sqlstate;
  end;
  begin
    perform public.create_planned_conversation_session_with_blueprint(pg_temp.blueprint(6), v_plan_b, null);
    raise exception 'CHECK 6 FAILED: a six-question blueprint was accepted';
  exception when others then
    if sqlerrm like 'CHECK 6 FAILED%' then raise; end if;
    raise notice 'CHECK 6b ok: 6 questions refused (%)', sqlstate;
  end;

  ---------------------------------------------------------------- CHECK 7
  -- Cross-user: another user's plan, and another user's opportunity.
  begin
    perform public.create_planned_conversation_session_with_blueprint(pg_temp.blueprint(3), v_other_plan, null);
    raise exception 'CHECK 7 FAILED: started another user''s plan';
  exception when others then
    if sqlerrm like 'CHECK 7 FAILED%' then raise; end if;
    raise notice 'CHECK 7a ok: another user''s plan is refused (%)', sqlstate;
  end;
  begin
    perform public.create_planned_conversation_session_with_blueprint(pg_temp.blueprint(3), v_plan_hands_on, v_other_opportunity);
    raise exception 'CHECK 7 FAILED: attached another user''s opportunity';
  exception when others then
    if sqlerrm like 'CHECK 7 FAILED%' then raise; end if;
    raise notice 'CHECK 7b ok: another user''s opportunity is refused (%)', sqlstate;
  end;

  ---------------------------------------------------------------- CHECK 8
  -- An owned but UNLINKED opportunity is refused, and a `primary` link is honored.
  begin
    perform public.create_planned_conversation_session_with_blueprint(pg_temp.blueprint(3), v_plan_hands_on, v_second_opportunity);
    raise exception 'CHECK 8 FAILED: an opportunity not linked to the plan was accepted';
  exception when others then
    if sqlerrm like 'CHECK 8 FAILED%' then raise; end if;
    raise notice 'CHECK 8 ok: an unlinked/non-primary opportunity is refused (%)', sqlstate;
  end;

  ---------------------------------------------------------------- CHECK 9
  -- The hands-on RPC enforces the same ownership and atomicity rules.
  begin
    perform public.start_hands_on_practice_session(v_other_plan, null, '{}'::jsonb);
    raise exception 'CHECK 9 FAILED: hands-on started another user''s plan';
  exception when others then
    if sqlerrm like 'CHECK 9 FAILED%' then raise; end if;
    raise notice 'CHECK 9a ok: hands-on refuses another user''s plan (%)', sqlstate;
  end;
  begin
    perform public.start_hands_on_practice_session(v_plan_hands_on, v_other_opportunity, '{"title":"x"}'::jsonb);
    raise exception 'CHECK 9 FAILED: hands-on attached another user''s opportunity';
  exception when others then
    if sqlerrm like 'CHECK 9 FAILED%' then raise; end if;
    raise notice 'CHECK 9b ok: hands-on refuses another user''s opportunity (%)', sqlstate;
  end;

  select session_id into v_session
  from public.start_hands_on_practice_session(v_plan_hands_on, v_opportunity, '{"title":"Debounced search"}'::jsonb);
  select status into v_status from public.practice_plans where id = v_plan_hands_on;
  if v_status <> 'started' then raise exception 'CHECK 9 FAILED: hands-on left the plan at %', v_status; end if;
  select count(*) into v_count from public.interview_sessions
  where id = v_session and kind = 'hands-on' and practice_plan_id = v_plan_hands_on
    and opportunity_id = v_opportunity and exercise ->> 'title' = 'Debounced search';
  if v_count <> 1 then raise exception 'CHECK 9 FAILED: the hands-on session lost its exercise or its context'; end if;
  raise notice 'CHECK 9c ok: hands-on start is atomic and keeps its exercise';

  ---------------------------------------------------------------- CHECK 10
  -- Legacy sessions with no plan/opportunity still read back.
  insert into public.interview_sessions (user_id, kind, status)
  values (v_owner, 'conversation', 'active') returning id into v_session;
  select count(*) into v_count from public.interview_sessions
  where id = v_session and practice_plan_id is null and opportunity_id is null;
  if v_count <> 1 then raise exception 'CHECK 10 FAILED: a legacy session without plan context did not hydrate'; end if;
  raise notice 'CHECK 10 ok: legacy sessions without plan context still hydrate';

  ---------------------------------------------------------------- CHECK 11
  -- The widened ceiling still has a floor and a roof.
  begin
    insert into public.interview_sessions (user_id, kind, status, blueprint_max_questions)
    values (v_owner, 'conversation', 'active', 0);
    raise exception 'CHECK 11 FAILED: blueprint_max_questions accepted 0';
  exception when others then
    if sqlerrm like 'CHECK 11 FAILED%' then raise; end if;
    raise notice 'CHECK 11a ok: blueprint_max_questions still rejects 0 (%)', sqlstate;
  end;
  begin
    insert into public.interview_sessions (user_id, kind, status, blueprint_max_questions)
    values (v_owner, 'conversation', 'active', 9);
    raise exception 'CHECK 11 FAILED: blueprint_max_questions accepted 9';
  exception when others then
    if sqlerrm like 'CHECK 11 FAILED%' then raise; end if;
    raise notice 'CHECK 11b ok: blueprint_max_questions still rejects 9 (%)', sqlstate;
  end;
  insert into public.interview_sessions (user_id, kind, status, blueprint_max_questions)
  values (v_owner, 'conversation', 'active', 1);
  raise notice 'CHECK 11c ok: blueprint_max_questions now accepts 1';

  execute 'reset role';
  raise notice 'ALL CHECKS PASSED';
end
$verify$;

rollback;
