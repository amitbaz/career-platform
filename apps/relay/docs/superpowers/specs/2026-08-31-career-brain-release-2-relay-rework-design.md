# Career Brain Release 2 — Relay Rework Design

Date: 2026-08-31
Status: Approved product direction; implementation plan follows this written spec
Depends on:
- `docs/superpowers/specs/2026-08-30-career-brain-architecture-design.md`
- `docs/superpowers/specs/2026-08-30-career-brain-release-1-foundation-design.md`

## 1. Purpose

Release 2 turns the deployed Career Brain foundation into the product Relay is supposed to be: a personal daily interview-preparation command center.

The primary interaction becomes:

> Open Relay → see what to practice next and why → start it with one action.

The product must stop asking the user to design every practice session before Relay has offered a recommendation. Manual practice remains available as an override.

Release 2 also makes the new Career Brain entities usable:

- opportunities/applications and interview dates;
- career stories;
- coach observations with evidence and user review;
- practice plans linked to opportunities;
- sessions linked to the plan and real job they prepare for.

Release 2 deliberately does **not** implement the full adaptive learning loop. It uses explicit Career Brain data plus the existing deterministic progress model to produce a useful baseline recommendation. Release 3 will add automatic observation creation/reconciliation, trend evolution, and the richer deterministic priority model.

The job-hunter remains unchanged throughout Release 2.

## 2. Release invariant

Release 2 must satisfy all of these at once:

1. the home screen leads with a recommended practice, not a mode picker;
2. the recommendation has an inspectable human-readable rationale;
3. recommended conversational practice can be shorter than a five-question mock interview;
4. the existing manual five-question conversation and hands-on flows remain valid;
5. applications, stories, and coach memory can be managed without direct browser-to-table knowledge;
6. no recommendation depends on automatic AI memory extraction that does not exist yet;
7. no job-hunter code, secrets, SQLite persistence, GitHub Actions behavior, or Telegram delivery changes.

## 3. Current baseline and constraints

Release 1 is deployed. The repository now has server-only repositories for opportunities, career stories, coach observations, practice plans, and interview Career Brain context.

The current UI is still the pre-Career-Brain product model:

```text
home
practice
progress
profile
```

`src/app/page.tsx` is a large client shell containing authentication, data loading, navigation, onboarding, home, practice, progress, profile, interview, results, voice recording, and hands-on flows.

The current API surface contains only:

```text
/api/profile
/api/interview
/api/transcribe
```

The current grounded conversational database function deliberately requires an exact five-question backbone. Release 2 must not silently redefine that existing contract because current manual interview behavior and tests rely on it.

Two Release 1 implementation notes are carried forward:

- replacing a practice plan's opportunity links does not retroactively revalidate sessions already linked to that plan;
- `scheduleOpportunityInterview` records the scheduling event at database `now()` while `next_interview_at` stores the actual scheduled interview time.

Release 2 therefore treats context on a started/completed practice plan as immutable in normal UI flows, and it does not expose backdated interview-scheduling-event editing.

## 4. Product information architecture

The primary authenticated navigation becomes:

```text
Home
Applications
Practice
Stories
Coach
Profile
```

The existing Progress experience is not deleted, but it is de-emphasized as a top-level decision. Home surfaces the useful progress summary and can link to the existing detailed Progress view.

Interview and Results remain transient session views rather than primary navigation destinations.

### 4.1 Home — command center

Home answers four questions in this order:

1. **What should I practice now?**
2. **Why is this the best use of my time?**
3. **Which real applications/interviews need attention?**
4. **What is Relay currently noticing about me?**

The first screen should contain:

- a dominant Recommended Practice card;
- primary CTA: `Start recommended practice`;
- recommendation format and approximate duration;
- `Why this?` rationale plus the signals used;
- upcoming/active applications requiring preparation;
- top reviewed coach observations, if any;
- story-bank gap/coverage summary;
- concise progress trend and recent-practice context.

A useful example:

```text
Recommended practice
Architecture communication · ~12 min

Why this?
Your next interview is for Senior Frontend Engineer at Example Co.
Your recent progress data shows architecture/system design as the weakest
active competency, so this drill will focus on decisions and tradeoffs.

[ Start recommended practice ]
```

The dashboard must have good empty states. A newly upgraded Release 1 account may have no opportunities, stories, or coach observations yet.

### 4.2 Applications

The UI label is **Applications**, while the database/domain entity remains `Opportunity` because it also stores `considering` roles.

