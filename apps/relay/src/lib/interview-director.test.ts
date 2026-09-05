import { describe, expect, it } from "vitest";
import { decideIntent, type DirectorInput } from "@/lib/interview-director";
import { modePolicyFor, roundFor } from "@/lib/interview-rounds";
import type { CoverageTarget, Intent, InterviewMode, ModePolicy, TargetState, TargetStatus } from "@/lib/types";

function target(id: string, required = true): CoverageTarget {
  return {
    id,
    competencyId: `comp-${id}`,
    competencyName: `Competency ${id}`,
    category: "experience",
    evidenceIds: [],
    difficulty: "senior",
    objective: `Probe ${id}.`,
    expectedSignals: ["ownership"],
    rubricCriteria: ["Name a concrete example."],
    required,
  };
}

function state(id: string, overrides: Partial<TargetState> = {}): TargetState {
  return {
    target: target(id),
    status: "unasked",
    turnsSpent: 0,
    rescuesSpent: 0,
    askedIntents: [],
    ...overrides,
  };
}

function input(overrides: Partial<DirectorInput> = {}): DirectorInput {
  return {
    round: roundFor("tech-lead"),
    policy: modePolicyFor("coach"),
    states: [state("a")],
    currentTargetId: "a",
    read: "answered",
    unsupportedClaims: [],
    answer: "I owned the design system migration.",
    turnsUsed: 1,
    turnBudget: 8,
    sessionRescues: 0,
    canContinueCurrentTarget: true,
    now: "2026-01-01T00:00:00.000Z",
    ...overrides,
  };
}

/**
 * Shorthand builder for the "stuck" branch's own tests. Fixes every field the
 * park-destination tests don't vary, so each test only states the target
 * shape and mode that its assertion actually depends on.
 */
function stuckInput(options: {
  mode: InterviewMode;
  states: Array<{ id: string; status: TargetStatus; rescuesSpent: number; askedIntents: Intent[] }>;
  currentTargetId: string | null;
}): DirectorInput {
  return {
    round: roundFor("tech-lead"),
    policy: modePolicyFor(options.mode),
    states: options.states.map((state) => ({
      target: {
        id: state.id,
        competencyId: null,
        competencyName: null,
        category: "communication",
        evidenceIds: [],
        difficulty: "foundational",
        objective: "",
        expectedSignals: ["signal"],
        rubricCriteria: [],
        required: true,
      },
      status: state.status,
      turnsSpent: 1,
      rescuesSpent: state.rescuesSpent,
      askedIntents: state.askedIntents,
    })),
    currentTargetId: options.currentTargetId,
    read: "stuck",
    unsupportedClaims: [],
    answer: "i don't know",
    turnsUsed: 1,
    turnBudget: 8,
    sessionRescues: 1,
    canContinueCurrentTarget: true,
    now: "2026-09-04T09:00:00.000Z",
  };
}

describe("decideIntent — park destination", () => {
  it("parks by moving to a different target and naming the one it leaves", () => {
    const decision = decideIntent(stuckInput({
      mode: "coach",
      states: [
        { id: "a", status: "open", rescuesSpent: 1, askedIntents: [{ kind: "rescue", targetId: "a", style: "narrow", hook: null }] },
        { id: "b", status: "unasked", rescuesSpent: 0, askedIntents: [] },
      ],
      currentTargetId: "a",
    }));
    expect(decision.intent).toMatchObject({ kind: "rescue", style: "park", targetId: "b", parkedTargetId: "a" });
    expect(decision.setAside).toBe("parked");
  });

  it("sets the stuck target aside when the rescue budget is spent", () => {
    const decision = decideIntent(stuckInput({
      mode: "real",
      states: [
        { id: "a", status: "open", rescuesSpent: 1, askedIntents: [{ kind: "rescue", targetId: "a", style: "narrow", hook: null }] },
        { id: "b", status: "unasked", rescuesSpent: 0, askedIntents: [] },
      ],
      currentTargetId: "a",
    }));
    expect(decision.intent).toMatchObject({ kind: "advance", targetId: "b", reason: "rescue-budget-spent" });
    expect(decision.setAside).toBe("rescue-budget-spent");
  });

  it("never advances back onto the target it is leaving", () => {
    const decision = decideIntent(stuckInput({
      mode: "real",
      states: [
        { id: "a", status: "parked", rescuesSpent: 1, askedIntents: [{ kind: "rescue", targetId: "a", style: "narrow", hook: null }] },
      ],
      currentTargetId: "a",
    }));
    expect(decision.intent.kind).toBe("candidate-questions");
  });

  it("does not park when there is nowhere to move to", () => {
    const decision = decideIntent(stuckInput({
      mode: "coach",
      states: [
        { id: "a", status: "open", rescuesSpent: 1, askedIntents: [{ kind: "rescue", targetId: "a", style: "narrow", hook: null }] },
      ],
      currentTargetId: "a",
    }));
    expect(decision.intent.kind).toBe("candidate-questions");
    expect(decision.setAside).toBe("rescue-budget-spent");
  });
});

