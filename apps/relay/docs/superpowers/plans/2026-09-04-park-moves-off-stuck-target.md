# Park Moves Off A Stuck Target Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give an interview question a way to be finished without being answered, so that parking (and running out of rescues) actually moves the candidate onto a different target instead of re-showing the one they blanked on.

**Architecture:** A question row gains an explicit set-aside marker (`set_aside_at` / `set_aside_reason`) plus a record of the exchanges the assessor refused to score (`non_answers`). One shared helper decides which row is the candidate's current question, using "not answered **and** not set aside" instead of "not answered". The director's park decision starts naming the target it moves *to*, and coverage status is derived from the set-aside marker rather than from whichever intent was written to a row last.

**Tech Stack:** Next.js (App Router, route handlers), TypeScript strict, Supabase/Postgres with plpgsql RPCs, Vitest (jsdom), Tailwind.

**Spec:** `docs/superpowers/specs/2026-09-01-adaptive-interviewer-conversation-design.md` — §2.1 (the blackout defect), §8.2 (`park` = acknowledge, move to another target, return later if turns remain), §9.2 (coverage state), §9.3 rules 3/5/6/7. Issue: https://github.com/amitbaz/interviewer-app/issues/10

## Global Constraints

- **Database is Supabase/Postgres, not SQLite.** `AGENTS.md` is wrong on this point; do not act on it. Migrations are numbered SQL files in `supabase/migrations/` named `YYYYMMDDNNNN_<topic>.sql`.
- Every migration gets a matching text-assertion test in `src/lib/supabase/<topic>-migration.test.ts` that reads the SQL file and asserts on its contents. Follow `src/lib/supabase/coverage-target-required-migration.test.ts` exactly.
- **Any change to `record_conversation_turn` SQL must be mirrored in the in-memory fake** in `src/lib/adaptive-interviewer-flow.test.ts` (the `recordTurnRpc` function, `src/lib/adaptive-interviewer-flow.test.ts:389-473`). That fake is the only thing standing between a SQL change and a green-but-lying test suite.
- Postgres functions stay `security invoker` and `set search_path = public`. Every function definition is followed by `revoke all on function ... from public;` and `grant execute on function ... to authenticated;` naming the **full argument type list**.
- TypeScript strict, two-space indent, semicolons, `@/*` alias for `src/`. PascalCase types, camelCase values.
- Document exported functions, types and RPC contracts: purpose, inputs/outputs, side effects, invariants. Comments explain intent and trade-offs, never restate code.
- Run `npm test`, `npm run lint`, and `npx next build --webpack` before the final commit.
- Never commit anything under `data/`. Never commit `.env.local`.
- Conventional Commit subjects, imperative mood.

### Decisions locked during design (do not re-litigate)

1. **The candidate's blackout words are stored and shown.** Each unscored exchange is appended to the row's `non_answers` array with the prompt it answered, and the transcript renders them.
2. **Real mode gets `skipped`, coach mode gets `parked`.** This falls out of the set-aside reason: reason `parked` → status `parked`; reason `rescue-budget-spent` → status `skipped`. Real mode has parking disabled, so it only ever produces the second.
3. **Coverage statuses are persisted-and-returned now, not rendered.** The completion response gains a coverage report; no results-screen work in this plan.
4. **Turn budget is unchanged.** Blackouts still do not count as spent turns; the interview drains through the target list instead.
5. **Out of scope, mentioned separately:** targets left in `open` (answered but not covered) are still never revisited by the mover. Pre-existing, unrelated to this bug, file separately if wanted.

---

## File Structure

**Created:**
- `supabase/migrations/202609040001_park_moves_off_target.sql` — adds the set-aside columns and the unscored-exchange log; replaces `record_conversation_turn` to write them.
- `src/lib/supabase/park-moves-off-target-migration.test.ts` — text assertions over that SQL.
- `src/lib/interview-current-question.ts` — the single definition of "which row is the candidate looking at".
- `src/lib/interview-current-question.test.ts` — unit tests for it.

**Modified:**
- `src/lib/types.ts` — `SetAsideReason`, `NonAnswerRecord`, three new `PlannedQuestion` fields, `parkedTargetId` on the rescue intent.
- `src/lib/repositories/interviews.ts` — row mapping, transcript rendering, the turn-persistence payload.
- `src/lib/interview-director.ts` — park names a destination; the mover excludes the target being left; decisions report what they set aside.
- `src/lib/interview-coverage.ts` — status derived from the set-aside marker; end-of-session coverage report.
- `src/app/api/interview/route.ts` — use the shared current-question helper; pass the set-aside reason through; return the coverage report.
- `src/lib/coach.ts` — carry the set-aside reason out of `nextTurn`.
- `src/lib/adaptive-interviewer-flow.test.ts` — the acceptance test, and the fake RPC mirroring the SQL.
- `src/lib/interview-director.test.ts`, `src/lib/interview-coverage.test.ts` — updated for the new shapes.

---

### Task 1: Reproduce the blackout end-to-end

A failing acceptance test, committed as a `it.fails` so the suite stays green while it is red. Task 8 flips it. Nothing else in this task.

**Files:**
- Test: `src/lib/adaptive-interviewer-flow.test.ts` (add inside the existing `describe("adaptive interviewer flow", ...)` block, after the rescue test at `src/lib/adaptive-interviewer-flow.test.ts:736`)

**Interfaces:**
- Consumes: `runScriptedSession({ mode, answers })` — the existing in-file harness (`src/lib/adaptive-interviewer-flow.test.ts:581`). It drives the real repository functions against an in-memory Supabase fake, and picks each turn's question with the same rule the route uses.
- Produces: nothing. This is a characterization test.

- [ ] **Step 1: Write the failing acceptance test**

```ts
  // Issue #10. Four consecutive blackouts in coach mode: narrow rescue, park,
  // then the rescue budget is spent. The director advances to a second target
  // on turn 3 and writes its question down -- but the candidate is shown the
  // first row's prompt on every single turn, because a row with no answer is
  // still "the current question". Flipped to `it` in the final task.
  it.fails("moves the candidate onto a different question after a blackout", async () => {
    const session = await runScriptedSession({
      mode: "coach",
      answers: ["i don't know", "i am having a blackout", "i don't know", "i am having a blackout"],
    });
    const asked = session.messages
      .filter((message) => message.role === "interviewer")
      .map((message) => message.content);
    expect(new Set(asked).size).toBeGreaterThan(1);
  });
```

- [ ] **Step 2: Run it and confirm it fails for the right reason**

Run: `npx vitest run src/lib/adaptive-interviewer-flow.test.ts -t "moves the candidate onto a different question"`
Expected: PASS as a test (because `it.fails` inverts it). To see the real failure, temporarily change `it.fails` to `it` and re-run — expect `expected 1 to be greater than 1`, meaning the candidate saw exactly one distinct interviewer line across four turns. Change it back to `it.fails` before committing.

