-- Recompile the conversation-turn RPC with qualified session_id filters.
-- The original function's RETURNS TABLE(session_id ...) output variable made
-- unqualified filters ambiguous at runtime (Postgres error 42702).
do $$
declare
  definition text;
begin
  select pg_get_functiondef(p.oid)
    into definition
  from pg_proc p
  join pg_namespace n on n.oid = p.pronamespace
  where n.nspname = 'public'
    and p.proname = 'record_conversation_turn'
    and pg_get_function_identity_arguments(p.oid) = 'p_question_id uuid, p_answer text, p_score numeric, p_dimensions jsonb, p_strengths jsonb, p_needs_work jsonb, p_missing_points jsonb, p_better_structure jsonb, p_improved_answer text, p_next_question_id uuid, p_next_prompt text, p_follow_up jsonb';

  if definition is null then
    raise exception 'record_conversation_turn function was not found';
  end if;

  definition := replace(
    definition,
    'where session_id = v_question.session_id',
    'where public.interview_questions.session_id = v_question.session_id'
  );
  execute definition;
end;
$$;