The Applications view supports:

- list/filter by lifecycle status;
- create an opportunity manually;
- edit descriptive fields such as company, role, URL, job description, location, notes, strengths, and gaps;
- transition lifecycle state using the existing transactional repository flow;
- schedule/update the next interview time using the existing scheduling flow;
- see the append-only event timeline;
- see practice sessions/plans associated with the opportunity;
- open the job URL when present.

Creating an already-applied opportunity is a two-step server operation: create it in `considering`, then transition it transactionally to `applied`. The browser does not fake an initial status.

The view should emphasize `applied` and `interviewing` roles. `considering` is visible but is a weaker preparation signal. Terminal statuses remain searchable/history but do not drive normal recommendations.

Release 2 does not fetch a job description from a supplied URL. The user can paste/edit the description. Automated bot publication arrives in Release 4.

### 4.3 Stories

The Stories view makes the Release 1 story bank usable.

It supports:

- list active draft/confirmed stories;
- create a story;
- edit structured fields;
- tags;
- completeness display;
- confirm a story when the user considers the facts accurate;
- retire a story without deleting historical provenance;
- show attached provenance when available;
- optionally attach current profile evidence to a story from the UI.

Release 2 does not automatically extract a story from interview answers or automatically compute story quality. Direct user-entered story details are treated as user-confirmed data when the user explicitly confirms the story.

The UI must distinguish:

- **completeness** — whether the story has enough factual structure;
- **delivery quality** — how well the story was told in practice.

Release 2 only manages the former directly. Delivery learning belongs to the observation/evaluation loop and becomes richer in Release 3.

### 4.4 Coach

The Coach view is the inspectable long-term-memory surface.

For each observation it displays:

- effective observation text;
- observation type;
- trend;
- confidence/importance in a restrained secondary presentation;
- review state;
- `Why does Relay think this?` evidence detail;
- actions: Confirm, Correct, Dismiss.

For a corrected observation, the **effective text shown as guidance is the user's correction**, while the original `claim` remains available in history/details and remains unchanged in storage.

Dismissed observations are hidden from the default active list but can be shown under history.

Release 2 does not automatically generate coach observations after a session. Empty-state copy must be honest about that: persistent observations appear when evidence-backed learning is introduced/seeded, while current recommendations can still use existing progress evidence.

The UI does not expose a normal `Create observation` form because observations are coach inferences, not notes the user should have to maintain manually.

### 4.5 Practice

The Practice view becomes the manual override and practice history surface rather than the primary starting point.

It supports:

- current recommended practice summary;
- manual focus override;
- format choice;
- optional primary opportunity;
- recent practice plans/sessions;
- direct access to the existing hands-on flow.

Manual practice creates the same persisted `PracticePlan` contract as recommended practice. There must not be two unrelated session-start architectures.

## 5. Baseline recommendation engine

Release 2 needs a recommendation now, but Release 3 owns the full adaptive prioritization system. Therefore Release 2 introduces a small deterministic **baseline recommendation selector**.

It must be a pure/testable function. It does not call an LLM.

Suggested module:

```text
src/lib/practice-recommendation.ts
```

### 5.1 Inputs

The selector receives:

- opportunities;
- coach observations;
- career stories;
- existing `ProgressSnapshot`;
- recent completed sessions;
- recent practice plans;
- current time supplied explicitly for deterministic tests.

### 5.2 Output

A recommendation preview is not yet a persisted plan:

```ts
export type PracticeRecommendation = {
  format: PracticeFormat;
  primaryFocus: string;
  secondaryFocus: string | null;
  rationale: string;
  estimatedMinutes: number;
  successCriteria: string[];
  primaryOpportunityId: string | null;
  supportingOpportunityIds: string[];
  signals: PracticeRecommendationSignal[];
};
```

`signals` are user-displayable facts such as:

```text
upcoming interview · Example Co · in 3 days
reviewed weakness · ownership is not explicit enough
progress signal · architecture/system design is currently weakest
story bank · no confirmed stories yet
```

The recommendation preview deliberately does not persist a Release 3-style weighted score.

### 5.3 Deterministic precedence

Release 2 uses the following order. The first satisfied branch wins.

