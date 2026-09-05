# Career Brain Release 1 — Foundation Design

Date: 2026-08-30
Status: Approved design; implementation plan follows after written-spec review
Depends on: `docs/superpowers/specs/2026-08-30-career-brain-architecture-design.md`

## 1. Purpose

Release 1 creates the durable Career Brain data foundation required by the approved Relay rework. It is intentionally backend-focused and additive.

The release must make it possible for later releases to represent:

- jobs worth considering and their lifecycle through application/interview outcomes;
- reusable, source-backed career stories;
- inspectable coach observations and the evidence behind them;
- explicit practice plans and the opportunities they serve;
- practice sessions linked to both the plan that created them and, where applicable, a primary real-world opportunity.

Release 1 does **not** redesign the Relay UI, choose recommended practice, generate coach memory automatically, import the Google tracker, or change the job-hunter bot.

The core release invariant is:

> The new Career Brain schema can be empty and every existing Relay profile/interview flow still works.

## 2. Scope boundary

### In scope

- additive Supabase migrations for Career Brain entities;
- stable profile-evidence identity needed for long-term provenance;
- typed TypeScript domain models for new entities;
- server-only repository modules for persistence/hydration;
- atomic opportunity lifecycle transitions where database invariants require them;
- RLS and same-user foreign-key integrity;
- nullable links from existing interview sessions into the new model;
- repository and migration-oriented tests;
- compatibility verification for existing profile/interview behavior.

### Out of scope

- recommended-practice dashboard or other new UI;
- automatic observation extraction/reconciliation;
- the Release 3 prioritization algorithm;
- job-description research or web intelligence;
- Google Sheets synchronization/import code;
- job-hunter Supabase publishing;
- changes to the job-hunter SQLite/GitHub Actions/Telegram workflow;
- vector search, embeddings, or model fine-tuning;
- a generic multi-user product redesign.

## 3. Chosen modeling approach

Release 1 uses normalized domain tables with typed provenance links.

Alternatives considered:

1. **Mostly JSONB records** — fastest initially, but weak relational integrity and difficult provenance queries.
2. **One generic polymorphic evidence ledger** — flexible, but would require retrofitting existing evidence into a new abstraction and would sacrifice normal foreign-key enforcement for `source_type/source_id` references.
3. **Normalized domain tables with typed provenance links** — chosen. It keeps current Relay evidence models, adds clear domain boundaries, and preserves database-enforced ownership/integrity.

Small variable-length values such as tags, success criteria, strengths, and gaps may remain JSONB arrays. Identity, lifecycle, ownership, and provenance remain relational.

## 4. High-level relationship model

```text
profiles / profile_evidence
          |
          +-------------------+
          |                   |
          v                   v
    opportunities        career_stories
          |                   |
          v                   v
 opportunity_events   career_story_evidence
          |                   |
          +---------+---------+
                    |
                    v
            coach_observations
                    |
                    v
           observation_evidence

opportunities --< practice_plan_opportunities >-- practice_plans
                                                    |
                                                    v
                                            interview_sessions

interview_sessions also carry a nullable primary opportunity_id.
```

Unless a section says otherwise, every new entity table uses:

- `id uuid primary key default gen_random_uuid()`;
- `user_id uuid not null references auth.users(id) on delete cascade`;
- `created_at timestamptz not null default now()`;
- `updated_at timestamptz not null default now()` where the entity is mutable;
- `unique (id, user_id)` when another table needs an ownership-preserving composite foreign key.

## 5. Opportunities: one durable record across the lifecycle

The user-approved model is one durable opportunity record covering both interesting/shortlisted jobs and actual applications. Release 1 therefore uses the domain name `opportunities`, not `applications`.

The UI may later label subsets of these records as “Applications,” but the database entity represents the full lifecycle.

### 5.1 Opportunity status

`opportunities.status` is constrained to:

- `considering` — saved/shortlisted and worth tracking, but not yet applied;
- `applied` — application submitted;
- `interviewing` — an active interview process exists;
- `offer` — an offer has been received and remains the current outcome/state;
- `rejected` — employer ended the process;
- `withdrawn` — user ended the process after applying;
- `closed` — no longer being pursued before application or otherwise intentionally archived without a rejection/withdrawal outcome.

Release 1 does not enforce a rigid state-machine graph for every transition because real application histories can be irregular. It does enforce that lifecycle fields are changed together with their history where those fields represent an event.

### 5.2 `opportunities`

Required columns:

- `id uuid primary key default gen_random_uuid()`;
- `user_id uuid not null references auth.users(id) on delete cascade`;
- `company text not null`;
- `role text not null`;
- `status text not null default 'considering'` with the status check above;
- `location text`;
- `remote boolean`;
- `job_url text`;
- `job_description text`;
- `source_label text` — human-readable source such as an employer/ATS name;
- `source_system text` — stable integration namespace, e.g. `manual`, `job-hunter`, `tracker-import`;
- `source_external_id text` — stable source-owned identity when available;
- `match_score numeric` constrained to `0..100` when present;
- `strengths jsonb not null default '[]'::jsonb`;
- `gaps jsonb not null default '[]'::jsonb`;
- `notes text`;
- `applied_at timestamptz`;
- `next_interview_at timestamptz`;
- `created_at timestamptz not null default now()`;
- `updated_at timestamptz not null default now()`;
- `unique (id, user_id)` for composite ownership foreign keys.

A partial unique index must prevent duplicate external records when both source identity fields are present:

```text
(user_id, source_system, source_external_id)
where source_system is not null and source_external_id is not null
```

`job_url` is not globally unique; employers and aggregators may change or duplicate URLs.

Useful indexes include current status, next interview time, and recent update time scoped by user.

### 5.3 `opportunity_events`

Opportunity history is append-oriented rather than reconstructed only from the current row.

Columns:

- `id uuid primary key default gen_random_uuid()`;
- `user_id uuid not null`;
- `opportunity_id uuid not null`;
- `event_type text` constrained initially to `created`, `status_changed`, `interview_scheduled`, `interview_completed`, `note`, `source_updated`;
- `from_status text` nullable;
- `to_status text` nullable;
- `occurred_at timestamptz not null default now()`;
- `note text`;
- `metadata jsonb not null default '{}'::jsonb`;
- `created_at timestamptz not null default now()`;
- `unique (id, user_id)`.

The opportunity foreign key is ownership-preserving:

```text
(opportunity_id, user_id) -> opportunities(id, user_id)
```

Events are historical facts. Normal application code must not mutate an event after creation. Parent deletion may cascade when the user explicitly deletes the opportunity.

### 5.4 Atomic lifecycle mutations

Lifecycle fields must not be updated independently from the event history they summarize.

Release 1 introduces narrow database operations for the lifecycle invariants it stores. At minimum:

```text
transition_opportunity(opportunity_id, to_status, occurred_at, note, metadata)
```

must atomically:

1. require authentication;
2. lock and verify the owned opportunity;
3. capture the previous status;
4. update current status;
5. set `applied_at` when moving to `applied` if not already set;
6. append the corresponding `status_changed` event;
7. return the updated opportunity/event identity;
8. commit all-or-nothing.

Likewise, scheduling an interview must not write `next_interview_at` without history. A narrow operation such as:

```text
schedule_opportunity_interview(opportunity_id, interview_at, note, metadata)
```

must atomically set `next_interview_at`, move a pre-interview opportunity into `interviewing` when appropriate, and append an `interview_scheduled` event.

Normal repository CRUD may update descriptive job fields, but must not bypass transactional lifecycle operations for `status`, `applied_at`, or `next_interview_at`.

The implementation should not create a large catch-all Career Brain RPC.

## 6. Stable profile evidence for provenance

### 6.1 Existing compatibility problem

Current `save_profile_bundle` deletes all `profile_evidence` rows and recreates them. This means database IDs are not currently stable across profile regeneration.

That behavior is incompatible with long-lived story/observation provenance: a career story cannot safely reference evidence that disappears on the next CV save.

Release 1 therefore makes profile evidence identity durable without changing what the visible profile considers “current evidence.”

### 6.2 `profile_evidence` additions

Add:

- `evidence_key text`;
- `is_active boolean not null default true`;
- `retired_at timestamptz`.

Migration sequence for `evidence_key` is explicit:

1. add it nullable;
2. backfill every existing row with a unique legacy key such as `legacy:<uuid>`;
3. add `not null`;
4. add `unique (user_id, evidence_key)`.

Existing rows remain active during migration.

### 6.3 Stable evidence key

The server profile repository computes a deterministic canonical fingerprint for each extracted evidence item and sends it to `save_profile_bundle` as `evidence_key`.

The fingerprint must use normalized source-backed content, not the temporary LLM/output array position and not an arbitrary generated `evidence-1` identifier.

A SHA-256 hash of canonicalized evidence content is appropriate. The canonical input must exclude volatile values such as confidence, timestamps, generated IDs, and array position. It should normalize whitespace/casing where that normalization does not change factual meaning.

