# Career Brain Architecture Design

Date: 2026-08-30
Status: Approved architecture; implementation is intentionally split into separate release specs

## 1. Purpose

Relay is being re-centered from a generic AI interview-coach product into a personal career-preparation system whose primary job is to make Amit measurably better prepared for real software-engineering interviews.

The core product behavior is:

1. know which jobs and interviews matter now;
2. know the candidate profile, evidence, recurring strengths, weaknesses, answer habits, and reusable career stories;
3. choose the highest-value practice automatically;
4. explain why that practice was selected;
5. evaluate the practice using grounded evidence;
6. update persistent coaching memory from the result;
7. adapt the next recommendation.

The default home-screen action is therefore not “pick an interview mode.” It is “start the recommended practice.” The dashboard exists to explain that recommendation and surface the current application and coaching context.

This document defines the stable architecture, product boundaries, data ownership, sequencing, and release gates. It does not contain the detailed schema or implementation plan for each release. Those belong in dedicated release specs.

## 2. Product goal

The primary success criterion is:

> When a real interview arrives, Amit performs better because Relay has been learning from his previous answers, understanding the roles he is targeting, and deliberately training the areas that matter most.

This is more important than generic product completeness, broad user support, or feature breadth.

The system should optimize for short time-to-value for one real user before optimizing for a multi-user commercial product.

## 3. Architectural choice

Relay and the existing Python job-hunter bot remain separate applications, but they will converge on one canonical career data store: Relay's existing Supabase project.

Supabase becomes the **Career Brain**.

Conceptually:

```text
                         Supabase
                      CAREER BRAIN
                           |
        +------------------+------------------+
        |                  |                  |
        v                  v                  v
     PROFILE          APPLICATIONS       COACH MEMORY
   CV evidence        jobs/status       observations
   experience         descriptions      story bank
   strengths          interview dates   weaknesses
   target role        timeline          trends
        |                  |                  |
        +------------------+------------------+
                           |
                           v
                    RELAY DECISION ENGINE
                           |
                           v
                 RECOMMENDED PRACTICE NOW
```

The job hunter remains responsible for discovery, ranking, evaluation, cover-letter preparation, and Telegram delivery. Relay remains responsible for the interactive career-preparation experience.

The applications must not be merged into one codebase merely to obtain one database. Their responsibilities and runtimes are different, and the existing Python job-search pipeline already provides immediate value.

## 4. Migration safety boundary

The job hunter must continue to work throughout the Relay rework.

No Release 1, 2, or 3 change may require a change to `job-hunter-bot` for the daily job-search workflow to continue operating.

Until Release 4, the job hunter keeps its current operational model:

- Python pipeline;
- SQLite persistence and deduplication;
- GitHub Actions scheduling and artifact restore/upload;
- existing Gemini configuration;
- Telegram digest and document delivery;
- existing secrets and failure behavior.

The job hunter is migrated only after the Career Brain, Relay UX, and adaptive learning loop have been proven independently.

This is a hard sequencing requirement, not an implementation preference.

## 5. Source-of-truth strategy

### 5.1 Long-term canonical store

Supabase becomes the canonical store for durable career state:

- candidate profile and source-backed experience evidence;
- applications and application lifecycle;
- upcoming interview dates and interview notes;
- practice sessions and answer evidence;
- coach observations and their evidence;
- career stories and provenance;
- practice plans and recommendation rationale.

### 5.2 Job-hunter SQLite

The current SQLite database is not the long-term cross-application source of truth. It is an operational store optimized for the scheduled job-discovery pipeline.

During the eventual bot migration, SQLite may remain as a local discovery/deduplication cache if it is still useful. The design does not require eliminating it.

### 5.3 Google Job Search Tracker

The existing Google Sheet remains useful during the transition, but it is not intended to become permanent integration infrastructure.

Relevant existing records may be imported into Supabase when needed. The project should avoid building a complex bidirectional Google Sheets-to-Supabase synchronization layer unless later evidence proves it necessary.

Once Supabase is canonical, the tracker may become a report/export or be retired.

## 6. Facts versus coach inferences