1. **Upcoming interview within 7 days** for a non-terminal applied/interviewing opportunity → `role_prep`, ~18 minutes, that opportunity is primary.
2. **Any interviewing opportunity** without a near-term date → `role_prep`, ~18 minutes.
3. **Reviewed coach observation** (`confirmed` or `corrected`, never `dismissed`) with importance >= 0.6 → choose a mapped format:
   - `story_gap` → `story_work`;
   - `answer_habit` or `delivery_pattern` → `technical_communication` unless clearly behavioral;
   - `knowledge_gap` or `weakness` → `targeted_drill`;
   - other reviewed weaknesses → `targeted_drill`.
4. **Applied/interviewing opportunity exists but no confirmed career story exists** → `story_work`, ~15 minutes, centered on building a reusable real example for active roles.
5. **Existing progress has a weakest competency or recurring weakness** → `targeted_drill`, ~12 minutes.
6. **Applied opportunity exists** → `role_prep`, ~18 minutes.
7. **No completed practice sessions yet** → `self_presentation`, ~10 minutes.
8. Fallback → `full_simulation`, ~30 minutes.

Terminal opportunity states (`rejected`, `withdrawn`, `closed`) never create recommendation urgency. `offer` is not normal interview-prep urgency either.

Unreviewed observations may be displayed in Coach but do not become the decisive primary recommendation in Release 2. This prevents an unreviewed AI inference from silently controlling training before Release 3 defines reconciliation behavior.

### 5.4 What Release 2 explicitly does not do

It does not:

- update observation confidence after new answers;
- decay old observations;
- compute recurrence weights;
- compute cross-job skill frequency;
- apply an over-practice formula;
- automatically extract story gaps;
- write a recommendation score into `priority_score`;
- ask an LLM to decide what the user should practice.

Those belong in Release 3.

## 6. From recommendation to persisted practice plan

A recommendation preview becomes durable only when the user starts it.

This avoids mutation on dashboard GET and prevents unused recommendation rows from accumulating.

### 6.1 Recommended start

`Start recommended practice` performs server-side orchestration:

1. authenticate;
2. reload current Career Brain inputs;
3. recompute the baseline recommendation on the server;
4. create a `PracticePlan` in `ready` state;
5. persist primary/supporting opportunity links;
6. generate the delivery artifact for that plan;
7. create a session with plan/opportunity context atomically where practical;
8. mark the plan `started`;
9. return the session plus persisted plan.

The server recomputes rather than trusting the browser's recommendation payload.

### 6.2 Manual start

Manual practice accepts validated user choices:

- focus;
- optional secondary focus;
- format;
- approximate duration;
- optional primary opportunity.

The server creates the same `PracticePlan` and then uses the same delivery orchestration as recommended practice.

### 6.3 Plan immutability after start

Once a practice plan is `started` or `completed`, normal Release 2 UI must not replace its opportunity links or redefine its focus. This avoids the Release 1 edge case where changing plan links can make previously linked session context semantically stale.

Draft/ready plans may be updated before start.

## 7. Practice delivery architecture

Release 2 needs short practice without breaking the established five-question manual interview.

### 7.1 Preserve the existing manual interview contract

The existing `create_conversation_session_with_blueprint` database function continues to require the exact five-question backbone:

```text
introduction
experience
technical
architecture
behavioral
```

Existing generic `start` conversation behavior remains source-compatible.

### 7.2 Add a separate planned-conversation contract

Release 2 adds a new migration and new repository path for practice-plan-driven conversation sessions.

Conceptually:

```text
create_planned_conversation_session_with_blueprint(
  p_blueprint,
  p_practice_plan_id,
  p_opportunity_id
)
```

Rules:

- authenticated ownership required;
- `practice_plan_id` required and owned;
- optional `opportunity_id` must be owned and valid for the plan context;
- base question count 1–5;
- base question sequences must be contiguous from 1;
- categories use existing `QuestionCategory` values but do not need the fixed five-category order;
- follow-up limits remain bounded;
- `blueprint_max_questions` database constraint expands from `5..8` to `1..8` so short planned sessions can persist honestly;
- the existing five-question function continues to clamp/validate its own contract exactly as before;
- session `practice_plan_id` and `opportunity_id` are written at creation, not attached in a later best-effort browser step.

New TypeScript repository function:

```ts
createSessionWithPracticeBlueprint(
  supabase,
  userId,
  blueprint,
  context
): Promise<InterviewSession>
```

The old `createSessionWithBlueprint` remains unchanged for generic full interviews.

### 7.3 Practice blueprint generation

Add a separate coach function rather than overloading the generic interview planner conceptually:

