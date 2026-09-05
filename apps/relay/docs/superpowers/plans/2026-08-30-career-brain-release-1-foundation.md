# Career Brain Release 1 Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the durable, user-isolated Career Brain persistence foundation while keeping all existing Relay behavior and the external job-hunter workflow unchanged.

**Architecture:** Extend the current Supabase schema additively with normalized Career Brain entities and typed provenance. Keep all browser-facing behavior unchanged; expose the new model through small `server-only` repository modules. Make profile evidence IDs stable before anything references them, and protect opportunity lifecycle changes with narrow transactional SQL functions.

**Tech Stack:** Next.js 16.3.3, TypeScript 5, Supabase Postgres/Auth/RLS, `@supabase/supabase-js` 2.x, Vitest 4.1.11, Node `crypto`.

**Spec:** `docs/superpowers/specs/2026-08-30-career-brain-release-1-foundation-design.md`

## Global Constraints

- Release 1 is backend-focused and additive; no recommended-practice/dashboard UI work.
- The new Career Brain schema may be empty and every existing Relay profile/interview flow must still work.
- `opportunities` is the canonical domain table for both considering and applied/interviewing jobs.
- Every new durable record is user-scoped; cross-table references must preserve same-user ownership with composite `(id, user_id)` foreign keys where applicable.
- Profile evidence must stop using destructive delete/recreate semantics before stories or observations may reference it.
- Coach observations are stored but are not automatically generated or reconciled in Release 1.
- The Release 3 recommendation/prioritization algorithm is not implemented here.
- No Google Sheets import/synchronization is implemented here.
- No job-hunter code, workflow, secrets, SQLite state, or Telegram behavior is changed.
- No service-role credential is exposed to browser code.
- Use the existing `RepositoryError` pattern for persistence failures.
- Historical `opportunity_events`, `career_story_evidence`, and `observation_evidence` are append-oriented: normal application clients may select/insert them but may not update/delete them directly; parent deletion may clean them through foreign-key cascades where specified.
- Follow red → green → refactor for each task and keep commits scoped to the task.
- Run migrations only against a disposable/local or explicitly designated development Supabase target during implementation. Do not use production as the migration test environment.

## File Structure

Files created by this release:

```text
supabase/migrations/
  202608300001_stable_profile_evidence.sql
  202608300002_opportunities.sql
  202608300003_career_stories.sql
  202608300004_coach_observations.sql
  202608300005_practice_plans.sql
  202608300006_session_career_context.sql

src/lib/repositories/
  opportunities.ts
  opportunities.test.ts
  stories.ts
  stories.test.ts
  observations.ts
  observations.test.ts
  practice-plans.ts
  practice-plans.test.ts
```

Existing files modified:

```text
src/lib/types.ts
src/lib/repositories/profile.ts
src/lib/repositories/profile.test.ts
src/lib/repositories/interviews.ts
src/lib/repositories/interviews.test.ts
README.md
```

Responsibilities:

- `profile.ts` owns stable profile-evidence fingerprints and active-evidence hydration.
- `opportunities.ts` owns opportunity CRUD, lifecycle RPC calls, and event hydration.
- `stories.ts` owns career-story persistence and typed source provenance.
- `observations.ts` owns coach-observation persistence, review state, and typed evidence links.
- `practice-plans.ts` owns explicit practice-plan persistence and opportunity relationships.
- `interviews.ts` only gains nullable Career Brain context mapping/linking; existing interview/evaluation behavior remains intact.
- SQL migrations own relational constraints, RLS, indexes, append-only access boundaries, and transactional invariants.

---

### Task 1: Make profile evidence identity durable

**Files:**
- Create: `supabase/migrations/202608300001_stable_profile_evidence.sql`
- Modify: `src/lib/repositories/profile.ts`
- Modify: `src/lib/repositories/profile.test.ts`

**Interfaces:**
- Produces: `export function evidenceKeyFor(item: EvidenceItem): string`
- Produces: `profile_evidence.evidence_key text not null`
- Produces: `profile_evidence.is_active boolean not null default true`
- Produces: `profile_evidence.retired_at timestamptz`
- Changes: `save_profile_bundle(...)` reconciles evidence by stable key instead of deleting rows.
- Changes: `getProfile(...)` returns only active `profile_evidence` rows.

- [ ] **Step 1: Write failing unit tests for deterministic evidence keys**

Add tests to `src/lib/repositories/profile.test.ts` that import `evidenceKeyFor` and assert equivalent source-backed evidence produces the same key while materially changed factual evidence produces a different key.

