# Career Brain Release 2 — Execution Handoff

**Branch:** `codex/career-brain-release-2`
**Plan:** `docs/superpowers/plans/2026-08-31-career-brain-release-2-relay-rework.md`
**Spec:** `docs/superpowers/specs/2026-08-31-career-brain-release-2-relay-rework-design.md`
**Status as of 2026-09-01:** All 11 tasks complete and reviewed. The whole-branch final review's
four Important findings are fixed. **The branch is code-complete; the resume point is the human
migration verification below, then merge.**

## Verification state at `aa090ee`

```bash
npm install
npm test                    # 502 passing, 0 failing
npm run lint                # clean
npx tsc --noEmit            # clean
npx next build --webpack    # clean
```

Baseline before this branch was 215 tests. Everything added since is on the branch.

## Commits (oldest first)

| Commit | Task | Subject |
|---|---|---|
| `e5074b5` | 1 | feat: add baseline practice recommendation |
| `ebf1fa6` | 2 | feat: add planned practice session starts |
| `fa640f7` | 3 | feat: generate plan-specific practice blueprints |
| `0403075` | 3 fix | test: cover AI-path base-count rejection for practice blueprints |
| `a4fb2b5` | 4 | feat: orchestrate persisted recommended practice |
| `6e7f269` | 4 fix | fix: bound practice plan listing and guard failed-plan compensation |
| `9620241` | 5 | feat: add Career Brain dashboard read model |
| `2f54cc3` | 5 fix | fix: dedupe dashboard observation-text and input-loading logic |
| `c7acf2a` | 5 fix | test: cover loadPracticeInputs' progress composition |

Tasks 1–5 each passed an independent code review. Tasks 3, 4, and 5 required fix rounds; all
findings were addressed and re-reviewed. No Critical findings were ever raised.

## Final review findings — all four fixed on 2026-09-01

The whole-branch review raised 0 Critical and 4 Important findings. Each was verified against the
source before being fixed, and each fix carries a test that fails without it.

| Commit | Finding | Fix |
|---|---|---|
| `2dc14bd` | Finish was gated on the generic five-answer rule while the API had moved to `canExplicitlyCompleteConversation`, leaving the control permanently dead for every 2–4 question planned format | Rule extracted to the client-safe `@/lib/conversation-completion`, imported by both the view and the route; planned sessions now label as "Practice session" |
| `ba30228` | The manual-practice forwarding assertion sent only zero values, so hardcoding `primaryOpportunityId` or `successCriteria` kept the suite green | Assertion sends non-default values for every field; a second case covers the omitted-field defaults |
| `b274218` | `pickReviewedObservation` had no `observationType` filter, so a confirmed `strength` was rendered to the user as "Work on: …" | `strength` and `story_strength` excluded, per design §5.3's "reviewed weaknesses" scope |
| `aa090ee` | `jobUrl` and `location` were editable and persisted but never rendered, so design §4.2's "open the job URL when present" was unmet | Detail panel renders the location and an `Open job posting` link |

Each fix was mutation-checked: reverting the production change makes the new test fail.

## Remaining work

No plan tasks remain, and the migration is verified. Before merge:

1. Push the migration to the hosted project (`supabase db push`) — the only remaining side effect.
2. Triage the deferred minor findings listed further down; none is a correctness defect.

## Migration verification (ruling R1) — DONE 2026-09-01

`supabase/migrations/202608310001_planned_practice_sessions.sql` ran green against a local Supabase
stack (`supabase start && supabase db reset`), and every invariant below was checked at runtime by
`supabase/tests/202608310001_planned_practice_sessions.verify.sql`, which is committed on this branch
and ends in `rollback` so it leaves nothing behind. All 11 checks passed.

The check that mattered most: **the JS `p_blueprint` payload keys DO match the SQL
`jsonb_to_recordset` field list.** That was the one genuinely open item -- a mismatch would have
nulled the fields silently at runtime and no test covered it. Check 3 now pins it.

Every RPC-level rejection also returned the SQLSTATE the route mapping expects: `P0002` for an
unowned plan or opportunity, `22023` for a non-`ready` plan, a bad question count, and an
unlinked/non-primary opportunity. That closes the second unverified item -- the `P0002` -> 404 and
`22023` -> 409 mapping in `/api/practice` is asserted against a mocked `RepositoryError`, and the live
codes agree with it.

The historical note below is kept for context.

### Original outstanding action (now closed)

`supabase/migrations/202608310001_planned_practice_sessions.sql` had **never been executed**. It was
verified only by SQL desk-check against the existing migrations, plus TypeScript repository tests.

The Supabase CLI is linked from the main checkout, not from a worktree. You do **not** need to merge
the branch to test the migration — pull just that one file into the main checkout:

```bash
cd /Users/amitbaz/interviewer-app       # the main checkout, where supabase is linked
git checkout codex/career-brain-release-2 -- supabase/migrations/202608310001_planned_practice_sessions.sql
supabase db push                        # against a disposable/development project first
```

Then verify these invariants, which static review could not settle:

- the OLD `create_conversation_session_with_blueprint` still rejects a three-question blueprint
- the NEW `create_planned_conversation_session_with_blueprint` accepts 1, 3, and 5 base questions and rejects 0 and 6
- both new RPCs reject a plan or opportunity belonging to another user
- both new RPCs reject an opportunity not linked to the plan, and honor a `primary` link
- both atomically set session context AND flip the plan to `started`
- a second start of the same plan fails
- legacy sessions with null `practice_plan_id`/`opportunity_id` still hydrate

One item is genuinely open: nothing pins the JS `p_blueprint` payload key names against the SQL
`jsonb_to_recordset` field list. A field-name mismatch would only appear at runtime.

