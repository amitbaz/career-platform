# Adaptive Interview Foundation Design

## Purpose

Replace Relay's single-user local persistence with a secure, deployable foundation that proves the P0 feedback loop: a user signs in, confirms their professional profile, completes a career-grounded interview, receives evidence-backed competency updates, and starts a more targeted subsequent interview.

This is the first P0 slice. It covers relational persistence, Google authentication, professional competency scope, adaptive conversational planning, and the minimal no-evidence home state. It intentionally defers richer readiness/progress visualizations and expandable per-question feedback to the next slice.

## Decisions

- Deployable data store: Supabase Postgres, not local SQLite.
- Authentication: Supabase Auth with Google OAuth only.
- Authorization: every user-owned record has a required `user_id`; Row Level Security limits access to `auth.uid() = user_id`.
- File privacy: source documents are stored in a private Supabase Storage bucket and use ownership policies equivalent to database rows.
- Current local data: disposable. Remove the local SQLite database and create fresh local/hosted schema from migrations; do not attempt data conversion.
- Model resilience: the interview engine always has deterministic planning and turn-generation fallbacks when the model is unavailable or returns invalid structured output.

## System boundaries

The browser uses a Supabase publishable key only. It authenticates through a Google sign-in action and receives a cookie-backed session after `/auth/callback` exchanges the OAuth code. Middleware refreshes sessions and redirects unauthenticated people to the sign-in screen.

Server-side route handlers obtain the authenticated user before touching coach data. Repository functions encapsulate Supabase queries; the interview service encapsulates planning, prompting, evaluation validation, and competency aggregation. The UI and AI provider do not issue arbitrary database queries.

Google OAuth configuration is external to the codebase. Google Cloud requires an OAuth web client; its credentials are added in Supabase. Supabase must allow local and production redirect URLs and use the Supabase callback URL configured by its Google provider setup.

## Relational model

The schema uses stable records and foreign keys rather than serialized profile/session documents.

### `profiles`

One row per authenticated user. It holds editable career identity fields: role, seniority, summary, narrative, expertise, and characteristics. Its primary key is `user_id`, which references `auth.users`.

### `source_documents`

Stores one user-owned CV and optional cover-letter source, including text content and optional private-storage object metadata. The CV remains the factual source; a cover letter supplies positioning context and cannot override factual claims.

### `competencies`

One record per user and competency name. It includes:

- professional relevance (0–1)
- expected level
- evidence-based estimated level (nullable until evidence exists)
- confidence (nullable until evidence exists)
- last practiced timestamp
- question count
- average and recent score
- strengths and weaknesses

Profile generation initializes the competency *scope* from the confirmed role, expertise, factual CV content, and career narrative. It does not seed a proficiency score, confidence, or estimated level. A new user therefore has no readiness score or implied assessed ability.

### `interview_sessions`

Owns session kind, lifecycle status, timestamps, optional exercise data, result summary, and overall score. This slice preserves the existing conversational interview and its existing hands-on mode without redesigning the hands-on evaluator.

### `hands_on_checkpoints`

Stores user-owned code snapshots and think-aloud notes for a hands-on session. It preserves the current checkpoint workflow and lets the existing hands-on evaluator and interviewer retrieve the latest code without relying on serialized SQLite session content.

### `interview_questions`

Records the planner's intended and actual questions: session, sequence, category, competency, difficulty, planned/follow-up flag, prompt, candidate answer, and timestamps. Keeping the plan makes adaptive behavior inspectable and prevents accidental repeated coverage.

### `question_evaluations`

One record for each evaluated answer. It stores an overall score; applicable correctness, depth, clarity, structure, practical experience, trade-off awareness, communication, confidence, and relevance dimension scores; plus concise strengths and weaknesses.

## Planning and interview flow

At start, the planner queries only the signed-in user's competency and prior-answer records. It persists a five-question backbone that includes an introduction/experience prompt, technical or practical coverage, architecture or system design, and behavioral or communication coverage.

The planner ranks competencies using relevance, weakness or uncertainty, time since practice, difficulty fit, and controlled variety. Its distribution favors weak/uncertain relevant areas while retaining core and strong-area coverage. It uses seniority plus recorded evidence to choose a difficulty from foundational, intermediate, senior, and advanced.

The interviewer receives the planned category, competency, difficulty, selected factual CV details, relevant narrative context, and the transcript. It asks one question at a time, avoids feedback and score disclosure, asks follow-ups only where the answer warrants them, and does not expose teaching content. The service permits up to three follow-ups and never exceeds eight interviewer questions. It completes naturally after the backbone and any selected follow-ups are covered.

The deterministic fallback uses the same persisted plan and career context, ensuring that demo mode remains adaptive and does not collapse to a global fixed sequence.

For each answer, the service validates the evaluation, inserts the answer/evaluation, and updates the corresponding competency aggregates in one transaction. It derives estimated level and confidence from accumulated evidence. A later session therefore has durable recency, score, confidence, strength, and weakness inputs.

## UI scope

- Add a dedicated Google sign-in screen and sign-out control.
- Preserve direct entry after authentication: a new user sees onboarding; a returning user sees Home.
- On Home, show “Not enough data yet” before any completed interview evidence exists.
- Keep the current conversational UI but display the session's explored-question count from its persisted plan and transition to results when the natural completion condition is reached.
- Keep current Profile, Practice, hands-on, and Results screens functional. Detailed progress trends, readiness weighting, and expandable answer feedback are deferred.

## Error handling and safety

- Missing Supabase configuration fails closed with an actionable setup error; no shared local-profile fallback exists.
- Unauthenticated API requests return an authorization response and the UI routes users to sign in.
- RLS protects the database and private storage even if a client request is manipulated.
- Model failures and invalid JSON fall back to deterministic, valid questions/evaluations.
- Data writes for an answer and competency update are atomic.
- No production migration deletes data. The requested reset applies only to the current local SQLite file, which is removed as part of this change.

## Verification

Automated tests will cover planner ranking and difficulty selection, five-to-eight-question limits, career-context selection, competency evidence aggregation, and authenticated user scoping. The completed implementation must also pass linting and a production Next.js build.

## Deferred work

- Weighted readiness score and progress trends
- Expandable per-question feedback and tailored improved answers
- Adaptive hands-on exercise generation/execution depth
- Source-document deletion/reset controls and complete profile preferences
- AI provider abstraction, research, story coaching, focused practice, and interviewer hints