Equivalent evidence produced again from the same source-backed content must generate the same key. Materially different source evidence may correctly receive a new identity.

### 6.4 Reconciliation instead of deletion

`save_profile_bundle` changes from “delete all evidence, insert all evidence” to reconciliation:

1. mark the user’s currently active evidence inactive and set `retired_at`;
2. for each incoming evidence item, upsert on `(user_id, evidence_key)`;
3. preserve the existing row ID on conflict;
4. refresh mutable extracted fields/confidence, mark the row active, and clear `retired_at`;
5. leave evidence that is no longer present inactive;
6. never delete historical evidence merely because the current CV/profile extraction changed.

`getProfile` reads only `is_active = true` evidence for normal current-profile behavior.

Historical provenance may continue to reference inactive evidence.

This is a targeted compatibility change required by the Career Brain, not a general profile redesign.

## 7. Career stories

A career story is a real reusable professional experience. It is not itself a coach inference.

### 7.1 `career_stories`

Columns:

- standard `id`, `user_id`, `created_at`, `updated_at` fields;
- `title text not null`;
- `situation text`;
- `responsibility text`;
- `problem text`;
- `actions text`;
- `alternatives text`;
- `tradeoffs text`;
- `ownership text`;
- `outcome text`;
- `lessons text`;
- `tags jsonb not null default '[]'::jsonb`;
- `completeness numeric not null default 0` constrained to `0..1`;
- `review_state text not null default 'draft'` constrained to `draft`, `confirmed`, `retired`;
- `confirmed_at timestamptz`;
- `unique (id, user_id)`.

Release 1 does not invent a story-strength scoring system. `completeness` describes whether enough factual fields exist to use the story, not how well the user delivers it in an interview.

### 7.2 `career_story_evidence`

Story provenance uses typed links to durable evidence.

Columns:

- `id uuid primary key default gen_random_uuid()`;
- `user_id uuid not null`;
- `career_story_id uuid not null`;
- `profile_evidence_id uuid`;
- `interview_question_id uuid`;
- `note text` — optional explanation of what this source supports;
- `created_at timestamptz not null default now()`.

Database checks require exactly one source column (`profile_evidence_id` or `interview_question_id`) to be non-null, implemented with an invariant equivalent to `num_nonnulls(...) = 1`.

All foreign keys use `(id, user_id)` ownership pairs. Interview-question evidence is valid because the existing question row contains the exact candidate answer as well as the prompt.

A later release may add additional evidence types such as explicit real-interview notes without redesigning the story table.

## 8. Coach observations

Coach observations are persistent, inspectable inferences. Release 1 stores them but does not generate or reconcile them automatically.

### 8.1 `coach_observations`

Columns:

- standard `id`, `user_id`, `created_at`, `updated_at` fields;
- `observation_type text` constrained to `strength`, `weakness`, `answer_habit`, `knowledge_gap`, `story_gap`, `story_strength`, `delivery_pattern`, `other`;
- `claim text not null` — original inference text;
- `confidence numeric not null default 0` constrained to `0..1`;
- `importance numeric not null default 0` constrained to `0..1`;
- `trend text not null default 'unresolved'` constrained to `unresolved`, `improving`, `stable`, `worsening`;
- `review_state text not null default 'unreviewed'` constrained to `unreviewed`, `confirmed`, `corrected`, `dismissed`;
- `user_correction text` — preserves corrected wording/context without overwriting the original claim;
- `first_seen_at timestamptz`;
- `last_seen_at timestamptz`;
- `confirmed_at timestamptz`;
- `corrected_at timestamptz`;
- `dismissed_at timestamptz`;
- `unique (id, user_id)`.

The original AI/system claim is preserved for auditability. Future recommendation logic must honor review state and user correction; Release 3 defines those reconciliation rules.

### 8.2 `observation_evidence`

Columns:

- `id uuid primary key default gen_random_uuid()`;
- `user_id uuid not null`;
- `observation_id uuid not null`;
- `profile_evidence_id uuid`;
- `question_evaluation_id uuid`;
- `career_story_id uuid`;
- `opportunity_event_id uuid`;
- `evidence_role text not null default 'supporting'` constrained to `supporting`, `contradicting`, `context`;
- `weight numeric not null default 1` constrained to `0..1`;
- `reason text`;
- `created_at timestamptz not null default now()`.

A database check requires exactly one evidence-source foreign key to be non-null, implemented with an invariant equivalent to `num_nonnulls(...) = 1`.

