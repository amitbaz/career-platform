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