```ts
it("creates a stable evidence key that ignores confidence and temporary ids", () => {
  const base: EvidenceItem = {
    id: "temporary-1",
    sourceKind: "cv",
    sourceExcerpt: "Led a React migration for checkout.",
    projectOrEmployer: "Checkout Platform",
    ownership: "Owned the frontend migration end to end.",
    technologies: ["React", "TypeScript"],
    decision: "Split a large route into smaller bundles.",
    constraint: "Tight launch window.",
    outcome: "Cut bundle size by 28%.",
    recency: "2025-02",
    confidence: 0.94,
  };

  expect(evidenceKeyFor(base)).toBe(evidenceKeyFor({
    ...base,
    id: "temporary-99",
    confidence: 0.61,
  }));
  expect(evidenceKeyFor(base)).not.toBe(evidenceKeyFor({
    ...base,
    outcome: "Cut bundle size by 35%.",
  }));
});
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
npm test -- src/lib/repositories/profile.test.ts
```

Expected: FAIL because `evidenceKeyFor` is not exported/implemented and `saveProfile` does not send `evidence_key`.

- [ ] **Step 3: Implement canonical hashing in `profile.ts`**

Add a Node crypto import and canonicalization that excludes `id` and `confidence`.

```ts
import { createHash } from "node:crypto";

function normalizedEvidenceText(value: string | null | undefined): string {
  return (value ?? "").trim().replace(/\s+/g, " ").toLowerCase();
}

export function evidenceKeyFor(item: EvidenceItem): string {
  const canonical = JSON.stringify({
    sourceKind: item.sourceKind,
    sourceExcerpt: normalizedEvidenceText(item.sourceExcerpt),
    projectOrEmployer: normalizedEvidenceText(item.projectOrEmployer),
    ownership: normalizedEvidenceText(item.ownership),
    technologies: [...item.technologies].map(normalizedEvidenceText).sort(),
    decision: normalizedEvidenceText(item.decision),
    constraint: normalizedEvidenceText(item.constraint),
    outcome: normalizedEvidenceText(item.outcome),
    recency: normalizedEvidenceText(item.recency),
  });
  return createHash("sha256").update(canonical).digest("hex");
}
```

Update the `p_evidence` mapping in `saveProfile` to include:

```ts
evidence_key: evidenceKeyFor(item),
```

- [ ] **Step 4: Update profile repository tests for active evidence**

Change the profile-evidence Supabase mock chain so `getProfile` must call `.eq("is_active", true)` before ordering. Add an assertion that inactive rows are not part of current profile hydration.

Representative expectation:

```ts
expect(evidenceQuery.eq).toHaveBeenCalledWith("user_id", "user-1");
expect(evidenceQuery.eq).toHaveBeenCalledWith("is_active", true);
```

Also change the save assertion to require:

```ts
expect((calls[0].payload.p_evidence as Array<Record<string, unknown>>)[0]).toMatchObject({
  evidence_key: expect.stringMatching(/^[0-9a-f]{64}$/),
  source_excerpt: "Led a React migration for checkout.",
});
```

- [ ] **Step 5: Create the profile-evidence migration**

`202608300001_stable_profile_evidence.sql` must:

1. add `evidence_key`, `is_active`, and `retired_at`;
2. backfill `evidence_key = 'legacy:' || id::text` for existing rows;
3. make `evidence_key` `not null`;
4. add `unique (user_id, evidence_key)`;
5. replace `save_profile_bundle` with the same existing profile/source/competency behavior but evidence reconciliation instead of delete/reinsert.

The evidence portion of the replacement RPC must follow this shape:

```sql
update public.profile_evidence
set is_active = false,
    retired_at = coalesce(retired_at, now()),
    updated_at = now()
where user_id = v_user_id
  and is_active = true;

for v_evidence in select value from jsonb_array_elements(coalesce(p_evidence, '[]'::jsonb))
loop
  insert into public.profile_evidence (
    user_id,
    evidence_key,
    source_kind,
    source_excerpt,
    project_or_employer,
    ownership,
    technologies,
    decision,
    constraint_text,
    outcome,
    recency,
    confidence,
    is_active,
    retired_at,
    updated_at
  ) values (
    v_user_id,
    trim(v_evidence ->> 'evidence_key'),
    nullif(trim(v_evidence ->> 'source_kind'), ''),
    trim(coalesce(v_evidence ->> 'source_excerpt', '')),
    nullif(trim(v_evidence ->> 'project_or_employer'), ''),
    nullif(trim(v_evidence ->> 'ownership'), ''),
    coalesce(v_evidence -> 'technologies', '[]'::jsonb),
    nullif(trim(v_evidence ->> 'decision'), ''),
    nullif(trim(v_evidence ->> 'constraint'), ''),
    nullif(trim(v_evidence ->> 'outcome'), ''),
    nullif(trim(v_evidence ->> 'recency'), ''),
    greatest(0::numeric, least(1::numeric, coalesce((v_evidence ->> 'confidence')::numeric, 0))),
    true,
    null,
    now()
  )
  on conflict (user_id, evidence_key) do update
  set source_kind = excluded.source_kind,
      source_excerpt = excluded.source_excerpt,
      project_or_employer = excluded.project_or_employer,
      ownership = excluded.ownership,
      technologies = excluded.technologies,
      decision = excluded.decision,
      constraint_text = excluded.constraint_text,
      outcome = excluded.outcome,
      recency = excluded.recency,
      confidence = excluded.confidence,
      is_active = true,
      retired_at = null,
      updated_at = now();
end loop;
```

