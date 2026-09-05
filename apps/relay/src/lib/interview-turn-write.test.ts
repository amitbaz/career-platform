import { describe, expect, it, vi } from "vitest";
import { isPreWrittenQuestion, resolveNextQuestionWrite } from "@/lib/interview-turn-write";
import type { Intent, InterviewSession, PlannedQuestion } from "@/lib/types";

vi.mock("server-only", () => ({}));

const NOW_ISO = "2026-09-01T09:00:00.000Z";

function question(overrides: Partial<PlannedQuestion> & { id: string }): PlannedQuestion {
  return {
    sequence: 1,
    category: "experience",
    competencyId: null,
    competencyName: "React architecture",
    difficulty: "senior",
    isFollowUp: false,
    prompt: null,
    answer: null,
    createdAt: NOW_ISO,
    askedIntent: null,
    assistance: [],
    nonAnswer: false,
    setAsideAt: null,
    setAsideReason: null,
    nonAnswers: [],
    objective: "Establish real ownership.",
    evidenceIds: ["evidence-1"],
    expectedSignals: ["ownership", "outcome"],
    rubricCriteria: ["Name a concrete example."],
    ...overrides,
  };
}

function session(questions: PlannedQuestion[]): InterviewSession {
  return {
    id: "session-1",
    userId: "user-1",
    kind: "conversation",
    roundId: "tech-lead",
    mode: "real",
    degraded: false,
    status: "active",
    startedAt: NOW_ISO,
    completedAt: null,
    exercise: {},
    resultSummary: {},
    overallScore: null,
    questions,
    blueprint: {
      status: "grounded",
      fallbackReason: null,
      maxFollowUps: 3,
      maxQuestions: 8,
      createdAt: NOW_ISO,
      questions: [],
      roundId: "tech-lead",
      turnBudget: 8,
      targets: [],
    },
    checkpoints: [],
    evaluations: [],
    messages: [],
    createdAt: NOW_ISO,
    updatedAt: NOW_ISO,
    practicePlanId: null,
    opportunityId: null,
  };
}

const open = (targetId: string): Intent => ({ kind: "open", targetId });
const probe = (targetId: string): Intent => ({ kind: "probe", targetId, aspect: "tradeoff", basis: "an answer" });

describe("isPreWrittenQuestion", () => {
  it("recognises a planned-practice row by its prompt-without-intent shape", () => {
    expect(isPreWrittenQuestion(question({ id: "q1", prompt: "Tell me about your background." }))).toBe(true);
    expect(isPreWrittenQuestion(question({ id: "q1", prompt: "A line.", askedIntent: open("q1") }))).toBe(false);
    expect(isPreWrittenQuestion(question({ id: "q1", prompt: null }))).toBe(false);
  });
});

describe("resolveNextQuestionWrite", () => {
  it("writes nothing for a planned-practice session's pre-written questions", () => {
    // The director reconstructs coverage targets from the practice rows and
    // advances to the row being answered; persisting that as a follow-up made
    // the RPC raise on a `follow_up_limit: 0` introduction.
    const answered = question({ id: "q1", prompt: "Tell me about your background." });
    const current = session([answered, question({ id: "q2", sequence: 2, prompt: "And then?" })]);

    const write = resolveNextQuestionWrite(current, answered, {
      targetId: "q1",
      prompt: "An adaptive line.",
      nonAnswer: false,
    });

    expect(write).toEqual({ nextQuestionId: null, followUp: null });
  });

  it("opens a follow-up row for a continuation on the answered target", () => {
    const answered = question({ id: "q1", prompt: "A line.", askedIntent: open("q1") });
    const current = session([answered, question({ id: "q2", sequence: 2 })]);

    const write = resolveNextQuestionWrite(current, answered, {
      targetId: "q1",
      prompt: "Which trade-off did you choose?",
      nonAnswer: false,
    });

    expect(write.nextQuestionId).toBeNull();
    expect(write.followUp).toMatchObject({
      isFollowUp: true,
      prompt: "Which trade-off did you choose?",
      // Carried forward so the new row stays scoreable against the same target.
      objective: "Establish real ownership.",
      expectedSignals: ["ownership", "outcome"],
      rubricCriteria: ["Name a concrete example."],
    });
  });

  it("reads the continued target off the answered row's intent, not its own id", () => {
    // A follow-up row has its own fresh id while still belonging to its parent
    // target. Comparing ids missed every continuation past the first, wrote
    // neither a next question nor a follow-up, and left the candidate looking
    // at an empty interviewer bubble.
    const parent = question({ id: "q1", prompt: "A line.", answer: "an answer", askedIntent: open("q1") });
    const followUp = question({
      id: "follow-1",
      sequence: 2,
      isFollowUp: true,
      prompt: "A deeper line.",
      askedIntent: probe("q1"),
    });
    const current = session([parent, followUp, question({ id: "q2", sequence: 3 })]);

    const write = resolveNextQuestionWrite(current, followUp, {
      targetId: "q1",
      prompt: "One more on the same thread?",
      nonAnswer: false,
    });

    // The store cannot carry it -- the RPC refuses a follow-up whose parent is
    // itself a follow-up -- and `q1`'s own row is already answered, so this
    // resolves to nothing rather than to an unrelated row. `decideIntent` is
    // told the same thing up front (`canContinueCurrentTarget`) so it advances
    // instead of ever asking for this.
    expect(write).toEqual({ nextQuestionId: null, followUp: null });
  });

  it("updates the target's own row when the director advances", () => {
    const answered = question({ id: "q1", prompt: "A line.", askedIntent: open("q1") });
    const next = question({ id: "q2", sequence: 2 });
    const current = session([answered, next]);

    const write = resolveNextQuestionWrite(current, answered, {
      targetId: "q2",
      prompt: "A new subject?",
      nonAnswer: false,
    });

    expect(write).toEqual({ nextQuestionId: "q2", followUp: null });
  });

  it("re-asks the same still-unanswered row after a non-answer", () => {
    const answered = question({ id: "q1", prompt: "A line.", askedIntent: open("q1") });
    const current = session([answered, question({ id: "q2", sequence: 2 })]);

    const write = resolveNextQuestionWrite(current, answered, {
      targetId: "q1",
      prompt: "A smaller version?",
      nonAnswer: true,
    });

    expect(write).toEqual({ nextQuestionId: "q1", followUp: null });
  });
});
