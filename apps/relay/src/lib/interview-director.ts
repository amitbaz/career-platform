import type { InterviewRound } from "@/lib/interview-rounds";
import type {
  AdvanceReason,
  AssessmentRead,
  AssistanceRecord,
  Intent,
  ModePolicy,
  ProbeAspect,
  RescueStyle,
  SetAsideReason,
  TargetState,
} from "@/lib/types";

export type DirectorInput = {
  round: InterviewRound;
  policy: ModePolicy;
  states: TargetState[];
  currentTargetId: string | null;
  read: AssessmentRead;
  unsupportedClaims: string[];
  answer: string;
  turnsUsed: number;
  turnBudget: number;
  sessionRescues: number;
  /**
   * Whether the store can still carry another turn on `currentTargetId` after
   * a real answer (see `canContinueOnAnsweredRow`). False means a probe or
   * challenge here would have nowhere to be written, so the round moves on
   * instead of asking a question the candidate would never see.
   */
  canContinueCurrentTarget: boolean;
  /** ISO-8601 timestamp supplied by the caller, stamped onto any `AssistanceRecord` this turn produces. Keeps `decideIntent` pure -- it never reads the clock itself. */
  now: string;
};

export type DirectorDecision = {
  intent: Intent;
  /** Non-null exactly when the intent spends rescue budget. */
  assistance: AssistanceRecord | null;
  /**
   * How this decision finishes the row the candidate just failed to answer, or
   * null when it finishes nothing. Persisted as the row's set-aside reason,
   * which is what stops it from being served again (issue #10).
   */
  setAside: SetAsideReason | null;
};

/**
 * The order probe aspects are tried. Specifics first because an unsupported
 * claim is usually a missing detail rather than a missing idea; hindsight last
 * because it only makes sense once the story is on the table.
 */
const ASPECT_ORDER: ProbeAspect[] = ["specifics", "ownership", "tradeoff", "outcome", "collaboration", "hindsight"];

function currentState(input: DirectorInput): TargetState | null {
  return input.states.find((state) => state.target.id === input.currentTargetId) ?? null;
}

function askedAspects(state: TargetState): Set<ProbeAspect> {
  return new Set(
    state.askedIntents.flatMap((intent) => (intent.kind === "probe" ? [intent.aspect] : [])),
  );
}

/**
 * Rescue styles already spent ON this target. A `park` intent is stored against
 * the target it moved TO, not the one it set aside, so it is excluded here --
 * it is not a rescue of the target whose row happens to carry it. Parking the
 * same target twice is prevented by its per-question rescue budget instead.
 */
function usedRescueStyles(state: TargetState): Set<RescueStyle> {
  return new Set(
    state.askedIntents.flatMap((intent) =>
      intent.kind === "rescue" && !intent.parkedTargetId ? [intent.style] : [],
    ),
  );
}

function turnsRemaining(input: DirectorInput): number {
  return Math.max(0, input.turnBudget - input.turnsUsed);
}

/** Targets that still need a first question, required ones first. */
function unaskedTargets(input: DirectorInput): TargetState[] {
  return input.states
    .filter((state) => state.status === "unasked")
    .sort((left, right) => Number(right.target.required) - Number(left.target.required));
}

function parkedTargets(input: DirectorInput): TargetState[] {
  return input.states.filter((state) => state.status === "parked");
}

/**
 * The next target to work on, never the one being left. Excluding it matters
 * because a just-parked target is itself `parked`, so the parked fallback below
 * would otherwise hand back the very target the interview is trying to escape.
 */
function nextTarget(input: DirectorInput, leaving: string | null): TargetState | null {
  const elsewhere = (state: TargetState) => state.target.id !== leaving;
  return unaskedTargets(input).find(elsewhere) ?? parkedTargets(input).find(elsewhere) ?? null;
}

function advance(input: DirectorInput, reason: AdvanceReason, setAside: SetAsideReason | null = null): DirectorDecision | null {
  const next = nextTarget(input, input.currentTargetId);
  if (!next) return null;
  return { intent: { kind: "advance", targetId: next.target.id, reason }, assistance: null, setAside };
}

function closing(input: DirectorInput, setAside: SetAsideReason | null = null): DirectorDecision {
  return { intent: { kind: input.round.closing } as Intent, assistance: null, setAside };
}