Keep the RPC `security invoker`, `auth.uid()` ownership check, grants, and the existing atomic profile/source/competency updates.

- [ ] **Step 6: Run focused tests and verify GREEN**

Run:

```bash
npm test -- src/lib/repositories/profile.test.ts
```

Expected: PASS.

- [ ] **Step 7: Verify the migration on a disposable Supabase target**

With the repository linked to a non-production Supabase project, run:

```bash
supabase db push
```

Then save the same profile/evidence payload twice and query `profile_evidence`; verify the active evidence row keeps the same UUID. Change/remove one fact, save again, and verify the previous evidence row remains present with `is_active = false`.

- [ ] **Step 8: Commit**

```bash
git add supabase/migrations/202608300001_stable_profile_evidence.sql src/lib/repositories/profile.ts src/lib/repositories/profile.test.ts
git commit -m "feat: stabilize profile evidence identity"
```

---

### Task 2: Add opportunities and atomic lifecycle history

**Files:**
- Create: `supabase/migrations/202608300002_opportunities.sql`
- Create: `src/lib/repositories/opportunities.ts`
- Create: `src/lib/repositories/opportunities.test.ts`
- Modify: `src/lib/types.ts`

**Interfaces:**
- Produces types: `OpportunityStatus`, `OpportunityEventType`, `Opportunity`, `OpportunityEvent`, `CreateOpportunityInput`, `UpdateOpportunityDetailsInput`.
- Produces repository functions:

```ts
createOpportunity(supabase, userId, input): Promise<Opportunity>
getOpportunity(supabase, userId, opportunityId): Promise<Opportunity | null>
listOpportunities(supabase, userId): Promise<Opportunity[]>
updateOpportunityDetails(supabase, userId, opportunityId, input): Promise<Opportunity>
transitionOpportunity(supabase, userId, opportunityId, toStatus, options?): Promise<Opportunity>
scheduleOpportunityInterview(supabase, userId, opportunityId, interviewAt, options?): Promise<Opportunity>
listOpportunityEvents(supabase, userId, opportunityId): Promise<OpportunityEvent[]>
```

- [ ] **Step 1: Add failing repository tests for opportunity creation, mapping, and lifecycle RPC calls**

Create `src/lib/repositories/opportunities.test.ts` with `vi.mock("server-only", () => ({}))` and mocked Supabase calls.

Minimum RED cases:

```ts
it("creates an opportunity through the transactional create RPC", async () => {
  await createOpportunity(supabase as never, "user-1", {
    company: "Example",
    role: "Senior Frontend Engineer",
    sourceSystem: "job-hunter",
    sourceExternalId: "job-123",
  });

  expect(rpc).toHaveBeenCalledWith("create_opportunity", expect.objectContaining({
    p_company: "Example",
    p_role: "Senior Frontend Engineer",
    p_source_system: "job-hunter",
    p_source_external_id: "job-123",
  }));
});

it("uses the transactional status RPC instead of direct status update", async () => {
  await transitionOpportunity(supabase as never, "user-1", "opp-1", "applied", {
    occurredAt: "2026-08-30T20:00:00.000Z",
    note: "Applied from company site",
  });
  expect(rpc).toHaveBeenCalledWith("transition_opportunity", expect.objectContaining({
    p_opportunity_id: "opp-1",
    p_to_status: "applied",
  }));
});

it("uses the interview scheduling RPC for next_interview_at", async () => {
  await scheduleOpportunityInterview(
    supabase as never,
    "user-1",
    "opp-1",
    "2026-09-03T10:00:00.000Z",
  );
  expect(rpc).toHaveBeenCalledWith("schedule_opportunity_interview", expect.any(Object));
});
```

- [ ] **Step 2: Run the focused test and verify RED**

```bash
npm test -- src/lib/repositories/opportunities.test.ts
```

Expected: FAIL because types/repository do not exist.

- [ ] **Step 3: Add opportunity domain types to `src/lib/types.ts`**

Add exact status/event unions:

```ts
export type OpportunityStatus =
  | "considering"
  | "applied"
  | "interviewing"
  | "offer"
  | "rejected"
  | "withdrawn"
  | "closed";

export type OpportunityEventType =
  | "created"
  | "status_changed"
  | "interview_scheduled"
  | "interview_completed"
  | "note"
  | "source_updated";
```