The Career Brain must explicitly distinguish durable evidence from AI-generated interpretation.

### 6.1 Facts and evidence

Examples include:

- CV/source-document evidence;
- application records and job descriptions;
- interview dates and application events;
- the exact questions Relay asked;
- Amit's exact answers;
- grounded question evaluations;
- notes recorded after real interviews;
- confirmed career-story details.

These are the source material from which coaching conclusions may be derived.

### 6.2 Coach observations

Coach observations are inspectable inferences, not facts.

Examples:

- “Often moves into implementation before establishing the architectural decision and tradeoffs.”
- “Explains frontend performance debugging with strong concrete production examples.”
- “Leadership answers often undersell personal ownership.”
- “No sufficiently complete conflict/disagreement story is currently available.”

Each observation must retain enough structure to answer:

- what kind of observation is this;
- what is the exact claim;
- how important is it;
- how confident is Relay;
- which evidence supports it;
- when was it first and last observed;
- is it improving, stable, worsening, or unresolved;
- has the user confirmed, corrected, or dismissed it.

A single weak answer must not automatically become a durable personal weakness. Repeated evidence strengthens an observation; contradictory evidence weakens it; improvement changes its trend and should eventually reduce its practice priority.

## 7. User control over learned memory

Persistent AI memory must be correctable.

Relay must support three explicit user actions on coach observations:

- **Confirm** — the observation is useful and accurate;
- **Correct** — the observation is materially wrong or needs replacement wording/context;
- **Dismiss** — the observation should stop influencing recommendations.

A correction or dismissal is strong counter-evidence. Relay must not recreate the same dismissed claim from the same old evidence.

A previously dismissed observation may reappear only when substantial new evidence supports a materially new conclusion. The system must preserve enough provenance to distinguish this from silently ignoring user feedback.

## 8. Career story bank

Relay needs a first-class story bank because senior interviews depend heavily on selecting and communicating concrete professional examples.

A career story represents a real experience and may include:

- title;
- situation/context;
- role and responsibility;
- problem;
- actions and decisions;
- alternatives considered;
- tradeoffs;
- personal ownership;
- outcome;
- lessons or what would be done differently;
- evidence/provenance;
- completeness state;
- tags for interview use cases.

Typical tags include architecture, ownership, leadership, conflict, failure, ambiguity, debugging, performance, mentoring, collaboration, and product thinking.

The AI may propose story candidates from CV evidence or interview answers, but it must not invent unsupported details. Missing story fields remain missing and can become future practice targets.

Story quality and answer-delivery quality are separate concepts. Relay should be able to conclude that a strong story exists while the user is still presenting it poorly.

## 9. Practice plans

Practice is no longer synonymous with a fixed mock interview.

Relay should be free to recommend the format with the highest expected value, including:

- a short targeted drill;
- story construction or refinement;
- self-presentation practice;
- behavioral practice;
- technical communication practice;
- role-specific preparation;
- a full interview simulation;
- hands-on coding where justified.

Each generated practice session should be driven by an explicit practice plan rather than an unstructured request to an LLM.

A practice plan should describe at minimum:

- primary focus;
- optional secondary focus;
- relevant applications or interview context;
- rationale;
- recommended format;
- approximate scope/duration;
- success criteria.

Existing interview sessions should eventually link to the practice plan that created them.

## 10. Decision engine

The core decision is:

> What is the highest-value practice Amit can do right now?

The prioritization mechanism must be deterministic enough to inspect and test. An LLM may interpret job descriptions, classify evidence, or generate exercises, but it must not be the sole opaque authority deciding the next practice.

Candidate practice targets should be scored from factors such as:

- relevance across active applications;
- urgency from an upcoming interview;
- severity of a recurring weakness;
- confidence in that weakness;
- story-gap severity;
- recurrence across recent evidence;
- time since last practice;
- recent performance trend;
- frequency across target roles;
- an over-practice penalty for recently drilled areas;
- existing strengths that reduce the marginal value of more practice.

The exact formula belongs in the Release 3 spec.

The selected recommendation must include a human-readable explanation such as:

> Three active applications emphasize architecture and ownership. Recent answers show strong implementation depth but recurring weakness in decision framing.