Each foreign key is ownership-preserving through `(id, user_id)`.

The existing `question_evaluations` table must receive `unique (id, user_id)` before it is referenced by this composite foreign key; other existing parent tables already follow that ownership-key pattern or must receive the same targeted uniqueness constraint if verification shows it is absent.

This gives Release 3 enough structure to strengthen, weaken, or contextualize an observation without converting raw evidence into an untraceable summary.

## 9. Practice plans

A practice plan is the explicit persisted contract explaining what a future practice session is trying to improve and why.

### 9.1 `practice_plans`

Columns:

- standard `id`, `user_id`, `created_at`, `updated_at` fields;
- `status text not null default 'draft'` constrained to `draft`, `ready`, `started`, `completed`, `cancelled`, `failed`;
- `primary_focus text not null`;
- `secondary_focus text`;
- `rationale text not null default ''`;
- `format text not null` constrained to `targeted_drill`, `story_work`, `self_presentation`, `behavioral`, `technical_communication`, `role_prep`, `full_simulation`, `hands_on`;
- `estimated_minutes integer` constrained to `1..180` when present;
- `success_criteria jsonb not null default '[]'::jsonb`;
- `priority_score numeric` nullable;
- `priority_factors jsonb not null default '{}'::jsonb` — reserved for inspectable decision inputs in Release 3;
- `generation_error text` nullable for safe failed-plan persistence when later AI generation is introduced;
- `completed_at timestamptz`;
- `unique (id, user_id)`.

Release 1 does not define the prioritization formula. Nullable score/factors exist so Release 3 can persist its deterministic recommendation snapshot without another fundamental model change.

### 9.2 `practice_plan_opportunities`

A practice plan may serve several opportunities.

Columns:

- `user_id uuid not null`;
- `practice_plan_id uuid not null`;
- `opportunity_id uuid not null`;
- `relevance text not null default 'supporting'` constrained to `primary`, `supporting`;
- `created_at timestamptz not null default now()`;
- primary key across `(practice_plan_id, opportunity_id)`.

Both parent foreign keys include `user_id`.

A partial unique index enforces at most one primary opportunity per plan:

```text
unique (practice_plan_id) where relevance = 'primary'
```

## 10. Existing interview-session links

`interview_sessions` receives two nullable columns:

- `practice_plan_id uuid`;
- `opportunity_id uuid`.

Both use same-user composite foreign keys:

```text
(practice_plan_id, user_id) -> practice_plans(id, user_id)
(opportunity_id, user_id) -> opportunities(id, user_id)
```

Both are nullable so historical sessions remain valid without backfill guesses.

Semantics:

- `practice_plan_id` answers **why this practice existed and what success meant**;
- `opportunity_id` identifies the **primary real job/interview being prepared for**, when there is one.

A plan may still relate to multiple opportunities through the join table.

If a session has both a `practice_plan_id` and `opportunity_id`, normal domain code must ensure that opportunity is associated with the plan. If the plan marks one opportunity as `primary`, the session’s `opportunity_id` must match it. This is enforced in repository/domain validation rather than a complex cross-table SQL constraint.

Existing session hydration must tolerate both columns as null. No historical session is assigned an opportunity or plan automatically.

## 11. Ownership, RLS, and relational integrity

Every new durable domain table contains `user_id` and enables RLS.

The default visible-data rule follows current Relay behavior:

```text
auth.uid() = user_id
```

For tables that support user CRUD, select/insert/update/delete policies must constrain ownership.

Historical event/provenance rows are append-oriented through normal application code; they are not edited in place to rewrite history.

Every cross-table relationship uses composite same-user foreign keys wherever the existing parent table exposes `unique (id, user_id)`. If any existing evidence parent lacks that uniqueness, Release 1 adds it before creating the dependent foreign key.

RLS is not the only isolation mechanism: a user-owned child row must be structurally unable to reference another user’s parent row.

No service-role key is exposed to the browser.

Release 4 will design the separate server-to-server publishing credential/endpoint for the job hunter.

## 12. Server repository boundaries

Release 1 extends the repository pattern already present under `src/lib/repositories/`.

Expected modules:

```text
src/lib/repositories/
  opportunities.ts
  stories.ts
  observations.ts
  practice-plans.ts
```

Existing `profile.ts` changes only as required for stable evidence reconciliation. Existing `interviews.ts` changes only as required to hydrate/persist nullable Career Brain links.

### 12.1 Responsibilities

`opportunities.ts`