Add `Opportunity` fields matching the spec in camelCase, including `sourceSystem`, `sourceExternalId`, `matchScore`, `appliedAt`, and `nextInterviewAt`. Add input types that deliberately exclude lifecycle fields from ordinary detail updates.

- [ ] **Step 4: Create the opportunities migration**

Create `opportunities` and `opportunity_events` with:

- status/event checks from the spec;
- `match_score` `0..100` check;
- `unique (id, user_id)`;
- partial unique external identity index on `(user_id, source_system, source_external_id)`;
- user/status, user/next-interview, and user/updated indexes;
- composite same-user event foreign key;
- `opportunities`: own-row select/insert/update/delete RLS;
- `opportunity_events`: own-row select/insert RLS only, with no direct update/delete policies.

Create three narrow `security invoker` functions. None accepts a caller-supplied `user_id`.

`create_opportunity(...)` must insert the opportunity and its `created` event in one transaction and return the new opportunity ID. Its event must use `to_status = 'considering'`.

`transition_opportunity(...)` must require `auth.uid()`, lock/verify the owned row, update status, set `applied_at` on the first transition to `applied`, and append one `status_changed` event.

Core status update shape:

```sql
select * into v_opportunity
from public.opportunities
where id = p_opportunity_id and user_id = v_user_id
for update;

if not found then
  raise exception 'Owned opportunity was not found' using errcode = 'P0002';
end if;

update public.opportunities
set status = p_to_status,
    applied_at = case
      when p_to_status = 'applied' then coalesce(applied_at, p_occurred_at, now())
      else applied_at
    end,
    updated_at = now()
where id = p_opportunity_id and user_id = v_user_id;

insert into public.opportunity_events (...)
values (..., 'status_changed', v_opportunity.status, p_to_status, ...);
```

`schedule_opportunity_interview(...)` must atomically set `next_interview_at`, append `interview_scheduled`, and change status to `interviewing` only when the current status is `considering` or `applied`. It must not silently move `offer`, `rejected`, `withdrawn`, or `closed` back to `interviewing`.

- [ ] **Step 5: Implement `opportunities.ts`**

Follow existing repository conventions:

- `import "server-only";`
- map snake_case rows to camelCase types;
- `.eq("user_id", userId)` on normal reads/writes even though RLS also exists;
- throw `RepositoryError` with safe messages;
- never update `status`, `applied_at`, or `next_interview_at` through `updateOpportunityDetails`.

`createOpportunity` calls `create_opportunity`, extracts `opportunity_id`, and reloads with `getOpportunity`.

After every lifecycle RPC, reload with `getOpportunity` and fail with `NO_OWNED_ROW` if the row cannot be reloaded.

- [ ] **Step 6: Run focused tests and verify GREEN**

```bash
npm test -- src/lib/repositories/opportunities.test.ts
```

Expected: PASS.

- [ ] **Step 7: Verify SQL invariants on the disposable Supabase target**

After `supabase db push`, verify:

- creation produces exactly one `created` event;
- duplicate `(user_id, source_system, source_external_id)` fails;
- the same external identity succeeds for another user;
- `transition_opportunity` creates one event and changes the summary row together;
- `schedule_opportunity_interview` creates `interview_scheduled` history and updates `next_interview_at` together;
- direct update/delete of `opportunity_events` is denied to normal authenticated clients.

- [ ] **Step 8: Commit**

```bash
git add supabase/migrations/202608300002_opportunities.sql src/lib/types.ts src/lib/repositories/opportunities.ts src/lib/repositories/opportunities.test.ts
git commit -m "feat: add opportunity lifecycle foundation"
```

---

### Task 3: Add career stories with typed provenance

**Files:**
- Create: `supabase/migrations/202608300003_career_stories.sql`
- Create: `src/lib/repositories/stories.ts`
- Create: `src/lib/repositories/stories.test.ts`
- Modify: `src/lib/types.ts`

**Interfaces:**
- Produces types: `CareerStoryReviewState`, `CareerStory`, `CareerStoryEvidence`, `CareerStoryEvidenceSource`, `CreateCareerStoryInput`, `UpdateCareerStoryInput`.
- Produces repository functions:

```ts
createCareerStory(supabase, userId, input): Promise<CareerStory>
getCareerStory(supabase, userId, storyId): Promise<CareerStory | null>
listCareerStories(supabase, userId): Promise<CareerStory[]>
updateCareerStory(supabase, userId, storyId, input): Promise<CareerStory>
attachCareerStoryEvidence(supabase, userId, storyId, source, note?): Promise<CareerStoryEvidence>
listCareerStoryEvidence(supabase, userId, storyId): Promise<CareerStoryEvidence[]>
```