The user may override the recommendation, but choosing a practice mode manually is secondary to the recommended flow.

## 11. Upcoming-interview override

A scheduled real interview changes prioritization materially.

When an application has a near-term interview date, Relay should shift from general development toward interview-preparation mode for that application.

The recommendation should combine:

- likely interview focus from the role description and known process information;
- weaknesses relevant to that role;
- missing or weak stories likely to be needed;
- strengths that should be sharpened into convincing interview signals;
- the remaining preparation time.

After the interview has passed, normal cross-application prioritization resumes unless follow-up preparation is needed.

## 12. Reuse of the existing Relay foundation

The rework is not a rewrite.

The following existing capabilities are valuable and should be preserved or evolved:

- Supabase project and authentication;
- source-document storage;
- CV-derived, source-backed engineering evidence;
- persisted interview sessions and questions;
- grounded question generation;
- detailed answer evaluations;
- follow-up questions;
- voice recording and transcription;
- existing interview history;
- competency evidence where it remains useful.

The current competency model becomes one signal among several. It is not sufficient as the long-term learning model because numeric scores and short strength/weakness lists cannot represent nuanced recurring answer behavior.

## 13. Product areas to de-emphasize

Until the personal coaching loop is proven, the project should not prioritize work whose main value is productization or generic breadth.

De-emphasized or postponed areas include:

- cloud-IDE infrastructure;
- sophisticated executable-code environments;
- generalization to arbitrary professions;
- billing or commercial packaging;
- generic onboarding optimization;
- elaborate score dashboards;
- broad market-intelligence infrastructure not directly needed for current applications;
- infrastructure whose only purpose is a temporary Google Sheets synchronization bridge.

Hands-on coding can remain available, but it competes for priority like any other practice format rather than driving the roadmap by default.

## 14. Reliability and failure model

Durable career evidence must survive AI failures.

If downstream AI processing fails after an answer is submitted:

- the answer remains saved;
- its successful evaluation remains saved if already produced;
- the session can still complete where possible;
- memory extraction/reconciliation is marked pending or failed;
- processing can be retried safely without duplicating evidence.

Likewise, a practice-plan generation failure must not corrupt applications, profile evidence, previous observations, or prior sessions.

AI-derived writes must be structured, validated, and idempotent where retries are possible.

## 15. Security and data ownership

Career data remains user-scoped under Supabase RLS.

New Career Brain tables must follow the ownership model already used by Relay: durable records are associated with the authenticated user, direct client access is constrained by RLS, and cross-record references must not allow one user to link to another user's data.

Any future job-hunter integration must use a narrowly scoped server-side publishing credential or endpoint. It must not expose privileged Supabase secrets to the browser or Telegram workflow output.

Detailed RLS and publishing contracts belong in their relevant release specs.

## 16. Release decomposition

The architecture is intentionally implemented through four independent release specs.

### Release 1 — Career Brain foundation

Goal: create the durable data model and compatibility layer needed by the new product direction without breaking existing Relay behavior.

Expected scope includes:

- applications and application lifecycle records;
- career stories and provenance;
- coach observations and observation evidence;
- practice plans;
- relationships from existing sessions into the new model;
- ownership/RLS rules;
- migration/compatibility strategy for existing Relay data;
- minimal server-side domain access needed to exercise the new schema.

Release 1 should be primarily foundation work. It must not require the new dashboard or the job-hunter integration to be considered complete.

### Release 2 — Relay rework

Goal: make Relay behave like a personal daily interview-preparation command center.

Expected scope includes:

- recommended-practice-first home experience;
- application management;
- practice-plan-driven session start;
- observation review, confirmation, correction, and dismissal;
- story-bank UX;
- application-linked preparation;
- adapting existing practice flows to the new architecture.

Manual practice remains an override rather than the primary flow.

### Release 3 — Adaptive learning loop

Goal: prove that Relay genuinely learns from history and changes training priorities appropriately.

Expected scope includes:

- deterministic prioritization model;
- observation creation/reconciliation rules;
- trend and confidence evolution;
- story-gap prioritization;
- interview-date urgency;
- recommendation explanation;
- acceptance fixtures that demonstrate recommendations changing after new evidence.