Also unverified: the `P0002` → 404 and `22023` → 409 SQLSTATE mapping in `/api/practice` is asserted
only against a mocked `RepositoryError`. If PostgREST surfaces those differently, those cases degrade
to 500 with a user-safe message — a UX gap, not a safety one.

## Decisions made during execution (rulings)

These were judgment calls made without the user present. Revisit any you disagree with.

- **R1** — Do not run `supabase db push` or smoke-test a deployed environment from the agent session;
  external side effect on a shared resource. *Cost if wrong:* SQL defects survive until the human runs it.
- **R2** — Task 1 implements the spec §5.3 observation-type→format mapping that the plan abbreviated
  (`story_gap`→`story_work`; `answer_habit`/`delivery_pattern`→`technical_communication`;
  `knowledge_gap`/`weakness`→`targeted_drill`; else `targeted_drill`). *Cost:* one extra branch + test.
- **R3** — `canExplicitlyCompleteConversation` lives in `src/lib/practice-service.ts`, imported by the
  interview route. The plan named both files without assigning the export. *Cost:* a one-line move.
- **R4** — Task 4 EXTENDED the existing `src/app/api/interview/route.test.ts`; the plan said "Create"
  but the file already existed with the passing suite. *Cost:* none, strictly safer.
- **R5** — Practice blueprints set `maxQuestions = min(8, baseCount + maxFollowUps)`, never
  `maxQuestions === baseCount`. The migration's clamp is a FLOOR, not headroom, so equal values would
  make `record_conversation_turn` refuse every follow-up and silently void the rule that a planned
  conversation cannot complete while a follow-up is unanswered. *Cost:* one or two more follow-ups
  than intended.
- **R6** — Orchestration always passes the plan's primary opportunity id. Both RPCs validate the
  primary match only when the id is non-null. *Cost if wrong:* planned sessions lose role context.
- **R7** — `practicePlanWarning` is always emitted as `string | null`, never omitted, so Tasks 8/9
  cannot trip over "absent means fine". *Cost:* one always-present null field on two responses.
- **R8** — Tasks 4 and 5 were authorized to edit files outside their plan file lists
  (`practice-plans.test.ts`; `practice-service.ts` and `practice-recommendation.ts`), because those
  lists were forcing duplicated logic. *Cost:* extra files in two commits, all additive.

## Deferred minor findings — triage before merge

Recorded by reviewers, not fixed. The final whole-branch review (Task 11) should triage these.

**Task 1** — `Date.parse` used without a `Number.isNaN` guard in `compareOpportunityUrgency` and
`pickInterviewingOpportunity`, unlike `pickNearTermInterview`. · Weakness-priority ternary duplicated in
`buildProgressWeaknessRecommendation`. · `isClearlyBehavioral` is a keyword heuristic standing in for a
missing data-model field — confirm with the design owner before Release 3 builds on it.

**Task 2** — *(recommended before merge)* `assertPracticeConversationBlueprint`'s rejection branches have
zero negative-path coverage; the `isFollowUp` rule is a stated spec requirement and TypeScript is the only
layer enforcing it. · *(recommended before merge)* RPC tests don't pin the `p_blueprint.questions[0]` key
names — the one part of the unverified-SQL risk a cheap test could close. · Test double always returns
`kind: "conversation"`, including for the hands-on test. · ~40 lines of ownership/authorization checks are
duplicated verbatim between the two RPCs; a fix applied to one copy only is a silent security divergence. ·
`question.category` is inserted unvalidated and has no DB check constraint, while `difficulty` does.

**Task 3** — `validatePracticeBlueprint` omits the generic validator's "General objective:" rule. · Fallback
evidence-to-question mapping is a round-robin stand-in for `interview-planner.ts`'s keyword scoring.

**Task 4** — Unmapped `RepositoryError` codes fall to a generic 500 body while `practice-service.ts` asserts
the opposite contract for the same class. · 401s are logged at `console.error`, which will bury real errors. ·
The route-level follow-up reject test doesn't discriminate the planned vs generic rule (the unit test does). ·
`deliverPractice` takes the whole `PracticeInputs` but needs three fields. · `GET /api/practice` now caps at
20 plans with no cursor for older ones.

**Task 5** — Career-story evidence display sets both `label` and `summary` to the story title. ·
Per-observation evidence resolution isn't globally batched (N observations → up to 4N queries).

## Two bugs the review gate caught

Worth knowing, because they shape how much rigor the remaining tasks deserve:

1. **`markPlanFailed` could corrupt a good plan.** If the session-start RPC committed but the response read
   then failed, the catch marked the plan `failed` while a live session pointed at it — `updatePracticePlan`
   applied status unconditionally. Fixed with an atomic conditional update guarded on `status = 'ready'`.
2. **A duplicated helper had already drifted.** `effectiveObservationText` existed in two modules; one
   trimmed the claim, the other didn't. A whitespace-padded claim would make Home display one thing while
   the recommender matched on another, for the same user. No test caught it — every fixture was
   whitespace-clean. Fixed by exporting one implementation.

## Notes for whoever resumes

- The detailed per-task ledger and implementer/reviewer reports live in
  `.superpowers/sdd/2026-08-31-career-brain-release-2-relay-rework/` in the executing worktree. That
  directory is **git-ignored** — it does not travel with this branch. This file is the durable record.
- The plan was executed with the `superpowers:subagent-driven-development` workflow: one implementer
  subagent per task, an independent reviewer per task, then fix rounds with scoped re-reviews. That costs
  roughly three agent runs per task. Tasks 8–10 are the most expensive remaining work; batching 6+7 and
  9+10 into single dispatches, and reviewing only the API auth surface, the `page.tsx` refactor, and the
  final whole-branch pass, would cut the remaining cost substantially.
- Do not start implementation on `main`.
