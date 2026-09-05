import type { InterviewMode, IntentKind, ModePolicy, ProbeAspect, RoundId } from "@/lib/types";

/**
 * A round is one interview in a real loop, conducted by one person with one
 * agenda. The director may only issue intents whose kind appears in `moves`,
 * which is what gives a session a stable identity instead of drifting between
 * registers (spec §7).
 */
export type InterviewRound = {
  id: RoundId;
  label: string;
  agenda: string;
  register: string;
  /**
   * The interviewer's motivation, not a costume. No invented name, biography,
   * or anecdotes -- a consistent motivation produces a consistent voice, and
   * fabricated personality reads worse, not better (spec §7.2).
   */
  personaStake: string;
  moves: IntentKind[];
  probeAspects: ProbeAspect[];
  outOfScope: string[];
  opening: IntentKind;
  closing: IntentKind;
};

/** Rounds with a built repertoire. Others in `RoundId` are specified but deferred (spec §15). */
export const IMPLEMENTED_ROUNDS: RoundId[] = ["tech-lead"];

const TECH_LEAD: InterviewRound = {
  id: "tech-lead",
  label: "Tech lead evaluation",
  agenda: "Can this person actually own what they claim to have owned?",
  register:
    "Direct, unhurried, specific. Follows one thread to its end before opening another. Sceptical of unsupported claims, not hostile.",
  personaStake:
    "You are the senior engineer this candidate would work alongside. You are deciding whether they can own frontend architecture without supervision. You have read their CV and you do not accept claims without specifics.",
  moves: ["open", "probe", "challenge", "rescue", "advance", "hypothetical", "candidate-questions", "close"],
  probeAspects: ["specifics", "ownership", "tradeoff", "outcome", "collaboration", "hindsight"],
  outOfScope: ["salary", "notice period", "visa status", "company values", "why us", "live coding", "take-home logistics"],
  opening: "open",
  closing: "candidate-questions",
};

const ROUNDS: Partial<Record<RoundId, InterviewRound>> = {
  "tech-lead": TECH_LEAD,
};

/** Throws for a deferred round rather than silently degrading to a wrong repertoire. */
export function roundFor(id: RoundId): InterviewRound {
  const round = ROUNDS[id];
  if (!round) throw new Error(`Interview round "${id}" is specified but not implemented yet.`);
  return round;
}

const COACH: ModePolicy = {
  rescuesPerQuestion: 2,
  rescuesPerSession: 5,
  rescueStyles: ["narrow", "hook", "reframe", "park"],
  pushback: "light",
  parkAndReturn: true,
  acknowledgeStruggle: true,
};

// Real mode is accurate rather than punitive: a real interviewer rephrases once
// out of politeness and then moves on, and a blank costs the candidate.
const REAL: ModePolicy = {
  rescuesPerQuestion: 1,
  rescuesPerSession: 2,
  rescueStyles: ["narrow"],
  pushback: "firm",
  parkAndReturn: false,
  acknowledgeStruggle: false,
};

export function modePolicyFor(mode: InterviewMode): ModePolicy {
  return mode === "coach" ? COACH : REAL;
}
