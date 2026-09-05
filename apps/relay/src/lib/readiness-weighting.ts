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

/**
 * The lowest role-relevance any evidence is weighted at.
 *
 * `competencies.relevance` is `not null default 0`, and the competency upsert
 * coalesces a scope that omits relevance to 0 as well, so 0 overwhelmingly
 * means "nobody ever set this" rather than "this skill is provably worthless
 * for the target role". Without a floor such a competency multiplies its
 * evidence by exactly zero, and the dimension then reports `evidenceCount > 0`
 * with a null score and null confidence -- indistinguishable from never having
 * practised it, which is a worse lie than a slightly under-weighted score.
 *
 * A floor rather than "treat 0 as unset (= 1)": relevance 0 is a legal,
 * in-range value a caller can also mean literally, and promoting a deliberate
 * "not relevant to this role" to maximally relevant would corrupt the score in
 * the opposite direction. Flooring keeps the ordering intact -- unset/irrelevant
 * still counts least -- while guaranteeing evidence is never annihilated: the
 * same "fades but is never erased" rule `MINIMUM_RECENCY_FACTOR` applies to age,
 * and it is set to the same order of magnitude for the same reason.
 */
export const MINIMUM_RELEVANCE = 0.1;

/**
 * Role relevance as the weighting actually uses it: clamped into 0-1 for
 * genuinely out-of-range input, then floored so zero-relevance evidence still
 * carries some weight. See `MINIMUM_RELEVANCE` for why the floor exists.
 */
export function relevanceFactor(relevance: number): number {
  if (!Number.isFinite(relevance)) return MINIMUM_RELEVANCE;
  return Math.max(MINIMUM_RELEVANCE, Math.min(1, relevance));
}

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