`CareerStoryEvidenceSource` must be a discriminated union so callers cannot supply two source IDs:

```ts
export type CareerStoryEvidenceSource =
  | { kind: "profile_evidence"; profileEvidenceId: string }
  | { kind: "interview_question"; interviewQuestionId: string };
```

- [ ] **Step 1: Write failing story repository tests**

Cover structured mapping, tags, and source-union persistence.

```ts
it("persists profile evidence provenance with exactly one source", async () => {
  await attachCareerStoryEvidence(
    supabase as never,
    "user-1",
    "story-1",
    { kind: "profile_evidence", profileEvidenceId: "evidence-1" },
    "Supports the migration outcome",
  );

  expect(insert).toHaveBeenCalledWith(expect.objectContaining({
    user_id: "user-1",
    career_story_id: "story-1",
    profile_evidence_id: "evidence-1",
    interview_question_id: null,
  }));
});
```

- [ ] **Step 2: Run focused tests and verify RED**

```bash
npm test -- src/lib/repositories/stories.test.ts
```

- [ ] **Step 3: Add story types**

Add exact camelCase fields from the spec. Keep `completeness` as a `number` `0..1` and `reviewState` as `"draft" | "confirmed" | "retired"`.

- [ ] **Step 4: Create the story migration**

Create `career_stories` and `career_story_evidence` with same-user FKs and these RLS boundaries:

- `career_stories`: own-row select/insert/update/delete;
- `career_story_evidence`: own-row select/insert only, with no direct update/delete policy.

The exact-one-source check must be:

```sql
check (num_nonnulls(profile_evidence_id, interview_question_id) = 1)
```

Use:

```sql
foreign key (career_story_id, user_id)
  references public.career_stories (id, user_id) on delete cascade,
foreign key (profile_evidence_id, user_id)
  references public.profile_evidence (id, user_id),
foreign key (interview_question_id, user_id)
  references public.interview_questions (id, user_id)
```

Use restrictive/default delete behavior for evidence parents so referenced provenance cannot disappear silently; story deletion may cascade its link rows.

- [ ] **Step 5: Implement `stories.ts`**

Map story rows and evidence rows explicitly. Convert the discriminated source union to nullable DB columns in exactly one helper:

```ts
function storyEvidenceColumns(source: CareerStoryEvidenceSource) {
  return source.kind === "profile_evidence"
    ? { profile_evidence_id: source.profileEvidenceId, interview_question_id: null }
    : { profile_evidence_id: null, interview_question_id: source.interviewQuestionId };
}
```

Do not compute story completeness automatically in Release 1; persist the caller-provided value/default only.

- [ ] **Step 6: Run focused tests and verify GREEN**

```bash
npm test -- src/lib/repositories/stories.test.ts
```

Expected: PASS.

- [ ] **Step 7: Verify DB source constraints on disposable Supabase**

Attempt rows with zero sources and two sources; both must fail. Verify a user cannot reference another user’s story or evidence ID. Verify normal authenticated clients cannot update/delete an existing `career_story_evidence` row directly.

- [ ] **Step 8: Commit**

```bash
git add supabase/migrations/202608300003_career_stories.sql src/lib/types.ts src/lib/repositories/stories.ts src/lib/repositories/stories.test.ts
git commit -m "feat: add career story provenance"
```

---

### Task 4: Add coach observations and evidence review state

**Files:**
- Create: `supabase/migrations/202608300004_coach_observations.sql`
- Create: `src/lib/repositories/observations.ts`
- Create: `src/lib/repositories/observations.test.ts`
- Modify: `src/lib/types.ts`

**Interfaces:**
- Produces types: `CoachObservationType`, `CoachObservationTrend`, `CoachObservationReviewState`, `CoachObservation`, `ObservationEvidenceRole`, `ObservationEvidence`, `ObservationEvidenceSource`.
- Produces repository functions:

```ts
createCoachObservation(supabase, userId, input): Promise<CoachObservation>
getCoachObservation(supabase, userId, observationId): Promise<CoachObservation | null>
listCoachObservations(supabase, userId): Promise<CoachObservation[]>
reviewCoachObservation(supabase, userId, observationId, review): Promise<CoachObservation>
attachObservationEvidence(supabase, userId, observationId, source, options?): Promise<ObservationEvidence>
listObservationEvidence(supabase, userId, observationId): Promise<ObservationEvidence[]>
```

`ObservationEvidenceSource` is:

```ts
export type ObservationEvidenceSource =
  | { kind: "profile_evidence"; profileEvidenceId: string }
  | { kind: "question_evaluation"; questionEvaluationId: string }
  | { kind: "career_story"; careerStoryId: string }
  | { kind: "opportunity_event"; opportunityEventId: string };
```