```ts
generatePracticeBlueprint(
  profile,
  profileEvidence,
  plan,
  context
): Promise<InterviewBlueprint>
```

The plan controls focus, format, approximate scope, and success criteria.

The generation context may include:

- primary opportunity/job description;
- supporting opportunities;
- reviewed effective coach observations relevant to the focus;
- confirmed career stories;
- current profile evidence.

Candidate facts must remain grounded in the user's evidence. A job description may shape what is asked, but it may not be used as evidence that the user has a skill or experience.

### 7.4 Format-to-question-count defaults

Release 2 uses simple defaults:

```text
targeted_drill            3 base questions
story_work                3 base questions
self_presentation         2 base questions
behavioral                3 base questions
technical_communication   3 base questions
role_prep                 4 base questions
full_simulation           5 base questions
hands_on                  existing hands-on session flow
```

The server may use fewer only when grounding cannot support the planned scope; it may not silently expand beyond the plan's intended base count.

`story_work` is delivered conversationally in Release 2. It helps the user construct/refine a real example but does **not** automatically rewrite a `career_stories` row from the answers. Story extraction/synthesis can be added later once the learning pipeline is defined.

### 7.5 Completion rules

For plan-driven conversations, fixed `>= 5 answers` completion logic is removed from that branch.

A planned conversation may complete when every persisted non-follow-up base question has an answer and any currently persisted required follow-up has been handled.

Generic legacy/manual conversation keeps its existing full-interview expectations.

When a session linked to a practice plan completes successfully:

- mark the plan `completed`;
- set `completed_at`;
- do not generate persistent coach observations automatically in Release 2.

If post-session plan completion fails after the interview evidence is safely saved, the session remains complete and the server reports/logs a recoverable plan-state error. Interview evidence must never be rolled back or lost because practice-plan bookkeeping failed.

## 8. Server application layer and APIs

Browser components must not directly know Career Brain table structure.

Release 2 adds server-side orchestration/services and authenticated API routes.

### 8.1 Dashboard service

Suggested module:

```text
src/lib/career-dashboard.ts
```

It composes existing repositories and progress logic into a UI-ready read model.

```ts
export type CareerDashboard = {
  profile: Profile;
  progress: ProgressSnapshot;
  recentSessions: InterviewSession[];
  opportunities: Opportunity[];
  upcomingOpportunities: Opportunity[];
  observations: CoachObservationSummary[];
  stories: CareerStorySummary[];
  recentPracticePlans: PracticePlan[];
  recommendation: PracticeRecommendation;
};
```

The dashboard service owns aggregation, not persistence.

### 8.2 API routes

Add:

```text
GET  /api/career/dashboard
GET  /api/opportunities
POST /api/opportunities
GET  /api/stories
POST /api/stories
GET  /api/observations
POST /api/observations
GET  /api/practice
POST /api/practice
```

Action-based POST bodies are acceptable and match the current app's API style, but each action must be explicit and validated.

#### Opportunities POST actions

```text
create
update
transition
schedule_interview
add_note
```

`create` may accept an intended initial UI state. If it is not `considering`, the route must call `createOpportunity` then the proper lifecycle repository function; it must not directly update lifecycle columns.

#### Stories POST actions

```text
create
update
confirm
retire
attach_profile_evidence
```

#### Observations POST actions

```text
confirm
correct
dismiss
```

No normal create action is exposed.

#### Practice POST actions

```text
start_recommended
start_manual
```

GET returns recent plans plus the current recomputed recommendation preview if the Practice view needs refresh independent of the full dashboard.

### 8.3 Error semantics

Routes should distinguish:

- 400 invalid input/state;
- 401 unauthenticated;
- 404 owned resource not found;
- 409 stale/incompatible practice context where appropriate;
- 500 unexpected persistence/AI failures.

User-safe errors are returned; structured server logs retain error name/message/code.

## 9. Observation evidence presentation

The Release 1 repository stores typed evidence IDs. The UI needs meaningful evidence summaries, not raw UUIDs.

Add a server read-model resolver, conceptually:

```text
src/lib/coach-memory.ts
```

It resolves observation evidence into safe display items:

```ts
export type CoachEvidenceDisplay = {
  kind: "profile_evidence" | "question_evaluation" | "career_story" | "opportunity_event";
  label: string;
  summary: string;
  role: ObservationEvidenceRole;
  reason: string | null;
};
```