describe("decideIntent — stuck candidates", () => {
  it("rescues rather than probes when the candidate is stuck", () => {
    const decision = decideIntent(input({
      read: "stuck",
      answer: "i am having a blackout",
      now: "2026-03-14T09:30:00.000Z",
    }));
    expect(decision.intent.kind).toBe("rescue");
    expect(decision.assistance).not.toBeNull();
    // The director must not read the clock itself -- the timestamp is exactly
    // the caller-supplied `now`, not a value it manufactured, which is what
    // makes this function assertable without freezing time.
    expect(decision.assistance?.at).toBe("2026-03-14T09:30:00.000Z");
  });

  it("never probes or challenges a stuck candidate", () => {
    for (const policy of [modePolicyFor("coach"), modePolicyFor("real")]) {
      const decision = decideIntent(input({ read: "stuck", policy }));
      expect(["probe", "challenge"]).not.toContain(decision.intent.kind);
    }
  });

  it("advances once the per-question rescue budget is spent", () => {
    const spent = state("a", {
      status: "open",
      turnsSpent: 2,
      rescuesSpent: 2,
      askedIntents: [{ kind: "open", targetId: "a" }],
    });
    const decision = decideIntent(input({
      read: "stuck",
      states: [spent, state("b")],
      policy: modePolicyFor("coach"),
    }));
    expect(decision.intent.kind).toBe("advance");
    expect(decision.assistance).toBeNull();
  });

  it("parks in coach mode but never in real mode", () => {
    const stuckTwice = state("a", {
      status: "open",
      turnsSpent: 1,
      rescuesSpent: 1,
      askedIntents: [{ kind: "rescue", targetId: "a", style: "narrow", hook: null }],
    });
    const coach = decideIntent(input({ read: "stuck", states: [stuckTwice, state("b")], policy: modePolicyFor("coach") }));
    // Park moves to "b" and names "a" as the target it leaves -- it must never
    // hand back the stuck target itself (that was the bug, issue #10).
    expect(coach.intent).toMatchObject({ kind: "rescue", style: "park", targetId: "b", parkedTargetId: "a" });

    const real = decideIntent(input({ read: "stuck", states: [stuckTwice, state("b")], policy: modePolicyFor("real") }));
    expect(real.intent.kind).toBe("advance");
  });

  it("stops rescuing once the session budget is spent", () => {
    // A second target is required here: `advance` never returns to the target
    // being left (see the park-destination tests below), so with only "a" in
    // play there would be nowhere to advance to.
    const decision = decideIntent(input({
      read: "stuck",
      states: [state("a"), state("b")],
      sessionRescues: 5,
      policy: modePolicyFor("coach"),
    }));
    expect(decision.intent.kind).toBe("advance");
  });

  it("escalates rescue styles in the policy's own order, not a hardcoded one", () => {
    // A policy whose style order deliberately differs from the ["narrow", "hook",
    // "reframe", "park"] constant a hardcoded implementation would fall back to.
    // With no styles used yet, a hardcoded-order implementation would pick
    // "narrow" (first in its constant); the policy lists "reframe" first.
    const reorderedPolicy: ModePolicy = {
      rescuesPerQuestion: 3,
      rescuesPerSession: 5,
      rescueStyles: ["reframe", "narrow", "park"],
      pushback: "light",
      parkAndReturn: true,
      acknowledgeStruggle: true,
    };
    const decision = decideIntent(input({ read: "stuck", policy: reorderedPolicy }));
    expect(decision.intent).toMatchObject({ kind: "rescue", style: "reframe" });
  });
});

describe("decideIntent — continuation budget", () => {
  it("moves on rather than deepening a target the store cannot carry another turn on", () => {
    // The persisted model gives an answered row at most one continuation
    // (a follow-up row), and refuses one whose parent is itself a follow-up.
    // Probing anyway produced a question with nowhere to be written, which
    // reached the candidate as an empty interviewer bubble.
    const decision = decideIntent(input({
      states: [state("a", { status: "open", turnsSpent: 2, askedIntents: [{ kind: "open", targetId: "a" }] }), state("b")],
      canContinueCurrentTarget: false,
    }));
    expect(decision.intent).toMatchObject({ kind: "advance", targetId: "b" });
  });

  it("still deepens the current target while the store can carry it", () => {
    const decision = decideIntent(input({
      states: [state("a", { status: "open", turnsSpent: 2, askedIntents: [{ kind: "open", targetId: "a" }] }), state("b")],
      canContinueCurrentTarget: true,
    }));
    expect(decision.intent).toMatchObject({ kind: "probe", targetId: "a" });
  });

  it("keeps rescuing a stuck candidate, which re-asks the same unanswered row", () => {
    // A non-answer never sets the row's `answer`, so a rescue needs no new row
    // and the continuation budget does not apply to it.
    const decision = decideIntent(input({
      read: "stuck",
      states: [state("a", { status: "open", turnsSpent: 1, askedIntents: [{ kind: "open", targetId: "a" }] }), state("b")],
      canContinueCurrentTarget: false,
      policy: modePolicyFor("coach"),
    }));
    expect(decision.intent.kind).toBe("rescue");
  });
});