- [ ] **Step 1: Write failing observation tests**

Required RED cases:

```ts
it("keeps the original claim when the user corrects an observation", async () => {
  const result = await reviewCoachObservation(supabase as never, "user-1", "obs-1", {
    state: "corrected",
    correction: "I explain tradeoffs well; I need to make ownership more explicit.",
  });

  expect(update).toHaveBeenCalledWith(expect.objectContaining({
    review_state: "corrected",
    user_correction: "I explain tradeoffs well; I need to make ownership more explicit.",
    corrected_at: expect.any(String),
  }));
  expect(result.claim).toBe("I skip tradeoffs.");
});
```

Also cover `supporting`, `contradicting`, and `context` evidence roles.

- [ ] **Step 2: Run focused tests and verify RED**

```bash
npm test -- src/lib/repositories/observations.test.ts
```

- [ ] **Step 3: Add observation types**

Use exact unions from the spec. Review input must prevent meaningless combinations:

```ts
export type CoachObservationReview =
  | { state: "confirmed" }
  | { state: "dismissed" }
  | { state: "corrected"; correction: string };
```

- [ ] **Step 4: Create the observation migration**

Before the new FK, add:

```sql
alter table public.question_evaluations
  add constraint question_evaluations_id_user_key unique (id, user_id);
```

Create `coach_observations` and `observation_evidence` with:

```sql
check (confidence between 0 and 1)
check (importance between 0 and 1)
check (weight between 0 and 1)
check (num_nonnulls(
  profile_evidence_id,
  question_evaluation_id,
  career_story_id,
  opportunity_event_id
) = 1)
```

Use composite same-user FKs to every evidence parent. RLS boundaries:

- `coach_observations`: own-row select/insert/update/delete;
- `observation_evidence`: own-row select/insert only, with no direct update/delete policy.

- [ ] **Step 5: Implement `observations.ts`**

Keep creation/review/evidence attachment persistence-only. Do not add LLM calls or inference reconciliation.

For review timestamps:

- `confirmed` sets `confirmed_at` and clears correction/dismissal timestamps;
- `corrected` sets `corrected_at` and `user_correction` and clears confirmation/dismissal timestamps;
- `dismissed` sets `dismissed_at`, clears confirmation/correction timestamps, and leaves the original `claim` untouched.

Use one source-to-columns helper like the story repository.

- [ ] **Step 6: Run focused tests and verify GREEN**

```bash
npm test -- src/lib/repositories/observations.test.ts
```

- [ ] **Step 7: Verify provenance constraints and RLS in disposable Supabase**

Verify exact-one-source checks and cross-user FK failures. Confirm an inactive `profile_evidence` row may still be referenced because inactive means historical, not invalid. Verify normal authenticated clients cannot update/delete existing `observation_evidence` rows directly.

- [ ] **Step 8: Commit**

```bash
git add supabase/migrations/202608300004_coach_observations.sql src/lib/types.ts src/lib/repositories/observations.ts src/lib/repositories/observations.test.ts
git commit -m "feat: add evidence-backed coach observations"
```

---

### Task 5: Add explicit practice plans and opportunity relationships

**Files:**
- Create: `supabase/migrations/202608300005_practice_plans.sql`
- Create: `src/lib/repositories/practice-plans.ts`
- Create: `src/lib/repositories/practice-plans.test.ts`
- Modify: `src/lib/types.ts`

**Interfaces:**
- Produces types: `PracticePlanStatus`, `PracticeFormat`, `PracticePlan`, `PracticePlanOpportunity`, `CreatePracticePlanInput`.
- Produces repository functions:

```ts
createPracticePlan(supabase, userId, input): Promise<PracticePlan>
getPracticePlan(supabase, userId, planId): Promise<PracticePlan | null>
listPracticePlans(supabase, userId): Promise<PracticePlan[]>
updatePracticePlan(supabase, userId, planId, input): Promise<PracticePlan>
setPracticePlanOpportunities(supabase, userId, planId, links): Promise<PracticePlan>
```

- [ ] **Step 1: Write failing practice-plan tests**

Cover plan mapping, JSON criteria/factors, several opportunity links, and the one-primary rule expected from the DB.

```ts
it("persists one primary and multiple supporting opportunities", async () => {
  await setPracticePlanOpportunities(supabase as never, "user-1", "plan-1", [
    { opportunityId: "opp-1", relevance: "primary" },
    { opportunityId: "opp-2", relevance: "supporting" },
  ]);

  expect(insert).toHaveBeenCalledWith([
    { user_id: "user-1", practice_plan_id: "plan-1", opportunity_id: "opp-1", relevance: "primary" },
    { user_id: "user-1", practice_plan_id: "plan-1", opportunity_id: "opp-2", relevance: "supporting" },
  ]);
});
```