- [ ] **Step 3: Commit**

```bash
git add src/lib/adaptive-interviewer-flow.test.ts
git commit -m "test: pin the blackout that traps a candidate on one question"
```

---

### Task 2: Add the set-aside columns and the unscored-exchange log

Schema plus the RPC that writes it. Nothing reads these fields yet, so behavior is unchanged and the suite must stay green.

**Files:**
- Create: `supabase/migrations/202609040001_park_moves_off_target.sql`
- Create: `src/lib/supabase/park-moves-off-target-migration.test.ts`
- Modify: `src/lib/types.ts` (add the two new types near `RescueStyle` at `src/lib/types.ts:25`; extend `PlannedQuestion` at `src/lib/types.ts:142-167`; extend the rescue variant of `Intent` at `src/lib/types.ts:44`)
- Modify: `src/lib/repositories/interviews.ts` (`mapQuestion` around `src/lib/repositories/interviews.ts:76-100`; the inline fallback question object at `src/lib/repositories/interviews.ts:295`; `ConversationTurnPersistence` at `src/lib/repositories/interviews.ts:684-692`; the RPC call at `src/lib/repositories/interviews.ts:703-720`)
- Modify: `src/lib/adaptive-interviewer-flow.test.ts` (row defaults around `src/lib/adaptive-interviewer-flow.test.ts:373`; `recordTurnRpc` at `src/lib/adaptive-interviewer-flow.test.ts:405-473`)

**Interfaces:**
- Consumes: the existing `record_conversation_turn(uuid, text, numeric, jsonb, jsonb, jsonb, jsonb, jsonb, text, numeric, jsonb, jsonb, jsonb, jsonb, uuid, text, jsonb, jsonb, jsonb, boolean, boolean)` from `supabase/migrations/202609010001_adaptive_interviewer.sql:130`.
- Produces:
  - `type SetAsideReason = "parked" | "rescue-budget-spent"`
  - `type NonAnswerRecord = { prompt: string; answer: string; at: string }`
  - `PlannedQuestion.setAsideAt: string | null`, `PlannedQuestion.setAsideReason: SetAsideReason | null`, `PlannedQuestion.nonAnswers: NonAnswerRecord[]`
  - `Intent` rescue variant gains `parkedTargetId?: string | null`
  - `ConversationTurnPersistence.setAsideReason: SetAsideReason | null`
  - RPC `record_conversation_turn(..., p_set_aside_reason text)` — 22 arguments.

- [ ] **Step 1: Write the failing migration test**

Create `src/lib/supabase/park-moves-off-target-migration.test.ts`:

```ts
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const sql = readFileSync("supabase/migrations/202609040001_park_moves_off_target.sql", "utf8");

describe("park moves off target migration", () => {
  it("adds the set-aside marker and the unscored-exchange log", () => {
    expect(sql).toMatch(/add column set_aside_at timestamptz/);
    expect(sql).toMatch(/add column set_aside_reason text/);
    expect(sql).toMatch(/add column non_answers jsonb not null default '\[\]'::jsonb/);
  });

  it("constrains the set-aside reason to the two the director can produce", () => {
    expect(sql).toMatch(/set_aside_reason in \('parked', 'rescue-budget-spent'\)/);
  });

  it("drops the old turn function before replacing it with the wider signature", () => {
    expect(sql).toMatch(/drop function if exists public\.record_conversation_turn\(uuid, text, numeric/);
    expect(sql).toMatch(/p_set_aside_reason text\s*\n\s*\)/);
  });

  it("appends the unscored exchange instead of overwriting it", () => {
    expect(sql).toMatch(/non_answers = case\s*\n\s*when p_non_answer then coalesce\(non_answers, '\[\]'::jsonb\) \|\|/);
  });

  it("clears the set-aside marker when a row is asked again", () => {
    expect(sql).toMatch(/set_aside_at = null,\s*\n\s*set_aside_reason = null/);
  });

  it("keeps the function security invoker and grants the new signature", () => {
    expect(sql).not.toMatch(/security definer/);
    expect(sql).toMatch(/security invoker/);
    expect(sql).toMatch(/grant execute on function public\.record_conversation_turn\(uuid, text, numeric, jsonb, jsonb, jsonb, jsonb, jsonb, text, numeric, jsonb, jsonb, jsonb, jsonb, uuid, text, jsonb, jsonb, jsonb, boolean, boolean, text\) to authenticated/);
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `npx vitest run src/lib/supabase/park-moves-off-target-migration.test.ts`
Expected: FAIL with `ENOENT: no such file or directory, open 'supabase/migrations/202609040001_park_moves_off_target.sql'`

- [ ] **Step 3: Write the migration**

Create `supabase/migrations/202609040001_park_moves_off_target.sql`. Copy the entire function body from `supabase/migrations/202609010001_adaptive_interviewer.sql:130-333` and change only what is shown below — the declare block, the lock, the budget lookups, the degraded update, the evidence call, the follow-up branch and the grants are all unchanged apart from the new trailing parameter.

```sql
-- Issue #10: a row the candidate blanked on could never stop being their
-- current question. `answer` was the only "this row is done" signal, and a
-- non-answer is deliberately never scored, so the row stayed current forever
-- and every later question was written to rows nobody was ever shown.
--
-- `set_aside_at`/`set_aside_reason` give a row a second way to be finished.
-- `non_answers` keeps what the candidate actually typed on a turn that was not
-- scored, together with the prompt it answered: the row's `prompt` column is
-- overwritten by each re-ask, so without this the exchange leaves no trace.
alter table public.interview_questions
add column set_aside_at timestamptz,
add column set_aside_reason text,
add column non_answers jsonb not null default '[]'::jsonb;

alter table public.interview_questions
add constraint interview_questions_set_aside_reason_check
check (set_aside_reason is null or set_aside_reason in ('parked', 'rescue-budget-spent'));

-- The signature gains a parameter, so the old overload must go rather than be
-- replaced in place: leaving both would make every call ambiguous.
drop function if exists public.record_conversation_turn(uuid, text, numeric, jsonb, jsonb, jsonb, jsonb, jsonb, text, numeric, jsonb, jsonb, jsonb, jsonb, uuid, text, jsonb, jsonb, jsonb, boolean, boolean);

