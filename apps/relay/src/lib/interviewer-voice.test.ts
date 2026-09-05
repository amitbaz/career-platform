import { describe, expect, it } from "vitest";
import { deterministicLine, validateInterviewerLine } from "@/lib/interviewer-voice";
import { modePolicyFor } from "@/lib/interview-rounds";
import type { Intent } from "@/lib/types";

const context = {
  forbiddenRubricText: ["Probe Frontend Architecture with concrete evidence.", "ownership"],
  askedPrompts: ["Tell me about the design system migration."],
  policy: modePolicyFor("real"),
};

describe("validateInterviewerLine", () => {
  it("accepts a short, single, final question", () => {
    expect(validateInterviewerLine("What did you change first?", context)).toBeNull();
  });

  it("rejects rubric text", () => {
    const line = "Probe Frontend Architecture with concrete evidence. What did you own?";
    expect(validateInterviewerLine(line, context)).toBe("rubric-leak");
  });

  it("rejects contact details and URLs", () => {
    expect(validateInterviewerLine("Your CV lists amitbaz2@gmail.com. What did you own?", context)).toBe("contact-details");
    expect(validateInterviewerLine("You link linkedin.com/in/amit-baz. What did you own?", context)).toBe("contact-details");
    expect(validateInterviewerLine("Your number is +49 177 2276319. What did you own?", context)).toBe("contact-details");
  });

  it("rejects more than two sentences", () => {
    const line = "You mentioned the migration. It sounds involved. It ran for months. What did you own?";
    expect(validateInterviewerLine(line, context)).toBe("too-long");
  });

  it("rejects a line whose question is not last", () => {
    const line = "What did you own? Take your time with that.";
    expect(validateInterviewerLine(line, context)).toBe("question-not-last");
  });

  it("rejects a line with no question", () => {
    expect(validateInterviewerLine("That sounds like a big migration.", context)).toBe("no-question");
  });

  it("rejects a line with more than one question, even with the last sentence a question", () => {
    const line = "What did you mean? What did you own?";
    expect(validateInterviewerLine(line, context)).toBe("no-question");
  });

  it("accepts a single question containing a question mark inside a quoted clause", () => {
    const line = 'When you asked yourself "why not?" what did you decide to do?';
    expect(validateInterviewerLine(line, context)).toBeNull();
  });

  it("rejects a paraphrase of a question already asked", () => {
    const line = "Tell me about the design system migration.";
    expect(validateInterviewerLine(line, { ...context, askedPrompts: [line] })).toBe("repeats-asked");
  });

  it("rejects praise in real mode but allows a brief acknowledgement in coach mode", () => {
    const line = "Great answer. What did you own?";
    expect(validateInterviewerLine(line, context)).toBe("coaching");
    expect(validateInterviewerLine(line, { ...context, policy: modePolicyFor("coach") })).toBeNull();
  });
});

// One entry per IntentKind, paired with the competencyName deterministicLine
// would realistically be called with. The `challenge` claim is deliberately
// adversarial -- multi-sentence, with an embedded phone number and a "?" --
// because `claim` carries the candidate's own words verbatim (see its type
// doc) with no length or content bound. It stands in for the worst the
// interviewer call's answer text could produce, to prove deterministicLine
// never echoes it into the fallback line.
const ALL_INTENTS: [Intent, string | null][] = [
  [{ kind: "open", targetId: "a" }, "Frontend Architecture"],
  [{ kind: "probe", targetId: "a", aspect: "ownership", basis: "x" }, "Frontend Architecture"],
  [{ kind: "challenge", targetId: "a", claim: "80% faster. Call me at +49 177 2276319, right?" }, "Frontend Architecture"],
  [{ kind: "rescue", targetId: "a", style: "narrow", hook: null }, "Frontend Architecture"],
  [{ kind: "advance", targetId: "b", reason: "satisfied" }, "System Design"],
  [{ kind: "hypothetical", targetId: "a", basis: "x" }, "Frontend Architecture"],
  [{ kind: "candidate-questions" }, null],
  [{ kind: "close" }, null],
];

describe("deterministicLine", () => {
  it("returns a distinct line for every intent kind", () => {
    const lines = new Set(ALL_INTENTS.map(([intent, name]) => deterministicLine(intent, name)));
    expect(lines.size).toBe(ALL_INTENTS.length);
  });

  it("passes its own validation for every intent kind, even with an adversarial claim", () => {
    const emptyContext = { forbiddenRubricText: [], askedPrompts: [], policy: modePolicyFor("real") };
    for (const [intent, name] of ALL_INTENTS) {
      expect(validateInterviewerLine(deterministicLine(intent, name), emptyContext)).toBeNull();
    }
  });
});