This release is the proof gate before the bot may be migrated.

### Release 4 — Job-hunter migration

Goal: publish selected job/application data into the Career Brain without reducing reliability of the working daily bot.

Initial integration should be additive:

```text
Job Hunter
    |
    +--> existing SQLite/cache + Telegram flow
    |
    +--> publish selected durable data to Supabase
```

A Supabase publishing failure must not prevent the bot from completing its current daily Telegram workflow.

Only after the integration is stable should the project decide whether SQLite or the Google tracker can be reduced or retired.

## 17. Release gates

### Release 1 gate

The new schema is additive and safe. Existing profile/interview functionality still has valid data relationships. Ownership constraints are tested. No bot change is required.

### Release 2 gate

A user can open Relay, understand the current applications and coaching context, receive a recommended practice, see why it was selected, and complete that practice without manually designing the session.

### Release 3 gate

The adaptive loop is demonstrably working:

```text
practice
  -> answer
  -> grounded evaluation
  -> coaching memory changes
  -> recommendation priority changes
```

Required behavioral cases include:

1. repeated weak architecture framing increases the priority of architecture communication practice;
2. repeated improvement changes the observation trend and eventually reduces that priority;
3. a corrected or dismissed false observation no longer drives recommendations from old evidence;
4. a scheduled interview can move role-specific preparation ahead of otherwise higher general weaknesses;
5. an existing strength does not receive excessive practice merely because it is common in job descriptions.

These must be verified with deterministic fixtures around the decision logic, not only by observing live LLM output.

### Release 4 gate

The job hunter can publish relevant data to Supabase while its existing daily behavior continues to succeed independently. Supabase failures fail open for the bot's current workflow.

## 18. Testing strategy across releases

Three layers are required as the new architecture matures.

### Database/domain tests

Cover ownership, relationship integrity, evidence provenance, correction/dismissal behavior, retry/idempotency, and migration compatibility.

### Deterministic decision-engine tests

Use fixed applications, weaknesses, strengths, upcoming interviews, stories, and practice history. Assert the chosen target, priority ordering, and explanation inputs.

The central prioritization algorithm must be testable without an LLM.

### AI contract tests

Validate structured model output, evidence references, unsupported-claim rejection, and safe fallback behavior. Exact wording may vary; factual grounding and schema validity may not.

## 19. Explicit non-goals of this architecture phase

This architecture does not require:

- fine-tuning a model on Amit's history;
- vector search or embeddings;
- merging the Python bot into the Next.js repository;
- replacing the job hunter's persistence before Release 4;
- real-time synchronization with Google Sheets;
- autonomous job application submission;
- a generic multi-user product roadmap.

Structured relational data and targeted retrieval are preferred initially because the data volume is small, correctness matters, and the learned state must be inspectable.

## 20. Architecture invariants

The following decisions should remain stable across release specs unless a later design review explicitly changes them:

1. Supabase is the long-term canonical Career Brain.
2. Relay and the job hunter remain separate applications.
3. The job hunter is not migrated until Releases 1-3 are complete and proven.
4. Persistent coaching claims are evidence-backed and user-correctable.
5. Career stories may not contain invented unsupported facts.
6. Recommended practice is the primary product action; manual mode selection is an override.
7. Practice format is adaptive and may be shorter than a mock interview.
8. Upcoming real interviews can override normal training priorities.
9. Core career history remains valid when AI processing fails.
10. The decision engine is inspectable and deterministically testable.
11. Existing useful Relay foundations are evolved rather than discarded.
12. Productization work is secondary to measurable improvement in real interview readiness.

## 21. Next design step

After approval of this umbrella architecture, the next document is the dedicated **Release 1 — Career Brain Foundation** design spec.

That spec will define the concrete data model, table relationships, lifecycle/status semantics, provenance rules, RLS policy design, compatibility with current Relay tables, server-side boundaries, migration strategy, error behavior, and Release 1 verification criteria.

No implementation plan should be written until the Release 1 design spec itself has been reviewed and approved.
