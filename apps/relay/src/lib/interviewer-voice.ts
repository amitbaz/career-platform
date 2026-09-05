import type { Intent, ModePolicy } from "@/lib/types";

export type LineViolation =
  | "rubric-leak"
  | "contact-details"
  | "too-long"
  | "question-not-last"
  | "no-question"
  | "repeats-asked"
  | "coaching";

export type LineContext = {
  /** Objective, expected signal, and rubric strings that must never be echoed. */
  forbiddenRubricText: string[];
  askedPrompts: string[];
  policy: ModePolicy;
};

const EMAIL = /[\w.+-]+@[\w-]+\.[\w.]+/;
const URL = /\b(?:https?:\/\/|www\.)\S+|\b[\w-]+\.(?:com|io|dev|de|org|net|co)\/\S*/i;
const PHONE = /(?:\+\d[\d\s().-]{7,})|(?:\b\d{3,}[\s.-]\d{3,}[\s.-]\d{3,}\b)/;
const PRAISE = /\b(great|excellent|perfect|well done|nice|good answer|brilliant|impressive)\b/i;

/**
 * Jaccard-overlap floor above which a candidate line counts as a repeat of an
 * asked prompt. Untuned beyond the brief's one example; a named constant so
 * later tuning against real model output (Task 6) is a one-line change.
 */
const REPEAT_SIMILARITY_THRESHOLD = 0.6;

function sentences(line: string): string[] {
  return line.split(/(?<=[.!?])\s+/).map((part) => part.trim()).filter(Boolean);
}

function normalize(text: string): string {
  return text.toLowerCase().replace(/[^a-z0-9 ]+/g, " ").replace(/\s+/g, " ").trim();
}

/** Jaccard overlap on word sets: cheap, and enough to catch a reworded repeat. */
function similarity(left: string, right: string): number {
  const a = new Set(normalize(left).split(" ").filter((word) => word.length > 3));
  const b = new Set(normalize(right).split(" ").filter((word) => word.length > 3));
  if (a.size === 0 || b.size === 0) return 0;
  const shared = [...a].filter((word) => b.has(word)).length;
  return shared / new Set([...a, ...b]).size;
}

/**
 * Enforces spec §11 on interviewer output. Returns the violated rule, or null
 * when the line is acceptable.
 *
 * This is a net, not the primary defence: the interviewer call never receives
 * rubric text in the first place (spec §11.1). Catching a leak here means the
 * prompt assembly is wrong, not merely that the model misbehaved.
 *
 * Deliberately does not log `line` or `context` on rejection — both may carry
 * candidate answer text, CV text, or job-description text. Callers must report
 * only the returned violation kind.
 */
export function validateInterviewerLine(line: string, context: LineContext): LineViolation | null {
  const trimmed = line.trim();
  const lower = trimmed.toLowerCase();

  for (const forbidden of context.forbiddenRubricText) {
    const needle = forbidden.trim().toLowerCase();
    if (needle.length > 12 && lower.includes(needle)) return "rubric-leak";
  }

  if (EMAIL.test(trimmed) || URL.test(trimmed) || PHONE.test(trimmed)) return "contact-details";

  const parts = sentences(trimmed);
  if (parts.length > 2) return "too-long";

  // Checked before the question-shape rules: a repeated prompt can be an
  // imperative ("Tell me about...") with no "?", and that repetition is the
  // more important violation to report.
  if (context.askedPrompts.some((asked) => similarity(asked, trimmed) > REPEAT_SIMILARITY_THRESHOLD)) {
    return "repeats-asked";
  }

  // Spec §11.4: exactly one question, and it comes last. Counting by sentence
  // (not by raw "?" count) lets a question mark inside a quotation or a
  // parenthetical mid-sentence clause stand without tripping this — that
  // punctuation belongs to the one sentence that contains it, not a second
  // question. Zero and "more than one" both land on the same violation kind:
  // the caller only needs to know the line failed the question-shape rule.
  const questionSentences = parts.filter((part) => part.includes("?"));
  if (questionSentences.length !== 1) return "no-question";
  if (!parts[parts.length - 1].includes("?")) return "question-not-last";

  if (!context.policy.acknowledgeStruggle && PRAISE.test(trimmed)) return "coaching";

  return null;
}

/**
 * The degraded path (spec §13.2). One short line per intent kind, used only
 * when the interviewer call fails or fails validation twice. This is the sole
 * surviving use of templates in the system.
 */
export function deterministicLine(intent: Intent, competencyName: string | null): string {
  const subject = competencyName ?? "that work";
  switch (intent.kind) {
    case "open":
      return `Tell me about your work on ${subject}?`;
    case "probe":
      return `What part of that was yours specifically?`;
    case "challenge":
      // Deliberately does not interpolate `intent.claim`: that field carries
      // the candidate's own words verbatim (see its type doc), unbounded in
      // length or content. This is the unconditional fallback after the
      // model call has already failed, so it must not itself be able to trip
      // `too-long`, the one-question rule, or `contact-details` by echoing
      // whatever the candidate said. A generic challenge is an acceptable
      // trade for a fallback that cannot fail its own guardrails.
      return `How do you know that was the result?`;
    case "rescue":
      return `Let's make it smaller — what is one thing you changed?`;
    case "advance":
      return `Let's move on — what can you tell me about ${subject}?`;
    case "hypothetical":
      return `If that constraint doubled, what would you change first?`;
    case "candidate-questions":
      return `That is what I wanted to cover — what would you like to ask me?`;
    case "close":
      return `Thanks, that is everything from my side — anything you want to add?`;
  }
}
