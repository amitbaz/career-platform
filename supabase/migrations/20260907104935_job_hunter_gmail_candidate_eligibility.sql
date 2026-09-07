-- Gmail candidates must survive materialization until their logical job reaches
-- a terminal state or has a current successful evaluation. The old function
-- treated materialization as terminal and therefore lost jobs that missed one
-- shortlist before Gemini could evaluate them.

create function public.job_hunter_gmail_candidate_complete(
  p_job_id uuid, p_status text, p_description_hash text, p_content_confidence text
) returns boolean
language sql
security invoker
set search_path = ''
stable
as $$
  select p_status in ('rejected', 'closed')
      or exists (
        select 1
          from (
            select e.status, e.description_hash_at_eval, e.content_confidence_at_eval
              from public.job_hunter_evaluations e
             where e.job_id = p_job_id
             order by e.evaluated_at desc, e.created_at desc, e.id desc
             limit 1
          ) latest
         where latest.status <> 'failed'
           and latest.description_hash_at_eval = p_description_hash
           and latest.content_confidence_at_eval = p_content_confidence
      );
$$;

drop function public.job_hunter_unmaterialized_inbound_jobs();

create function public.job_hunter_eligible_inbound_jobs()
returns setof jsonb
language sql
security invoker
set search_path = ''
as $$
  with cand as materialized (
    select c.*,
           'gmail:' || c.source_platform as match_source,
           public.job_hunter_canonicalize_url(c.url) as match_canonical_url,
           public.job_hunter_normalize_text(c.company) || '|' ||
           public.job_hunter_normalize_text(c.title) || '|' ||
           public.job_hunter_normalize_text(c.location) as match_identity
      from public.job_hunter_inbound_job_candidates c
     where c.user_id = (select auth.uid())
       and c.last_seen_at >= now() - interval '14 days'
  )
  select to_jsonb(cand) - 'match_source' - 'match_canonical_url' - 'match_identity'
    from cand
   where not exists (
           select 1 from public.job_hunter_jobs j
            where j.user_id = (select auth.uid())
              and j.source = cand.match_source
              and j.source_job_id = cand.source_candidate_key
              and public.job_hunter_gmail_candidate_complete(j.id, j.status, j.description_hash, j.content_confidence)
         )
     and not exists (
           select 1 from public.job_hunter_jobs j
            where j.user_id = (select auth.uid())
              and cand.url <> '' and j.url <> ''
              and j.canonical_url_of_url = cand.match_canonical_url
              and public.job_hunter_gmail_candidate_complete(j.id, j.status, j.description_hash, j.content_confidence)
         )
     and not exists (
           select 1 from public.job_hunter_jobs j
            where j.user_id = (select auth.uid())
              and cand.match_identity <> '||'
              and j.normalized_identity = cand.match_identity
              and public.job_hunter_gmail_candidate_complete(j.id, j.status, j.description_hash, j.content_confidence)
         )
   order by cand.created_at, cand.id;
$$;