Examples:

- profile evidence → project/employer + source excerpt summary;
- question evaluation → question prompt + concise evaluation weakness/strength summary;
- story → title;
- opportunity event → company/role + event description.

The browser should never have to join these sources itself.

## 10. Story completeness in Release 2

Release 1 deliberately did not compute story completeness. Release 2 may add a **deterministic UI/domain helper** because the editor needs a useful completeness indicator.

Suggested helper:

```ts
careerStoryCompleteness(storyDraft): number
```

It scores only presence of factual structure, not quality. Suggested required dimensions:

```text
context/problem
personal responsibility/ownership
actions/decisions
tradeoff or alternative
outcome
lesson/reflection
```

The helper may produce a 0–1 fraction of covered dimensions and save that value when the user edits a story.

It must never claim that a complete story is a strong interview answer.

## 11. UI decomposition

Do not add Release 2 to the existing 46 KB `page.tsx` as one more monolith.

Keep the current single authenticated client-shell architecture for this release, but extract focused view components.

Suggested structure:

```text
src/app/
  page.tsx
  relay-shell.tsx
  api-client.ts
  views/
    home-view.tsx
    applications-view.tsx
    practice-view.tsx
    stories-view.tsx
    coach-view.tsx
    profile-view.tsx
    progress-view.tsx
    interview-view.tsx
    results-view.tsx
```

The exact split may follow existing test seams, but responsibilities must become clear.

`page.tsx` should become a small entry that renders the client shell.

The shell owns:

- authentication state;
- top-level navigation/view selection;
- shared refresh after mutations;
- active session selection;
- sign-in/sign-out;
- global user-safe errors.

Views own their forms and presentation but call typed API-client functions rather than Supabase tables.

### 11.1 Navigation and transitions

Keep the repository's mobile-first rules:

- use native View Transitions API for view changes where already supported;
- do not animate layout-triggering properties;
- preserve native scrolling;
- keep touch targets large enough for mobile use.

Release 2 should prioritize clarity and speed over ornamental animation.

## 12. Recommendation UX details

The Recommended Practice card must always make the recommendation explainable.

Required fields shown:

- focus;
- format label;
- estimated minutes;
- one-paragraph rationale;
- up to three signal chips/rows;
- primary opportunity when applicable;
- success criteria preview;
- primary CTA.

If the recommendation uses a corrected observation, display the user correction, not the superseded original claim.

If there is no profile readiness for grounded conversational practice, the card must not offer a practice start that is guaranteed to fail. It should recommend finishing profile evidence first or permit only a safe non-grounded/manual fallback consistent with current product rules.

## 13. Empty-state behavior

Release 2 must work immediately after deployment even though Release 1 tables may be empty.

### No opportunities

Home shows:

> Add an application to make practice role-specific.

Recommendation falls back to progress/self-presentation/full simulation.

### No stories

Home/Stories explain that the story bank is empty and provide `Add first story`.

An active application with no confirmed story may trigger the deterministic `story_work` recommendation.

### No observations

Coach explains that no persistent coaching observations exist yet. It must not fabricate one from legacy progress text merely to fill the screen.

### No completed sessions

Recommendation defaults to self-presentation after higher urgency/application rules.

## 14. Security and ownership

All Release 1 RLS and same-user FK invariants remain authoritative.

API routes:

- use `requireUser()`;
- pass the authenticated user ID into repositories;
- do not accept a caller-provided `userId`;
- never expose service-role credentials;
- validate plan/opportunity ownership before practice generation;
- resolve observation/story provenance only within the authenticated user's data.

The Release 2 planned-conversation RPC must use `security invoker` and `auth.uid()` and preserve same-user practice-plan/opportunity foreign-key integrity.

## 15. AI boundaries

AI is allowed to:

- generate plan-specific interview questions/blueprints;
- use an opportunity's job description to shape role-specific questions;
- evaluate answers using the existing grounded evaluation path;
- generate follow-up questions using the existing interview mechanics.

AI is **not** allowed in Release 2 to:

- choose the primary recommendation;
- create durable coach observations automatically;
- update observation confidence/trend;
- invent career-story facts;
- infer that a job-description requirement is a candidate skill;
- rewrite opportunity lifecycle facts.

If practice-blueprint generation fails, the created plan may be marked `failed` with `generation_error`; no corrupt/half-created session should be presented as started.

## 16. Testing strategy

### 16.1 Recommendation unit tests

