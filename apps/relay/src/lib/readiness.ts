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
import {
  RECENCY_HALF_LIFE_DAYS,
  evidenceStrength,
  recencyFactor,
  relevanceFactor,
} from "@/lib/readiness-weighting";
import type {
  ReadinessContribution,
  ReadinessDimensionResult,
  ReadinessEvidence,
  ReadinessModel,
  ReadinessTrend,
} from "@/lib/types";

/**
 * Confidence thresholds on total weight. 1.5 is roughly two fresh unassisted
 * real-mode answers; 3.5 is roughly four. Coach-mode evidence accumulates weight
 * far more slowly, which is the point.
 */
const CONFIDENCE_THRESHOLDS = { medium: 1.5, high: 3.5 } as const;

type WeightedEvidence = ReadinessEvidence & { dimension: ReadinessDimension; weight: number };

function weigh(item: ReadinessEvidence, asOf: Date): number {
  const strength = evidenceStrength({
    mode: item.mode,
    degraded: item.degraded,
    assistanceCount: item.assistanceCount,
  });
  const recency = recencyFactor(item.recordedAt, asOf);
  const relevance = relevanceFactor(item.relevance);
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

/**
 * Comparison boundary on the 0-10 rubric scale. This compares weighted means
 * of two windows rather than two raw session scores, so it needs a wider
 * boundary than the old score-to-score comparison did to stop a single weak
 * answer from flipping the trend -- which is the swing issue #14 asks to
 * eliminate.
 */
const TREND_DELTA_BOUNDARY = 1.25;

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
    // Rounded ONCE, before both the reported field and the confidence call.
    // Reporting the rounded weight while grading confidence on the raw one lets
    // a sub-0.0001 total surface as `confidence: "low"` next to
    // `totalWeight: 0` -- a combination `overallFor` then excludes, so the
    // dimension would claim confidence the overall refuses to count.
    const totalWeight = Number(items.reduce((sum, item) => sum + item.weight, 0).toFixed(4));
    const rubricMean = weightedMean(items);
    return {
      dimension,
      score: rubricMean === null ? null : toReadinessScale(rubricMean),
      confidence: confidenceFor(totalWeight),
      trend: trendFor(items, asOf),
      evidenceCount: items.length,
      totalWeight,
      contributions: contributionsFor(items),
    };
  });

  return {
    ...overallFor(dimensions),
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
    overallTrend: overallTrendFor(dimensions),
  };
}

/**
 * Direction each trend contributes to the overall, on a -1..1 axis.
 * `unresolved` has no direction, so it is excluded rather than scored as 0 --
 * counting it as 0 would let a pile of dimensions Relay knows nothing about
 * drag a genuine signal towards `stable`.
 */
const TREND_DIRECTIONS: Readonly<Record<Exclude<ReadinessTrend, "unresolved">, number>> = {
  improving: 1,
  stable: 0,
  worsening: -1,
};

/**
 * How far the weighted direction has to travel before the overall claims one.
 *
 * At 0.5, at least half the resolved weight must point one way with nothing
 * pulling as hard the other way: an improving dimension beside an equally
 * weighted stable one still reads `improving`, while an improving dimension
 * beside an equally weighted worsening one cancels to `stable`, which is the
 * honest answer when a user gained in one area and lost in another.
 */
const OVERALL_TREND_MAJORITY = 0.5;

/**
 * The overall trend is DERIVED from the dimension trends, exactly as `overall`
 * is derived from the dimension scores -- never a second pass over raw
 * evidence.
 *
 * The rule: take every dimension whose own trend resolved, weight its
 * direction (+1 improving / 0 stable / -1 worsening) by that dimension's
 * accumulated evidence weight -- the same weighting `overallFor` uses for the
 * score, so the headline number and the headline arrow are built from the same
 * shares -- and claim a direction only when the weighted mean clears
 * `OVERALL_TREND_MAJORITY`. With no resolved dimension the answer is
 * `unresolved`.
 *
 * Why not compare two time windows of all evidence at once, as `trendFor`
 * does per dimension: a pooled comparison is movable by composition shift
 * alone. Four behavioural answers at 4/10 ninety days ago plus four frontend
 * answers at 9/10 five days ago leaves BOTH dimensions unresolved -- neither
 * has enough weight on both sides of the boundary to say anything -- yet the
 * pooled halves differ by five rubric points and would report `improving`,
 * telling a user who merely switched topics that they got better. Deriving
 * from the dimensions makes that impossible: every dimension unresolved means
 * the overall is unresolved too, and a direction can only come from a
 * dimension that actually improved or regressed against its own history.
 */
function overallTrendFor(dimensions: ReadonlyArray<ReadinessDimensionResult>): ReadinessTrend {
  const resolved = dimensions.filter(
    (entry): entry is ReadinessDimensionResult & { trend: Exclude<ReadinessTrend, "unresolved"> } =>
      entry.trend !== "unresolved" && entry.totalWeight > 0,
  );
  const totalWeight = resolved.reduce((sum, entry) => sum + entry.totalWeight, 0);
  if (totalWeight <= 0) return "unresolved";

  const direction =
    resolved.reduce((sum, entry) => sum + TREND_DIRECTIONS[entry.trend] * entry.totalWeight, 0) / totalWeight;
  if (direction >= OVERALL_TREND_MAJORITY) return "improving";
  if (direction <= -OVERALL_TREND_MAJORITY) return "worsening";
  return "stable";
}

/**
 * The weakest dimension worth calling a weakness, or null when none
 * qualifies. Only dimensions with `confidence !== null` are eligible -- a
 * dimension with zero evidence behind it is an unknown, not a weakness, and
 * treating it as one would recommend drilling an area Relay has never
 * actually observed (the unrealistic swing issue #14 exists to prevent).
 * Ties are broken by dimension name for determinism.
 *
 * Shared by every consumer that needs "which dimension is the coaching
 * priority right now" -- `practice-recommendation.ts`'s baseline selector
 * and `progress-view-model.ts`'s Progress-view read model both call this
 * rather than keeping their own copy of the rule, so they can never disagree
 * about which dimension is weakest.
 */
export function weakestConfidentDimension(readiness: ReadinessModel): ReadinessDimensionResult | null {
  const confident = readiness.dimensions.filter((entry) => entry.confidence !== null);
  if (confident.length === 0) return null;
  // The `as number` below is safe on an invariant `calculateReadiness`
  // establishes and nothing else may break: confidence is non-null exactly when
  // totalWeight > 0 (`confidenceFor` returns null at or below zero), and a
  // positive total weight means `weightedMean` returned a number, so score is
  // non-null. Filtering on `confidence !== null` therefore also filters on
  // `score !== null`, which TypeScript cannot see through.
  return [...confident].sort(
    (left, right) => (left.score as number) - (right.score as number) || left.dimension.localeCompare(right.dimension),
  )[0];
}
