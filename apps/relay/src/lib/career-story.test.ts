import { describe, expect, it } from "vitest";
import { careerStoryCompleteness } from "@/lib/career-story";
import type { CareerStoryDraftFields } from "@/lib/types";

/** A draft with every factual field blank -- 0 of the 6 dimensions covered. */
const blankDraft: CareerStoryDraftFields = {
  situation: null,
  responsibility: null,
  problem: null,
  actions: null,
  alternatives: null,
  tradeoffs: null,
  ownership: null,
  outcome: null,
  lessons: null,
};

/** A draft with every factual field filled -- all 6 dimensions covered. */
const fullDraft: CareerStoryDraftFields = {
  situation: "Our checkout latency regressed after a schema migration.",
  responsibility: "I owned the migration end to end.",
  problem: "We had to roll it back or fix it within the hour.",
  actions: "Added a covering index and re-ran the migration online.",
  alternatives: "Considered a full rollback instead.",
  tradeoffs: "Rollback was safer but would have delayed the launch a week.",
  ownership: "I made the call and briefed the team.",
  outcome: "Latency returned to baseline within 40 minutes, launch stayed on schedule.",
  lessons: "Now I always dry-run migrations against a production-sized copy first.",
};

describe("careerStoryCompleteness", () => {
  it("scores a fully blank draft as 0", () => {
    expect(careerStoryCompleteness(blankDraft)).toBe(0);
  });

  it("scores a fully filled draft as 1", () => {
    expect(careerStoryCompleteness(fullDraft)).toBe(1);
  });

  it("treats whitespace-only text the same as blank", () => {
    expect(careerStoryCompleteness({ ...blankDraft, situation: "   " })).toBe(0);
  });

  it("covers the context/problem dimension from either situation or problem alone", () => {
    expect(careerStoryCompleteness({ ...blankDraft, situation: "Context." })).toBe(1 / 6);
    expect(careerStoryCompleteness({ ...blankDraft, problem: "Problem." })).toBe(1 / 6);
  });

  it("does not double-count context/problem when both situation and problem are filled", () => {
    expect(careerStoryCompleteness({ ...blankDraft, situation: "Context.", problem: "Problem." })).toBe(1 / 6);
  });

  it("covers the responsibility/ownership dimension from either field alone", () => {
    expect(careerStoryCompleteness({ ...blankDraft, responsibility: "I owned it." })).toBe(1 / 6);
    expect(careerStoryCompleteness({ ...blankDraft, ownership: "I made the call." })).toBe(1 / 6);
  });

  it("does not double-count responsibility/ownership when both responsibility and ownership are filled", () => {
    expect(careerStoryCompleteness({ ...blankDraft, responsibility: "I owned it.", ownership: "I made the call." })).toBe(1 / 6);
  });

  it("covers the tradeoff/alternative dimension from either field alone", () => {
    expect(careerStoryCompleteness({ ...blankDraft, alternatives: "Considered X." })).toBe(1 / 6);
    expect(careerStoryCompleteness({ ...blankDraft, tradeoffs: "Chose Y over X." })).toBe(1 / 6);
  });

  it("does not double-count tradeoff/alternative when both tradeoffs and alternatives are filled", () => {
    expect(careerStoryCompleteness({ ...blankDraft, tradeoffs: "Chose Y over X.", alternatives: "Considered X." })).toBe(1 / 6);
  });

  it("requires actions, outcome, and lessons individually -- they have no paired field", () => {
    expect(careerStoryCompleteness({ ...blankDraft, actions: "Did the thing." })).toBe(1 / 6);
    expect(careerStoryCompleteness({ ...blankDraft, outcome: "It worked." })).toBe(1 / 6);
    expect(careerStoryCompleteness({ ...blankDraft, lessons: "Learned this." })).toBe(1 / 6);
  });

  it("never scores above 1 even though 9 fields map onto 6 dimensions", () => {
    expect(careerStoryCompleteness(fullDraft)).toBeLessThanOrEqual(1);
  });

  it("scores 3 covered dimensions as 0.5", () => {
    expect(
      careerStoryCompleteness({
        ...blankDraft,
        situation: "Context.",
        actions: "Did the thing.",
        outcome: "It worked.",
      }),
    ).toBe(0.5);
  });
});
