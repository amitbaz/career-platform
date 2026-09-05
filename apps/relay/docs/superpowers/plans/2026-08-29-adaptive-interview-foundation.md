# Adaptive Interview Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Relay deployable and private with Google sign-in, Supabase Postgres/RLS persistence, and adaptive career-grounded conversational interviews.

**Architecture:** Supabase Auth provides Google OAuth and cookie-backed sessions. Supabase Postgres stores user-scoped profile, competency, session, question, and evaluation records protected by RLS; server-side repositories provide all application access. A deterministic planner creates and persists a five-question backbone that model and demo flows share, then writes answer evidence transactionally to target later interviews.

**Tech Stack:** Next.js 16.3 App Router, React 19, TypeScript, Tailwind CSS, Supabase Auth/Storage/Postgres, `@supabase/ssr`, `@supabase/supabase-js`, Gemini API, Zod, Vitest.

**Spec:** `docs/superpowers/specs/2026-08-29-adaptive-interview-foundation-design.md`

## Global Constraints

- Use Supabase Postgres; remove the runtime dependency on local SQLite and `better-sqlite3`.
- Implement Google OAuth only; use `src/proxy.ts`, not the deprecated `middleware.ts` convention.
- Every user-owned row and source file must be protected by `auth.uid() = user_id` Row Level Security policies.
- Do not seed competency proficiency, confidence, or estimated level; a new profile has no assessment evidence.
- Start each conversational session with a persisted five-question backbone; permit at most three persisted follow-ups and no more than eight interviewer questions total.
- Keep model prompts scoped to planned question data, selected factual CV context, career narrative, and session transcript.
- Use deterministic planner and turn fallbacks for missing model configuration or invalid model output.
- The current local SQLite data is disposable and must be removed; deployed migrations must never delete user data.
- Store only Supabase publishable values in `NEXT_PUBLIC_*` environment variables. Never expose Google OAuth secrets, Gemini secrets, or a Supabase service-role key to browser code.
- Run `npm run lint`, `npm test`, and `npx next build --webpack` before completion.

---

## File structure

| Path | Responsibility |
| --- | --- |
| `supabase/migrations/202608290001_adaptive_interview_foundation.sql` | Postgres schema, indexes, RLS, and private source-document storage policies. |
| `src/lib/supabase/client.ts` | Browser Supabase client for OAuth and sign-out. |
| `src/lib/supabase/server.ts` | Cookie-aware server Supabase client and `requireUser()` guard. |
| `src/lib/supabase/proxy.ts` | Session-refresh helper used by the Next 16 proxy. |
| `src/proxy.ts` | Current Next.js request proxy that refreshes auth cookies and excludes static assets. |
| `src/app/auth/callback/route.ts` | Exchanges the Google PKCE authorization code for a server session. |
| `src/lib/types.ts` | Shared public domain records and interview API shapes. |
| `src/lib/competencies.ts` | Pure competency aggregation, confidence, and level derivation. |
| `src/lib/interview-planner.ts` | Pure ranking and five-to-eight-question plan construction. |
| `src/lib/coach.ts` | Profile analysis, contextual interviewer prompts, evaluation parsing, and deterministic turn fallback. |
| `src/lib/repositories/profile.ts` | Authenticated profile/source-document/competency persistence. |
| `src/lib/repositories/interviews.ts` | Authenticated session, question, evaluation, and transactional evidence persistence. |
| `src/app/api/profile/route.ts` | Authenticated profile read/create/edit endpoint. |
| `src/app/api/interview/route.ts` | Authenticated interview start/respond/complete endpoint. |
| `src/app/api/transcribe/route.ts` | Authenticated transcription endpoint. |
| `src/app/page.tsx` | Google sign-in, sign-out, unassessed Home state, and natural conversational completion. |
| `src/lib/interview-planner.test.ts` | Planner/difficulty/question-limit unit coverage. |
| `src/lib/competencies.test.ts` | Evidence aggregation unit coverage. |
| `vitest.config.ts` | Vitest configuration for TypeScript node tests. |
| `.env.example` | Non-secret variable names and setup guidance. |
| `README.md` | Local Supabase, Google OAuth, and Vercel deployment steps. |

## Interfaces

The following types are introduced in `src/lib/types.ts` and are the contract shared by UI, coaching, planning, and repositories:

```ts
export type QuestionCategory = "introduction" | "experience" | "technical" | "practical" | "architecture" | "system-design" | "behavioral" | "communication";
export type Difficulty = "foundational" | "intermediate" | "senior" | "advanced";

export type Competency = {
  id: string;
  name: string;
  relevance: number;
  expectedLevel: Difficulty;
  estimatedLevel: Difficulty | null;
  confidence: "low" | "medium" | "high" | null;
  lastPracticedAt: string | null;
  questionCount: number;
  averageScore: number | null;
  recentScore: number | null;
  strengths: string[];
  weaknesses: string[];
};

export type PlannedQuestion = {
  id: string;
  sequence: number;
  category: QuestionCategory;
  competencyId: string | null;
  competencyName: string | null;
  difficulty: Difficulty;
  isFollowUp: boolean;
  prompt: string;
  answer: string | null;
  createdAt: string;
};

export type Evaluation = {
  score: number;
  competencyId: string | null;
  competency: string;
  dimensions: Partial<Record<"correctness" | "depth" | "clarity" | "structure" | "practicalExperience" | "tradeOffAwareness" | "communication" | "confidence" | "relevance", number>>;
  strengths: string[];
  needsWork: string[];
};
```

## Tasks

### Task 1: Establish Supabase, test, and environment foundations

**Files:**
- Modify: `package.json`
- Modify: `package-lock.json`
- Create: `vitest.config.ts`
- Create: `.env.example`
- Delete: `src/lib/db.ts`
- Delete: `data/interview-coach.db` if present

**Consumes:** Current Next.js app using `better-sqlite3` and no test runner.

**Produces:** `npm test` executes TypeScript unit tests; Supabase packages are available; no local database implementation remains.

- [ ] **Step 1: Add a failing planner test file to establish the test command**

Create `src/lib/interview-planner.test.ts` with this initial import expectation:

```ts
import { describe, expect, it } from "vitest";
import { buildInterviewPlan } from "@/lib/interview-planner";

describe("buildInterviewPlan", () => {
  it("creates a five-question backbone", () => {
    expect(buildInterviewPlan([], "senior")).toHaveLength(5);
  });
});
```

- [ ] **Step 2: Install runtime and test dependencies**

Run:

```bash
npm uninstall better-sqlite3 @types/better-sqlite3
npm install @supabase/ssr @supabase/supabase-js
npm install -D vitest
```

Add scripts to `package.json`:

```json
"test": "vitest run",
"test:watch": "vitest"
```

- [ ] **Step 3: Add the minimal Vitest configuration and prove the test fails for the intended reason**

Create `vitest.config.ts`:

```ts
import path from "node:path";
import { defineConfig } from "vitest/config";

export default defineConfig({
  resolve: { alias: { "@": path.resolve(__dirname, "src") } },
  test: { environment: "node", include: ["src/**/*.test.ts"] },
});
```

Run: `npm test -- src/lib/interview-planner.test.ts`

Expected: FAIL because `@/lib/interview-planner` does not yet exist.

- [ ] **Step 4: Add documented environment-variable names**

Create `.env.example`:

```dotenv
NEXT_PUBLIC_SUPABASE_URL=https://your-project.supabase.co
NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY=sb_publishable_your_key
GEMINI_API_KEY=your_gemini_api_key
GEMINI_MODEL=gemini-3.6-flash
```

Ensure `.env.local` and `data/` are ignored in `.gitignore` without adding their contents to git.

- [ ] **Step 5: Remove obsolete persistence artifacts**

Delete `src/lib/db.ts`. If `data/interview-coach.db` exists, delete it. Do not delete the `data/` directory recursively.

- [ ] **Step 6: Commit the foundation**

```bash
git add package.json package-lock.json vitest.config.ts .env.example .gitignore src/lib/interview-planner.test.ts src/lib/db.ts data/interview-coach.db
git commit -m "chore: replace local database foundation"
```

### Task 2: Add the deployable schema and user-scoped Supabase access

**Files:**
- Create: `supabase/migrations/202608290001_adaptive_interview_foundation.sql`
- Create: `src/lib/supabase/client.ts`
- Create: `src/lib/supabase/server.ts`
- Create: `src/lib/supabase/proxy.ts`
- Create: `src/proxy.ts`
- Create: `src/app/auth/callback/route.ts`

