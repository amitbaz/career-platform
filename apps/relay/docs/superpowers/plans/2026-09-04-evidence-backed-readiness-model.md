# Evidence-Backed Interview Readiness Model — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace Relay's single lifetime-average readiness number with a deterministic, evidence-weighted model that reports a score, confidence, and trend for each of seven named readiness dimensions plus one derived overall score.

**Architecture:** Every graded answer already persisted becomes one piece of evidence. Each piece is assigned to exactly one readiness dimension, given a strength (how realistic the conditions were) and a recency decay (how old it is), and aggregated by weighted mean. Dimensions sit *above* the existing per-skill competency rows, which stay as-is so the practice recommender keeps its fine-grained targets. Aggregation is a pure function with no LLM involvement, recomputed per request from persisted inputs — the same shape as the `calculateProgress` function it replaces.

**Tech Stack:** TypeScript (strict), Next.js route handlers, Supabase Postgres, Vitest + jsdom.

**Issue:** [#14](https://github.com/amitbaz/interviewer-app/issues/14) — parent epic [#13](https://github.com/amitbaz/interviewer-app/issues/13).

---

## Decisions already made (do not re-litigate)

| Decision | Choice |
|---|---|
| The existing 0–100 `ProgressSnapshot.readiness` | **Replaced outright.** Its two consumers (progress UI, practice recommender) are updated in this plan. |
| Evidence strength for rows that already exist | **Inferred from what sessions already record** (`mode`, `degraded`, `assistance`, `non_answer`). No new column, no data migration. |
| Recency decay rate | **A configurable exported constant**, starting at a 60-day half-life. |
| Evidence source | **Per-answer graded evaluations** (`question_evaluations`). Requires a new cross-session repository query; none exists today. |
| Seven dimensions vs. the per-skill competency list | **Dimensions sit above competencies.** Competency rows are untouched; each one maps to a dimension. |
| The unmerged `feature/release3-adaptive-learning-loop` branch | **No dependency.** Plan against `main`. Its synchronous post-session hook is a precedent, not a prerequisite. |
| Market-relevance weighting | **Out of scope** — owned by issue #19. Leave a generic per-dimension weight hook, no market-shaped fields. |

## Background an implementer needs

Read these before starting. Each is short.

- `src/lib/progress.ts` — the whole file being replaced. 150 lines. The readiness formula is at `:87-111`, the trend rule at `:113-121`.
- `src/lib/types.ts:438-446` — `ProgressSnapshot`, the type being replaced.
- `src/lib/types.ts:123-136` — `Competency`, the per-skill row that stays.
- `supabase/migrations/202609010001_adaptive_interviewer.sql:1-13` — where `mode`, `degraded`, `assistance`, and `non_answer` were added. These four fields are the entire basis for evidence strength.
- `src/lib/repositories/interviews.ts:420-450` — how a session hydrates its questions and evaluations today. The new cross-session query follows the same multi-query style.

Two traps:

1. **"Readiness" is already two different things.** `ProfileReadiness` (`src/lib/types.ts:158`) is a yes/no gate for "do we have enough CV material to ground questions". It is unrelated to this work and must not be touched. The thing being replaced is `ProgressSnapshot.readiness`.
2. **Sessions have a `kind` (`conversation` | `hands-on`) and a `mode` (`coach` | `real`).** Evidence strength comes from `mode`, not `kind`.

---

## Task 1: Readiness dimensions and evidence-to-dimension mapping

**Files:**
- Create: `src/lib/readiness-dimensions.ts`
- Test: `src/lib/readiness-dimensions.test.ts`

**Step 1: Write the failing test**

```ts
import { describe, expect, it } from "vitest";
import { READINESS_DIMENSIONS, dimensionFor } from "@/lib/readiness-dimensions";

describe("READINESS_DIMENSIONS", () => {
  it("declares the seven dimensions from issue #14", () => {
    expect(READINESS_DIMENSIONS).toEqual([
      "frontend",
      "backend",
      "system-design",
      "coding",
      "behavioral",
      "communication",
      "ai-engineering",
    ]);
  });
});

describe("dimensionFor", () => {
  it("prefers the competency name over the question category", () => {
    expect(dimensionFor({ competencyName: "React architecture", category: "technical" })).toBe("frontend");
  });

  it("falls back to the question category when the competency name matches nothing", () => {
    expect(dimensionFor({ competencyName: "Widget wrangling", category: "system-design" })).toBe("system-design");
  });

  it("falls back to the category when there is no competency name at all", () => {
    expect(dimensionFor({ competencyName: null, category: "behavioral" })).toBe("behavioral");
  });

  it("returns null when neither the name nor the category resolves", () => {
    expect(dimensionFor({ competencyName: "Widget wrangling", category: "introduction" })).toBe("behavioral");
    expect(dimensionFor({ competencyName: null, category: null })).toBeNull();
  });

  it("recognises AI and agentic work as its own dimension", () => {
    expect(dimensionFor({ competencyName: "LLM agent design", category: "technical" })).toBe("ai-engineering");
    expect(dimensionFor({ competencyName: "RAG pipelines", category: null })).toBe("ai-engineering");
  });

  it("is case-insensitive", () => {
    expect(dimensionFor({ competencyName: "BACKEND APIs", category: null })).toBe("backend");
  });

  it("resolves each baseline competency name to a dimension", () => {
    const baseline = [
      "Coding and implementation",
      "Debugging and reliability",
      "Architecture and system design",
      "Testing and quality",
      "Performance and scalability",
      "Accessibility and user impact",
      "Delivery and trade-offs",
      "Collaboration and communication",
    ];
    for (const name of baseline) {
      expect(dimensionFor({ competencyName: name, category: null })).not.toBeNull();
    }
  });
});
```

**Step 2: Run it and confirm it fails**

Run: `npx vitest run src/lib/readiness-dimensions.test.ts`
Expected: FAIL — `Failed to resolve import "@/lib/readiness-dimensions"`.

**Step 3: Write the implementation**

```ts
/**
 * The seven readiness dimensions Relay reports on, plus the deterministic rules
 * that assign a piece of evidence to exactly one of them.
 *
 * These sit ABOVE the per-skill `Competency` rows rather than replacing them:
 * competencies remain the fine-grained unit the practice recommender targets,
 * while dimensions are the stable vocabulary every consumer in epic #13 shares.
 *
 * Assignment is single-valued on purpose. Splitting one answer across several
 * dimensions would require dividing its weight, which makes the aggregate far
 * harder to explain to a user and to pin in a test, for no coaching benefit.
 */
import type { QuestionCategory } from "@/lib/types";

export const READINESS_DIMENSIONS = [
  "frontend",
  "backend",
  "system-design",
  "coding",
  "behavioral",
  "communication",
  "ai-engineering",
] as const;

export type ReadinessDimension = (typeof READINESS_DIMENSIONS)[number];

/**
 * Ordered most-specific first. `ai-engineering` is checked before every other
 * rule because an AI competency almost always also mentions a stack word
 * ("LLM agent backend") and the AI axis is the one epic #13 cares about most.
 */
const NAME_RULES: ReadonlyArray<readonly [ReadinessDimension, RegExp]> = [
  ["ai-engineering", /\bai\b|\bllm\b|\bml\b|agent|prompt|machine learning|\brag\b|embedding|model eval/],
  ["frontend", /react|frontend|front-end|\bui\b|\bux\b|css|browser|accessib|\ba11y\b|component|rendering/],
  ["backend", /backend|back-end|\bapi\b|server|database|\bsql\b|node|infra|platform|distributed|queue|reliab|debug/],
  ["system-design", /architect|system design|scalab|performance|latency|throughput|capacity/],
  ["coding", /coding|implementation|algorithm|data structure|test|quality|refactor/],
  ["behavioral", /behavio|story|storytelling|ownership|delivery|trade-off|tradeoff|leadership|stakeholder|conflict/],
  ["communication", /communicat|collaborat|clarity|presentation|explain|writing/],
];

const CATEGORY_RULES: Readonly<Record<QuestionCategory, ReadinessDimension>> = {
  introduction: "behavioral",
  experience: "behavioral",
  behavioral: "behavioral",
  communication: "communication",
  technical: "coding",
  practical: "coding",
  architecture: "system-design",
  "system-design": "system-design",
};

/**
 * Resolves the dimension a single graded answer belongs to.
 *
 * Returns `null` when nothing matches. Unresolved evidence is deliberately
 * dropped rather than dumped into a catch-all dimension: a wrong assignment
 * silently corrupts a score, whereas a dropped one is surfaced as a count on
 * the readiness result and can be investigated.
 */
export function dimensionFor(input: {
  competencyName: string | null;
  category: QuestionCategory | null;
}): ReadinessDimension | null {
  const name = input.competencyName?.toLowerCase().trim() ?? "";
  if (name) {
    for (const [dimension, pattern] of NAME_RULES) {
      if (pattern.test(name)) return dimension;
    }
  }
  if (input.category && input.category in CATEGORY_RULES) {
    return CATEGORY_RULES[input.category];
  }
  return null;
}
```

**Step 4: Run the test to verify it passes**

Run: `npx vitest run src/lib/readiness-dimensions.test.ts`
Expected: PASS, 7 tests.

**Step 5: Commit**

```bash
git add src/lib/readiness-dimensions.ts src/lib/readiness-dimensions.test.ts
git commit -m "feat: define readiness dimensions and evidence mapping"
```

---

## Task 2: Evidence strength from recorded interview conditions

**Files:**
- Create: `src/lib/readiness-weighting.ts`
- Test: `src/lib/readiness-weighting.test.ts`

This is the task that satisfies the acceptance criterion *"Teaching/self-report evidence has materially less influence than realistic demonstrated performance."* No new column is needed — the four fields it reads all already exist.

**Step 1: Write the failing test**

```ts
import { describe, expect, it } from "vitest";
import { evidenceStrength } from "@/lib/readiness-weighting";

const conditions = (overrides: Partial<Parameters<typeof evidenceStrength>[0]> = {}) => ({
  mode: "real" as const,
  degraded: false,
  assistanceCount: 0,
  ...overrides,
});

describe("evidenceStrength", () => {
  it("gives full strength to an unassisted answer under real interview conditions", () => {
    expect(evidenceStrength(conditions())).toBe(1);
  });

  it("materially discounts a coach-mode answer against a real-mode one", () => {
    expect(evidenceStrength(conditions({ mode: "coach" }))).toBeLessThan(
      evidenceStrength(conditions({ mode: "real" })) / 2,
    );
  });

  it("discounts each rescue the interviewer had to give", () => {
    const none = evidenceStrength(conditions({ assistanceCount: 0 }));
    const one = evidenceStrength(conditions({ assistanceCount: 1 }));
    const two = evidenceStrength(conditions({ assistanceCount: 2 }));
    expect(one).toBeLessThan(none);
    expect(two).toBeLessThan(one);
  });

  it("does not discount further beyond two rescues", () => {
    expect(evidenceStrength(conditions({ assistanceCount: 5 }))).toBe(
      evidenceStrength(conditions({ assistanceCount: 2 })),
    );
  });

  it("halves a degraded session, which ran on the fallback interviewer", () => {
    expect(evidenceStrength(conditions({ degraded: true }))).toBe(0.5);
  });

  it("never returns a negative or above-one strength", () => {
    const worst = evidenceStrength(conditions({ mode: "coach", degraded: true, assistanceCount: 9 }));
    expect(worst).toBeGreaterThan(0);
    expect(worst).toBeLessThan(0.1);
  });
});
```

**Step 2: Run it and confirm it fails**

Run: `npx vitest run src/lib/readiness-weighting.test.ts`
Expected: FAIL — module not found.

**Step 3: Write the implementation**

```ts
/**
 * How much a single piece of evidence counts, before any aggregation.
 *
 * Weight has three independent factors, multiplied:
 *   strength  -- how realistic the conditions were (this file)
 *   decay     -- how long ago it happened (this file)
 *   relevance -- how much the underlying skill matters for the target role
 *
 * All three are pure functions of already-persisted data. Nothing here calls a
 * model, so the same evidence always produces the same weight -- the
 * determinism issue #14 requires.
 */
import type { InterviewMode } from "@/lib/types";

/**
 * Half-life in days: evidence carries half its weight after this long.
 *
 * Tuning knob, not a law. Shorter makes readiness responsive but jumpy across a
 * quiet fortnight; longer makes an old baseline hard to outrun. 60 days was
 * chosen so a few weeks of consistent practice can visibly move a dimension
 * while a single stale session cannot dominate it.
 */
export const RECENCY_HALF_LIFE_DAYS = 60;

/**
 * Evidence fades but is never erased -- issue #14 requires old evidence to
 * "gradually lose influence without being deleted". This floor is what keeps a
 * long, sparse history from silently becoming no history at all.
 */
export const MINIMUM_RECENCY_FACTOR = 0.05;

const REAL_MODE_STRENGTH = 1;
/**
 * A coach-mode answer is teaching, not proof. The gap has to be wide enough
 * that a run of coaching sessions cannot pass for demonstrated ability.
 */
const COACH_MODE_STRENGTH = 0.4;

/** Indexed by rescue count; anything beyond the last entry uses the last entry. */
const ASSISTANCE_FACTORS = [1, 0.6, 0.35] as const;

/** A degraded session ran on the deterministic fallback, so it proves less. */
const DEGRADED_FACTOR = 0.5;

export type EvidenceConditions = {
  mode: InterviewMode;
  degraded: boolean;
  /** How many times the interviewer had to rescue this specific question. */
  assistanceCount: number;
};

export function evidenceStrength(conditions: EvidenceConditions): number {
  const base = conditions.mode === "real" ? REAL_MODE_STRENGTH : COACH_MODE_STRENGTH;
  const index = Math.min(Math.max(0, Math.trunc(conditions.assistanceCount)), ASSISTANCE_FACTORS.length - 1);
  const assistance = ASSISTANCE_FACTORS[index];
  const degraded = conditions.degraded ? DEGRADED_FACTOR : 1;
  return Number((base * assistance * degraded).toFixed(4));
}

/**
 * Exponential decay on the age of the evidence, floored so nothing disappears.
 * `asOf` is injected rather than read from the clock so tests are deterministic.
 */
export function recencyFactor(recordedAt: string, asOf: Date, halfLifeDays = RECENCY_HALF_LIFE_DAYS): number {
  const recorded = new Date(recordedAt).getTime();
  if (Number.isNaN(recorded)) return MINIMUM_RECENCY_FACTOR;
  const ageDays = Math.max(0, (asOf.getTime() - recorded) / 86_400_000);
  const decayed = Math.pow(0.5, ageDays / halfLifeDays);
  return Number(Math.max(MINIMUM_RECENCY_FACTOR, decayed).toFixed(4));
}
```

**Step 4: Run the test to verify it passes**

Run: `npx vitest run src/lib/readiness-weighting.test.ts`
Expected: PASS, 6 tests.

**Step 5: Commit**

```bash
git add src/lib/readiness-weighting.ts src/lib/readiness-weighting.test.ts
git commit -m "feat: derive evidence strength from interview conditions"
```

---

## Task 3: Recency decay

**Files:**
- Modify: `src/lib/readiness-weighting.test.ts`

`recencyFactor` was written in Task 2 but is not yet covered. Add its tests now rather than earlier, so each test run has a single reason to fail.

**Step 1: Append the failing tests**

```ts
import { MINIMUM_RECENCY_FACTOR, RECENCY_HALF_LIFE_DAYS, recencyFactor } from "@/lib/readiness-weighting";

describe("recencyFactor", () => {
  const now = new Date("2026-09-04T12:00:00.000Z");
  const daysAgo = (days: number) => new Date(now.getTime() - days * 86_400_000).toISOString();

  it("gives evidence recorded right now its full weight", () => {
    expect(recencyFactor(daysAgo(0), now)).toBe(1);
  });

  it("halves the weight after exactly one half-life", () => {
    expect(recencyFactor(daysAgo(RECENCY_HALF_LIFE_DAYS), now)).toBeCloseTo(0.5, 3);
  });

  it("quarters the weight after two half-lives", () => {
    expect(recencyFactor(daysAgo(RECENCY_HALF_LIFE_DAYS * 2), now)).toBeCloseTo(0.25, 3);
  });

  it("never lets old evidence reach zero", () => {
    expect(recencyFactor(daysAgo(5000), now)).toBe(MINIMUM_RECENCY_FACTOR);
  });

  it("treats an unparseable timestamp as the oldest possible evidence", () => {
    expect(recencyFactor("not-a-date", now)).toBe(MINIMUM_RECENCY_FACTOR);
  });

  it("does not reward a timestamp in the future", () => {
    expect(recencyFactor(new Date(now.getTime() + 86_400_000).toISOString(), now)).toBe(1);
  });
});
```

**Step 2: Run and verify**

Run: `npx vitest run src/lib/readiness-weighting.test.ts`
Expected: PASS, 12 tests total. If any fail, the Task 2 implementation is wrong — fix it there, not by loosening the test.

**Step 3: Commit**

```bash
git add src/lib/readiness-weighting.test.ts
git commit -m "test: cover recency decay for readiness weighting"
```

---

## Task 4: The readiness result types

**Files:**
- Modify: `src/lib/types.ts`

No test of its own — types are exercised by every task that follows. Keep this commit type-only so a later `git bisect` reads cleanly.

**Step 1: Add the types**

Add near the existing `ProgressSnapshot` declaration (`src/lib/types.ts:438`):

```ts
/**
 * Why one dimension scored what it did. Carried on the result rather than
 * stored, so a consumer can explain a score without a second query. Ordered
 * heaviest-contribution first.
 */
export type ReadinessContribution = {
  questionEvaluationId: string;
  sessionId: string;
  recordedAt: string;
  score: number;
  /** strength x recency x relevance, the product actually used in the mean. */
  weight: number;
};

export type ReadinessTrend = "improving" | "stable" | "worsening" | "unresolved";

export type ReadinessDimensionResult = {
  dimension: ReadinessDimension;
  /** 0-100, or null when this dimension has no evidence at all. */
  score: number | null;
  confidence: "low" | "medium" | "high" | null;
  trend: ReadinessTrend;
  evidenceCount: number;
  /** Total accumulated weight. This, not the raw count, drives confidence. */
  totalWeight: number;
  contributions: ReadinessContribution[];
};

/**
 * Relay's single evidence-backed view of interview preparedness. Replaces
 * `ProgressSnapshot`; there is deliberately no second competing score.
 *
 * `overall` is DERIVED from `dimensions` -- never computed independently and
 * never generated by a model (issue #14 acceptance criteria).
 */
export type ReadinessModel = {
  overall: number | null;
  overallConfidence: "low" | "medium" | "high" | null;
  overallTrend: ReadinessTrend;
  dimensions: ReadinessDimensionResult[];
  /** Graded answers that matched no dimension. Non-zero means a mapping gap. */
  unmappedEvidenceCount: number;
  /** When the model was computed, so a consumer can show its age. */
  computedAt: string;
};

/**
 * One graded answer, flattened with the session conditions needed to weight it.
 * This is the only input the readiness aggregation reads.
 */
export type ReadinessEvidence = {
  questionEvaluationId: string;
  sessionId: string;
  recordedAt: string;
  /** The rubric score on the existing 0-10 scale. */
  score: number;
  competencyId: string | null;
  competencyName: string | null;
  category: QuestionCategory | null;
  /** 0-1 role relevance from the competency row; 1 when there is no competency. */
  relevance: number;
  mode: InterviewMode;
  degraded: boolean;
  assistanceCount: number;
};
```

Add the import at the top of `src/lib/types.ts`:

```ts
import type { ReadinessDimension } from "@/lib/readiness-dimensions";
```

> If that import creates a cycle (`readiness-dimensions.ts` imports `QuestionCategory` from `types.ts`), break it by moving `ReadinessDimension` and `READINESS_DIMENSIONS` into `types.ts` and having `readiness-dimensions.ts` import them. Do not add a third file.

**Step 2: Verify types compile**

Run: `npx tsc --noEmit`
Expected: no errors.

**Step 3: Commit**

```bash
git add src/lib/types.ts
git commit -m "feat: add evidence-backed readiness model types"
```

---

## Task 5: Per-dimension score and confidence

**Files:**
- Create: `src/lib/readiness.ts`
- Test: `src/lib/readiness.test.ts`

**Step 1: Write the failing test**

```ts
import { describe, expect, it } from "vitest";
import { calculateReadiness } from "@/lib/readiness";
import type { ReadinessEvidence } from "@/lib/types";

const NOW = new Date("2026-09-04T12:00:00.000Z");
const daysAgo = (days: number) => new Date(NOW.getTime() - days * 86_400_000).toISOString();

let seq = 0;
const evidence = (overrides: Partial<ReadinessEvidence> = {}): ReadinessEvidence => ({
  questionEvaluationId: `eval-${(seq += 1)}`,
  sessionId: "session-1",
  recordedAt: daysAgo(1),
  score: 7,
  competencyId: "competency-1",
  competencyName: "React architecture",
  category: "technical",
  relevance: 1,
  mode: "real",
  degraded: false,
  assistanceCount: 0,
  ...overrides,
});

const dimension = (model: ReturnType<typeof calculateReadiness>, name: string) =>
  model.dimensions.find((entry) => entry.dimension === name)!;

describe("calculateReadiness", () => {
  it("returns every dimension even when there is no evidence", () => {
    const model = calculateReadiness([], NOW);
    expect(model.dimensions).toHaveLength(7);
    expect(model.overall).toBeNull();
    for (const entry of model.dimensions) {
      expect(entry.score).toBeNull();
      expect(entry.confidence).toBeNull();
      expect(entry.trend).toBe("unresolved");
    }
  });

  it("scores a dimension on 0-100 from its 0-10 rubric evidence", () => {
    const model = calculateReadiness([evidence({ score: 8 })], NOW);
    expect(dimension(model, "frontend").score).toBe(80);
  });

  it("does not let one outlier overwrite a consistent history", () => {
    const consistent = Array.from({ length: 8 }, () => evidence({ score: 8, recordedAt: daysAgo(10) }));
    const outlier = evidence({ score: 1, recordedAt: daysAgo(1) });
    const model = calculateReadiness([...consistent, outlier], NOW);
    expect(dimension(model, "frontend").score).toBeGreaterThan(70);
  });

  it("lets recent repeated evidence materially outweigh an old baseline", () => {
    const oldBaseline = Array.from({ length: 6 }, () => evidence({ score: 3, recordedAt: daysAgo(300) }));
    const recentStrong = Array.from({ length: 6 }, () => evidence({ score: 9, recordedAt: daysAgo(3) }));
    const model = calculateReadiness([...oldBaseline, ...recentStrong], NOW);
    expect(dimension(model, "frontend").score).toBeGreaterThan(80);
  });

  it("counts coach-mode teaching for materially less than a real-mode demonstration", () => {
    const taught = calculateReadiness(
      Array.from({ length: 4 }, () => evidence({ score: 9, mode: "coach" })),
      NOW,
    );
    const demonstrated = calculateReadiness(
      Array.from({ length: 4 }, () => evidence({ score: 9, mode: "real" })),
      NOW,
    );
    expect(dimension(taught, "frontend").totalWeight).toBeLessThan(
      dimension(demonstrated, "frontend").totalWeight / 2,
    );
    expect(dimension(taught, "frontend").confidence).not.toBe("high");
    expect(dimension(demonstrated, "frontend").confidence).toBe("high");
  });

  it("reports low confidence on sparse evidence and high confidence on plentiful strong evidence", () => {
    const sparse = calculateReadiness([evidence({ score: 7 })], NOW);
    expect(dimension(sparse, "frontend").confidence).toBe("low");

    const plentiful = calculateReadiness(
      Array.from({ length: 6 }, () => evidence({ score: 7 })),
      NOW,
    );
    expect(dimension(plentiful, "frontend").confidence).toBe("high");
  });

  it("weights evidence by the relevance of its underlying competency", () => {
    const model = calculateReadiness(
      [
        evidence({ score: 10, relevance: 1 }),
        evidence({ score: 0, relevance: 0.1 }),
      ],
      NOW,
    );
    expect(dimension(model, "frontend").score).toBeGreaterThan(85);
  });

  it("records the evidence that produced each dimension score, heaviest first", () => {
    const model = calculateReadiness(
      [evidence({ score: 5, recordedAt: daysAgo(200) }), evidence({ score: 9, recordedAt: daysAgo(1) })],
      NOW,
    );
    const contributions = dimension(model, "frontend").contributions;
    expect(contributions).toHaveLength(2);
    expect(contributions[0].weight).toBeGreaterThan(contributions[1].weight);
    expect(contributions[0].score).toBe(9);
  });

  it("drops evidence that matches no dimension and reports the count", () => {
    const model = calculateReadiness(
      [evidence({ competencyName: "Widget wrangling", category: null })],
      NOW,
    );
    expect(model.unmappedEvidenceCount).toBe(1);
    expect(model.dimensions.every((entry) => entry.score === null)).toBe(true);
  });
});
```

**Step 2: Run it and confirm it fails**

Run: `npx vitest run src/lib/readiness.test.ts`
Expected: FAIL — module not found.

**Step 3: Write the implementation**

Only the score/confidence half. `trendFor` is stubbed to `"unresolved"` and filled in by Task 6.

```ts
/**
 * Relay's single interview-readiness model.
 *
 * One graded answer is one piece of evidence. Each is assigned to exactly one
 * of the seven readiness dimensions and weighted by
 *
 *   weight = strength(interview conditions) x recency(age) x relevance(role fit)
 *
 * A dimension's score is the weight-weighted mean of its evidence, rescaled
 * from the 0-10 rubric to 0-100. Confidence comes from total accumulated
 * weight, not from a raw count -- three cold demonstrations should read as
 * more confident than eight walked-through answers.
 *
 * Everything here is a pure function of persisted rows. No model call, no
 * clock read (the caller injects `asOf`), so the same evidence always yields
 * the same result.
 */
import { READINESS_DIMENSIONS, dimensionFor, type ReadinessDimension } from "@/lib/readiness-dimensions";
import { evidenceStrength, recencyFactor } from "@/lib/readiness-weighting";
import type {
  ReadinessContribution,
  ReadinessDimensionResult,
  ReadinessEvidence,
  ReadinessModel,
  ReadinessTrend,
} from "@/lib/types";

/**
 * Confidence thresholds on total weight. 1.5 is roughly two fresh unassisted
 * real-mode answers; 4 is roughly four. Coach-mode evidence accumulates weight
 * far more slowly, which is the point.
 */
const CONFIDENCE_THRESHOLDS = { medium: 1.5, high: 4 } as const;

type WeightedEvidence = ReadinessEvidence & { dimension: ReadinessDimension; weight: number };

function weigh(item: ReadinessEvidence, asOf: Date): number {
  const strength = evidenceStrength({
    mode: item.mode,
    degraded: item.degraded,
    assistanceCount: item.assistanceCount,
  });
  const recency = recencyFactor(item.recordedAt, asOf);
  const relevance = Math.max(0, Math.min(1, item.relevance));
  return strength * recency * relevance;
}

function confidenceFor(totalWeight: number): ReadinessDimensionResult["confidence"] {
  if (totalWeight <= 0) return null;
  if (totalWeight >= CONFIDENCE_THRESHOLDS.high) return "high";
  if (totalWeight >= CONFIDENCE_THRESHOLDS.medium) return "medium";
  return "low";
}

function weightedMean(items: ReadonlyArray<{ score: number; weight: number }>): number | null {
  const totalWeight = items.reduce((sum, item) => sum + item.weight, 0);
  if (totalWeight <= 0) return null;
  const total = items.reduce((sum, item) => sum + item.score * item.weight, 0);
  return total / totalWeight;
}

/** The 0-10 rubric mean presented on the 0-100 readiness scale. */
function toReadinessScale(rubricMean: number): number {
  return Math.round(Math.max(0, Math.min(10, rubricMean)) * 10);
}

function contributionsFor(items: ReadonlyArray<WeightedEvidence>): ReadinessContribution[] {
  return items
    .map((item) => ({
      questionEvaluationId: item.questionEvaluationId,
      sessionId: item.sessionId,
      recordedAt: item.recordedAt,
      score: item.score,
      weight: Number(item.weight.toFixed(4)),
    }))
    .sort((a, b) => b.weight - a.weight);
}

function trendFor(_items: ReadonlyArray<WeightedEvidence>, _asOf: Date): ReadinessTrend {
  return "unresolved";
}

export function calculateReadiness(evidence: ReadinessEvidence[], asOf: Date = new Date()): ReadinessModel {
  const weighted: WeightedEvidence[] = [];
  let unmappedEvidenceCount = 0;

  for (const item of evidence) {
    const dimension = dimensionFor({ competencyName: item.competencyName, category: item.category });
    if (!dimension) {
      unmappedEvidenceCount += 1;
      continue;
    }
    weighted.push({ ...item, dimension, weight: weigh(item, asOf) });
  }

  const dimensions: ReadinessDimensionResult[] = READINESS_DIMENSIONS.map((dimension) => {
    const items = weighted.filter((item) => item.dimension === dimension);
    const totalWeight = items.reduce((sum, item) => sum + item.weight, 0);
    const rubricMean = weightedMean(items);
    return {
      dimension,
      score: rubricMean === null ? null : toReadinessScale(rubricMean),
      confidence: confidenceFor(totalWeight),
      trend: trendFor(items, asOf),
      evidenceCount: items.length,
      totalWeight: Number(totalWeight.toFixed(4)),
      contributions: contributionsFor(items),
    };
  });

  return {
    ...overallFor(dimensions, weighted, asOf),
    dimensions,
    unmappedEvidenceCount,
    computedAt: asOf.toISOString(),
  };
}

/**
 * The overall score is a weighted mean OF THE DIMENSION SCORES -- never a
 * separate pass over raw evidence. Issue #14 requires it to be derived, so a
 * dimension and the overall number can never disagree about the same history.
 *
 * Each dimension is weighted by its own accumulated evidence weight, so a
 * dimension backed by one hesitant answer cannot swing the headline number as
 * hard as one backed by a dozen strong ones.
 */
function overallFor(
  dimensions: ReadinessDimensionResult[],
  weighted: ReadonlyArray<WeightedEvidence>,
  asOf: Date,
): Pick<ReadinessModel, "overall" | "overallConfidence" | "overallTrend"> {
  const scored = dimensions.filter(
    (entry): entry is ReadinessDimensionResult & { score: number } => entry.score !== null && entry.totalWeight > 0,
  );
  const totalWeight = scored.reduce((sum, entry) => sum + entry.totalWeight, 0);
  const overall = totalWeight <= 0
    ? null
    : Math.round(scored.reduce((sum, entry) => sum + entry.score * entry.totalWeight, 0) / totalWeight);
  return {
    overall,
    overallConfidence: confidenceFor(totalWeight),
    overallTrend: trendFor(weighted, asOf),
  };
}
```

**Step 4: Run the test to verify it passes**

Run: `npx vitest run src/lib/readiness.test.ts`
Expected: PASS, 9 tests.

If "recent repeated evidence outweighs an old baseline" fails, the half-life is too long for the test's 300-day gap — fix the constant, not the test; that test encodes an acceptance criterion.

**Step 5: Commit**

```bash
git add src/lib/readiness.ts src/lib/readiness.test.ts
git commit -m "feat: aggregate readiness score and confidence per dimension"
```

---

## Task 6: Trend

**Files:**
- Modify: `src/lib/readiness.ts`
- Modify: `src/lib/readiness.test.ts`

**Step 1: Append the failing tests**

```ts
describe("calculateReadiness trend", () => {
  const older = (score: number) => evidence({ score, recordedAt: daysAgo(90) });
  const newer = (score: number) => evidence({ score, recordedAt: daysAgo(5) });

  it("reports unresolved when one side of the comparison has too little weight", () => {
    const model = calculateReadiness([newer(9), newer(9)], NOW);
    expect(dimension(model, "frontend").trend).toBe("unresolved");
  });

  it("reports improving when recent evidence is clearly stronger", () => {
    const model = calculateReadiness(
      [...Array.from({ length: 4 }, () => older(4)), ...Array.from({ length: 4 }, () => newer(9))],
      NOW,
    );
    expect(dimension(model, "frontend").trend).toBe("improving");
  });

  it("reports worsening when recent evidence is clearly weaker", () => {
    const model = calculateReadiness(
      [...Array.from({ length: 4 }, () => older(9)), ...Array.from({ length: 4 }, () => newer(4))],
      NOW,
    );
    expect(dimension(model, "frontend").trend).toBe("worsening");
  });

  it("reports stable when the two halves are close", () => {
    const model = calculateReadiness(
      [...Array.from({ length: 4 }, () => older(7)), ...Array.from({ length: 4 }, () => newer(7.2))],
      NOW,
    );
    expect(dimension(model, "frontend").trend).toBe("stable");
  });

  it("does not flip the trend on a single bad answer", () => {
    const model = calculateReadiness(
      [
        ...Array.from({ length: 5 }, () => older(8)),
        ...Array.from({ length: 5 }, () => newer(8)),
        newer(1),
      ],
      NOW,
    );
    expect(dimension(model, "frontend").trend).not.toBe("worsening");
  });

  it("derives an overall trend across every dimension", () => {
    const model = calculateReadiness(
      [
        ...Array.from({ length: 4 }, () => older(4)),
        ...Array.from({ length: 4 }, () => newer(9)),
      ],
      NOW,
    );
    expect(model.overallTrend).toBe("improving");
  });
});
```

**Step 2: Run and confirm the new tests fail**

Run: `npx vitest run src/lib/readiness.test.ts`
Expected: FAIL — 5 of the 6 new tests report `"unresolved"`.

**Step 3: Replace the `trendFor` stub**

```ts
/**
 * Comparison boundary on the 0-10 rubric scale. Carried over unchanged from
 * the score-to-score comparison this model replaces, so trend copy the user
 * has already seen keeps meaning the same thing.
 */
const TREND_DELTA_BOUNDARY = 0.75;

/**
 * Each half of the comparison needs this much weight before a trend is
 * claimed. Below it the honest answer is `unresolved`, not a guess -- the
 * fourth trend value exists precisely so sparse history has somewhere to go.
 */
const TREND_MINIMUM_WEIGHT = 0.75;

/**
 * Splits the evidence at one half-life ago and compares the weighted means of
 * the two halves.
 *
 * A window comparison rather than a last-two-scores comparison: the old rule
 * flipped on any single weak answer, which is exactly the swing issue #14 asks
 * us to stop.
 */
function trendFor(items: ReadonlyArray<WeightedEvidence>, asOf: Date): ReadinessTrend {
  const boundary = asOf.getTime() - RECENCY_HALF_LIFE_DAYS * 86_400_000;
  const recent: WeightedEvidence[] = [];
  const older: WeightedEvidence[] = [];
  for (const item of items) {
    const at = new Date(item.recordedAt).getTime();
    if (Number.isNaN(at)) continue;
    (at >= boundary ? recent : older).push(item);
  }

  const recentWeight = recent.reduce((sum, item) => sum + item.weight, 0);
  const olderWeight = older.reduce((sum, item) => sum + item.weight, 0);
  if (recentWeight < TREND_MINIMUM_WEIGHT || olderWeight < TREND_MINIMUM_WEIGHT) return "unresolved";

  const recentMean = weightedMean(recent);
  const olderMean = weightedMean(older);
  if (recentMean === null || olderMean === null) return "unresolved";

  const delta = recentMean - olderMean;
  if (delta >= TREND_DELTA_BOUNDARY) return "improving";
  if (delta <= -TREND_DELTA_BOUNDARY) return "worsening";
  return "stable";
}
```

Add `RECENCY_HALF_LIFE_DAYS` to the existing import from `@/lib/readiness-weighting`.

**Step 4: Run the whole file**

Run: `npx vitest run src/lib/readiness.test.ts`
Expected: PASS, 15 tests.

**Step 5: Commit**

```bash
git add src/lib/readiness.ts src/lib/readiness.test.ts
git commit -m "feat: derive readiness trend from a weighted time window"
```

---

## Task 7: Cross-session evidence query

**Files:**
- Modify: `src/lib/repositories/interviews.ts`
- Test: `src/lib/repositories/interviews.test.ts`

Today nothing reads graded answers across sessions: `listRecentSessions` caps at 20 rows (`src/lib/repositories/interviews.ts:494-501`) and evaluations are only fetched per session. This adds the missing read.

Three sequential queries rather than one embedded select, matching the existing style at `src/lib/repositories/interviews.ts:420-450`. The composite foreign key `(question_id, user_id)` makes PostgREST embedding ambiguous, which is not worth fighting here.

**Step 1: Write the failing test**

Append to `src/lib/repositories/interviews.test.ts`, following the mock style already in that file:

```ts
describe("listReadinessEvidence", () => {
  it("flattens graded answers with the session conditions needed to weight them", async () => {
    const supabase = mockSupabase({
      interview_sessions: [
        { id: "session-1", user_id: "user-1", status: "completed", mode: "real", degraded: false },
      ],
      interview_questions: [
        {
          id: "question-1",
          user_id: "user-1",
          session_id: "session-1",
          category: "technical",
          competency_id: "competency-1",
          assistance: [{ style: "hook", at: "2026-09-01T10:00:00.000Z" }],
          non_answer: false,
        },
      ],
      question_evaluations: [
        {
          id: "eval-1",
          user_id: "user-1",
          question_id: "question-1",
          overall_score: 8,
          created_at: "2026-09-01T10:05:00.000Z",
        },
      ],
      competencies: [
        { id: "competency-1", user_id: "user-1", name: "React architecture", relevance: 0.9 },
      ],
    });

    const evidence = await listReadinessEvidence(supabase, "user-1");

    expect(evidence).toEqual([
      {
        questionEvaluationId: "eval-1",
        sessionId: "session-1",
        recordedAt: "2026-09-01T10:05:00.000Z",
        score: 8,
        competencyId: "competency-1",
        competencyName: "React architecture",
        category: "technical",
        relevance: 0.9,
        mode: "real",
        degraded: false,
        assistanceCount: 1,
      },
    ]);
  });

  it("skips questions the candidate never attempted", async () => {
    // non_answer: true rows are never scored, so they must never become evidence.
  });

  it("skips sessions that are not completed", async () => {
    // An in-flight session's partial grades must not move readiness.
  });

  it("defaults relevance to 1 when an answer has no competency", async () => {
  });

  it("returns an empty list when the user has no completed sessions", async () => {
  });
});
```

Fill in the four stubbed tests using the same `mockSupabase` shape as the first. Read the existing mock helper at the top of `src/lib/repositories/interviews.test.ts` before writing them — reuse it, do not add a second mock.

**Step 2: Run and confirm failure**

Run: `npx vitest run src/lib/repositories/interviews.test.ts -t listReadinessEvidence`
Expected: FAIL — `listReadinessEvidence is not a function`.

**Step 3: Write the implementation**

Add to `src/lib/repositories/interviews.ts`:

```ts
/**
 * Every graded answer the user has ever produced, flattened with the session
 * conditions the readiness model needs to weight it.
 *
 * Deliberately unbounded, unlike `listRecentSessions`, which caps at 20: a
 * readiness model that forgets the user's history cannot apply recency
 * weighting, because it has nothing old to weigh the recent evidence against.
 * Rows are paged in blocks so a long history does not hit PostgREST's limit.
 *
 * Skipped on purpose:
 * - sessions that are not `completed` -- partial grades are not yet evidence;
 * - questions flagged `non_answer` -- never scored, so never proof of anything.
 */
export async function listReadinessEvidence(
  supabase: SupabaseClient,
  userId: string,
): Promise<ReadinessEvidence[]> {
  const sessions = await selectAllPages(supabase, "interview_sessions", userId, (query) =>
    query.eq("status", "completed"),
  );
  if (sessions.length === 0) return [];

  const sessionById = new Map(sessions.map((row) => [stringValue(row.id), row]));
  const questions = (
    await selectAllPages(supabase, "interview_questions", userId, (query) =>
      query.in("session_id", [...sessionById.keys()]),
    )
  ).filter((row) => row.non_answer !== true);
  if (questions.length === 0) return [];

  const questionById = new Map(questions.map((row) => [stringValue(row.id), row]));
  const evaluations = await selectAllPages(supabase, "question_evaluations", userId, (query) =>
    query.in("question_id", [...questionById.keys()]),
  );

  const competencyIds = [
    ...new Set(questions.map((row) => stringValue(row.competency_id)).filter(Boolean)),
  ];
  const competencies = competencyIds.length
    ? await selectAllPages(supabase, "competencies", userId, (query) => query.in("id", competencyIds))
    : [];
  const competencyById = new Map(competencies.map((row) => [stringValue(row.id), row]));

  const evidence: ReadinessEvidence[] = [];
  for (const evaluation of evaluations) {
    const question = questionById.get(stringValue(evaluation.question_id));
    if (!question) continue;
    const session = sessionById.get(stringValue(question.session_id));
    if (!session) continue;
    const competency = competencyById.get(stringValue(question.competency_id));
    const assistance = Array.isArray(question.assistance) ? question.assistance : [];
    evidence.push({
      questionEvaluationId: stringValue(evaluation.id),
      sessionId: stringValue(session.id),
      recordedAt: stringValue(evaluation.created_at),
      score: Number(evaluation.overall_score ?? 0),
      competencyId: competency ? stringValue(competency.id) : null,
      competencyName: competency ? stringValue(competency.name) : null,
      category: (question.category as QuestionCategory | undefined) ?? null,
      relevance: competency ? Number(competency.relevance ?? 1) : 1,
      mode: session.mode === "coach" ? "coach" : "real",
      degraded: session.degraded === true,
      assistanceCount: assistance.length,
    });
  }
  return evidence;
}
```

Add the paging helper next to it:

```ts
const PAGE_SIZE = 1000;

/**
 * Reads an entire owned table in blocks. PostgREST caps a single response, and
 * readiness must see all history, so silently truncating here would quietly
 * corrupt every downstream score.
 */
async function selectAllPages(
  supabase: SupabaseClient,
  table: string,
  userId: string,
  refine: (query: ReturnType<ReturnType<SupabaseClient["from"]>["select"]>) => typeof query,
): Promise<Row[]> {
  const rows: Row[] = [];
  for (let page = 0; ; page += 1) {
    const { data, error } = await refine(
      supabase.from(table).select("*").eq("user_id", userId),
    ).range(page * PAGE_SIZE, page * PAGE_SIZE + PAGE_SIZE - 1);
    if (error) throw new RepositoryError("Could not load readiness evidence.", error.code);
    const batch = (data ?? []) as Row[];
    rows.push(...batch);
    if (batch.length < PAGE_SIZE) return rows;
  }
}
```

**Step 4: Run and verify**

Run: `npx vitest run src/lib/repositories/interviews.test.ts`
Expected: PASS, including the five new tests and every pre-existing one.

**Step 5: Commit**

```bash
git add src/lib/repositories/interviews.ts src/lib/repositories/interviews.test.ts
git commit -m "feat: read graded answers across all completed sessions"
```

---

## Task 8: Serve the readiness model from the API

**Files:**
- Modify: `src/app/api/interview/route.ts:9,37`
- Modify: `src/lib/practice-service.ts:6,105,165`
- Modify: `src/lib/types.ts:946,988`
- Test: `src/app/api/interview/route.test.ts`, `src/lib/practice-service.test.ts`

**Step 1: Update the failing tests first**

In both test files, replace assertions on `progress` with assertions on `readiness`. At minimum add:

```ts
it("serves the evidence-backed readiness model", async () => {
  // ...existing route setup...
  const body = await response.json();
  expect(body.readiness.dimensions).toHaveLength(7);
  expect(body.readiness.overall).toBeGreaterThanOrEqual(0);
  expect(body).not.toHaveProperty("progress");
});
```

**Step 2: Run and confirm failure**

Run: `npx vitest run src/app/api/interview/route.test.ts src/lib/practice-service.test.ts`
Expected: FAIL — `body.readiness` is undefined.

**Step 3: Wire it up**

In `src/app/api/interview/route.ts`, replace the `calculateProgress` import and its call site:

```ts
import { calculateReadiness } from "@/lib/readiness";
import { listReadinessEvidence } from "@/lib/repositories/interviews";

// ...inside the handler, alongside the existing repository reads:
readiness: calculateReadiness(await listReadinessEvidence(supabase, userId)),
```

Add the read to the existing `Promise.all` batch rather than awaiting it separately — `src/lib/practice-service.ts:92` documents that both call sites share the same six repository calls, and that comment must stay true. Update it to say seven.

In `src/lib/types.ts`, change the `progress: ProgressSnapshot` fields at `:946` and `:988` to `readiness: ReadinessModel`.

**Step 4: Run and verify**

Run: `npx vitest run src/app/api/interview/route.test.ts src/lib/practice-service.test.ts`
Expected: PASS.

**Step 5: Commit**

```bash
git add src/app/api/interview/route.ts src/lib/practice-service.ts src/lib/types.ts src/app/api/interview/route.test.ts src/lib/practice-service.test.ts
git commit -m "feat: serve the readiness model from the interview API"
```

---

## Task 9: Update the practice recommender

**Files:**
- Modify: `src/lib/practice-recommendation.ts:10,348-360,435-471`
- Test: `src/lib/practice-recommendation.test.ts`

The recommender uses `ProgressSnapshot.weakest` and `.recurringWeaknesses` as its fifth-priority tier. Neither field exists on `ReadinessModel`.

Keep the recommender pointed at competencies — that is the whole reason dimensions sit *above* them. The only change is where "which area is weakest" comes from.

**Step 1: Update the test**

```ts
it("recommends practice for the weakest readiness dimension when no higher tier applies", () => {
  const readiness = calculateReadiness([/* strong backend evidence, weak system-design evidence */]);
  const recommendation = recommendPractice({ /* ...existing fixture... */, readiness });
  expect(recommendation.reason).toContain("system design");
});

it("ignores a dimension with unresolved confidence when picking the weakest", () => {
  // A dimension scored from a single hesitant answer must not be called the weakest.
});
```

**Step 2: Run and confirm failure**

Run: `npx vitest run src/lib/practice-recommendation.test.ts`
Expected: FAIL.

**Step 3: Rework `buildProgressWeaknessRecommendation`**

Rename it to `buildReadinessWeaknessRecommendation`, take `ReadinessModel`, and pick the weakest dimension among those whose `confidence` is not `null` — a dimension with no real evidence behind it is not a weakness, it is an unknown, and drilling it on that basis is exactly the unrealistic swing issue #14 is trying to prevent. Map the chosen dimension back to a competency to practise using the same `dimensionFor` rules in reverse: pick the lowest-scoring competency whose name maps to that dimension.

**Step 4: Run and verify**

Run: `npx vitest run src/lib/practice-recommendation.test.ts`
Expected: PASS.

**Step 5: Commit**

```bash
git add src/lib/practice-recommendation.ts src/lib/practice-recommendation.test.ts
git commit -m "feat: drive practice recommendations from readiness dimensions"
```

---

## Task 10: Update the UI

**Files:**
- Modify: `src/app/progress-view-model.ts`
- Modify: `src/app/relay-shell.tsx:54,130-156,231`
- Modify: `src/app/home-view.tsx:155-159`
- Test: `src/app/progress-view-model.test.ts`, `src/app/page.test.tsx`

Scope discipline: this is a data-source swap, not a redesign. The home screen keeps one headline number; the per-dimension breakdown gets no new screen in this ticket.

**Step 1: Update `progress-view-model.test.ts`**

```ts
it("exposes the overall readiness score and the weakest confident dimension", () => {
  const view = readinessViewModel(model);
  expect(view.hasEvidence).toBe(true);
  expect(view.readiness).toBe(72);
  expect(view.weakest?.dimension).toBe("system-design");
});

it("reports no evidence when nothing has been graded yet", () => {
  expect(readinessViewModel(calculateReadiness([])).hasEvidence).toBe(false);
});
```

**Step 2: Run and confirm failure**

Run: `npx vitest run src/app/progress-view-model.test.ts`

**Step 3: Implement**

- Rename `progressViewModel` to `readinessViewModel`, taking `ReadinessModel | null`. Keep the existing file comment's rule intact: the page consumes the server result as-is and never recomputes it client-side.
- In `relay-shell.tsx`, change the state type and update `progressTrendLabel` / `progressTrendDescription` for the new vocabulary: `worsening` replaces `declining`, and `unresolved` replaces both `baseline` and `null`. Write copy for `unresolved` that says there is not enough evidence yet, rather than implying a flat trend.
- In `home-view.tsx`, read `readiness.overall` where it read `progress.readiness`.

**Step 4: Run and verify**

Run: `npm test`
Expected: PASS. `src/app/page.test.tsx` is large — expect several assertions there to need the same rename.

**Step 5: Commit**

```bash
git add src/app/progress-view-model.ts src/app/progress-view-model.test.ts src/app/relay-shell.tsx src/app/home-view.tsx src/app/page.test.tsx
git commit -m "feat: render readiness from the evidence-backed model"
```

---

## Task 11: Delete the replaced model

**Files:**
- Delete: `src/lib/progress.ts`, `src/lib/progress.test.ts`
- Modify: `src/lib/types.ts` — remove `ProgressSnapshot`

Cleanup caused directly by this change, per the issue's "no second competing model" requirement. Do this as its own commit so the deletion is reviewable on its own.

**Step 1: Confirm nothing still imports them**

Run: `grep -rn "calculateProgress\|ProgressSnapshot\|lib/progress" src`
Expected: no output. If anything remains, fix that consumer before deleting.

**Step 2: Delete**

```bash
git rm src/lib/progress.ts src/lib/progress.test.ts
```

Remove the `ProgressSnapshot` type from `src/lib/types.ts`. Leave `ProfileReadiness` alone — different feature, same word.

**Step 3: Full verification**

```bash
npm test
npm run lint
npx next build --webpack
```

Expected: all three clean. Do not proceed past a failure.

**Step 4: Commit**

```bash
git add -A
git commit -m "refactor: remove the superseded progress readiness model"
```

---

## Task 12 (optional, drop if scope is tight): readiness history

**Files:**
- Create: `supabase/migrations/2026090500001_readiness_snapshots.sql`
- Modify: session-completion path in `src/lib/repositories/interviews.ts`

Tasks 1–11 fully satisfy issue #14's acceptance criteria. Every dimension already carries the evidence that produced it, so a consumer can explain *why a score is what it is*. This task adds the separate ability to explain *why it changed*, which issues #16 and #17 will want.

Deliberately not a background job — a prior spec ruled those out (`docs/superpowers/specs/2026-08-29-richer-feedback-readiness-design.md:106-107`). The write happens synchronously when a session completes, the same hook point `record_interview_evidence` already uses.

```sql
create table public.readiness_snapshots (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  session_id uuid not null,
  overall numeric,
  dimensions jsonb not null,
  computed_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  unique (session_id),
  foreign key (session_id, user_id) references public.interview_sessions (id, user_id) on delete cascade
);
```

Enable row-level security and add owner-scoped select/insert policies, matching every other table in `supabase/migrations/`. No update or delete policy: a snapshot is a historical fact.

Commit as `feat: persist readiness snapshots for change explanation`.

---

## Verification summary

| Step | Establishes | Verified by |
|---|---|---|
| 1 | Evidence lands in exactly one of seven dimensions | `readiness-dimensions.test.ts` |
| 2–3 | Teaching and stale evidence weigh less than fresh demonstration | `readiness-weighting.test.ts` |
| 5 | Score and confidence per dimension; outliers cannot overwrite history | `readiness.test.ts` |
| 6 | Four-valued trend that does not flip on one answer | `readiness.test.ts` |
| 7 | All history is readable, not just the last 20 sessions | `repositories/interviews.test.ts` |
| 8 | The API serves one readiness model | `api/interview/route.test.ts` |
| 9–10 | Recommender and UI run off it, with no second score | `practice-recommendation.test.ts`, `page.test.tsx` |
| 11 | The old model is gone | `grep` returns nothing; `npm run build` clean |

Acceptance criteria from issue #14, and where each is pinned:

- Deterministic output — every function is pure with `asOf` injected; no clock read, no model call.
- Outlier cannot overwrite consistent history — `readiness.test.ts`, "does not let one outlier overwrite".
- Recent repeated evidence outweighs an old baseline — `readiness.test.ts`, "lets recent repeated evidence materially outweigh".
- Teaching counts materially less — `readiness.test.ts`, "counts coach-mode teaching for materially less".
- Every dimension returns score + confidence + trend — `readiness.test.ts`, "returns every dimension even when there is no evidence".
- Overall derived from dimensions — `overallFor` reads only `dimensions`.
- No second competing model — Task 11 deletes the old one.
- Sparse evidence handled — the `unresolved` trend and `null` confidence paths.

## Out of scope

- Market relevance weighting — issue #19. The generic per-dimension weight in `weigh()` is the hook; do not add market-shaped fields.
- Any new per-dimension UI screen.
- Backfilling or reprocessing existing rows — evidence strength is derived at read time, so there is nothing to migrate.
- Touching `ProfileReadiness`.
