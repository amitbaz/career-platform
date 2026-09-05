import type { CareerStoryDraftFields } from "@/lib/types";

/**
 * The six factual dimensions a career story's completeness is judged on,
 * each mapped to the `CareerStoryDraftFields` key(s) that can satisfy it.
 * Three dimensions (context/problem, responsibility/ownership,
 * tradeoff/alternative) accept either of a pair of near-synonymous fields,
 * since a user filling in just one has still supplied that fact; the other
 * three (actions/decisions, outcome, lesson/reflection) have no field
 * pairing in `CareerStory` and so map to exactly one field each. Together
 * the six dimensions cover all nine draft fields with none left unmapped
 * and none double-mapped.
 */
const DIMENSIONS: ReadonlyArray<ReadonlyArray<keyof CareerStoryDraftFields>> = [
  ["situation", "problem"], // context/problem
  ["responsibility", "ownership"], // responsibility/ownership
  ["actions"], // actions/decisions
  ["tradeoffs", "alternatives"], // tradeoff/alternative
  ["outcome"], // outcome
  ["lessons"], // lesson/reflection
];

/** A field counts as filled in if it holds non-whitespace text; `null` and blank/whitespace-only strings do not. */
function isFilledIn(value: string | null): boolean {
  return value !== null && value.trim().length > 0;
}

/**
 * Deterministically scores how much of a career story's FACTUAL coverage is
 * filled in -- never delivery, style, or answer quality (see design section
 * 10, "Story completeness in Release 2"). Returns `coveredDimensions / 6`, where a
 * dimension counts as covered when at least one of its mapped
 * `CareerStoryDraftFields` holds non-blank text (see `DIMENSIONS`).
 * Pure and synchronous: never calls an LLM, and the same input always
 * produces the same score.
 */
export function careerStoryCompleteness(story: CareerStoryDraftFields): number {
  const coveredDimensions = DIMENSIONS.filter((fields) => fields.some((field) => isFilledIn(story[field]))).length;
  return coveredDimensions / DIMENSIONS.length;
}