**Consumes:** `NEXT_PUBLIC_SUPABASE_URL` and `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY` from Task 1.

**Produces:** Cookie-backed Google OAuth session handling and a RLS-protected relational schema.

- [ ] **Step 1: Write the SQL migration with tables, indexes, and RLS before writing client code**

Create the migration with `profiles`, `source_documents`, `competencies`, `interview_sessions`, `interview_questions`, `question_evaluations`, and `hands_on_checkpoints`. Every table must have `user_id uuid not null references auth.users(id) on delete cascade`, timestamps, RLS enabled, and four policies named `select_own`, `insert_own`, `update_own`, and `delete_own` using `auth.uid() = user_id` for both `using` and `with check` where applicable.

Use these constraints and indexes:

```sql
create unique index competencies_user_name_key on public.competencies (user_id, lower(name));
create index interview_sessions_user_created_idx on public.interview_sessions (user_id, created_at desc);
create unique index interview_questions_session_sequence_key on public.interview_questions (session_id, sequence);
create unique index question_evaluations_question_key on public.question_evaluations (question_id);
create index hands_on_checkpoints_session_created_idx on public.hands_on_checkpoints (session_id, created_at desc);
```

Store `strengths`, `weaknesses`, and evaluation `dimensions` as `jsonb not null default '[]'::jsonb` or `jsonb not null default '{}'::jsonb` as appropriate. Restrict difficulty values with a check constraint to `foundational`, `intermediate`, `senior`, and `advanced`.

Create a private `career-documents` Storage bucket and `storage.objects` policies that require the first folder segment in `name` to equal `auth.uid()::text`.

- [ ] **Step 2: Implement browser and server clients with explicit missing-config errors**

Create `src/lib/supabase/client.ts`:

```ts
import { createBrowserClient } from "@supabase/ssr";

export function createBrowserSupabaseClient() {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const key = process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY;
  if (!url || !key) throw new Error("Supabase is not configured. Add NEXT_PUBLIC_SUPABASE_URL and NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY.");
  return createBrowserClient(url, key);
}
```

Create `src/lib/supabase/server.ts` with `createServerClient` from `@supabase/ssr`, wired to asynchronous `cookies()` getters/setters, and this guard:

```ts
export async function requireUser() {
  const supabase = await createServerSupabaseClient();
  const { data: { user }, error } = await supabase.auth.getUser();
  if (error || !user) throw new Error("UNAUTHENTICATED");
  return { supabase, user };
}
```

- [ ] **Step 3: Add current Next.js proxy and OAuth callback routes**

Create `src/lib/supabase/proxy.ts` to refresh the auth session with a `createServerClient` that copies changed cookies to both request and response. Create `src/proxy.ts` exporting `proxy(request: NextRequest)` and a static matcher that excludes `_next/static`, `_next/image`, and common image files.

Create `src/app/auth/callback/route.ts` that reads `code`, invokes `exchangeCodeForSession(code)`, permits `next` only when it starts with `/`, and redirects to either `next` or `/`. Redirect an absent/invalid code to `/?authError=signin`.

- [ ] **Step 4: Validate migration and TypeScript wiring**

Run:

```bash
npx supabase db lint
npm run lint
```

Expected: migration has no SQL lint violations and lint completes without errors. If the CLI is not installed, run the SQL in the Supabase SQL editor and record that only the external schema validation remains for deployment setup.

- [ ] **Step 5: Commit auth/database infrastructure**

```bash
git add supabase/migrations src/lib/supabase src/proxy.ts src/app/auth/callback/route.ts
git commit -m "feat: add supabase auth and schema"
```

### Task 3: Define domain types and prove adaptive planning in isolation

**Files:**
- Modify: `src/lib/types.ts`
- Create: `src/lib/interview-planner.ts`
- Modify: `src/lib/interview-planner.test.ts`

**Consumes:** `Competency`, `PlannedQuestion`, `QuestionCategory`, and `Difficulty` interfaces described above.

**Produces:** `buildInterviewPlan(competencies: Competency[], seniority: string): PlannedQuestion[]` and `chooseDifficulty(competency, seniority)`.

- [ ] **Step 1: Write failing behavior-focused tests**

Replace the starter test with fixtures that include a weak, relevant System design competency and a strong, relevant React competency. Add these expectations:

```ts
expect(plan).toHaveLength(5);
expect(plan.map((question) => question.category)).toEqual(expect.arrayContaining([
  "introduction", "technical", "architecture", "behavioral",
]));
expect(plan.find((question) => question.category === "architecture")?.competencyName).toBe("System design");
expect(chooseDifficulty(strongReact, "Senior")).toBe("advanced");
expect(chooseDifficulty(unassessedReact, "Senior")).toBe("senior");
```

Add a test that calls `appendFollowUp(plan, followUp)` three times and expects length eight, then expects a fourth call to return the unchanged plan.

- [ ] **Step 2: Run the planner tests to verify failure**

Run: `npm test -- src/lib/interview-planner.test.ts`

Expected: FAIL because planner exports and domain types do not yet exist.

- [ ] **Step 3: Implement the domain contracts and minimal deterministic planner**

In `src/lib/types.ts`, replace legacy JSON-session types with the shared contracts from this plan plus `Profile`, `InterviewSession`, and `Message` representations derived from relational records.

In `src/lib/interview-planner.ts`, use a deterministic priority score:

```ts
const score = competency.relevance * 0.45
  + weakness(competency) * 0.25
  + uncertainty(competency) * 0.2
  + staleness(competency) * 0.1;
```

Implement `buildInterviewPlan` with sequences 1–5 and category templates `introduction`, `experience`, `technical`, `architecture`, and `behavioral`. Select competencies in priority order while avoiding the same competency for adjacent slots. Implement `chooseDifficulty` so unassessed competencies use normalized role seniority, low evidence uses one level lower, strong/high-confidence evidence uses one level higher, and values remain within the four permitted levels. Implement `appendFollowUp` to reject any addition after sequence 8.

- [ ] **Step 4: Run planner tests to verify success**

Run: `npm test -- src/lib/interview-planner.test.ts`

Expected: PASS.

- [ ] **Step 5: Commit adaptive planning domain logic**

```bash
git add src/lib/types.ts src/lib/interview-planner.ts src/lib/interview-planner.test.ts
git commit -m "feat: add adaptive interview planner"
```

### Task 4: Aggregate evidence into competency knowledge records

**Files:**
- Create: `src/lib/competencies.ts`
- Create: `src/lib/competencies.test.ts`

**Consumes:** `Competency` and `Evaluation` from Task 3.

**Produces:** `applyEvaluation(competency, evaluation, practicedAt): Competency` with evidence-driven confidence and estimated level.

- [ ] **Step 1: Write failing evidence aggregation tests**

Create `src/lib/competencies.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { applyEvaluation } from "@/lib/competencies";

describe("applyEvaluation", () => {
  it("turns an unassessed competency into low-confidence evidence", () => {
    const next = applyEvaluation(unassessedReact, evaluation(7), "2026-08-29T12:00:00.000Z");
    expect(next.questionCount).toBe(1);
    expect(next.averageScore).toBe(7);
    expect(next.confidence).toBe("low");
    expect(next.estimatedLevel).toBe("senior");
  });

  it("keeps a recent score and promotes confidence after three answers", () => {
    const afterThree = [6, 7, 8].reduce((competency, score) => applyEvaluation(competency, evaluation(score), "2026-08-29T12:00:00.000Z"), unassessedReact);
    expect(afterThree.recentScore).toBe(8);
    expect(afterThree.confidence).toBe("medium");
  });
});
```

- [ ] **Step 2: Run the evidence tests to verify failure**

Run: `npm test -- src/lib/competencies.test.ts`

Expected: FAIL because `applyEvaluation` does not yet exist.

- [ ] **Step 3: Implement bounded aggregate updates**

Implement `applyEvaluation` without mutating inputs. It increments `questionCount`; calculates `averageScore` from existing aggregate and the new score; sets `recentScore` and `lastPracticedAt`; merges unique non-empty strengths/needs-work values while retaining the most recent five; derives confidence as `low` for 1–2 answers, `medium` for 3–5, and `high` for 6+; and maps average score `< 5.5` to `intermediate`, `< 7.5` to `senior`, and otherwise `advanced`. Return `null` estimated level only when there is no evidence.

- [ ] **Step 4: Run all unit tests to verify success**

Run: `npm test`