/**
 * Chooses the next rescue style: an unused style from the mode's escalation
 * order, with `park` reserved for the question's last permitted rescue
 * attempt (or as the fallback once every other style is used). Park is the
 * strongest move -- setting the question aside -- and per spec §8.2 it is
 * what lets a stuck candidate be set down gently instead of dropped once the
 * budget runs out; a positional walk through the full style list would leave
 * it unreachable under coach's two-rescue budget.
 */
function nextRescueStyle(state: TargetState, policy: ModePolicy): RescueStyle | null {
  const used = usedRescueStyles(state);
  const canPark = policy.parkAndReturn && policy.rescueStyles.includes("park") && !used.has("park");
  const escalation = policy.rescueStyles.filter((style) => style !== "park" && !used.has(style));
  const isLastAttempt = state.rescuesSpent + 1 >= policy.rescuesPerQuestion;
  if (canPark && (isLastAttempt || escalation.length === 0)) return "park";
  return escalation[0] ?? null;
}

function hookFor(state: TargetState): string | null {
  return state.target.competencyName;
}

/**
 * Computes the single move the interviewer makes next.
 *
 * Ordering matters and encodes spec §9.3: a stuck candidate is never probed;
 * running out of turns outranks deepening an open thread; a target is finished
 * when its signals are present, not after a fixed number of questions.
 */
export function decideIntent(input: DirectorInput): DirectorDecision {
  const state = currentState(input);
  const remaining = turnsRemaining(input);
  const unasked = unaskedTargets(input);
  const requiredUnasked = unasked.filter((item) => item.target.required);

  // Rule 6: a non-answer never earns a harder question.
  if (input.read === "stuck" && state) {
    const questionBudget = state.rescuesSpent < input.policy.rescuesPerQuestion;
    const sessionBudget = input.sessionRescues < input.policy.rescuesPerSession;
    const style = nextRescueStyle(state, input.policy);

    if (questionBudget && sessionBudget && style) {
      if (style !== "park") {
        return {
          intent: { kind: "rescue", targetId: state.target.id, style, hook: style === "hook" ? hookFor(state) : null },
          assistance: { style, at: input.now },
          setAside: null,
        };
      }
      // Park is "acknowledge, move to another target, come back later if turns
      // remain" (spec §8.2), so it only exists when there is another target.
      // With nowhere to go, "I'll come back to it" would re-ask the same
      // question -- the exact blackout this move exists to prevent.
      const destination = nextTarget(input, state.target.id);
      if (destination) {
        return {
          intent: { kind: "rescue", targetId: destination.target.id, style: "park", hook: null, parkedTargetId: state.target.id },
          assistance: { style: "park", at: input.now },
          setAside: "parked",
        };
      }
    }
    return advance(input, "rescue-budget-spent", "rescue-budget-spent")
      ?? closing(input, "rescue-budget-spent");
  }

  // Rule 4: unasked required coverage outranks deepening when turns run short.
  if (requiredUnasked.length > 0 && remaining <= requiredUnasked.length) {
    return advance(input, "turn-budget") ?? closing(input);
  }

  if (remaining <= 0) return closing(input);

  // Rule 2: a satisfied target is finished, whatever its question count.
  if (!state || state.status === "satisfied") {
    return advance(input, "satisfied") ?? closing(input);
  }

  if (state.status === "unasked") {
    return { intent: { kind: "open", targetId: state.target.id }, assistance: null, setAside: null };
  }

  // Every intent below deepens the current target, and after a real answer
  // that costs a new row. "line-exhausted" rather than a reason of its own:
  // from the round's point of view this line of questioning is over, and the
  // reason is only ever read as an advance's justification.
  if (!input.canContinueCurrentTarget) return advance(input, "line-exhausted") ?? closing(input);

  const unsupported = input.unsupportedClaims[0];
  const alreadyChallenged = state.askedIntents.some((intent) => intent.kind === "challenge" && intent.claim === unsupported);
  if (unsupported && !alreadyChallenged && input.round.moves.includes("challenge")) {
    return { intent: { kind: "challenge", targetId: state.target.id, claim: unsupported }, assistance: null, setAside: null };
  }

  // Rule 1: never repeat an intent already issued for this target.
  const asked = askedAspects(state);
  const aspect = ASPECT_ORDER.find((item) => input.round.probeAspects.includes(item) && !asked.has(item));
  if (aspect) {
    return {
      intent: { kind: "probe", targetId: state.target.id, aspect, basis: input.answer },
      assistance: null,
      setAside: null,
    };
  }

  return advance(input, "line-exhausted") ?? closing(input);
}