- create/get/list/update descriptive opportunity details;
- transition status through the atomic DB operation;
- schedule interviews through the atomic DB operation;
- append/read non-lifecycle opportunity events where needed;
- hydrate external source identity safely.

`stories.ts`

- create/get/list/update stories;
- attach and hydrate typed provenance;
- preserve review/completeness state.

`observations.ts`

- create/get/list/update observations;
- attach evidence;
- persist explicit review actions (confirm/correct/dismiss) without hiding the original claim.

`practice-plans.ts`

- create/get/list/update plans;
- attach opportunity relationships;
- hydrate rationale, criteria, and future priority-factor snapshots.

`profile.ts`

- compute/pass deterministic evidence keys;
- reconcile rather than delete evidence;
- hydrate only active current-profile evidence.

`interviews.ts`

- map nullable `practice_plan_id` and `opportunity_id` into typed session fields;
- validate plan/opportunity association when persisting new links;
- preserve all existing interview behavior when both are null.

### 12.2 What repositories must not do in Release 1

They do not:

- ask an LLM what to practice;
- create observations from interview answers automatically;
- reconcile observation confidence/trends;
- rank practice targets;
- import Google Sheets;
- call the job-hunter bot.

Those are later domain/service responsibilities.

### 12.3 API surface

No new public UI route or browser-facing API is required for Release 1 completion.

The repository layer plus tests is sufficient to exercise the foundation. Release 2 owns the user-facing routes/actions needed for application management, observation review, story management, and recommended practice.

## 13. TypeScript domain types

`src/lib/types.ts` receives explicit types for:

- `OpportunityStatus`, `Opportunity`, `OpportunityEvent`;
- `CareerStory`, `CareerStoryEvidence`;
- `CoachObservationType`, `CoachObservationTrend`, `CoachObservationReviewState`, `CoachObservation`, `ObservationEvidence`;
- `PracticeFormat`, `PracticePlan`, and plan-opportunity references.

`InterviewSession` gains nullable `practicePlanId` and `opportunityId`.

Types should expose domain names/camelCase rather than leaking snake_case Supabase column names into callers.

## 14. Migration and compatibility strategy

Release 1 is additive except for the deliberate internal change from destructive profile-evidence replacement to evidence reconciliation.

Migration order must ensure dependencies exist before foreign keys:

1. make profile evidence durable (`evidence_key`, active/retired state, stable save behavior);
2. add any missing `(id, user_id)` uniqueness needed on existing provenance parents, including `question_evaluations`;
3. create opportunities and opportunity events plus narrow lifecycle RPCs;
4. create career stories and story evidence;
5. create coach observations and observation evidence;
6. create practice plans and opportunity joins;
7. add nullable Career Brain links to interview sessions;
8. add/verify indexes, constraints, and RLS policies as their parents become available.

The implementation may use one or several SQL migration files, but dependency order and rollback/debuggability should remain clear.

No table or column required by existing Relay behavior is removed or renamed.

No existing interview, question, evaluation, competency, source-document, or profile row is rewritten into a new semantic category.

## 15. Existing-data behavior

Immediately after migration:

- existing profiles continue to load;
- existing active profile evidence continues to load;
- existing competencies continue to load;
- existing interview sessions continue to load with `practicePlanId = null` and `opportunityId = null`;
- all new Career Brain tables may contain zero rows;
- no new AI processing runs automatically;
- the current Relay home/practice/progress/profile UI remains functionally unchanged;
- the job-hunter bot remains untouched.

On the first profile save after Release 1, incoming evidence is written under stable deterministic keys. Legacy evidence no longer current becomes inactive rather than being destroyed.

## 16. Error and retry behavior

### Opportunity lifecycle

Status updates and interview scheduling are atomic with their corresponding history events. A failure leaves both the summary field and event history unchanged.

### Provenance links

Invalid, missing, zero-source, multi-source, or cross-user evidence references are rejected by constraints/foreign keys rather than silently ignored.

### Profile reconciliation

The profile bundle remains transactionally saved. If evidence reconciliation fails, the profile operation fails rather than leaving a partly refreshed active-evidence set.

### General repository behavior

Repository modules continue using the project’s `RepositoryError` convention or a compatible shared repository error abstraction. Persistence failures return stable, user-safe errors to higher layers while preserving provider error codes for diagnostics/tests.

Release 1 adds no new Gemini-dependent persistence path, so AI availability cannot destabilize the foundation.

## 17. Testing strategy

The repository already uses Vitest and co-located repository/domain tests. Release 1 extends that existing test setup.