Use pure fixed fixtures and explicit `now`.

Required cases:

1. an interview in three days beats a generic weakness;
2. a reviewed `story_gap` observation maps to `story_work` when no interview urgency exists;
3. a dismissed observation never drives a recommendation;
4. an unreviewed observation does not become the decisive primary recommendation;
5. no confirmed story + active applied role produces `story_work` before generic progress practice;
6. weakest competency produces a targeted drill when no stronger signal exists;
7. first-time user falls back to self-presentation;
8. terminal opportunities do not create urgency.

### 16.2 Planned-conversation repository/SQL tests

Verify:

- 1–5 contiguous base questions accepted;
- zero or >5 base questions rejected;
- invalid/non-owned plan rejected;
- non-owned opportunity rejected;
- mismatched plan/opportunity rejected;
- session links are written at creation;
- existing five-question RPC still rejects non-backbone blueprints;
- old sessions hydrate unchanged.

### 16.3 API tests

Add route-level tests for authenticated action validation and repository orchestration where existing project patterns allow.

At minimum test:

- create-applied opportunity uses lifecycle transition rather than direct status write;
- observation correction preserves original claim;
- recommended practice is recomputed server-side;
- failed practice generation marks/fails the plan safely;
- completing a linked session completes the practice plan without losing interview evidence if plan bookkeeping fails.

### 16.4 UI tests

Extend `page.test.tsx` or extracted view tests to cover user-visible behavior:

- recommended practice is the dominant Home CTA;
- rationale is visible;
- Applications can create/edit/transition/schedule;
- Stories can create/edit/confirm;
- Coach can confirm/correct/dismiss;
- manual Practice remains available;
- empty Career Brain tables do not break Home;
- old interview flow still works.

### 16.5 Full regression

Before completion:

```bash
npm test
npm run lint
npx next build --webpack
```

Apply any Release 2 migration only to a disposable/development Supabase target first and verify the existing five-question function remains behaviorally unchanged.

## 17. Acceptance criteria

Release 2 is complete only when all of the following are true.

### Command center

- Opening Relay with a ready profile lands on Home.
- Home presents one recommended practice as the primary action.
- The recommendation shows why it was chosen using real stored/deterministic signals.
- Starting it creates a persisted practice plan and starts the correct practice delivery.

### Applications

- The user can manually add a role, including an already-applied role.
- Status changes use append-only lifecycle history.
- The user can schedule an interview and see it on Home.
- Active applications influence the recommendation selector.

### Stories

- The user can create/edit/confirm/retire a story.
- The UI distinguishes factual completeness from delivery quality.
- Existing provenance can be viewed and profile evidence can be attached.

### Coach memory

- Observations can be inspected with evidence.
- Confirm/Correct/Dismiss works.
- Corrected text becomes the effective guidance text.
- Dismissed observations do not drive Release 2 recommendations.
- No automatic observation creation is claimed or performed.

### Practice

- Recommended targeted conversational sessions may contain fewer than five base questions.
- Existing manual generic conversation remains an exact five-question backbone.
- Hands-on practice still works.
- Sessions retain practice-plan/opportunity context.
- Completed linked sessions mark their practice plans complete.

### Safety/compatibility

- Existing profile/onboarding/session history continues to load.
- Empty Release 1 tables are a valid state.
- No job-hunter change is required.
- No Google Sheet sync/import is required.
- No Release 3 adaptive-memory behavior is smuggled into this release.

## 18. Explicit non-goals

Release 2 does not include:

- job-hunter Supabase publishing;
- Google Tracker synchronization;
- automatic coach-observation extraction;
- automatic story extraction from answers;
- the final weighted recommendation/priority formula;
- observation confidence decay/reconciliation;
- scheduled reminders or notifications;
- web research of employers/interview processes;
- URL-to-job-description scraping;
- cloud IDE or executable code infrastructure;
- fine-tuning or embeddings;
- multi-user product/business features beyond existing authentication/isolation.

## 19. Release gate into Release 3

Release 3 may begin when Release 2 proves this user-visible loop:

```text
Career Brain state
      ↓
baseline recommendation + explanation
      ↓
persisted practice plan
      ↓
short/full practice session
      ↓
existing grounded evaluation/history
```

At that point the system has a stable interface for the learning engine to improve. Release 3 can then replace/extend the baseline selector with evidence-backed observation reconciliation and richer deterministic prioritization without redesigning the entire product again.