Expected: PASS for planner and competency tests.

- [ ] **Step 5: Commit competency evidence logic**

```bash
git add src/lib/competencies.ts src/lib/competencies.test.ts
git commit -m "feat: track competency evidence"
```

### Task 5: Implement authenticated repositories and transactional interview persistence

**Files:**
- Create: `src/lib/repositories/profile.ts`
- Create: `src/lib/repositories/interviews.ts`
- Modify: `src/lib/coach.ts`

**Consumes:** Supabase server client from Task 2, types/planner from Task 3, and `applyEvaluation` from Task 4.

**Produces:** `getProfile`, `saveProfile`, `createSessionWithPlan`, `recordAnswerAndEvaluation`, `saveHandsOnCheckpoint`, `getSession`, `listRecentSessions`, and `completeSession` repositories, all scoped by authenticated user.

- [ ] **Step 1: Define the repository function signatures and map snake_case rows to camelCase domain records**

Export the following signatures:

```ts
export async function getProfile(supabase: SupabaseClient, userId: string): Promise<Profile | null>;
export async function saveProfile(supabase: SupabaseClient, userId: string, profile: ProfileDraft, source: ProfileSource): Promise<Profile>;
export async function createSessionWithPlan(supabase: SupabaseClient, userId: string, plan: PlannedQuestion[]): Promise<InterviewSession>;
export async function recordAnswerAndEvaluation(supabase: SupabaseClient, userId: string, questionId: string, answer: string, evaluation: Evaluation): Promise<InterviewSession>;
```

Throw a typed repository error if a Supabase mutation returns an error or no owned row. Never accept a `userId` from an HTTP request body.

- [ ] **Step 2: Implement profile and source-document repository methods**

`saveProfile` upserts `profiles`, replaces the user-owned CV/cover-letter source rows in a transaction-like RPC or controlled sequential write, and creates/upserts unassessed competency-scope rows. Create competency relevance deterministically: 1.0 for expertise names, 0.9 for “React architecture”, “TypeScript”, and “System design” when related expertise exists, otherwise 0.7 for baseline frontend competencies. Preserve existing evidence when the user edits their profile; do not reset score fields.

- [ ] **Step 3: Implement session/question/evaluation persistence**

`createSessionWithPlan` inserts the active conversation session and its five question rows. `recordAnswerAndEvaluation` verifies the question belongs to an active session owned by `userId`, updates its answer, inserts exactly one evaluation row, applies the aggregate with `applyEvaluation`, and updates the target competency. Implement this as a Supabase Postgres RPC named `record_interview_evidence` in the migration so all writes are atomic.

The RPC must take `p_question_id uuid`, `p_answer text`, `p_score numeric`, `p_dimensions jsonb`, `p_strengths jsonb`, and `p_needs_work jsonb`; derive the caller from `auth.uid()`; reject absent/foreign/completed questions; and return the updated question/session identifiers.

Preserve the current hands-on path by storing its exercise as session JSON and each code/note snapshot in `hands_on_checkpoints`. Implement `saveHandsOnCheckpoint(supabase, userId, sessionId, code, note)` to verify ownership and active hands-on kind before insertion, then return the refreshed session with ordered checkpoints. Keep the existing hands-on prompt/evaluator behavior, loading its latest checkpoint through this repository.

- [ ] **Step 4: Adapt coaching prompts and fallbacks to the persisted plan**

Update `analyzeProfile` to output a role, seniority, summary, narrative, expertise, characteristics, and competency *names/relevance* without a seeded assessment score. Change `initialQuestion` and `nextTurn` to accept the planned question and source context.

Build the live prompt with only `role`, `seniority`, `expertise`, `narrative`, selected CV excerpts, current planned category/competency/difficulty, transcript, and latest answer. Validate a returned evaluation against the nine optional dimensions and a 0–10 score. Make deterministic fallback prompts specific to the plan category and selected competency, including an experience question grounded in a CV excerpt where one exists.

- [ ] **Step 5: Run unit tests and lint**

Run:

```bash
npm test
npm run lint
```

Expected: PASS. Fix any broken type imports caused by removal of `src/lib/db.ts` before proceeding.

- [ ] **Step 6: Commit repositories and contextual coach**

```bash
git add src/lib/repositories src/lib/coach.ts supabase/migrations/202608290001_adaptive_interview_foundation.sql
git commit -m "feat: persist interview evidence in supabase"
```