describe("decideIntent — repetition", () => {
  it("never issues an intent already asked for the target", () => {
    const asked: Intent[] = [
      { kind: "open", targetId: "a" },
      { kind: "probe", targetId: "a", aspect: "specifics", basis: "x" },
    ];
    const decision = decideIntent(input({
      read: "partial",
      states: [state("a", { status: "open", turnsSpent: 2, askedIntents: asked })],
    }));
    if (decision.intent.kind === "probe") {
      expect(decision.intent.aspect).not.toBe("specifics");
    }
  });

  it("advances when every available probe aspect is exhausted", () => {
    const asked: Intent[] = roundFor("tech-lead").probeAspects.map((aspect) => ({
      kind: "probe" as const,
      targetId: "a",
      aspect,
      basis: "x",
    }));
    const decision = decideIntent(input({
      read: "partial",
      states: [state("a", { status: "open", turnsSpent: 6, askedIntents: asked }), state("b")],
    }));
    expect(decision.intent).toMatchObject({ kind: "advance", reason: "line-exhausted" });
  });
});

describe("decideIntent — coverage", () => {
  it("advances to the next target when the current one is satisfied", () => {
    const decision = decideIntent(input({
      states: [state("a", { status: "satisfied", turnsSpent: 2 }), state("b")],
    }));
    expect(decision.intent).toMatchObject({ kind: "advance", targetId: "b", reason: "satisfied" });
  });

  it("prefers an unasked required target over deepening when turns run short", () => {
    const decision = decideIntent(input({
      states: [state("a", { status: "open", turnsSpent: 1 }), state("b"), state("c")],
      turnsUsed: 6,
      turnBudget: 8,
    }));
    expect(decision.intent).toMatchObject({ kind: "advance", reason: "turn-budget" });
  });

  it("returns to a parked target when turns remain and nothing required is unasked", () => {
    const decision = decideIntent(input({
      states: [
        state("a", { status: "satisfied", turnsSpent: 2 }),
        state("b", {
          status: "parked",
          turnsSpent: 1,
          rescuesSpent: 1,
          askedIntents: [{ kind: "rescue", targetId: "b", style: "park", hook: null }],
        }),
      ],
      currentTargetId: "a",
    }));
    expect(decision.intent).toMatchObject({ kind: "advance", targetId: "b" });
  });

  it("closes with the round's closing move when the turn budget is exhausted", () => {
    const decision = decideIntent(input({
      states: [state("a", { status: "satisfied", turnsSpent: 3 })],
      turnsUsed: 8,
      turnBudget: 8,
    }));
    expect(decision.intent.kind).toBe("candidate-questions");
  });

  it("closes at the turn-budget hard stop even though the current target is still open", () => {
    // Without the unconditional `remaining <= 0` check, this would fall through
    // to the probe branch: target "a" is open, not satisfied, with no asked
    // intents yet, so there is an aspect available to probe. Neither "b" (unasked
    // but not required, so it can't trip the turn-budget rule) nor "c" (parked)
    // should let the director advance or deepen instead of closing.
    const decision = decideIntent(input({
      states: [
        state("a", { status: "open", turnsSpent: 3 }),
        state("b", { target: target("b", false) }),
        state("c", {
          status: "parked",
          turnsSpent: 1,
          rescuesSpent: 1,
          askedIntents: [{ kind: "rescue", targetId: "c", style: "park", hook: null }],
        }),
      ],
      currentTargetId: "a",
      turnsUsed: 8,
      turnBudget: 8,
    }));
    expect(decision.intent.kind).toBe("candidate-questions");
  });
});

describe("decideIntent — pressure", () => {
  it("challenges an unsupported claim in real mode", () => {
    const decision = decideIntent(input({
      policy: modePolicyFor("real"),
      read: "partial",
      unsupportedClaims: ["cut load time by 80%"],
      states: [state("a", { status: "open", turnsSpent: 1, askedIntents: [{ kind: "open", targetId: "a" }] })],
    }));
    expect(decision.intent).toMatchObject({ kind: "challenge", claim: "cut load time by 80%" });
  });

  it("never re-issues a challenge for a claim already challenged on this target", () => {
    const decision = decideIntent(input({
      policy: modePolicyFor("real"),
      read: "partial",
      unsupportedClaims: ["cut load time by 80%"],
      states: [state("a", {
        status: "open",
        turnsSpent: 2,
        askedIntents: [
          { kind: "open", targetId: "a" },
          { kind: "challenge", targetId: "a", claim: "cut load time by 80%" },
        ],
      })],
    }));
    expect(decision.intent.kind).not.toBe("challenge");
  });

  it("only issues intents in the round's repertoire", () => {
    const round = roundFor("tech-lead");
    const reads = ["answered", "partial", "evasive", "stuck"] as const;
    for (const read of reads) {
      const decision = decideIntent(input({ read }));
      expect(round.moves).toContain(decision.intent.kind);
    }
  });
});
