-- Career Brain Release 1: career stories with typed provenance.
--
-- A career story is a real, reusable professional experience the user can
-- tell in an interview -- it is not a coach inference. Coach inferences are
-- `coach_observations`, a separate table added later in this release.
--
-- `career_stories` carries full own-row CRUD, matching the existing
-- `opportunities` pattern. `career_story_evidence` is append-oriented like
-- `opportunity_events`: normal application clients may select/insert but
-- must never update/delete a link directly, so it gets no update/delete RLS
-- policy. Story deletion may still cascade its link rows.
--
-- Story evidence requires exactly one typed source (a durable profile
-- evidence item or an interview question/answer), enforced with
-- `num_nonnulls(...) = 1` rather than trusted to application code alone.
-- The two evidence-parent foreign keys deliberately use restrictive/default
-- delete behavior -- not `on delete cascade` -- so referenced provenance
-- cannot disappear silently out from under a story; only the
-- `career_story_id` link itself cascades when its owning story is deleted.
--
-- `career_stories` carries `unique (id, user_id)` because Task 4 adds
-- `observation_evidence.career_story_id` referencing it with the same
-- ownership-preserving composite foreign key pattern used throughout this
-- release.

create table public.career_stories (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  title text not null,
  situation text,
  responsibility text,
  problem text,
  actions text,
  alternatives text,
  tradeoffs text,
  ownership text,
  outcome text,
  lessons text,
  tags jsonb not null default '[]'::jsonb,
  completeness numeric not null default 0 check (completeness between 0 and 1),
  review_state text not null default 'draft' check (review_state in ('draft', 'confirmed', 'retired')),
  confirmed_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (id, user_id)
);

create index career_stories_user_updated_idx on public.career_stories (user_id, updated_at desc);

alter table public.career_stories enable row level security;

create policy select_own on public.career_stories for select using (auth.uid() = user_id);
create policy insert_own on public.career_stories for insert with check (auth.uid() = user_id);
create policy update_own on public.career_stories for update using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy delete_own on public.career_stories for delete using (auth.uid() = user_id);

create table public.career_story_evidence (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  career_story_id uuid not null,
  profile_evidence_id uuid,
  interview_question_id uuid,
  note text,
  created_at timestamptz not null default now(),
  check (num_nonnulls(profile_evidence_id, interview_question_id) = 1),
  foreign key (career_story_id, user_id)
    references public.career_stories (id, user_id) on delete cascade,
  foreign key (profile_evidence_id, user_id)
    references public.profile_evidence (id, user_id),
  foreign key (interview_question_id, user_id)
    references public.interview_questions (id, user_id)
);

create index career_story_evidence_story_idx on public.career_story_evidence (career_story_id, created_at desc);
create index career_story_evidence_user_idx on public.career_story_evidence (user_id, created_at desc);

alter table public.career_story_evidence enable row level security;

-- Append-oriented: own-row select/insert only. No update/delete policy
-- exists, so normal authenticated clients cannot mutate a provenance link
-- directly; it is only ever removed via the parent story's delete cascade.
create policy select_own on public.career_story_evidence for select using (auth.uid() = user_id);
create policy insert_own on public.career_story_evidence for insert with check (auth.uid() = user_id);