### 17.1 Profile-evidence compatibility tests

Required cases:

1. saving equivalent evidence twice preserves the same persisted evidence row ID;
2. changing only volatile confidence does not change the evidence identity;
3. changed/removed evidence becomes inactive rather than deleted;
4. current profile hydration returns active evidence only;
5. historical inactive evidence can still be referenced by provenance;
6. a failed reconciliation does not leave a partial active set.

### 17.2 Opportunity tests

Required cases:

1. create/list/get opportunity hydration;
2. external `(source_system, source_external_id)` identity prevents duplicate publishing for one user;
3. the same external identity may exist for a different user;
4. status transition updates current status and appends exactly one history event atomically;
5. interview scheduling updates `next_interview_at` and appends history atomically;
6. normal descriptive updates cannot bypass lifecycle repository operations;
7. invalid/cross-user references fail.

### 17.3 Story/provenance tests

Required cases:

1. story creation/hydration preserves structured fields and tags;
2. story evidence accepts exactly one supported source type;
3. zero-source and multi-source evidence rows are rejected;
4. same-user ownership is enforced.

### 17.4 Observation tests

Required cases:

1. observations hydrate confidence, importance, trend, and review state;
2. confirming/correcting/dismissing preserves the original claim;
3. corrected text is stored separately;
4. observation evidence accepts exactly one typed source;
5. supporting/contradicting/context evidence roles persist correctly.

Release 1 does **not** test automatic learning behavior because it does not implement that behavior.

### 17.5 Practice-plan/session tests

Required cases:

1. plans persist focus, rationale, format, criteria, and opportunity relationships;
2. one plan can relate to several opportunities but only one may be marked primary;
3. existing session hydration works when both new foreign keys are null;
4. a new session may link to a practice plan and primary opportunity belonging to the same user;
5. when both session links exist, the opportunity is associated with that plan and matches the plan’s primary opportunity if one exists;
6. cross-user links are rejected.

### 17.6 Regression verification

The complete existing test suite must remain green. Verification before completion includes:

- `npm test`;
- `npm run lint`;
- production build using the repository-documented command for this environment;
- applying migrations against a clean/current schema test environment;
- confirming existing profile creation/edit and interview-session persistence still operate.

## 18. Release 1 acceptance criteria

Release 1 is complete only when all of the following are true:

1. `opportunities` can represent both considering and applied/interviewing jobs with durable external identity.
2. opportunity state history exists and status changes/interview scheduling are atomic with their history events.
3. profile evidence has stable identity across equivalent profile saves and retired evidence is preserved for provenance.
4. career stories can be stored with typed evidence provenance.
5. coach observations can be stored with confidence/trend/review state and typed supporting/contradicting/context evidence.
6. user corrections/dismissals can be represented without overwriting the original observation claim.
7. practice plans can persist their focus, rationale, format, criteria, and associated opportunities, with at most one primary opportunity.
8. interview sessions can optionally reference both the plan that created them and one primary opportunity, with consistent plan/opportunity association.
9. every new cross-record relationship is user-isolated by RLS and same-user relational constraints.
10. existing Relay profile/interview behavior works when all new tables are empty and all new session links are null.
11. no automatic coach-learning or recommendation algorithm has leaked into Release 1.
12. no Google tracker synchronization is required.
13. **no job-hunter code, workflow, secrets, SQLite state, or Telegram behavior changes are required or performed.**

## 19. Handoff to Release 2

Release 2 may assume the following stable foundation:

- opportunities are the canonical durable job/application lifecycle records inside Relay;
- current and historical profile evidence have stable provenance identities;
- stories and observations have inspectable evidence links;
- observations can carry explicit user review/correction state;
- practice plans are persisted before/alongside sessions;
- sessions may be queried by primary opportunity and practice plan.

Release 2 then re-centers the UI and interaction model around these entities. It should not need to redesign their ownership or core relational model.

## 20. Architecture invariants inherited from the umbrella design

Release 1 must not violate these project-level decisions:

1. Supabase is the long-term canonical Career Brain.
2. Relay and the Python job hunter remain separate applications.
3. the job hunter is not migrated until Releases 1–3 are proven;
4. learned observations are inferences with evidence, not facts;
5. user corrections must be durable and authoritative input to later learning logic;
6. practice is driven by explicit plans rather than opaque one-shot LLM choice;
7. existing useful Relay interview/evaluation foundations are evolved, not rewritten;
8. AI failure must not corrupt durable career evidence.