### Task 6: Secure existing API routes and apply natural conversational completion

**Files:**
- Modify: `src/app/api/profile/route.ts`
- Modify: `src/app/api/interview/route.ts`
- Modify: `src/app/api/transcribe/route.ts`

**Consumes:** `requireUser`, repositories, plan APIs, and coach APIs from Tasks 2–5.

**Produces:** Every route authorizes its caller, profile and interview data is user-scoped, and the eighth question or completed plan returns a completed session with updated profile evidence.

- [ ] **Step 1: Replace local database calls with `requireUser` and repository calls**

At the beginning of each handler, call `requireUser()`. Convert `UNAUTHENTICATED` failures into `NextResponse.json({ error: "Sign in to continue." }, { status: 401 })`. Use `{ supabase, user }` to read/write only that identity's profile, documents, sessions, questions, transcript, and hands-on checkpoints.

- [ ] **Step 2: Preserve profile API validation and add explicit ownership-safe behavior**

Keep the existing CV minimum-length and PDF validation. Return the saved profile plus `demoMode`. On `PUT`, accept editable role, seniority, narrative, and expertise but never permit profile `user_id`, competence scores, or other ownership/evidence fields from the request body.

- [ ] **Step 3: Make interview start/respond flow plan-driven**

For `action: "start"`, load the signed-in profile and competencies, call `buildInterviewPlan`, generate the first planned prompt, and persist session/plan. For `action: "respond"`, load the next unanswered owned question, persist its answer/evaluation through `recordAnswerAndEvaluation`, then either present the next planned prompt, append one valid follow-up, or mark the session complete. A complete response must return `{ session, profile }` so the client can transition immediately to results.

Do not allow a user-supplied session or question ID to select another user's data; repository ownership checks and RLS must both enforce this.

- [ ] **Step 4: Protect transcription without changing its existing model behavior**

Call `requireUser()` at the start of `POST /api/transcribe`. Keep the browser audio validation and Gemini transcription behavior unchanged. Return 401 before reading multipart data when no session exists.

- [ ] **Step 5: Verify API behavior manually with two Supabase accounts**

With two different Google accounts, create a profile and session as account A. As account B, verify `GET /api/profile` returns B's data or no profile, and that calling the interview endpoint with A's session ID returns 404 or 403 rather than A's contents. Confirm the database policies reject a direct foreign-row read in Supabase SQL/RLS testing.

- [ ] **Step 6: Commit secured routes**

```bash
git add src/app/api/profile/route.ts src/app/api/interview/route.ts src/app/api/transcribe/route.ts
git commit -m "feat: secure interview api routes"
```

### Task 7: Update the client experience for sign-in, evidence state, and automatic results

**Files:**
- Modify: `src/app/page.tsx`
- Modify: `src/app/globals.css` only if a new sign-in layout needs styles unavailable through existing utility classes

**Consumes:** Browser Supabase client, API responses from Task 6, and relational `Profile`/`InterviewSession` types.

**Produces:** A Google sign-in gate, a sign-out action, a new-user “Not enough data yet” Home state, and automatic session completion UI.

- [ ] **Step 1: Implement the explicit sign-in state before loading coach data**

On mount, call `createBrowserSupabaseClient().auth.getUser()`. While loading, render a minimal status. When absent, render a dedicated sign-in card with a “Continue with Google” button that calls:

```ts
supabase.auth.signInWithOAuth({
  provider: "google",
  options: { redirectTo: `${window.location.origin}/auth/callback` },
});
```

Show a concise error if the call rejects or `authError=signin` exists in the URL.

- [ ] **Step 2: Load only authenticated profile/session data and add sign-out**

After an authenticated user is known, run existing profile/session fetches. On 401, clear local session state and return to the sign-in card. Add a header sign-out button that calls `supabase.auth.signOut()`, clears profile/session state, and returns to sign-in.

- [ ] **Step 3: Render the unassessed Home state accurately**

Replace the current seeded-score average with evidence detection:

```ts
const hasEvidence = profile?.competencies.some((item) => item.questionCount > 0) ?? false;
```

When `hasEvidence` is false, render “Not enough data yet” and explain that the first mixed interview establishes a baseline. Do not render a numerical readiness score or a “weakest” recommendation derived from zero-evidence competencies. Preserve numerical competency displays only when evidence exists.