- [ ] **Step 2: Run focused tests and verify RED**

```bash
npm test -- src/lib/repositories/practice-plans.test.ts
```

- [ ] **Step 3: Add practice-plan types**

Use exact format union:

```ts
export type PracticeFormat =
  | "targeted_drill"
  | "story_work"
  | "self_presentation"
  | "behavioral"
  | "technical_communication"
  | "role_prep"
  | "full_simulation"
  | "hands_on";
```

Use exact status union `draft | ready | started | completed | cancelled | failed`.

- [ ] **Step 4: Create the practice-plan migration**

Create `practice_plans` and `practice_plan_opportunities` with checks from the spec, including:

```sql
check (estimated_minutes is null or estimated_minutes between 1 and 180)
```

Add same-user composite FKs. RLS boundaries:

- `practice_plans`: own-row select/insert/update/delete;
- `practice_plan_opportunities`: own-row select/insert/delete because this relationship set is deliberately replaceable, but no update policy is needed.

Add:

```sql
create unique index practice_plan_one_primary_opportunity_idx
on public.practice_plan_opportunities (practice_plan_id)
where relevance = 'primary';
```

- [ ] **Step 5: Implement `practice-plans.ts`**

The repository may replace a plan’s opportunity links by deleting only rows scoped to both `user_id` and `practice_plan_id`, then inserting the requested links. Validate in TypeScript before writing:

```ts
if (links.filter((link) => link.relevance === "primary").length > 1) {
  throw new RepositoryError("A practice plan can have only one primary opportunity.", "INVALID_PLAN_CONTEXT");
}
```

Hydrate linked opportunities when returning a full `PracticePlan` so later Release 2 callers do not need table-specific queries.

- [ ] **Step 6: Run focused tests and verify GREEN**

```bash
npm test -- src/lib/repositories/practice-plans.test.ts
```

- [ ] **Step 7: Verify DB constraints on disposable Supabase**

Verify one plan accepts several links but rejects a second `primary` link and rejects cross-user opportunities.

- [ ] **Step 8: Commit**

```bash
git add supabase/migrations/202608300005_practice_plans.sql src/lib/types.ts src/lib/repositories/practice-plans.ts src/lib/repositories/practice-plans.test.ts
git commit -m "feat: add persisted practice plans"
```

---

### Task 6: Link interview sessions to Career Brain context without breaking legacy sessions

**Files:**
- Create: `supabase/migrations/202608300006_session_career_context.sql`
- Modify: `src/lib/types.ts`
- Modify: `src/lib/repositories/interviews.ts`
- Modify: `src/lib/repositories/interviews.test.ts`

**Interfaces:**
- Changes `InterviewSession` with:

```ts
practicePlanId: string | null;
opportunityId: string | null;
```

- Produces:

```ts
export type SessionCareerContext = {
  practicePlanId: string | null;
  opportunityId: string | null;
};

linkSessionCareerContext(
  supabase: SupabaseClient,
  userId: string,
  sessionId: string,
  context: SessionCareerContext,
): Promise<InterviewSession>
```

- [ ] **Step 1: Write failing session-mapping compatibility tests**

Add one legacy-null case and one linked-context case to `interviews.test.ts`.

```ts
it("hydrates legacy sessions with null Career Brain context", () => {
  const session = mapSession(legacyRow, [], [], [], new Map());
  expect(session.practicePlanId).toBeNull();
  expect(session.opportunityId).toBeNull();
});

it("hydrates persisted Career Brain context", () => {
  const session = mapSession({
    ...legacyRow,
    practice_plan_id: "plan-1",
    opportunity_id: "opp-1",
  }, [], [], [], new Map());
  expect(session.practicePlanId).toBe("plan-1");
  expect(session.opportunityId).toBe("opp-1");
});
```

Add repository tests that ensure `linkSessionCareerContext` rejects a context where the requested opportunity is not associated with the requested plan.

- [ ] **Step 2: Run focused tests and verify RED**

```bash
npm test -- src/lib/repositories/interviews.test.ts
```

- [ ] **Step 3: Create the session-context migration**

Add nullable columns and same-user foreign keys:

```sql
alter table public.interview_sessions
  add column practice_plan_id uuid,
  add column opportunity_id uuid,
  add foreign key (practice_plan_id, user_id)
    references public.practice_plans (id, user_id) on delete set null (practice_plan_id),
  add foreign key (opportunity_id, user_id)
    references public.opportunities (id, user_id) on delete set null (opportunity_id);
```

Add user/context indexes that make later Release 2 queries cheap, e.g. `(user_id, opportunity_id, created_at desc)` and `(user_id, practice_plan_id)`.

- [ ] **Step 4: Update `InterviewSession` and `mapSession`**

Add fields to the returned object:

```ts
practicePlanId: typeof row.practice_plan_id === "string" ? row.practice_plan_id : null,
opportunityId: typeof row.opportunity_id === "string" ? row.opportunity_id : null,
```

Do not change any existing blueprint, transcript, evaluation, checkpoint, or progress semantics.

- [ ] **Step 5: Implement `linkSessionCareerContext`**

Rules:

1. load the owned session;
2. if `practicePlanId` is non-null, load the owned plan;
3. if `opportunityId` is non-null, load the owned opportunity;
4. if both are non-null, query `practice_plan_opportunities` for that exact user/plan/opportunity pair;
5. if the plan has a `primary` opportunity, require `opportunityId` to equal it;
6. update only the owned session row;
7. reload and return it.

Use stable repository error codes:

```ts
throw new RepositoryError("The practice plan and opportunity do not match.", "INVALID_PLAN_CONTEXT");
```

Keep all existing session-creation functions source-compatible; do not add required parameters to them in Release 1.

- [ ] **Step 6: Run focused tests and verify GREEN**

```bash
npm test -- src/lib/repositories/interviews.test.ts
```

- [ ] **Step 7: Verify nullable compatibility on disposable Supabase**

After migration, query pre-existing `interview_sessions` and verify both new columns are null. Create/link a test session with same-user context and verify cross-user FK assignment fails.

- [ ] **Step 8: Commit**

```bash
git add supabase/migrations/202608300006_session_career_context.sql src/lib/types.ts src/lib/repositories/interviews.ts src/lib/repositories/interviews.test.ts
git commit -m "feat: link sessions to career context"
```

---

### Task 7: Run full Release 1 regression verification and document the schema boundary

**Files:**
- Modify: `README.md`
- Test: all existing `*.test.ts(x)` files
- Verify: all six Release 1 migrations on a disposable Supabase target

**Interfaces:**
- Produces no new runtime interface.
- Documents that Supabase now contains Career Brain foundation tables while the current UI and job-hunter remain unchanged.

- [ ] **Step 1: Update README current-boundary text**

Add a concise statement under “What works now” or the Supabase section:

```md
- Career Brain persistence foundation for opportunities, career stories, evidence-backed coach observations, and practice plans. The current UI does not expose these Release 1 entities yet; the job-hunter integration remains unchanged until a later release.
```

Do not claim adaptive recommendations or automatic learning are implemented.

- [ ] **Step 2: Run the complete test suite**

```bash
npm test
```

Expected: PASS with no regressions in profile, interview, coach, planner, progress, or UI tests.

- [ ] **Step 3: Run lint**

```bash
npm run lint
```

Expected: PASS.

- [ ] **Step 4: Run the repository-documented production build**

```bash
npx next build --webpack
```

Expected: production build succeeds.

- [ ] **Step 5: Apply all migrations from a clean/current disposable Supabase target**

Use the repository-supported workflow:

```bash
supabase db push
```

Expected: all Release 1 migrations apply in filename order without SQL errors.

Manually verify these database invariants before calling the release complete:

```text
1. Equivalent profile evidence keeps one UUID across saves.
2. Removed profile evidence is inactive, not deleted.
3. Opportunity creation creates one immutable created event.
4. Duplicate external opportunity identity is rejected per user.
5. Opportunity status/interview scheduling produce matching event history atomically.
6. Opportunity events cannot be directly updated/deleted by normal authenticated clients.
7. Story and observation evidence require exactly one typed source.
8. Story and observation provenance rows cannot be directly updated/deleted by normal authenticated clients.
9. Cross-user provenance links are rejected.
10. Practice plans allow many opportunities but at most one primary.
11. Existing interview sessions retain null Career Brain context.
12. New same-user session context can be linked successfully.
```

- [ ] **Step 6: Confirm Release 1 does not touch the job hunter**

Review the diff and verify there are no changes outside `interviewer-app`, no job-hunter credentials/configuration, and no new dependency on the bot being online.

- [ ] **Step 7: Commit final documentation/verification adjustment**

```bash
git add README.md
git commit -m "docs: describe Career Brain foundation"
```

- [ ] **Step 8: Final verification before merge/push**

Run again after the final commit:

```bash
npm test && npm run lint && npx next build --webpack
```

Expected: all commands succeed.

## Completion Gate

Do not mark Release 1 complete unless every acceptance criterion in the design spec has a corresponding passing test or verified database invariant, especially:

- stable profile-evidence identity;
- complete, append-only opportunity lifecycle history;
- typed story/observation provenance;
- durable observation correction/dismissal state;
- explicit practice plans with one primary opportunity at most;
- nullable, same-user session context;
- existing Relay behavior unchanged when new tables are empty;
- no automatic learning/recommendation logic;
- no Google tracker integration;
- no job-hunter changes.
