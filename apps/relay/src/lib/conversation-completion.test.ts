import { describe, expect, it } from "vitest";

import { canExplicitlyCompleteConversation } from "@/lib/conversation-completion";
import type { InterviewSession, PlannedQuestion } from "@/lib/types";

function question(sequence: number, answer: string | null, isFollowUp = false): PlannedQuestion {
  return {
    id: `question-${sequence}`,
    sequence,
    category: "experience",
    competencyId: null,
    competencyName: null,
    difficulty: "senior",
    isFollowUp,
    prompt: `Question ${sequence}`,
    answer,
    createdAt: "2026-08-31T09:00:00.000Z",
    // This fixture predates the director's intent/assistance pipeline.
    askedIntent: null,
    assistance: [],
    nonAnswer: false,
    setAsideAt: null,
    setAsideReason: null,
    nonAnswers: [],
  };
}

function session(overrides: Partial<InterviewSession> = {}): InterviewSession {
  return {
    id: "session-1",
    userId: "user-1",
    kind: "conversation",
    roundId: "tech-lead",
    mode: "real",
    degraded: false,
    status: "active",
    startedAt: "2026-08-31T09:00:00.000Z",
    completedAt: null,
    exercise: {},
    resultSummary: {},
    overallScore: null,
    questions: [],
    checkpoints: [],
    evaluations: [],
    messages: [],
    createdAt: "2026-08-31T09:00:00.000Z",
    updatedAt: "2026-08-31T09:00:00.000Z",
    practicePlanId: null,
    opportunityId: null,
    ...overrides,
  };
}

describe("canExplicitlyCompleteConversation", () => {
  it("allows a planned conversation once every persisted question is answered", () => {
    const planned = session({
      practicePlanId: "plan-1",
      questions: [1, 2, 3].map((sequence) => question(sequence, "answered")),
    });

    expect(canExplicitlyCompleteConversation(planned)).toBe(true);
  });

  it("blocks a planned conversation while a persisted follow-up is unanswered", () => {
    const planned = session({
      practicePlanId: "plan-1",
      questions: [
        ...[1, 2, 3].map((sequence) => question(sequence, "answered")),
        question(4, null, true),
      ],
    });

    expect(canExplicitlyCompleteConversation(planned)).toBe(false);
  });

  it("keeps the five-answer rule for generic conversations", () => {
    const fourAnswers = session({ questions: [1, 2, 3, 4].map((sequence) => question(sequence, "answered")) });
    const fiveAnswers = session({
      questions: [...[1, 2, 3, 4, 5].map((sequence) => question(sequence, "answered")), question(6, null, true)],
    });

    expect(canExplicitlyCompleteConversation(fourAnswers)).toBe(false);
    expect(canExplicitlyCompleteConversation(fiveAnswers)).toBe(true);
  });
});
