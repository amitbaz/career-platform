-- Career Brain Release 1: coach observations with evidence-backed review state.
--
-- A coach observation is a persistent, inspectable inference about the user
-- (e.g. "you skip tradeoffs"). Release 1 stores observations but never
-- generates, infers, or reconciles them -- that is a later release. The
-- single most important property is auditability: the original AI claim in
-- `claim` is preserved forever and is never overwritten, and a user's
-- confirmation/correction/dismissal is recorded alongside it via
-- `review_state` and the `*_at` timestamps in `coach_observations`, never
-- over it. `coach_observations` carries full own-row CRUD, matching the
-- existing `career_stories`/`opportunities` pattern.
--
-- `observation_evidence` is append-oriented like `career_story_evidence`
-- and `opportunity_events`: normal application clients may select/insert
-- but must never update/delete a link directly, so it gets no update/delete
-- RLS policy. It requires exactly one of four typed sources
-- (`profile_evidence`, `question_evaluation`, `career_story`,
-- `opportunity_event`), enforced with `num_nonnulls(...) = 1` rather than
-- trusted to application code alone. Those four evidence-parent foreign
-- keys deliberately use restrictive/default delete behavior -- not
-- `on delete cascade` -- so referenced provenance cannot disappear silently
-- out from under an observation; only the `observation_id` link itself
-- cascades when its owning observation is deleted. An inactive
-- `profile_evidence` row remains a valid, referenceable source: inactive
-- means historical, not invalid.
--
-- `question_evaluations` predates the `(id, user_id)` ownership-key
-- pattern this release uses for composite foreign keys, so it is given
-- that uniqueness constraint here, before it is referenced.

alter table public.question_evaluations
  add constraint question_evaluations_id_user_key unique (id, user_id);

create table public.coach_observations (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  observation_type text not null check (observation_type in (
    'strength', 'weakness', 'answer_habit', 'knowledge_gap',
    'story_gap', 'story_strength', 'delivery_pattern', 'other'
  )),
  claim text not null,
  confidence numeric not null default 0 check (confidence between 0 and 1),
  importance numeric not null default 0 check (importance between 0 and 1),
  trend text not null default 'unresolved' check (trend in ('unresolved', 'improving', 'stable', 'worsening')),
  review_state text not null default 'unreviewed' check (review_state in ('unreviewed', 'confirmed', 'corrected', 'dismissed')),
  user_correction text,
  first_seen_at timestamptz,
  last_seen_at timestamptz,
  confirmed_at timestamptz,
  corrected_at timestamptz,
  dismissed_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (id, user_id)
);

create index coach_observations_user_updated_idx on public.coach_observations (user_id, updated_at desc);

alter table public.coach_observations enable row level security;

create policy select_own on public.coach_observations for select using (auth.uid() = user_id);
create policy insert_own on public.coach_observations for insert with check (auth.uid() = user_id);
create policy update_own on public.coach_observations for update using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy delete_own on public.coach_observations for delete using (auth.uid() = user_id);

create table public.observation_evidence (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  observation_id uuid not null,
  profile_evidence_id uuid,
  question_evaluation_id uuid,
  career_story_id uuid,
  opportunity_event_id uuid,
  evidence_role text not null default 'supporting' check (evidence_role in ('supporting', 'contradicting', 'context')),
  weight numeric not null default 1 check (weight between 0 and 1),
  reason text,
  created_at timestamptz not null default now(),
  check (num_nonnulls(
    profile_evidence_id,
    question_evaluation_id,
    career_story_id,
    opportunity_event_id
  ) = 1),
  foreign key (observation_id, user_id)
    references public.coach_observations (id, user_id) on delete cascade,
  foreign key (profile_evidence_id, user_id)
    references public.profile_evidence (id, user_id),
  foreign key (question_evaluation_id, user_id)
    references public.question_evaluations (id, user_id),
  foreign key (career_story_id, user_id)
    references public.career_stories (id, user_id),
  foreign key (opportunity_event_id, user_id)
    references public.opportunity_events (id, user_id)
);

create index observation_evidence_observation_idx on public.observation_evidence (observation_id, created_at desc);
create index observation_evidence_user_idx on public.observation_evidence (user_id, created_at desc);

alter table public.observation_evidence enable row level security;

-- Append-oriented: own-row select/insert only. No update/delete policy
-- exists, so normal authenticated clients cannot mutate a provenance link
-- directly; it is only ever removed via the parent observation's delete
-- cascade.
create policy select_own on public.observation_evidence for select using (auth.uid() = user_id);
create policy insert_own on public.observation_evidence for insert with check (auth.uid() = user_id);
