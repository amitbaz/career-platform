update public.interview_sessions s
set blueprint_status = 'limited-grounding',
    blueprint_fallback_reason = coalesce(
      nullif(trim(s.blueprint_fallback_reason), ''),
      'Legacy session created before grounded blueprints were persisted.'
    ),
    updated_at = now()
where s.kind = 'conversation'
  and s.blueprint_status = 'grounded'
  and not exists (
    select 1
    from public.interview_questions q
    where q.session_id = s.id
      and q.user_id = s.user_id
      and nullif(trim(q.objective), '') is not null
  );