create or replace function public.record_conversation_turn(
  p_question_id uuid,
  p_answer text,
  p_score numeric,
  p_dimensions jsonb,
  p_strengths jsonb,
  p_needs_work jsonb,
  p_missing_points jsonb,
  p_better_structure jsonb,
  p_improved_answer text,
  p_relevance numeric,
  p_supported_claims jsonb,
  p_expected_signals_present jsonb,
  p_unsupported_claims jsonb,
  p_dimension_reasons jsonb,
  p_next_question_id uuid,
  p_next_prompt text,
  p_follow_up jsonb,
  p_asked_intent jsonb,
  p_assistance jsonb,
  p_non_answer boolean,
  p_degraded boolean,
  p_set_aside_reason text
)
returns table(session_id uuid)
language plpgsql
security invoker
set search_path = public
as $$
declare
  v_user_id uuid := auth.uid();
  v_question public.interview_questions%rowtype;
  v_shift public.interview_questions%rowtype;
  v_total integer;
  v_follow_ups integer;
  v_parent_follow_ups integer;
  v_session_max_follow_ups integer;
  v_session_max_questions integer;
  v_follow_up_limit integer;
begin
  -- ... unchanged from 202609010001 through the `record_interview_evidence`
  -- call: authentication check, `for update of q` lock, blueprint budget
  -- lookup, the degraded flag update, and `if not p_non_answer then ... end if`.

  -- Mark the answered question with the intent it was asked under and any
  -- assistance the interviewer granted before scoring it.
  update public.interview_questions
  set non_answer = p_non_answer,
      assistance = coalesce(p_assistance, '[]'::jsonb),
      non_answers = case
        when p_non_answer then coalesce(non_answers, '[]'::jsonb) || jsonb_build_object(
          'prompt', coalesce(prompt, ''),
          'answer', p_answer,
          'at', to_char(now() at time zone 'utc', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"')
        )
        else coalesce(non_answers, '[]'::jsonb)
      end,
      -- Only ever set, never cleared here: clearing belongs to the branch that
      -- asks a row again, below.
      set_aside_at = case when p_set_aside_reason is null then set_aside_at else now() end,
      set_aside_reason = coalesce(p_set_aside_reason, set_aside_reason),
      updated_at = now()
  where id = p_question_id
    and user_id = v_user_id;

  if p_follow_up is not null then
    -- ... unchanged from 202609010001: limit checks, sequence shift, insert.
  elsif p_next_question_id is not null then
    update public.interview_questions q
    set prompt = trim(p_next_prompt),
        asked_intent = p_asked_intent,
        asked_at = now(),
        -- Returning to a parked target makes its row the current question
        -- again (spec §9.3 rule 3). The `answer is null` guard below still
        -- holds for a set-aside row, which is what makes the return possible.
        set_aside_at = null,
        set_aside_reason = null,
        updated_at = now()
    where q.id = p_next_question_id
      and q.session_id = v_question.session_id
      and q.user_id = v_user_id
      and q.answer is null;

    if not found then
      raise exception 'Owned next question was not found' using errcode = 'P0002';
    end if;
  end if;

  return query select v_question.session_id;
end;
$$;

revoke all on function public.record_conversation_turn(uuid, text, numeric, jsonb, jsonb, jsonb, jsonb, jsonb, text, numeric, jsonb, jsonb, jsonb, jsonb, uuid, text, jsonb, jsonb, jsonb, boolean, boolean, text) from public;
grant execute on function public.record_conversation_turn(uuid, text, numeric, jsonb, jsonb, jsonb, jsonb, jsonb, text, numeric, jsonb, jsonb, jsonb, jsonb, uuid, text, jsonb, jsonb, jsonb, boolean, boolean, text) to authenticated;
```

- [ ] **Step 4: Run the migration test to verify it passes**

Run: `npx vitest run src/lib/supabase/park-moves-off-target-migration.test.ts`
Expected: PASS, 6 tests.

- [ ] **Step 5: Add the types**

In `src/lib/types.ts`, immediately after `RescueStyle` (`src/lib/types.ts:25`):

```ts
/**
 * Why a question stopped being the candidate's current one without ever being
 * answered. `parked` is recoverable -- the interview may come back to it while
 * turns remain (spec §9.3 rule 3); `rescue-budget-spent` is not.
 */
export type SetAsideReason = "parked" | "rescue-budget-spent";

/**
 * One exchange the assessor refused to score, kept verbatim. The prompt is
 * copied in because the row's own `prompt` is overwritten by the next re-ask.
 */
export type NonAnswerRecord = {
  prompt: string;
  answer: string;
  at: string;
};
```

Change the rescue variant of `Intent` (`src/lib/types.ts:44`):

```ts
  /**
   * `targetId` is the target the interviewer's line is ABOUT. For every style
   * but `park` that is the target being rescued. A `park` moves the interview
   * elsewhere, so its `targetId` is the destination and `parkedTargetId` names
   * the target being set aside. Absent on intents persisted before this change.
   */
  | { kind: "rescue"; targetId: string; style: RescueStyle; hook: string | null; parkedTargetId?: string | null }
```

Add to `PlannedQuestion` (`src/lib/types.ts:142-167`), next to `nonAnswer`:

```ts
  /** Set when this row was finished without an answer; null while it is still open. */
  setAsideAt: string | null;
  setAsideReason: SetAsideReason | null;
  /** Every unscored exchange on this row, oldest first. */
  nonAnswers: NonAnswerRecord[];
```

- [ ] **Step 6: Map the new columns**

In `src/lib/repositories/interviews.ts`, add to the object `mapQuestion` returns, next to `nonAnswer` (`src/lib/repositories/interviews.ts:90`):

```ts
    setAsideAt: typeof row.set_aside_at === "string" ? row.set_aside_at : null,
    setAsideReason: (row.set_aside_reason as PlannedQuestion["setAsideReason"]) ?? null,
    nonAnswers: Array.isArray(row.non_answers) ? (row.non_answers as NonAnswerRecord[]) : [],
```

Import `NonAnswerRecord` alongside the existing type imports. Add the same three fields to the inline fallback question literal at `src/lib/repositories/interviews.ts:295` (`setAsideAt: null, setAsideReason: null, nonAnswers: []`).

Extend `ConversationTurnPersistence` (`src/lib/repositories/interviews.ts:684`):

```ts
  /** Set when this turn finishes the answered row without an answer; null otherwise. */
  setAsideReason: SetAsideReason | null;
```

And pass it in the RPC call (`src/lib/repositories/interviews.ts:719`, after `p_degraded`):

```ts
    p_set_aside_reason: next.setAsideReason,
```

- [ ] **Step 7: Mirror the SQL in the in-memory fake**

In `src/lib/adaptive-interviewer-flow.test.ts`, add to the question-row defaults created by the blueprint fake (`src/lib/adaptive-interviewer-flow.test.ts:373`, next to `non_answer: false`):

```ts
        set_aside_at: null,
        set_aside_reason: null,
        non_answers: [],
```

In `recordTurnRpc`, replace the two-line mark-up of the answered row (`src/lib/adaptive-interviewer-flow.test.ts:442`) with:

```ts
    question.non_answer = nonAnswer;
    question.assistance = args.p_assistance ?? [];
    // Mirrors 202609040001: append the unscored exchange, and set (never
    // clear) the set-aside marker on the answered row.
    if (nonAnswer) {
      question.non_answers = [
        ...(Array.isArray(question.non_answers) ? question.non_answers : []),
        { prompt: (question.prompt as string) ?? "", answer: args.p_answer, at: nowIso() },
      ];
    }
    if (args.p_set_aside_reason != null) {
      question.set_aside_at = nowIso();
      question.set_aside_reason = args.p_set_aside_reason;
    }
    question.updated_at = nowIso();
```

And in the `p_next_question_id` branch (`src/lib/adaptive-interviewer-flow.test.ts:521`), after `next!.asked_intent = ...`:

```ts
      // Mirrors 202609040001: asking a row again makes it current again.
      next!.set_aside_at = null;
      next!.set_aside_reason = null;
```

Also add `set_aside_at: null, set_aside_reason: null, non_answers: []` to the follow-up row the fake inserts (the object ending at `src/lib/adaptive-interviewer-flow.test.ts:508`).

- [ ] **Step 8: Fix every call site the new required field breaks**

Run: `npx tsc --noEmit`
Expected: errors only where a `ConversationTurnPersistence` literal now lacks `setAsideReason`, and where a `PlannedQuestion` literal lacks the three new fields. Add `setAsideReason: null` / `setAsideAt: null, setAsideReason: null, nonAnswers: []` at each. Repeat until clean.

- [ ] **Step 9: Run the full suite**

Run: `npm test`
Expected: PASS. The acceptance test from Task 1 is still `it.fails`, still red underneath.

- [ ] **Step 10: Commit**

```bash
git add supabase/migrations/202609040001_park_moves_off_target.sql src/lib/supabase/park-moves-off-target-migration.test.ts src/lib/types.ts src/lib/repositories/interviews.ts src/lib/adaptive-interviewer-flow.test.ts
git commit -m "feat: let an interview question be set aside without an answer"
```

---

### Task 3: One definition of the candidate's current question

Four places decide which row the candidate is looking at, all by the same wrong rule. Replace them with one helper that also honours the set-aside marker, and render the unscored exchanges the transcript has been dropping.

**Files:**
- Create: `src/lib/interview-current-question.ts`
- Create: `src/lib/interview-current-question.test.ts`
- Modify: `src/app/api/interview/route.ts:112`, `:158`, `:230-238`
- Modify: `src/lib/repositories/interviews.ts:171-208` (`transcriptFor`)
- Modify: `src/lib/adaptive-interviewer-flow.test.ts:604` (the harness's own picker)

**Interfaces:**
- Consumes: `PlannedQuestion` including the fields added in Task 2.
- Produces:
  - `currentQuestion(questions: PlannedQuestion[]): PlannedQuestion | null`
  - `isAwaitingAnswer(question: PlannedQuestion): boolean`

- [ ] **Step 1: Write the failing unit tests**

Create `src/lib/interview-current-question.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { currentQuestion, isAwaitingAnswer } from "@/lib/interview-current-question";
import type { PlannedQuestion } from "@/lib/types";

function question(overrides: Partial<PlannedQuestion>): PlannedQuestion {
  return {
    id: "q1",
    sequence: 1,
    category: "communication",
    competencyId: null,
    competencyName: null,
    difficulty: "foundational",
    isFollowUp: false,
    prompt: "Tell me about that.",
    answer: null,
    createdAt: "2026-09-04T09:00:00.000Z",
    askedIntent: null,
    assistance: [],
    nonAnswer: false,
    setAsideAt: null,
    setAsideReason: null,
    nonAnswers: [],
    ...overrides,
  };
}

describe("currentQuestion", () => {
  it("is the first row with no answer", () => {
    const rows = [question({ id: "a", answer: "done" }), question({ id: "b" }), question({ id: "c" })];
    expect(currentQuestion(rows)?.id).toBe("b");
  });

  it("skips a row that was set aside without an answer", () => {
    const rows = [
      question({ id: "a", setAsideAt: "2026-09-04T09:01:00.000Z", setAsideReason: "parked" }),
      question({ id: "b" }),
    ];
    expect(currentQuestion(rows)?.id).toBe("b");
  });

  it("is null once every row is answered or set aside", () => {
    const rows = [
      question({ id: "a", answer: "done" }),
      question({ id: "b", setAsideAt: "2026-09-04T09:01:00.000Z", setAsideReason: "rescue-budget-spent" }),
    ];
    expect(currentQuestion(rows)).toBeNull();
  });

  it("treats a set-aside row as no longer awaiting an answer", () => {
    expect(isAwaitingAnswer(question({ setAsideAt: "2026-09-04T09:01:00.000Z", setAsideReason: "parked" }))).toBe(false);
    expect(isAwaitingAnswer(question({}))).toBe(true);
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `npx vitest run src/lib/interview-current-question.test.ts`
Expected: FAIL with `Failed to resolve import "@/lib/interview-current-question"`

- [ ] **Step 3: Write the helper**

Create `src/lib/interview-current-question.ts`:

```ts
import type { PlannedQuestion } from "@/lib/types";

/**
 * Whether this row is still waiting on the candidate.
 *
 * Both halves are load-bearing. `answer` alone cannot decide it: a non-answer
 * is deliberately never scored, so a row the candidate blanked on keeps a null
 * `answer` forever and would stay current for the rest of the interview
 * (issue #10). The set-aside marker is the other way a row can be finished.
 */
export function isAwaitingAnswer(question: PlannedQuestion): boolean {
  return question.answer === null && question.setAsideAt === null;
}

/**
 * The one question the candidate is looking at, or null when the interview has
 * run out of rows. Callers must not re-derive this: the rule lives here so the
 * question picker, the transcript, and the completion check cannot drift apart.
 */
export function currentQuestion(questions: PlannedQuestion[]): PlannedQuestion | null {
  return questions.find(isAwaitingAnswer) ?? null;
}
```

- [ ] **Step 4: Run the unit tests to verify they pass**

Run: `npx vitest run src/lib/interview-current-question.test.ts`
Expected: PASS, 4 tests.

- [ ] **Step 5: Route the four call sites through it**

In `src/app/api/interview/route.ts`, add the import and replace:

```ts
import { currentQuestion, isAwaitingAnswer } from "@/lib/interview-current-question";
```

`src/app/api/interview/route.ts:112`:

```ts
      const question = currentQuestion(session.questions);
```

`src/app/api/interview/route.ts:158`:

```ts
      if (!currentQuestion(updated.questions)) {
```

`src/app/api/interview/route.ts:230-238` — a set-aside row's bubbles must stay visible, so the visible set is "every row that is no longer awaiting an answer", plus the current one:

```ts
function visibleConversation(session: InterviewSession): InterviewSession {
  if (!session.questions) return session;
  const visibleQuestionIds = new Set(
    session.questions.filter((question) => !isAwaitingAnswer(question)).map((question) => question.id),
  );
  const nextQuestion = currentQuestion(session.questions);
  if (nextQuestion) visibleQuestionIds.add(nextQuestion.id);
  return {
    ...session,
    messages: session.messages.filter((message) => visibleQuestionIds.has(message.id.split(":")[0])),
  };
}
```

In `src/lib/adaptive-interviewer-flow.test.ts:604`, the harness picks its turn the same way the route does — keep them identical:

```ts
    const question = currentQuestion(session.questions);
```

(add `import { currentQuestion } from "@/lib/interview-current-question";` to that file).

- [ ] **Step 6: Render the unscored exchanges in the transcript**

Replace `transcriptFor` in `src/lib/repositories/interviews.ts:171-208`. The doc comment changes because the stopping rule changed:

```ts
/**
 * Renders the planned questions as a conversation transcript, stopping after
 * the question the candidate is currently on. The whole plan is persisted when
 * the session starts, but revealing it at once would show the candidate every
 * upcoming question (and, through the blueprint panel, its expected signals)
 * before they answer the current one.
 *
 * A row can carry more than one exchange: each unscored attempt overwrites the
 * row's `prompt`, so the prompts and answers of those attempts are replayed
 * from `nonAnswers` before the row's current prompt. Without that, a candidate
 * who blanked twice and then recovered would see a transcript in which neither
 * blank ever happened.
 */
function transcriptFor(questions: PlannedQuestion[], answerTimes: Map<string, string>): Message[] {
  const current = questions.findIndex(isAwaitingAnswer);
  const revealed = current === -1 ? questions : questions.slice(0, current + 1);
  return revealed.flatMap((question) => {
    const attempts: Message[] = question.nonAnswers.flatMap((record, index) => [
      {
        id: `${question.id}:attempt-${index}:question`,
        role: "interviewer" as const,
        content: record.prompt,
        createdAt: record.at,
      },
      {
        id: `${question.id}:attempt-${index}:answer`,
        role: "candidate" as const,
        content: record.answer,
        createdAt: record.at,
      },
    ]);
    const interviewer: Message = {
      id: `${question.id}:question`,
      role: "interviewer",
      // Null until the interviewer authors it (revealFirstQuestion or a
      // later turn); render an empty bubble rather than widen Message.
      content: question.prompt ?? "",
      createdAt: question.createdAt,
    };
    if (!question.answer) return [...attempts, interviewer];
    return [...attempts, interviewer, {
      id: `${question.id}:answer`,
      role: "candidate" as const,
      content: question.answer,
      createdAt: answerTimes.get(question.id) ?? question.createdAt,
    }];
  });
}
```

Add `isAwaitingAnswer` to that file's imports.

- [ ] **Step 7: Run the full suite**

Run: `npm test`
Expected: PASS. The acceptance test is still `it.fails` — nothing sets a row aside yet, so behavior is unchanged.

- [ ] **Step 8: Commit**

```bash
git add src/lib/interview-current-question.ts src/lib/interview-current-question.test.ts src/app/api/interview/route.ts src/lib/repositories/interviews.ts src/lib/adaptive-interviewer-flow.test.ts
git commit -m "refactor: decide the candidate's current question in one place"
```

---

### Task 4: Make park choose where it goes

**Files:**
- Modify: `src/lib/interview-director.ts:75-83` (target selection), `:118-137` (the stuck branch), `:35-39` (`DirectorDecision`)
- Test: `src/lib/interview-director.test.ts`

**Interfaces:**
- Consumes: `TargetState`, `SetAsideReason`, `DirectorInput` (unchanged shape).
- Produces:
  - `DirectorDecision` gains `setAside: SetAsideReason | null` — what this decision finishes the answered row as, `null` when it finishes nothing.
  - A `park` intent now carries `targetId` = the destination and `parkedTargetId` = the target being set aside.

- [ ] **Step 1: Write the failing director tests**

Add to `src/lib/interview-director.test.ts`, following the existing state-builder helpers in that file:

```ts
  it("parks by moving to a different target and naming the one it leaves", () => {
    const decision = decideIntent(stuckInput({
      mode: "coach",
      states: [
        { id: "a", status: "open", rescuesSpent: 1, askedIntents: [{ kind: "rescue", targetId: "a", style: "narrow", hook: null }] },
        { id: "b", status: "unasked", rescuesSpent: 0, askedIntents: [] },
      ],
      currentTargetId: "a",
    }));
    expect(decision.intent).toMatchObject({ kind: "rescue", style: "park", targetId: "b", parkedTargetId: "a" });
    expect(decision.setAside).toBe("parked");
  });

  it("sets the stuck target aside when the rescue budget is spent", () => {
    const decision = decideIntent(stuckInput({
      mode: "real",
      states: [
        { id: "a", status: "open", rescuesSpent: 1, askedIntents: [{ kind: "rescue", targetId: "a", style: "narrow", hook: null }] },
        { id: "b", status: "unasked", rescuesSpent: 0, askedIntents: [] },
      ],
      currentTargetId: "a",
    }));
    expect(decision.intent).toMatchObject({ kind: "advance", targetId: "b", reason: "rescue-budget-spent" });
    expect(decision.setAside).toBe("rescue-budget-spent");
  });

  it("never advances back onto the target it is leaving", () => {
    const decision = decideIntent(stuckInput({
      mode: "real",
      states: [
        { id: "a", status: "parked", rescuesSpent: 1, askedIntents: [{ kind: "rescue", targetId: "a", style: "narrow", hook: null }] },
      ],
      currentTargetId: "a",
    }));
    expect(decision.intent.kind).toBe("candidate-questions");
  });

  it("does not park when there is nowhere to move to", () => {
    const decision = decideIntent(stuckInput({
      mode: "coach",
      states: [
        { id: "a", status: "open", rescuesSpent: 1, askedIntents: [{ kind: "rescue", targetId: "a", style: "narrow", hook: null }] },
      ],
      currentTargetId: "a",
    }));
    expect(decision.intent.kind).toBe("candidate-questions");
    expect(decision.setAside).toBe("rescue-budget-spent");
  });
```

If `src/lib/interview-director.test.ts` has no `stuckInput` helper, add one above the describe block that builds a `DirectorInput` with `read: "stuck"`, `round: roundFor("tech-lead")`, `policy: modePolicyFor(mode)`, `turnsUsed: 1`, `turnBudget: 8`, `sessionRescues: 1`, `unsupportedClaims: []`, `answer: "i don't know"`, `canContinueCurrentTarget: true`, `now: "2026-09-04T09:00:00.000Z"`, and `states` mapped from the shorthand above into full `TargetState` objects (`target: { id, competencyId: null, competencyName: null, category: "communication", evidenceIds: [], difficulty: "foundational", objective: "", expectedSignals: ["signal"], rubricCriteria: [], required: true }`, `turnsSpent: 1`).

- [ ] **Step 2: Run to verify they fail**

Run: `npx vitest run src/lib/interview-director.test.ts`
Expected: FAIL — the park test reports `targetId: "a"` and `setAside` is `undefined`.

- [ ] **Step 3: Change the director**

In `src/lib/interview-director.ts`, extend the decision type (`src/lib/interview-director.ts:35-39`):

```ts
export type DirectorDecision = {
  intent: Intent;
  /** Non-null exactly when the intent spends rescue budget. */
  assistance: AssistanceRecord | null;
  /**
   * How this decision finishes the row the candidate just failed to answer, or
   * null when it finishes nothing. Persisted as the row's set-aside reason,
   * which is what stops it from being served again (issue #10).
   */
  setAside: SetAsideReason | null;
};
```

Add `SetAsideReason` to the type imports. Replace the selection helpers and `advance` (`src/lib/interview-director.ts:75-87`):

```ts
function parkedTargets(input: DirectorInput): TargetState[] {
  return input.states.filter((state) => state.status === "parked");
}

/**
 * The next target to work on, never the one being left. Excluding it matters
 * because a just-parked target is itself `parked`, so the parked fallback below
 * would otherwise hand back the very target the interview is trying to escape.
 */
function nextTarget(input: DirectorInput, leaving: string | null): TargetState | null {
  const elsewhere = (state: TargetState) => state.target.id !== leaving;
  return unaskedTargets(input).find(elsewhere) ?? parkedTargets(input).find(elsewhere) ?? null;
}

function advance(input: DirectorInput, reason: AdvanceReason, setAside: SetAsideReason | null = null): DirectorDecision | null {
  const next = nextTarget(input, input.currentTargetId);
  if (!next) return null;
  return { intent: { kind: "advance", targetId: next.target.id, reason }, assistance: null, setAside };
}

function closing(input: DirectorInput, setAside: SetAsideReason | null = null): DirectorDecision {
  return { intent: { kind: input.round.closing } as Intent, assistance: null, setAside };
}
```

Replace the stuck branch (`src/lib/interview-director.ts:125-137`):

```ts
  // Rule 6: a non-answer never earns a harder question.
  if (input.read === "stuck" && state) {
    const questionBudget = state.rescuesSpent < input.policy.rescuesPerQuestion;
    const sessionBudget = input.sessionRescues < input.policy.rescuesPerSession;
    const style = nextRescueStyle(state, input.policy);

    if (questionBudget && sessionBudget && style) {
      if (style !== "park") {
        return {
          intent: { kind: "rescue", targetId: state.target.id, style, hook: style === "hook" ? hookFor(state) : null },
          assistance: { style, at: input.now },
          setAside: null,
        };
      }
      // Park is "acknowledge, move to another target, come back later if turns
      // remain" (spec §8.2), so it only exists when there is another target.
      // With nowhere to go, "I'll come back to it" would re-ask the same
      // question -- the exact blackout this move exists to prevent.
      const destination = nextTarget(input, state.target.id);
      if (destination) {
        return {
          intent: { kind: "rescue", targetId: destination.target.id, style: "park", hook: null, parkedTargetId: state.target.id },
          assistance: { style: "park", at: input.now },
          setAside: "parked",
        };
      }
    }
    return advance(input, "rescue-budget-spent", "rescue-budget-spent")
      ?? closing(input, "rescue-budget-spent");
  }
```

Then add `setAside: null` to the four remaining decision literals: the open at `src/lib/interview-director.ts:152`, the challenge at `:164`, and the probe at `:171`.

- [ ] **Step 4: Run the director tests to verify they pass**

Run: `npx vitest run src/lib/interview-director.test.ts`
Expected: PASS. Existing tests in that file that assert `{ kind: "rescue", style: "park" }` on the *stuck* target need updating to the new shape — they were asserting the bug.

- [ ] **Step 5: Stop counting a park as the destination's own rescue**

A park intent is now recorded on the row of the target it moved *to*. Without this, that target would look like it had already used its park. In `src/lib/interview-director.ts:58-62`:

```ts
/**
 * Rescue styles already spent ON this target. A `park` intent is stored against
 * the target it moved TO, not the one it set aside, so it is excluded here --
 * it is not a rescue of the target whose row happens to carry it. Parking the
 * same target twice is prevented by its per-question rescue budget instead.
 */
function usedRescueStyles(state: TargetState): Set<RescueStyle> {
  return new Set(
    state.askedIntents.flatMap((intent) =>
      intent.kind === "rescue" && !intent.parkedTargetId ? [intent.style] : [],
    ),
  );
}
```

- [ ] **Step 6: Run the full suite**

Run: `npm test`
Expected: PASS, except any test asserting the old park shape — update those to the new one. The acceptance test stays `it.fails`: nothing writes `setAside` to the database yet.

- [ ] **Step 7: Commit**

```bash
git add src/lib/interview-director.ts src/lib/interview-director.test.ts
git commit -m "feat: make park name the target it moves to"
```

---

### Task 5: Derive parked and skipped from what happened

**Files:**
- Modify: `src/lib/interview-coverage.ts:43-95`
- Test: `src/lib/interview-coverage.test.ts`

**Interfaces:**
- Consumes: `PlannedQuestion.setAsideAt` / `.setAsideReason` from Task 2.
- Produces: `deriveCoverageState` unchanged in signature; `statusFor` now returns `parked` for reason `parked` and `skipped` for reason `rescue-budget-spent`.

- [ ] **Step 1: Write the failing coverage tests**

Add to `src/lib/interview-coverage.test.ts` (reuse that file's existing question/target builders):

```ts
  it("marks a target parked when one of its rows was set aside to come back to", () => {
    const state = deriveCoverageState(
      [target("a")],
      [question({ id: "a", askedIntent: { kind: "open", targetId: "a" }, setAsideAt: "2026-09-04T09:01:00.000Z", setAsideReason: "parked" })],
      [],
    );
    expect(state[0].status).toBe("parked");
  });

  it("marks a target skipped when it was set aside for good", () => {
    const state = deriveCoverageState(
      [target("a")],
      [question({ id: "a", askedIntent: { kind: "open", targetId: "a" }, setAsideAt: "2026-09-04T09:01:00.000Z", setAsideReason: "rescue-budget-spent" })],
      [],
    );
    expect(state[0].status).toBe("skipped");
  });

  it("does not mark the destination of a park as parked itself", () => {
    const state = deriveCoverageState(
      [target("b")],
      [question({ id: "b", askedIntent: { kind: "rescue", targetId: "b", style: "park", hook: null, parkedTargetId: "a" } })],
      [],
    );
    expect(state[0].status).toBe("open");
  });

  it("reopens a target whose row was asked again", () => {
    const state = deriveCoverageState(
      [target("a")],
      [question({ id: "a", askedIntent: { kind: "advance", targetId: "a", reason: "satisfied" }, setAsideAt: null, setAsideReason: null })],
      [],
    );
    expect(state[0].status).toBe("open");
  });
```

- [ ] **Step 2: Run to verify they fail**

Run: `npx vitest run src/lib/interview-coverage.test.ts`
Expected: FAIL — the first two report `"open"`, the third reports `"parked"`.

- [ ] **Step 3: Derive status from the marker**

Replace `statusFor` and the relevant part of `deriveCoverageState` in `src/lib/interview-coverage.ts:43-95`:

```ts
function statusFor(
  target: CoverageTarget,
  intents: Intent[],
  answeredEvaluations: Evaluation[],
  setAside: SetAsideReason | null,
): TargetStatus {
  if (intents.length === 0) return "unasked";

  const signalsPresent = new Set(
    answeredEvaluations.flatMap((item) => item.expectedSignalsPresent ?? []),
  );
  const covered = target.expectedSignals.length > 0
    && target.expectedSignals.every((signal) => signalsPresent.has(signal));
  if (covered) return "satisfied";

  // Read off the persisted marker, not off the newest intent. An intent is
  // overwritten every time its row is re-asked, so the old last-intent rule
  // could report `parked` for a target that had since been answered, and could
  // never report `skipped` at all (spec §9.3 rule 5).
  if (setAside === "parked") return "parked";
  if (setAside === "rescue-budget-spent") return "skipped";
  return "open";
}
```

And inside `deriveCoverageState`'s map, before the return:

```ts
    const setAside = forTarget.find((question) => question.setAsideAt !== null)?.setAsideReason ?? null;
```

passing it as the fourth argument to `statusFor`. Add `SetAsideReason` to the type imports.

- [ ] **Step 4: Run the coverage tests to verify they pass**

Run: `npx vitest run src/lib/interview-coverage.test.ts`
Expected: PASS. Any existing test asserting `parked` from a trailing park intent is asserting the old rule — rewrite it to set the marker instead.

- [ ] **Step 5: Commit**

```bash
git add src/lib/interview-coverage.ts src/lib/interview-coverage.test.ts
git commit -m "feat: derive parked and skipped from the set-aside marker"
```

---

### Task 6: Carry the set-aside reason from the director to the database

The wiring that makes Tasks 2–5 add up. After this the acceptance test should go green.

**Files:**
- Modify: `src/lib/coach.ts:1820-1842` (`nextTurn`'s return)
- Modify: `src/app/api/interview/route.ts:135-157`
- Modify: `src/lib/adaptive-interviewer-flow.test.ts` (the harness's `recordConversationTurn` call, around `src/lib/adaptive-interviewer-flow.test.ts:650-670`)

**Interfaces:**
- Consumes: `DirectorDecision.setAside` (Task 4), `ConversationTurnPersistence.setAsideReason` (Task 2).
- Produces: `NextTurnResult` gains `setAside: SetAsideReason | null`.

- [ ] **Step 1: Return the reason from `nextTurn`**

In `src/lib/coach.ts:1833-1841`, add to the returned object:

```ts
    setAside: decision.setAside,
```

and add `setAside: SetAsideReason | null;` to the `NextTurnResult` type declaration, with the doc line: `/** How this turn finishes the answered row without an answer, or null. */`

- [ ] **Step 2: Pass it through the route**

In `src/app/api/interview/route.ts`, just below the existing `nonAnswer` computation (`src/app/api/interview/route.ts:135`):

```ts
      // Gated on `nonAnswer` for the same reason it is: a pre-written session
      // (planned practice, or a legacy conversation) is not driven by the
      // coverage plan, so its rows always advance by being answered and must
      // never be set aside.
      const setAsideReason = nonAnswer ? turn.setAside : null;
```

and add `setAsideReason,` to the persistence object passed to `recordConversationTurn` (after `degraded: turn.degraded,` at `src/app/api/interview/route.ts:155`).

- [ ] **Step 3: Do the same in the flow harness**

In `src/lib/adaptive-interviewer-flow.test.ts`, the harness builds the same persistence object the route does. Add the identical two lines there so the two stay in step.

- [ ] **Step 4: Flip the acceptance test and run it**

Change `it.fails(` back to `it(` on the test added in Task 1.

Run: `npx vitest run src/lib/adaptive-interviewer-flow.test.ts -t "moves the candidate onto a different question"`
Expected: PASS — more than one distinct interviewer line across the four blackout turns.

If it still fails, the likeliest cause is the fake RPC not mirroring one of the SQL branches from Task 2 Step 7. Check that `set_aside_at` is being set on the answered row and cleared on a re-asked one.

- [ ] **Step 5: Run the full suite**

Run: `npm test`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/lib/coach.ts src/app/api/interview/route.ts src/lib/adaptive-interviewer-flow.test.ts
git commit -m "fix: move the interview off a target the candidate blanked on"
```

---

### Task 7: Report what the interview never reached

Spec §9.3 rule 5: a target the session closes without covering is recorded `skipped` with a reason, so results can state what was not reached. Persist-and-return only — no results-screen work.

**Files:**
- Modify: `src/lib/interview-coverage.ts` (append the report function)
- Modify: `src/app/api/interview/route.ts:186-192` (the `complete` action) and `:200-214` (`finishConversation`)
- Test: `src/lib/interview-coverage.test.ts`

**Interfaces:**
- Consumes: `TargetState[]` from `deriveCoverageState`.
- Produces: `type UncoveredTarget = { targetId: string; competencyName: string | null; reason: string }` and `uncoveredTargets(states: TargetState[]): UncoveredTarget[]`. The completion responses gain a `coverage: UncoveredTarget[]` field.

- [ ] **Step 1: Write the failing test**

```ts
  it("reports every target the session closed without covering, with a reason", () => {
    const states = deriveCoverageState(
      [target("a"), target("b"), target("c"), target("d")],
      [
        question({ id: "a", askedIntent: { kind: "open", targetId: "a" } }),
        question({ id: "b", askedIntent: { kind: "open", targetId: "b" }, setAsideAt: "2026-09-04T09:01:00.000Z", setAsideReason: "parked" }),
        question({ id: "c", askedIntent: { kind: "open", targetId: "c" }, setAsideAt: "2026-09-04T09:02:00.000Z", setAsideReason: "rescue-budget-spent" }),
        question({ id: "d" }),
      ],
      [],
    );
    expect(uncoveredTargets(states).map((item) => item.targetId)).toEqual(["a", "b", "c", "d"]);
    expect(uncoveredTargets(states)[1].reason).toBe("Set aside when the candidate could not answer, and never returned to.");
  });

  it("leaves satisfied targets out of the report", () => {
    const states = deriveCoverageState(
      [target("a")],
      [question({ id: "a", askedIntent: { kind: "open", targetId: "a" }, answer: "yes" })],
      [{ questionId: "a", expectedSignalsPresent: ["signal"] } as Evaluation],
    );
    expect(uncoveredTargets(states)).toEqual([]);
  });
```

- [ ] **Step 2: Run to verify it fails**

Run: `npx vitest run src/lib/interview-coverage.test.ts`
Expected: FAIL with `uncoveredTargets is not defined`.

- [ ] **Step 3: Write the report function**

Append to `src/lib/interview-coverage.ts`:

```ts
/** One coverage target the interview finished without covering, and why. */
export type UncoveredTarget = {
  targetId: string;
  competencyName: string | null;
  reason: string;
};

const UNCOVERED_REASONS: Record<Exclude<TargetStatus, "satisfied">, string> = {
  unasked: "Never reached before the interview ended.",
  open: "Asked, but the expected signals never came out.",
  parked: "Set aside when the candidate could not answer, and never returned to.",
  skipped: "Set aside after the rescue attempts for it were spent.",
};

/**
 * What the session did not cover, in target order (spec §9.3 rule 5). Everything
 * short of `satisfied` counts: at close there is no difference between a target
 * never asked and one asked without result, other than the reason to report.
 * Callers render this; it is deliberately plain text rather than a status code,
 * because its only consumer is prose shown to the candidate.
 */
export function uncoveredTargets(states: TargetState[]): UncoveredTarget[] {
  return states
    .filter((state) => state.status !== "satisfied")
    .map((state) => ({
      targetId: state.target.id,
      competencyName: state.target.competencyName,
      reason: UNCOVERED_REASONS[state.status],
    }));
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `npx vitest run src/lib/interview-coverage.test.ts`
Expected: PASS.

- [ ] **Step 5: Return it from both completion paths**

In `src/app/api/interview/route.ts`, add a small helper next to `visibleConversation`:

```ts
/**
 * The coverage report attached to a finished interview. Empty for a hands-on
 * or legacy session, which have no coverage plan to report against.
 */
function coverageFor(session: InterviewSession): UncoveredTarget[] {
  if (session.kind !== "conversation" || !session.blueprint) return [];
  return uncoveredTargets(deriveCoverageState(session.blueprint.targets, session.questions, session.evaluations));
}
```

Add `coverage: coverageFor(completed),` to the JSON returned by `finishConversation` (`src/app/api/interview/route.ts:209-214`) and by the `complete` action (`src/app/api/interview/route.ts:186-192`). Import `deriveCoverageState`, `uncoveredTargets`, and the `UncoveredTarget` type from `@/lib/interview-coverage`.

- [ ] **Step 6: Run the full suite**

Run: `npm test`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/lib/interview-coverage.ts src/lib/interview-coverage.test.ts src/app/api/interview/route.ts
git commit -m "feat: report the targets an interview never covered"
```

---

### Task 8: Prove the whole behaviour end-to-end

**Files:**
- Modify: `src/lib/adaptive-interviewer-flow.test.ts`

**Interfaces:**
- Consumes: everything above. No new exports.

- [ ] **Step 1: Write the remaining acceptance tests**

Add alongside the test from Task 1:

```ts
  it("keeps the candidate's unanswered attempts in the transcript", async () => {
    const session = await runScriptedSession({
      mode: "coach",
      answers: ["i don't know", "i am having a blackout", "ok — I owned the design system migration at Acme."],
    });
    const said = session.messages.filter((message) => message.role === "candidate").map((message) => message.content);
    expect(said).toContain("i don't know");
    expect(said).toContain("i am having a blackout");
  });

  it("sets a parked target aside as parked and a budget-spent one as skipped", async () => {
    const coach = await runScriptedSession({
      mode: "coach",
      answers: ["i don't know", "i am having a blackout", "i don't know", "i am having a blackout"],
    });
    const coachReasons = coach.questions.map((question) => question.setAsideReason).filter(Boolean);
    expect(coachReasons).toContain("parked");

    const real = await runScriptedSession({
      mode: "real",
      answers: ["i don't know", "i am having a blackout", "i don't know"],
    });
    const realReasons = real.questions.map((question) => question.setAsideReason).filter(Boolean);
    expect(realReasons).toContain("rescue-budget-spent");
    expect(realReasons).not.toContain("parked");
  });

  it("comes back to a parked target once nothing required is unasked", async () => {
    const session = await runScriptedSession({
      mode: "coach",
      answers: [
        "i don't know",
        "i am having a blackout",
        ...strongAnswers(),
      ],
    });
    // The parked row was returned to: its marker was cleared and it carries a
    // real answer by the end.
    const parked = session.questions.find((question) => question.nonAnswers.length > 0);
    expect(parked?.setAsideAt).toBeNull();
    expect(parked?.answer).toBeTruthy();
  });
```

- [ ] **Step 2: Run them**

Run: `npx vitest run src/lib/adaptive-interviewer-flow.test.ts`
Expected: PASS. If the return-to-parked test fails because the session closes first, extend `strongAnswers()` in the array so the session has enough turns to come back — do not weaken the assertion.

- [ ] **Step 3: Run lint, the full suite, and a production build**

```bash
npm test
npm run lint
npx next build --webpack
```

Expected: all three clean.

- [ ] **Step 4: Commit**

```bash
git add src/lib/adaptive-interviewer-flow.test.ts
git commit -m "test: cover parking, skipping, and returning end-to-end"
```

---

## Verification Summary

| Task | What it establishes | How you know |
|---|---|---|
| 1 | The blackout is real and reproducible | `it.fails` acceptance test, red underneath |
| 2 | A row can be marked finished without an answer | Migration text assertions; suite still green |
| 3 | One rule decides the current question; blanks stay in the transcript | Unit tests on the helper; suite still green |
| 4 | Park picks a destination and never re-picks the target it leaves | Director unit tests |
| 5 | `parked` and `skipped` are produced from what happened | Coverage unit tests |
| 6 | The whole path works against the real repository code | Task 1's acceptance test turns green |
| 7 | Results can say what was never reached | Coverage report unit tests |
| 8 | Coach parks and returns; real skips; blanks are visible | Three end-to-end tests, plus lint and a production build |

## Known adjacent debt, deliberately not in scope

A target left `open` — asked, answered, but its expected signals never appeared — is still never revisited: the mover only ever considers targets that are unasked or parked (`src/lib/interview-director.ts:80`). This predates the bug being fixed here and is untouched by it. Worth its own issue.
