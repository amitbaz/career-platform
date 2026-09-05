# Practice-First Profile Grounding Design

Date: 2026-09-01
Status: Product direction defined; implementation pending
Supersedes, only for interview-start readiness behavior, the hard profile gate in `docs/superpowers/specs/2026-08-29-tech-profile-grounded-interviews-design.md`.
Depends on:
- `docs/superpowers/specs/2026-08-30-career-brain-architecture-design.md`
- `docs/superpowers/specs/2026-08-31-career-brain-release-2-relay-rework-design.md`

## 1. Problem

Relay currently treats missing concrete work examples as a prerequisite failure.

A user can upload a valid CV, receive a usable role/seniority/expertise profile, and then be blocked from starting a conversational interview because the extracted profile does not contain all of:

- two concrete engineering projects or work examples;
- identifiable technologies;
- responsibilities or outcomes.

The API responds with a message such as:

> Add two concrete engineering projects or work examples, responsibilities or outcomes before starting a personalized interview.

This is internally consistent with the original profile-grounding milestone, but it conflicts with the current Career Brain product goal.

The user may be unable to produce polished examples precisely because that is an interview skill they need to practice. Requiring strong examples before practice creates a circular dependency:

```text
need strong examples to practice
        ^             |
        |             v
need practice to remember, structure, and improve strong examples
```

Relay should help break that loop rather than enforce it.

## 2. Product decision

**Profile readiness becomes advisory, not gating.**

A profile still needs to exist before Relay can personalize practice, but a profile does not need to pass the current evidence-readiness threshold before practice can start.

The governing principle becomes:

> Practice starts with the context Relay has, and practice helps uncover the context Relay is missing.

Missing work examples, ownership details, decisions, constraints, and outcomes are coaching targets. They are not prerequisites.

This change does **not** weaken factual-grounding rules. Relay must still avoid inventing candidate experience. When source evidence is thin, it asks broader discovery questions and lets the candidate supply details in their own answers.

## 3. Approaches considered

### A. Remove the API gate only

Delete the readiness check and let the existing generic planner run against sparse evidence.

Advantages:
- smallest code change;
- immediately unblocks the user.

Problems:
- sparse profiles may receive awkward questions optimized for evidence that is not there;
- provider behavior becomes responsible for discovering examples;
- the product still has no explicit semantics for why the session is broader.

### B. Advisory readiness plus an intentional discovery-oriented blueprint — chosen

Keep the deterministic readiness assessment, but reinterpret it as a source-grounding diagnostic. When readiness is false, start a deterministic `limited-grounding` conversation whose objectives include helping the candidate surface real examples.

Advantages:
- no user block;
- deterministic and testable behavior for sparse profiles;
- preserves anti-hallucination boundaries;
- reuses the existing five-question generic-session contract and `limited-grounding` persistence;
- fits the Career Brain direction that missing story fields become future practice targets.

Trade-off:
- sparse-profile sessions are intentionally broader than evidence-rich sessions.

### C. Automatically extract new profile facts and career stories during the same session

Use each answer to enrich profile evidence and the story bank immediately.

Advantages:
- fastest path toward a richer Career Brain.

Problems:
- crosses the current Release 2/Release 3 boundary;
- introduces provenance, confirmation, reconciliation, and correction semantics;
- risks treating an improvised or poorly remembered answer as a durable career fact.

This is deferred. The exact answer is already durable session evidence and can be used by the later adaptive-learning/story-suggestion loop.

## 4. Readiness semantics

The existing `ProfileReadiness` shape remains compatible:

```ts
export type ProfileReadiness = {
  ready: boolean;
  missing: string[];
};
```

Its meaning changes from **permission to practice** to **quality of source grounding**.

### `ready: true`

The source documents contain enough concrete detail for strongly evidence-grounded interview planning.

### `ready: false`

The source documents are usable for profile context but are thin in one or more categories. Relay should start practice anyway and use the missing categories to shape broader questions.

`missing` remains useful because it tells Relay and the user what still needs to emerge, for example:

```text
two concrete engineering projects or work examples
responsibilities or outcomes
```

No endpoint may return a validation error solely because `profile.readiness.ready === false`.

A completely missing profile remains a valid blocking condition: Relay still needs at least the existing profile/onboarding contract before personalized practice begins.

## 5. User journey

### 5.1 Evidence-rich profile

```text
Upload CV
  -> profile extraction
  -> readiness = ready
  -> confirm profile
  -> start practice
  -> evidence-grounded questions
```

This path stays effectively unchanged.

### 5.2 Sparse-but-usable profile

```text
Upload CV
  -> profile extraction
  -> readiness = not ready + missing signals
  -> confirm profile
  -> start practice immediately
  -> broader discovery-oriented questions
  -> candidate remembers/describes real examples
  -> answer is evaluated and saved as session evidence
  -> later Career Brain work may propose reusable stories/facts for confirmation
```

There is no intermediate form asking the user to manufacture two polished projects before Relay will help them.

## 6. Generic interview planning for sparse profiles

The existing generic conversation must keep its exact five-question backbone because its persistence contract depends on it:

```text
introduction
experience
technical
architecture
behavioral
```

When `assessProfileReadiness(evidence).ready` is false, `generateInterviewBlueprint(...)` should return a deterministic discovery-oriented blueprint instead of rejecting the start request.

Recommended pure planner boundary:

```ts
export function buildExperienceDiscoveryBlueprint(
  profile: Pick<ProfileDraft,
    "role" | "seniority" | "summary" | "narrative" |
    "expertise" | "characteristics" | "competencies"
  >,
  evidence: EvidenceItem[],
  readiness: ProfileReadiness,
  now?: Date,
): InterviewBlueprint;
```

The blueprint uses the existing contract:

```ts
status: "limited-grounding"
```

No new database enum/status is required. `fallbackReason` explains that source detail is limited and that the session is intentionally starting broader; it is not presented as a provider failure.

A representative reason is:

> Your source profile has limited concrete example detail, so this session starts broader and helps you uncover real projects, ownership, decisions, and outcomes as you answer.

### 6.1 Discovery question objectives

The exact wording can remain deterministic and role-aware, but the five objectives should be equivalent to:

1. **Introduction** — establish what kind of engineer the candidate is and what they have mainly worked on recently without demanding a polished story.
2. **Experience** — help the candidate recall one real piece of work, even if they do not yet consider it an impressive interview example; establish situation and personal responsibility.
3. **Technical** — stay with the discovered example when useful and uncover a real implementation decision, problem, alternative, or constraint.
4. **Architecture** — surface requirements, constraints, system boundaries, or design evolution from a real feature/project; do not assume architecture ownership that the source never claimed.
5. **Behavioral** — surface collaboration, ambiguity, disagreement, delivery pressure, or another interpersonal challenge and what the candidate did.

Example experience prompt:

> Think of one piece of work you remember clearly, even if it does not feel like a strong interview story yet. What was happening, and what part were you personally responsible for?

The important distinction is that Relay asks the user to **discover** an example rather than asking them to arrive with one already prepared.

### 6.2 Partial evidence

Sparse does not mean evidence-free.

If a reliable evidence item is relevant to a question, the discovery blueprint may reference it through `evidenceIds` and use it as an anchor. Questions without a safe source anchor use a general objective and `evidenceIds: []`.

Relay must never turn a weak source hint into a fabricated project name, metric, ownership claim, or result.

### 6.3 Evidence-rich planning remains unchanged

If readiness is true, the current Gemini-backed grounded blueprint path remains the default, including schema validation, one repair attempt, and the existing deterministic provider-failure fallback.

The new discovery builder is a **profile-quality branch**, not a replacement for grounded planning.

## 7. Plan-driven Practice remains non-blocking

Release 2 already has the correct high-level rule in `practice-service`: recommended and manual practice require a profile, not `profile.readiness.ready`.

This design makes that behavior an explicit invariant:

- do not add a readiness gate to `startRecommendedPractice`;
- do not add a readiness gate to `startManualPractice`;
- `generatePracticeBlueprint` must remain capable of producing practice from partial evidence;
- `story_work`, `self_presentation`, targeted drills, and role preparation may all be useful before the profile becomes source-rich.

The generic `/api/interview` start path and plan-driven Practice therefore share one philosophy after this change.

## 8. Evaluation semantics for discovery answers

A sparse-profile session creates an important grounding distinction.

The candidate may say a true autobiographical fact that was never written in the CV. Relay must not call that claim unsupported merely because the source document did not already contain it.

For a blueprint question with no source evidence target (`evidenceIds.length === 0`):

- the submitted answer is newly supplied session evidence;
- evaluate relevance, clarity, structure, specificity, ownership, decision framing, trade-offs, and outcomes from what the candidate actually said;
- do not classify a first-person career detail as unsupported solely because it was absent from the CV;
- still penalize irrelevant, internally incoherent, or contradictory answers;
- improved-answer examples may reorganize or sharpen facts from the current answer, but may not invent missing metrics, technologies, projects, or outcomes.

For a question that **does** reference source evidence:

- the existing evidence-grounded checks continue to apply;
- contradiction with known source evidence remains meaningful;
- feedback may point out when the answer failed to use the grounded example effectively.

The deterministic fallback evaluator should follow the same distinction. A no-evidence discovery question should not manufacture `unsupportedClaims` merely to satisfy a generic grounding heuristic; relevance and missing-signal coverage are sufficient reasons for a follow-up.

## 9. What gets learned from the session

This change deliberately separates **practice evidence** from **durable profile facts**.

Immediately after an answer:

- the exact answer is persisted with the interview session as it is today;
- the answer may be evaluated and used for follow-up questions in that session;
- the answer may influence existing session/progress outputs where current architecture already does so.

This change does **not** automatically:

- append the answer to `profile_evidence`;
- rewrite the uploaded CV/source document;
- create or confirm a `career_story`;
- create a persistent coach observation from one answer;
- mark the profile `ready` because the user mentioned two examples conversationally.

Those actions require the later Career Brain learning/reconciliation boundary so the user can inspect and confirm what becomes durable memory.

Future work may propose something like:

```text
practice answer
  -> candidate story/fact proposal
  -> user confirmation/correction
  -> durable Career Brain evidence
  -> readiness may improve
```

That is intentionally outside this slice.

## 10. UX and copy

The UI must stop presenting readiness as a gate.

### Profile/readiness copy

Evidence-rich example:

> Your source profile has enough detail for evidence-grounded practice.

Sparse example:

> You can practice now. Relay will start broader and help you uncover stronger project, ownership, and outcome examples as you answer.

If useful, the missing categories can be shown as secondary guidance, but never as a disabled-state requirement.

Avoid phrases such as:

- `Profile evidence gate still needs...`
- `Add ... before starting...`
- `You cannot start until...`

### Interview/result limited-grounding surface

The current warning heading `Limited grounding` is technically correct but reads like a failure. Use a neutral heading such as:

> Broader practice

Then render the blueprint's actual `fallbackReason` so the user can tell whether the session is broader because source detail is thin or because planning fell back after an AI failure.

The product should communicate reduced source specificity without making the user feel they used the product incorrectly.

## 11. API behavior

### `POST /api/interview`, `{ action: "start", mode: "conversation" }`

Before:

```text
profile missing       -> 400
profile not ready     -> 400
profile ready         -> 200 + grounded/fallback session
```

After:

```text
profile missing       -> 400
profile not ready     -> 200 + discovery-oriented limited-grounding session
profile ready         -> 200 + current grounded/fallback session
```

The route no longer owns the meaning of readiness. It obtains the profile and asks `generateInterviewBlueprint` for the appropriate blueprint.

Hands-on behavior remains unchanged.

## 12. Persistence and compatibility

No migration is expected.

Keep:

- existing `profiles.profile_ready` / `profile_missing` persistence;
- existing `ProfileReadiness` data shape;
- existing `InterviewBlueprint.status` values;
- existing generic five-question RPC/validation contract;
- existing planned-practice 1–5 question contract;
- existing session/question/evaluation persistence.

Only semantics and orchestration change.

Legacy profiles with persisted `ready: false` immediately benefit because the route stops rejecting them. Existing completed sessions require no backfill.

The `ProfileReadiness` type/documentation should be updated so future code does not reintroduce the old assumption that it is an authorization gate.

## 13. Failure behavior

- **No profile:** keep the existing actionable `Create your profile first.` failure.
- **Sparse profile:** never fail merely for sparse evidence; start discovery practice.
- **Evidence-rich blueprint provider failure:** keep the current bounded repair + deterministic fallback behavior.
- **Discovery blueprint construction:** deterministic and local; it should not introduce a new provider dependency.
- **Database/session-start failure:** keep current safe server error behavior.
- **Evaluation provider failure:** preserve the answer and use current deterministic evaluation fallback, with the discovery semantics above.

## 14. Testing and acceptance

Use red → green → refactor.

Required tests:

1. `assessProfileReadiness` still reports the same missing categories for sparse evidence.
2. `generateInterviewBlueprint` given a sparse profile returns `limited-grounding` instead of trying to reject the user.
3. The discovery blueprint has exactly five base questions in the existing generic category order.
4. Discovery questions do not invent evidence identifiers or candidate facts.
5. Partial reliable evidence can still anchor an appropriate discovery question.
6. `/api/interview` returns 200 for `readiness.ready === false` and persists/returns the generated session.
7. `/api/interview` still returns 400 when no profile exists.
8. Evidence-rich profiles continue through the existing grounded generator path.
9. Plan-driven recommended/manual practice remains startable with a sparse profile.
10. A relevant first-person answer to a no-evidence discovery question is not labelled unsupported merely because it was not present in the CV.
11. Irrelevant discovery answers still score lower on relevance and can trigger a follow-up.
12. Profile UI says practice can start now and no longer uses gate wording.
13. Limited-grounding interview/result UI uses neutral `Broader practice` framing and shows the reason.

Acceptance scenario:

```text
Given a signed-in user with a valid parsed CV
And the profile readiness result is false because fewer than two concrete examples were extracted
When the user starts conversational practice
Then Relay starts a session successfully
And the session explains that questions are broader because source evidence is thin
And Relay asks questions that help the user surface a real example
And the user's answer is evaluated as practice evidence
And no new durable CV fact or career story is silently created
```

## 15. Non-goals

This slice does not:

- remove the CV/profile onboarding concept entirely;
- make a blank profile sufficient for personalized practice;
- add a new database blueprint status;
- change the five-question generic persistence contract;
- change the plan-driven practice question-count contract;
- automatically extract/confirm career stories from answers;
- automatically promote interview answers into profile evidence;
- implement Release 3 observation reconciliation or adaptive prioritization;
- change the job-hunter bot or its SQLite-to-Supabase migration path.

## 16. Product invariant going forward

Future Relay features must not confuse **missing coaching context** with **lack of permission to practice**.

When safe personalization is possible from partial context, Relay should prefer:

```text
start -> discover -> coach -> learn with confirmation
```

over:

```text
collect perfect context -> allow practice
```

That is the product behavior this design establishes.
