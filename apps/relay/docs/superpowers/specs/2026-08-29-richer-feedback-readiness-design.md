# P1 Richer Feedback and Readiness Design

## Goal

Make completed interviews materially more useful after the conversation ends. The user should be able to inspect why an answer received its score, understand what a stronger answer would contain, and see a cautious readiness/progress signal derived from accumulated evidence.

This slice builds on the P0 evidence model. It does not add research, story-bank, focused-practice, or hands-on execution features.

## Product behavior

### Per-question feedback

Each completed conversational question displays a compact card with its score and competency. Expanding the card reveals:

- the exact interviewer question;
- the candidate's submitted answer;
- all nine evaluation dimensions: correctness, depth, clarity, structure, practical experience, trade-off awareness, communication, confidence, and relevance;
- observed strengths;
- missing points or weaknesses;
- a better answer structure;
- a tailored improved-answer example.

Cards are collapsed by default and use the existing View Transitions behavior. The UI must handle older evaluations that do not contain the new fields by showing available data and omitting empty sections.

Hands-on session results continue to show session-level evaluations. They use the same coaching fields where available, but the first version does not invent per-checkpoint dimension scores.

### Readiness and progress

Readiness remains unavailable until at least one completed session has recorded evidence. Once evidence exists, the app shows:

- a 0–100 coaching readiness signal, explicitly not a hiring prediction;
- the latest completed-session score;
- a recent trend only when at least two completed sessions exist;
- strongest and weakest active competencies;
- recurring weaknesses drawn from recent evidence.

With fewer than two completed sessions, the trend label is “Baseline established.” No improvement or decline is inferred from one data point.

## Data model

Extend `Evaluation` with backward-compatible fields:

```ts
missingPoints: string[];
betterStructure: string[];
improvedAnswer: string;
```

The evaluator schema requires these fields for new AI responses. Deterministic fallback evaluation supplies concise defaults. Existing rows map missing values to empty arrays or an empty string.

Store the fields in `question_evaluations` and `session_evaluations`. JSON is preferred for arrays and the existing JSON dimensions column remains the source of truth for dimension values. The migration must be additive and safe for existing rows.

Introduce a pure progress module with an explicit input/output contract:

```ts
type ProgressSnapshot = {
  readiness: number | null;
  latestScore: number | null;
  trend: "improving" | "stable" | "declining" | "baseline" | null;
  recentScores: number[];
  strongest: Competency | null;
  weakest: Competency | null;
  recurringWeaknesses: string[];
};
```

The calculator receives active competencies and completed sessions, sorts by completion time, clamps all scores, and returns null readiness for no evidence. Readiness combines competency average, confidence, and recent-session performance with deterministic weights documented in code. It must not mutate persistence state.

## Server and persistence flow

1. The coaching evaluator returns a validated `Evaluation` with all nine dimensions and coaching detail.
2. Existing atomic answer RPCs persist the expanded evaluation fields with the answer.
3. Session hydration maps the new fields while preserving legacy rows.
4. The profile/interview GET response exposes enough completed-session data for a server-computed `ProgressSnapshot`.
5. The client renders the snapshot and expanded result cards without recomputing business rules.

If the AI provider fails or returns incomplete JSON, deterministic evaluation remains usable. Provider errors must not prevent saving an answer when the fallback evaluator can produce a valid result.

## UI and accessibility

Use native buttons for expandable cards with `aria-expanded` and a labelled content region. Keyboard users must be able to open, close, and move through feedback without relying on hover. Keep the current mobile-first layout.

Only transform and opacity may transition. Expansion can use a View Transitions cross-fade or an unmount transition; do not animate height, width, margins, or shadows.

The readiness copy must explain that the signal is personal coaching evidence, not a hiring assessment. Empty states distinguish “no completed evidence” from “baseline established.”

## Testing and verification

Add tests before implementation for:

- complete evaluator output and deterministic fallback fields;
- legacy evaluation hydration;
- readiness with no evidence, one session, and multiple sessions;
- improving, stable, and declining trend boundaries;
- recurring weakness de-duplication and recency ordering;
- route response shape and authenticated access;
- expandable feedback rendering and accessible state.

Run `npm test`, `npm run lint`, `npx next build --webpack`, and `git diff --check`. Apply the additive Supabase migration to a disposable project before declaring the live flow complete.

## Non-goals

- no current-web research or cached market intelligence;
- no story-bank or self-presentation coaching;
- no new hands-on execution runtime;
- no hiring recommendation or externally benchmarked score;
- no analytics warehouse or background aggregation job.