- [ ] **Step 4: Reflect plan-driven interview completion**

Use `session.questions.length` for the explored question count. When the respond API returns a completed session and profile, persist both, clear the draft answer, and switch immediately to `results`; retain the manual finish action only after at least five answered questions, and have it invoke the same server completion logic.

- [ ] **Step 5: Verify the primary P0 user flow manually**

1. Sign in with Google.
2. Create and confirm a profile using a CV summary and narrative.
3. Confirm Home says “Not enough data yet.”
4. Start an interview and confirm the first question is profile-specific.
5. Answer enough questions to complete; confirm results display and competencies now have evidence.
6. Start a second interview; confirm the selected questions differ based on recorded evidence.
7. Sign out and sign in as another account; confirm the first account's data is never shown.

- [ ] **Step 6: Commit user-facing adaptive interview behavior**

```bash
git add src/app/page.tsx src/app/globals.css
git commit -m "feat: add authenticated adaptive interview flow"
```

### Task 8: Document and verify the deployable POC

**Files:**
- Modify: `README.md`
- Modify: `docs/prd-gap-analysis.md`

**Consumes:** Implemented Supabase schema, Google auth, adaptive interview behavior, and environment variables.

**Produces:** Accurate local/deployment instructions and an updated P0 gap record.

- [ ] **Step 1: Document exact operator setup**

Add a “Supabase + Google setup” section to `README.md` containing:

1. Create a free Supabase project and run `supabase db push` (or paste the migration in SQL Editor).
2. Copy project URL and publishable key into `.env.local`.
3. In Google Cloud Console, create an OAuth client of type Web application; add `http://localhost:3000` and the Vercel production origin to Authorized JavaScript origins; copy the Supabase Google provider callback URL into Authorized redirect URIs.
4. In Supabase Auth → Providers, enable Google and add the Google client ID/secret.
5. In Supabase Auth → URL Configuration, set Site URL and allow `http://localhost:3000/auth/callback` plus `https://<vercel-domain>/auth/callback`.
6. In Vercel, add `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY`, `GEMINI_API_KEY`, and optional `GEMINI_MODEL`; deploy from the connected repository.

State the free-tier caveat: Supabase free projects pause after one week of inactivity and lack automatic backups.

- [ ] **Step 2: Update the gap analysis precisely**

Mark the first recommended implementation slice as addressed: normalized competency knowledge graph, career-grounded conversation context, and adaptive subsequent conversational interviews. Leave per-question feedback, fuller readiness/trends, hands-on adaptation, and privacy reset/document deletion gaps open.

- [ ] **Step 3: Run final verification**

Run:

```bash
npm test
npm run lint
npx next build --webpack
git diff --check
git status --short
```

Expected: all three verification commands pass; `git diff --check` prints nothing; `git status --short` contains only intended changes.

- [ ] **Step 4: Commit documentation**

```bash
git add README.md docs/prd-gap-analysis.md .env.example
git commit -m "docs: explain hosted relay setup"
```

## Plan self-review

### Spec coverage

- Supabase Postgres, relational model, private source-document storage, and RLS: Task 2.
- Google OAuth, cookie session, callback, and authentication enforcement: Tasks 2, 6, and 7.
- No seeded competency assessment and evidence-backed knowledge graph: Tasks 3–5 and 7.
- Five-question backbone, controlled follow-ups, hard eight-question limit, seniority/evidence difficulty, and deterministic fallback: Tasks 3, 5, and 6.
- Scoped CV/narrative context and transactionally persisted evaluation: Task 5.
- Minimal “Not enough data yet” home state and natural results transition: Task 7.
- Local data reset without production data deletion: Task 1 and the migration constraints in Task 2.
- Test, build, and deployment instructions: Tasks 1, 3, 4, 6, and 8.

### Placeholder scan

No task uses an unresolved requirement marker or defers implementation with an undefined action. External credentials remain intentionally operator-supplied secrets and are represented only by documented environment-variable names.

### Type consistency

Planner and competency tests consume the interfaces defined in the shared types contract. Repositories consume planner and evidence functions; routes consume repositories; the page consumes route response shapes. The plan uses `src/proxy.ts`, matching Next.js 16's current convention.
