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
