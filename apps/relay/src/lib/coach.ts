import "server-only";

import { z } from "zod";
import { applyEvaluation } from "@/lib/competencies";
import { geminiFailureState, geminiModel, geminiRequestError } from "@/lib/gemini";
import { canContinueOnAnsweredRow, deriveCoverageState, rescuesSpentInSession, targetIdOf } from "@/lib/interview-coverage";
import { decideIntent } from "@/lib/interview-director";
import { buildCoverageTargets, buildExperienceDiscoveryBlueprint, buildFallbackInterviewBlueprint, defaultMaxQuestions, validateInterviewBlueprint } from "@/lib/interview-planner";
import { modePolicyFor, roundFor } from "@/lib/interview-rounds";
import type { InterviewRound } from "@/lib/interview-rounds";
import { deterministicLine, validateInterviewerLine } from "@/lib/interviewer-voice";
import { MAX_CV_PDF_BYTES } from "@/lib/upload-limits";
import type {
  AnswerTier,
  AssessmentRead,
  AssistanceRecord,
  BlueprintQuestion,
  CoachObservation,
  Competency,
  CoverageTarget,
  Difficulty,
  EvidenceItem,
  Evaluation,
  HandsOnExercise,
  Intent,
  InterviewBlueprint,
  InterviewSession,
  ModePolicy,
  Opportunity,
  PlannedQuestion,
  PracticeBlueprintContext,
  PracticeFormat,
  PracticePlan,
  Profile,
  ProfileDraft,
  ProfileReadiness,
  QuestionCategory,
  RescueStyle,
  RoundId,
  SetAsideReason,
  GroundedEvaluation,
} from "@/lib/types";

const dimensions = ["correctness", "depth", "clarity", "structure", "practicalExperience", "tradeOffAwareness", "communication", "confidence", "relevance"] as const;
/** The fixed set of scoring dimensions, exported for callers (e.g. `route.ts`'s `emptyEvaluationFor`) that need to build a placeholder `Evaluation` without duplicating this list. */
export const EVALUATION_DIMENSIONS = dimensions;
const dimensionShape = Object.fromEntries(dimensions.map((dimension) => [dimension, z.number().min(0).max(10)])) as Record<(typeof dimensions)[number], z.ZodNumber>;
const dimensionReasonShape = Object.fromEntries(dimensions.map((dimension) => [dimension, z.string().min(1)])) as Record<(typeof dimensions)[number], z.ZodString>;
const profileSchema = z.object({
  role: z.string(), seniority: z.string(), summary: z.string(), narrative: z.string(),
  expertise: z.array(z.string()).min(1).max(8), characteristics: z.array(z.string()).min(1).max(6),
  competencies: z.array(z.object({ name: z.string().min(1), relevance: z.number().min(0).max(1) })).min(1),
});
const answerTierEnum = z.enum(["incorrect", "incomplete", "shallow", "strong"]);
const groundedEvaluationSchema = z.object({
  score: z.number().min(0).max(10), competency: z.string().optional(),
  relevance: z.number().min(0).max(10),
  dimensions: z.object(dimensionShape),
  strengths: z.array(z.string()), needsWork: z.array(z.string()),
  missingPoints: z.array(z.string()).min(1),
  betterStructure: z.array(z.string()).min(1),
  improvedAnswer: z.string().min(1),
  supportedClaims: z.array(z.string()).default([]),
  expectedSignalsPresent: z.array(z.string()).default([]),
  unsupportedClaims: z.array(z.string()).default([]),
  dimensionReasons: z.object(dimensionReasonShape),
  answerTier: answerTierEnum,
});
// The assessor scores privately and reports a coarse read. It no longer
// authors questions: speech is a separate call that never sees this rubric.
const assessorSchema = z.object({
  read: z.enum(["answered", "partial", "evasive", "stuck"]),
  evaluation: groundedEvaluationSchema,
});
const interviewerLineSchema = z.object({ line: z.string().min(1) });
const evidenceSchema = z.object({
  id: z.string().min(1).optional(),
  sourceKind: z.enum(["cv", "cover_letter", "summary"]).nullable().optional(),
  sourceExcerpt: z.string().min(1),
  projectOrEmployer: z.string().nullable().optional(),
  ownership: z.string().nullable().optional(),
  technologies: z.array(z.string().min(1)).default([]),
  decision: z.string().nullable().optional(),
  constraint: z.string().nullable().optional(),
  outcome: z.string().nullable().optional(),
  recency: z.string().nullable().optional(),
  confidence: z.number().min(0).max(1),
});
const evidenceListSchema = z.union([z.array(evidenceSchema), z.object({ evidence: z.array(evidenceSchema) })]);
const workExampleVerbPattern = /\b(led|built|shipped|migrated|designed|owned|improved|implemented|launched|reduced|scaled|developed|created|refactored|rewrote|reworked|optimized|fixed|deployed|maintained|delivered|introduced|coordinated|debugged|automated)\b/i;
const blueprintQuestionDraftSchema = z.object({
  sequence: z.number().int().min(1).max(5),
  category: z.enum(["introduction", "experience", "technical", "architecture", "behavioral"]),
  competencyId: z.string().nullable().optional(),
  competencyName: z.string().nullable().optional(),
  difficulty: z.enum(["foundational", "intermediate", "senior", "advanced"]),
  objective: z.string().min(1),
  evidenceIds: z.array(z.string().min(1)).default([]),
  expectedSignals: z.array(z.string().min(1)).min(1),
  missingSignalPrompts: z.array(z.string().min(1)).min(1),
  rubricCriteria: z.array(z.string().min(1)).default([]),
  followUpLimit: z.number().int().min(0).max(3),
  prompt: z.string().min(1),
  sourceConfidence: z.number().min(0).max(1).nullable().optional(),
});
const blueprintDraftSchema = z.object({
  status: z.enum(["grounded", "limited-grounding"]).optional(),
  fallbackReason: z.string().nullable().optional(),
  maxFollowUps: z.number().int().min(0).max(3).default(3),
  maxQuestions: z.number().int().min(5).max(8).default(8),
  questions: z.array(blueprintQuestionDraftSchema).length(5),
});
/**
 * The sibling draft schema to `blueprintDraftSchema` for plan-driven practice
 * blueprints. A SEPARATE schema, not a relaxation of the generic one:
 * `generateInterviewBlueprint`'s exact five-question backbone contract stays
 * untouched (its `questions.length(5)` and `maxQuestions.min(5)` are load
 * bearing for that generator and must not be loosened). Practice blueprints
 * carry 1-5 base questions instead, per `assertPracticeConversationBlueprint`
 * in `src/lib/repositories/interviews.ts`.
 */
const practiceBlueprintDraftSchema = z.object({
  status: z.enum(["grounded", "limited-grounding"]).optional(),
  fallbackReason: z.string().nullable().optional(),
  maxFollowUps: z.number().int().min(0).max(3).default(3),
  maxQuestions: z.number().int().min(1).max(8).default(8),
  questions: z.array(blueprintQuestionDraftSchema).min(1).max(5),
});

function roleDescriptor(role: string | null): string {
  const normalized = role?.trim().toLowerCase();
  if (!normalized) return "engineering";
  if (normalized.includes("software engineer")) return "engineering";
  return normalized.replace(/\b(engineer|developer)\b/g, "").replace(/\s+/g, " ").trim() || "engineering";
}

function detectSoftwareEngineeringRole(source: string): string {
  const hasFrontend = /\b(frontend|front-end|ui|react|next\.?js|typescript|accessibility)\b/i.test(source);
  const hasBackend = /\b(backend|api|server|service|node\.?js|postgres|sql|microservice|distributed)\b/i.test(source);
  const hasMobile = /\b(mobile|ios|android|swift|kotlin|react native|flutter)\b/i.test(source);
  const hasInfrastructure = /\b(infrastructure|platform|devops|kubernetes|terraform|aws|gcp|azure|ci\/cd|observability|sre)\b/i.test(source);
  const hasSecurity = /\b(security|auth|authentication|authorization|vulnerability|threat|encryption|iam|incident response)\b/i.test(source);
  const hasData = /\b(data|analytics|pipeline|warehouse|etl|dbt|spark|airflow|snowflake|bigquery)\b/i.test(source);
  const hasFullStack = /\bfull[- ]stack\b/i.test(source) || (hasFrontend && hasBackend);
  if (hasFullStack) return "Full-Stack Engineer";
  if (hasBackend) return "Backend Engineer";
  if (hasMobile) return "Mobile Engineer";
  if (hasInfrastructure) return "Infrastructure Engineer";
  if (hasSecurity) return "Security Engineer";
  if (hasData) return "Data Engineer";
  if (hasFrontend) return "Frontend Engineer";
  return "Software Engineer";
}

function fallbackExpertise(source: string, role: string): string[] {
  const text = source.toLowerCase();
  const expertise = new Set<string>();
  const techs = [
    "React",
    "TypeScript",
    "JavaScript",
    "Next.js",
    "Node.js",
    "Postgres",
    "GraphQL",
    "Redux",
    "Supabase",
    "Vercel",
    "AWS",
    "Swift",
    "Kotlin",
    "iOS",
    "Android",
    "Flutter",
    "Python",
    "Go",
    "Java",
    "Rust",
    "Docker",
    "Kubernetes",
    "Terraform",
    "Kafka",
    "Redis",
    "Spark",
    "Airflow",
    "Snowflake",
    "BigQuery",
    "Testing",
    "Accessibility",
  ];
  for (const technology of techs) {
    if (text.includes(technology.toLowerCase())) expertise.add(technology);
  }
  if (/\b(reliability|resilience|availability|incident|outage|latency|monitoring|observability)\b/i.test(source)) {
    expertise.add("Reliability");
  }
  if (/\bbackend\b/i.test(role) || /\bfull[- ]stack\b/i.test(role)) {
    expertise.add("Backend systems");
    expertise.add("APIs");
  }
  if (/\bmobile\b/i.test(role)) expertise.add("Mobile apps");
  if (/\binfrastructure\b/i.test(role)) {
    expertise.add("Infrastructure");
    expertise.add("Reliability");
  }
  if (/\bsecurity\b/i.test(role)) expertise.add("Security");
  if (/\bdata\b/i.test(role)) expertise.add("Data pipelines");
  if (/\bfrontend\b/i.test(role)) expertise.add("Frontend interfaces");
  if (expertise.size === 0) expertise.add("Software engineering");
  return [...expertise].slice(0, 6);
}

const handsOnStarter = `import { useEffect, useRef, useState } from "react";

type Product = { id: string; name: string; category: string; };

export function ProductSearch() {
  const [query, setQuery] = useState("");
  const [products, setProducts] = useState<Product[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Build the search experience here. Think about asynchronous state,
  // keyboard interaction, and what a screen-reader user should hear.
  return <section><label htmlFor="product-search">Search products</label><input id="product-search" value={query} onChange={(event) => setQuery(event.target.value)} /></section>;
}`;

function fallbackProfile(cvText: string, coverLetter: string): ProfileDraft {
  const source = `${cvText} ${coverLetter}`.toLowerCase();
  const role = detectSoftwareEngineeringRole(source);
  const expertise = fallbackExpertise(source, role);
  return {
    role,
    seniority: /senior|lead|staff/i.test(source) ? "Senior" : "Mid-level",
    summary: `${role} with a software-engineering focus on reliable delivery.`,
    narrative: `A hands-on engineer who combines ${roleDescriptor(role)} delivery with thoughtful technical decisions and collaboration.`,
    expertise, characteristics: ["Product ownership", "Pragmatic problem solving", "Cross-functional collaboration"],
    competencies: expertise.map((name) => ({ name, relevance: 1 })),
  };
}

const answerVerbs = [
  "led",
  "built",
  "shipped",
  "migrated",
  "designed",
  "owned",
  "improved",
  "implemented",
  "launched",
  "reduced",
  "scaled",
  "measured",
  "split",
  "debugged",
  "tested",
  "coordinated",
  "delivered",
];

const stopWords = new Set([
  "the",
  "and",
  "with",
  "that",
  "this",
  "from",
  "your",
  "their",
  "about",
  "into",
  "what",
  "were",
  "been",
  "have",
  "will",
  "when",
  "then",
  "than",
  "would",
  "could",
  "should",
  "just",
  "also",
  "yourself",
  "recent",
  "work",
  "recently",
  "tell",
  "walk",
  "through",
  "start",
  "me",
  "you",
  "for",
  "its",
  "was",
  "are",
  "has",
  "had",
]);

function clampScore(value: number): number {
  return Number(Math.min(10, Math.max(0, value)).toFixed(1));
}

function normalizeText(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return trimmed.length ? trimmed : null;
}

function normalizeStrings(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return [...new Set(value.map((item) => normalizeText(item)).filter((item): item is string => item !== null))];
}

function normalizeForSupport(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, " ").replace(/\s+/g, " ").trim();
}

function sourceSupportScore(candidate: string, sourceText: string): number {
  const normalizedCandidate = normalizeForSupport(candidate);
  const normalizedSource = normalizeForSupport(sourceText);
  if (!normalizedCandidate || !normalizedSource) return 0;
  if (normalizedSource.includes(normalizedCandidate)) return 1;

  const candidateTokens = normalizedCandidate.split(" ").filter((token) => token.length > 2);
  if (!candidateTokens.length) return 0;
  const sourceTokens = new Set(normalizedSource.split(" ").filter((token) => token.length > 2));
  const overlap = candidateTokens.filter((token) => sourceTokens.has(token)).length / candidateTokens.length;
  return overlap >= 0.6 ? overlap : 0;
}

function supportedText(value: unknown, sourceText: string): string | null {
  const text = normalizeText(value);
  if (!text) return null;
  return sourceSupportScore(text, sourceText) > 0 ? text : null;
}

function supportedTechnology(value: unknown, sourceText: string): string | null {
  const technology = normalizeText(value);
  if (!technology) return null;
  const normalizedTechnology = normalizeForSupport(technology);
  const normalizedSource = normalizeForSupport(sourceText);
  if (!normalizedTechnology || !normalizedSource) return null;
  if (normalizedSource.includes(normalizedTechnology)) return technology;
  if (normalizedTechnology === "typescript" && /\btypescript\b|\bts\b/.test(normalizedSource)) return technology;
  if (normalizedTechnology === "javascript" && /\bjavascript\b|\bjs\b/.test(normalizedSource)) return technology;
  return sourceSupportScore(technology, sourceText) > 0 ? technology : null;
}

function bestSourceExcerpt(value: z.infer<typeof evidenceSchema>, sourceText: string): string {
  const normalizedSource = normalizeText(sourceText);
  if (!normalizedSource) return value.sourceExcerpt.trim();

  const sentences = normalizedSource
    .split(/(?<=[.!?])\s+|[\r\n]+/)
    .map((sentence) => sentence.trim())
    .filter(Boolean);
  if (!sentences.length) return normalizedSource.slice(0, 320);

  const anchor = [
    value.sourceExcerpt,
    value.projectOrEmployer ?? "",
    value.ownership ?? "",
    value.decision ?? "",
    value.constraint ?? "",
    value.outcome ?? "",
    value.technologies.join(" "),
  ].join(" ");

  const ranked = sentences
    .map((sentence) => ({
      sentence,
      score: Math.max(sourceSupportScore(sentence, anchor), sourceSupportScore(anchor, sentence)),
    }))
    .sort((left, right) => right.score - left.score || right.sentence.length - left.sentence.length);

  return (ranked[0]?.score ?? 0) > 0 ? ranked[0].sentence : normalizedSource.slice(0, 320);
}

function evidenceSignature(item: EvidenceItem): string {
  return [
    normalizeForSupport(item.sourceExcerpt),
    normalizeForSupport(item.projectOrEmployer ?? ""),
    normalizeForSupport(item.ownership ?? ""),
    [...item.technologies].map((technology) => normalizeForSupport(technology)).sort().join("|"),
    normalizeForSupport(item.decision ?? ""),
    normalizeForSupport(item.constraint ?? ""),
    normalizeForSupport(item.outcome ?? ""),
    normalizeForSupport(item.recency ?? ""),
  ].join("::");
}

function dedupeEvidenceItems(items: EvidenceItem[]): EvidenceItem[] {
  const seen = new Set<string>();
  return items.filter((item) => {
    const signature = evidenceSignature(item);
    if (seen.has(signature)) return false;
    seen.add(signature);
    return true;
  });
}

function splitSentences(value: string): string[] {
  return value.split(/(?<=[.!?])\s+/).map((sentence) => sentence.trim()).filter(Boolean);
}

function tokens(value: string): string[] {
  return value.toLowerCase().match(/[a-z0-9]+(?:-[a-z0-9]+)*/g)?.filter((token) => token.length > 2 && !stopWords.has(token)) ?? [];
}

function uniqueTokens(value: string): string[] {
  return [...new Set(tokens(value))];
}

function questionTokens(question: BlueprintQuestion): string[] {
  return uniqueTokens([
    question.prompt,
    question.objective,
    question.competencyName ?? "",
    ...question.expectedSignals,
    ...(question.rubricCriteria ?? []),
  ].join(" "));
}

function matchedSignals(answer: string, expectedSignals: string[]): string[] {
  return [...new Set(expectedSignals.filter((signal) => signalMatches(answer, signal)))]
    .slice(0, expectedSignals.length);
}

function signalMatches(answer: string, signal: string): boolean {
  const lower = answer.toLowerCase();
  if (signal === "ownership") return hasFirstPersonOwnership(answer);
  if (signal === "trade-off") return hasTradeOffLanguage(answer) || /\b(split|balance|compromise|constraint|compare|choice|decision)\b/i.test(answer);
  if (signal === "impact") return /\b\d+%|\b\d+(?:\.\d+)?x\b/i.test(answer) || /\b(measured|reduced|improved|cut|saved|lowered|faster|scaled|launched|shipped)\b/i.test(answer);
  return lower.includes(signal.toLowerCase());
}

function hasFirstPersonOwnership(sentence: string): boolean {
  return /\b(i|we)\b/i.test(sentence) && answerVerbs.some((verb) => new RegExp(`\\b${verb}\\b`, "i").test(sentence));
}

function questionOverlap(answerTokens: string[], rubricTokens: string[]): number {
  if (!answerTokens.length || !rubricTokens.length) return 0;
  const overlap = answerTokens.filter((token) => rubricTokens.includes(token));
  return overlap.length / rubricTokens.length;
}

function tokenOverlap(left: string, right: string): number {
  const leftTokens = uniqueTokens(left);
  const rightTokens = uniqueTokens(right);
  if (!leftTokens.length || !rightTokens.length) return 0;
  const overlap = leftTokens.filter((token) => rightTokens.includes(token));
  return overlap.length / leftTokens.length;
}

function supportedClaims(answerSentences: string[], question: BlueprintQuestion, signals: string[]): string[] {
  const rubricTokens = questionTokens(question);
  return answerSentences.filter((sentence) => {
    if (!sentence) return false;
    if (matchedSignals(sentence, signals).length > 0) return true;
    if (hasFirstPersonOwnership(sentence)) return true;
    return questionOverlap(uniqueTokens(sentence), rubricTokens) >= 0.35;
  }).slice(0, 3);
}

/**
 * True when `question` has a persisted source-evidence target. A `discovery`
 * question from `buildExperienceDiscoveryBlueprint` (spec §8) intentionally
 * carries `evidenceIds: []`: the candidate's answer to it IS the source
 * evidence, newly supplied in this session rather than read from the CV. A
 * career detail can only be "unsupported" relative to evidence the question
 * actually targets, so callers must skip unsupported-claim detection when
 * this returns false. Keyed strictly on `evidenceIds` -- no other signal.
 */
function hasSourceEvidenceTarget(question: BlueprintQuestion): boolean {
  return question.evidenceIds.length > 0;
}

function unsupportedClaims(answerSentences: string[], question: BlueprintQuestion, signals: string[]): string[] {
  if (!hasSourceEvidenceTarget(question)) return [];
  const rubricTokens = questionTokens(question);
  return answerSentences.filter((sentence) => {
    if (!sentence) return false;
    if (matchedSignals(sentence, signals).length > 0) return false;
    if (questionOverlap(uniqueTokens(sentence), rubricTokens) >= 0.35) return false;
    return /\b(i|we)\b/i.test(sentence);
  }).slice(0, 3);
}

function hasTradeOffLanguage(answer: string): boolean {
  return /\b(trade-?off|trade-?offs|constraint|constraints|compare|compared|balanc|choice|rollback|risk|measured?|impact)\b/i.test(answer);
}

function hasStructureMarkers(answer: string): boolean {
  return /\b(first|then|next|after|before|because|however|therefore|so)\b/i.test(answer);
}

function hasHedging(answer: string): boolean {
  return /\b(maybe|probably|sort of|kind of|guess|might|could be|i think)\b/i.test(answer);
}

function buildDimensionReasons(question: BlueprintQuestion, evaluation: {
  relevance: number;
  expectedSignalsPresent: string[];
  supportedClaims: string[];
  unsupportedClaims: string[];
  answerSentences: string[];
  answer: string;
}): Record<(typeof dimensions)[number], string> {
  return {
    correctness: evaluation.relevance >= 6.5
      ? `The answer addresses ${question.objective.toLowerCase()}.`
      : `The answer does not directly address ${question.objective.toLowerCase()}.`,
    depth: evaluation.supportedClaims.length >= 2
      ? "It includes multiple concrete claims and an outcome."
      : "It stays close to a single claim without much detail.",
    clarity: evaluation.answerSentences.length <= 3
      ? "It stays readable and focused."
      : "It is split into more pieces than the question needs.",
    structure: hasStructureMarkers(evaluation.answer)
      ? "It uses a clear sequence to move between ideas."
      : "It does not show an obvious sequence or progression.",
    practicalExperience: evaluation.supportedClaims.length > 0
      ? "It uses first-person ownership and concrete work verbs."
      : "It does not show hands-on ownership of the work.",
    tradeOffAwareness: hasTradeOffLanguage(evaluation.answer)
      ? "It names a trade-off, constraint, or outcome."
      : "It does not explain the trade-off or constraint behind the choice.",
    communication: evaluation.answerSentences.length > 0
      ? "It is expressed as an answer rather than a note fragment."
      : "It reads more like a fragment than a full answer.",
    confidence: hasHedging(evaluation.answer)
      ? "It hedges the claim instead of stating it directly."
      : "It states the example directly.",
    relevance: evaluation.expectedSignalsPresent.length > 0
      ? `It answers the prompt: ${question.prompt}`
      : `It does not directly answer the prompt: ${question.prompt}`,
  };
}

function groundedEvaluationFor(
  question: BlueprintQuestion,
  answer: string,
): GroundedEvaluation {
  const trimmed = answer.trim();
  const sentences = splitSentences(trimmed.length ? trimmed : answer);
  const expectedSignalsPresent = matchedSignals(trimmed, question.expectedSignals);
  const supported = supportedClaims(sentences, question, expectedSignalsPresent);
  const unsupported = unsupportedClaims(sentences, question, expectedSignalsPresent);
  const answerTokenList = uniqueTokens(trimmed);
  const rubricTokenList = questionTokens(question);
  const overlap = questionOverlap(answerTokenList, rubricTokenList);
  const ownership = hasFirstPersonOwnership(trimmed) ? 1.2 : 0;
  const signalCoverage = question.expectedSignals.length
    ? expectedSignalsPresent.length / question.expectedSignals.length
    : 0;
  const claimCoverage = supported.length > 0 ? Math.min(2, supported.length * 0.8) : 0;
  const unsupportedPenalty = unsupported.length * 1.2;

  const relevance = clampScore(1.3 + overlap * 5 + signalCoverage * 2.5 + claimCoverage - unsupportedPenalty + ownership);
  const tradeOffScore = clampScore(4.5 + (hasTradeOffLanguage(trimmed) ? 3.4 : 0) + (supported.length > 0 ? 0.7 : 0) - unsupportedPenalty * 0.2);
  const practicalScore = clampScore(4.2 + ownership * 2.2 + (supported.length > 0 ? 0.9 : 0) - unsupportedPenalty * 0.2);
  const correctness = clampScore(relevance * 0.9 + (expectedSignalsPresent.length > 0 ? 0.6 : -0.4));
  const depth = clampScore(4.4 + supported.length * 1.1 + (question.expectedSignals.length > 1 ? signalCoverage * 1.4 : 0) + (hasTradeOffLanguage(trimmed) ? 0.7 : 0) - unsupportedPenalty * 0.2);
  const clarity = clampScore(4.9 + (sentences.length > 1 ? 0.9 : 0) + (supported.length > 0 ? 0.6 : 0) - (trimmed.endsWith(".") ? 0 : 0.2));
  const structure = clampScore(4.8 + (hasStructureMarkers(trimmed) ? 1.4 : 0) + (sentences.length > 1 ? 0.6 : 0) - unsupportedPenalty * 0.1);
  const communication = clampScore(5 + (sentences.length > 0 ? 0.7 : 0) + (supported.length > 0 ? 0.6 : 0) - unsupportedPenalty * 0.1);
  const confidence = clampScore(4.6 + (hasHedging(trimmed) ? -1.2 : 1.1) + (/[\d%]/.test(trimmed) ? 0.7 : 0) + (supported.length > 0 ? 0.5 : 0));
  const score = clampScore((correctness + depth + clarity + structure + practicalScore + tradeOffScore + communication + confidence + relevance) / dimensions.length);
  // Heuristic tier for the deterministic (non-model) path, mirroring
  // `assessorPrompt`'s four categories from the signals this function already
  // computes: an unsupported claim reads as a wrong core claim, a missing
  // expected signal as an incomplete answer, and trade-off language as the
  // mark of senior depth once the answer is otherwise grounded.
  const answerTier: AnswerTier = unsupported.length > 0
    ? "incorrect"
    : expectedSignalsPresent.length < question.expectedSignals.length
      ? "incomplete"
      : hasTradeOffLanguage(trimmed) && supported.length > 0
        ? "strong"
        : "shallow";

  return {
    score,
    questionId: question.id,
    competencyId: question.competencyId,
    competency: question.competencyName ?? "Communication",
    relevance,
    dimensions: {
      correctness,
      depth,
      clarity,
      structure,
      practicalExperience: practicalScore,
      tradeOffAwareness: tradeOffScore,
      communication,
      confidence,
      relevance,
    },
    strengths: supported.length > 0
      ? ["The answer uses concrete ownership and evidence."]
      : ["The answer stays concise enough to inspect directly."],
    needsWork: unsupported.length > 0
      ? ["Remove unsupported generalities and answer the specific prompt."]
      : ["Add one more concrete detail tied to the question objective."],
    missingPoints: expectedSignalsPresent.length < question.expectedSignals.length
      ? question.missingSignalPrompts.slice(0, 1)
      : ["Add one concrete example of trade-off or outcome."],
    betterStructure: hasStructureMarkers(trimmed)
      ? ["Keep the same sequence but tighten the outcome."]
      : ["Lead with the specific decision, then explain the trade-off and result."],
    improvedAnswer: supported.length > 0
      ? `I would keep the same example but make the decision, trade-off, and outcome explicit: ${supported[0]}`
      : `I would answer the question directly with one concrete example from ${question.competencyName ?? "the work"} and close with the result.`,
    supportedClaims: supported,
    expectedSignalsPresent,
    unsupportedClaims: unsupported,
    dimensionReasons: buildDimensionReasons(question, {
      relevance,
      expectedSignalsPresent,
      supportedClaims: supported,
      unsupportedClaims: unsupported,
      answerSentences: sentences,
      answer: trimmed,
    }),
    answerTier,
  };
}

function groundedModelEvaluation(
  question: BlueprintQuestion,
  answer: string,
  value: z.infer<typeof groundedEvaluationSchema>,
): GroundedEvaluation {
  const normalized = normalizeGroundedEvaluation(question, value);
  const answerSentences = splitSentences(answer.trim().length ? answer.trim() : answer);
  return {
    ...normalized,
    supportedClaims: normalized.supportedClaims
      .filter((claim) => claimMatchesAnswer(claim, answerSentences, question))
      .slice(0, 3),
    expectedSignalsPresent: normalized.expectedSignalsPresent.slice(0, question.expectedSignals.length),
    // A discovery question (no evidenceIds) has no source evidence to be
    // "unsupported" against -- the candidate's answer supplies it. Discard
    // whatever the model returned there rather than trust its judgment; see
    // `hasSourceEvidenceTarget`.
    unsupportedClaims: hasSourceEvidenceTarget(question)
      ? normalized.unsupportedClaims.slice(0, 3)
      : [],
  };
}

function claimMatchesAnswer(claim: string, answerSentences: string[], question: BlueprintQuestion): boolean {
  const normalizedClaim = claim.toLowerCase();
  return answerSentences.some((sentence) => {
    if (sentence.toLowerCase().includes(normalizedClaim)) return true;
    const overlap = tokenOverlap(claim, sentence);
    if (overlap >= 0.66) return true;
    if (matchedSignals(sentence, question.expectedSignals).length > 0 && overlap >= 0.34) return true;
    return questionOverlap(uniqueTokens(sentence), questionTokens(question)) >= 0.35 && overlap >= 0.5;
  });
}

function validateGroundedModelEvaluation(
  question: BlueprintQuestion,
  answer: string,
  value: z.infer<typeof groundedEvaluationSchema>,
): { evaluation: GroundedEvaluation; trusted: boolean } {
  const normalized = groundedModelEvaluation(question, answer, value);
  const fallback = groundedEvaluationFor(question, answer);
  const answerSignals = question.expectedSignals.filter((signal) => signalMatches(answer, signal));
  const materiallyUngrounded = normalized.supportedClaims.length === 0
    && answerSignals.length === 0;

  // answerTier is a depth/seniority read, orthogonal to whether the model's
  // claims are grounded in evidence -- keep the model's classification even
  // when the rest of its evaluation is discarded as untrusted.
  return materiallyUngrounded
    ? { evaluation: { ...fallback, answerTier: value.answerTier }, trusted: false }
    : { evaluation: normalized, trusted: true };
}

/**
 * Converts a request schema into the JSON Schema Gemini enforces during
 * decoding, cached per Zod schema because every `modelJson` call would
 * otherwise re-convert the same object.
 *
 * `responseMimeType: "application/json"` alone only guarantees syntactically
 * valid JSON, not our shape: the model routinely invents out-of-enum values
 * (for example `difficulty: "medium"`), which `schema.parse` then rejects on
 * both the first and the repair attempt, silently degrading every generator
 * to its deterministic fallback. Sending the schema makes the provider
 * constrain decoding instead.
 *
 * Returns null when a schema has no JSON Schema representation (Gemini then
 * gets the prompt alone, as before) so an unconvertible schema degrades
 * rather than throwing.
 */
const responseJsonSchemaCache = new WeakMap<z.ZodType, Record<string, unknown> | null>();

function responseJsonSchemaFor(schema: z.ZodType): Record<string, unknown> | null {
  const cached = responseJsonSchemaCache.get(schema);
  if (cached !== undefined) return cached;
  let converted: Record<string, unknown> | null = null;
  try {
    // `io: "input"` describes what the model must send: fields with a Zod
    // `.default()` stay optional rather than becoming required output fields.
    // The `$schema` dialect marker is dropped -- Gemini takes the schema body
    // only.
    const jsonSchema: Record<string, unknown> = z.toJSONSchema(schema, { io: "input" });
    delete jsonSchema.$schema;
    converted = jsonSchema;
  } catch {
    converted = null;
  }
  responseJsonSchemaCache.set(schema, converted);
  return converted;
}

async function modelJson<T>(operation: string, prompt: string, schema: z.ZodType<T>): Promise<T | null> {
  const apiKey = process.env.GEMINI_API_KEY;
  if (!apiKey) return null;
  try {
    const model = geminiModel();
    const responseJsonSchema = responseJsonSchemaFor(schema);
    const response = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent`, {
      method: "POST", headers: { "Content-Type": "application/json", "x-goog-api-key": apiKey },
      body: JSON.stringify({
        contents: [{ role: "user", parts: [{ text: `${prompt}\nReturn only valid JSON.` }] }],
        generationConfig: {
          responseMimeType: "application/json",
          ...(responseJsonSchema ? { responseJsonSchema } : {}),
        },
      }),
      signal: AbortSignal.timeout(45_000),
    });
    if (!response.ok) {
      console.warn("[gemini] request failed", {
        operation,
        state: geminiFailureState(response.status),
        status: response.status,
        model,
      });
      return null;
    }
    const body = await response.json() as { candidates?: Array<{ content?: { parts?: Array<{ text?: string }> } }> };
    const output = body.candidates?.[0]?.content?.parts?.map((part) => part.text ?? "").join("");
    if (!output) {
      console.warn("[gemini] empty model output", { operation, state: "unknown", status: response.status, model });
      return null;
    }
    try {
      return schema.parse(JSON.parse(output));
    } catch (error) {
      // Log the failing field paths and codes only -- never the model text or
      // the parsed values, which carry candidate CV content.
      const issues = error instanceof z.ZodError
        ? error.issues.slice(0, 5).map((issue) => `${issue.path.join(".") || "<root>"}:${issue.code}`)
        : ["<unparseable-json>"];
      console.warn("[gemini] invalid model output", { operation, state: "unknown", status: response.status, model, issues });
      return null;
    }
  } catch (error) {
    console.warn("[gemini] request failed", {
      operation,
      state: "unknown",
      model: process.env.GEMINI_MODEL?.trim() || "gemini-3.6-flash",
      error: error instanceof Error ? error.name : "unknown",
    });
    return null;
  }
}

function normalizeEvidenceItem(value: z.infer<typeof evidenceSchema>, index: number, sourceText: string): EvidenceItem {
  return {
    id: normalizeText(value.id) ?? `evidence-${index + 1}`,
    sourceKind: value.sourceKind ?? null,
    sourceExcerpt: bestSourceExcerpt(value, sourceText),
    projectOrEmployer: supportedText(value.projectOrEmployer, sourceText),
    ownership: supportedText(value.ownership, sourceText),
    technologies: [...new Set(value.technologies.map((technology) => supportedTechnology(technology, sourceText)).filter((technology): technology is string => technology !== null))],
    decision: supportedText(value.decision, sourceText),
    constraint: supportedText(value.constraint, sourceText),
    outcome: supportedText(value.outcome, sourceText),
    recency: supportedText(value.recency, sourceText),
    confidence: value.confidence,
  };
}

function techMatches(text: string): string[] {
  const technologies = [
    "React",
    "TypeScript",
    "JavaScript",
    "Next.js",
    "Postgres",
    "Node.js",
    "GraphQL",
    "Redux",
    "Supabase",
    "Vercel",
    "AWS",
    "Swift",
    "Kotlin",
    "iOS",
    "Android",
    "Flutter",
    "Python",
    "Go",
    "Java",
    "Rust",
    "Docker",
    "Kubernetes",
    "Terraform",
    "Kafka",
    "Redis",
    "Spark",
    "Airflow",
    "Snowflake",
    "BigQuery",
    "Testing",
    "Accessibility",
  ];
  const lower = text.toLowerCase();
  return technologies.filter((technology) => lower.includes(technology.toLowerCase()));
}

function fallbackEngineeringEvidence(cvText: string, coverLetter: string): EvidenceItem[] {
  const text = `${cvText} ${coverLetter}`.replace(/\s+/g, " ").trim();
  if (!text) return [];
  const sentences = text.split(/(?<=[.!?])\s+/).map((sentence) => sentence.trim()).filter(Boolean);
  const verbs = /\b(led|built|shipped|migrated|designed|owned|improved|implemented|launched|reduced|scaled)\b/i;
  const items: EvidenceItem[] = [];
  for (const [index, sentence] of sentences.entries()) {
    if (!verbs.test(sentence)) continue;
    const technologies = techMatches(sentence);
    if (!technologies.length && !/\b(frontend|backend|platform|product|checkout|dashboard|search|api|workflow)\b/i.test(sentence)) continue;
    items.push({
      id: `evidence-${index + 1}`,
      sourceKind: null,
      sourceExcerpt: sentence.slice(0, 320),
      projectOrEmployer: null,
      ownership: /\b(i|we|owned|led|built|shipped|designed|implemented|migrated)\b/i.test(sentence) ? sentence.slice(0, 240) : null,
      technologies,
      decision: null,
      constraint: null,
      outcome: null,
      recency: null,
      confidence: technologies.length ? 0.4 : 0.25,
    });
    if (items.length === 2) break;
  }
  return items;
}

function normalizeBlueprintQuestion(
  value: z.infer<typeof blueprintQuestionDraftSchema>,
  createdAt: string,
): BlueprintQuestion {
  return {
    id: `blueprint-question-${value.sequence}`,
    sequence: value.sequence,
    category: value.category,
    competencyId: normalizeText(value.competencyId),
    competencyName: normalizeText(value.competencyName),
    difficulty: value.difficulty,
    isFollowUp: false,
    prompt: value.prompt.trim(),
    answer: null,
    createdAt,
    objective: value.objective.trim(),
    evidenceIds: value.evidenceIds,
    expectedSignals: value.expectedSignals.map((signal) => signal.trim()),
    missingSignalPrompts: value.missingSignalPrompts.map((prompt) => prompt.trim()),
    rubricCriteria: value.rubricCriteria.map((criteria) => criteria.trim()),
    followUpLimit: value.followUpLimit,
    sourceConfidence: value.sourceConfidence ?? null,
    // A model-authored base question predates the director's intent/assistance pipeline.
    askedIntent: null,
    assistance: [],
    nonAnswer: false,
    setAsideAt: null,
    setAsideReason: null,
    nonAnswers: [],
  };
}

function normalizeBlueprint(
  value: z.infer<typeof blueprintDraftSchema>,
  createdAt: string,
): InterviewBlueprint {
  return {
    status: value.status ?? "grounded",
    fallbackReason: normalizeText(value.fallbackReason),
    maxFollowUps: value.maxFollowUps,
    maxQuestions: value.maxQuestions,
    createdAt,
    questions: value.questions.map((question) => normalizeBlueprintQuestion(question, createdAt)),
    // Shared by both callers: `generateInterviewBlueprint`'s `withCoveragePlan`
    // overwrites these with the real round-derived values, and
    // `generatePracticeBlueprint`'s practice-plan path never consumes the
    // round/coverage-target system at all.
    roundId: "tech-lead",
    turnBudget: defaultMaxQuestions,
    targets: [],
  };
}

function fallbackBlueprintCompetencies(
  profile: Pick<ProfileDraft, "seniority" | "expertise" | "competencies">,
): Competency[] {
  const expectedLevel = /staff|principal|lead|advanced/i.test(profile.seniority ?? "")
    ? "advanced"
    : /senior/i.test(profile.seniority ?? "")
      ? "senior"
      : /junior|entry|graduate|foundational/i.test(profile.seniority ?? "")
        ? "foundational"
        : "intermediate";
  const names = profile.competencies.length
    ? profile.competencies.map((competency) => competency.name)
    : profile.expertise;
  return names.map((name, index) => ({
    id: `fallback-competency-${index + 1}`,
    name,
    relevance: profile.competencies[index]?.relevance ?? 1,
    expectedLevel,
    estimatedLevel: null,
    confidence: null,
    lastPracticedAt: null,
    questionCount: 0,
    averageScore: null,
    recentScore: null,
    strengths: [],
    weaknesses: [],
  }));
}

/**
 * Extracts software-engineering evidence items from user-provided source text.
 * Returns only schema-validated facts, preserves nulls for unknown optional
 * fields, and falls back to deterministic sentence extraction on provider
 * failure or malformed JSON.
 */
export async function extractEngineeringEvidence(cvText: string, coverLetter: string): Promise<EvidenceItem[]> {
  const sourceText = `${cvText} ${coverLetter}`;
  const result = await modelJson(
    "evidence extraction",
    `You are extracting engineering evidence for an interview coach. Return only valid JSON. Produce an array of evidence objects. Never invent facts. Preserve null for unknown optional fields. Each item must include a sourceExcerpt and may include sourceKind, projectOrEmployer, ownership, technologies, decision, constraint, outcome, recency, confidence, and a stable id if available. Use only facts explicitly supported by the supplied text.\nCV:\n${cvText}\nCover letter:\n${coverLetter}`,
    evidenceListSchema,
  );
  if (!result) return fallbackEngineeringEvidence(cvText, coverLetter);
  const list = Array.isArray(result) ? result : result.evidence;
  return dedupeEvidenceItems(list.map((item, index) => normalizeEvidenceItem(item, index, sourceText)));
}

/**
 * Generates the persisted interview blueprint from the validated profile and
 * extracted evidence. `assessProfileReadiness(evidence)` is the single
 * decision point: when the profile lacks enough source-backed detail, this
 * returns a deterministic discovery blueprint (`buildExperienceDiscoveryBlueprint`)
 * with zero model calls. Otherwise it asks the model for a grounded blueprint,
 * retries once on malformed or unsupported output, then falls back to a
 * deterministic limited-grounding plan (`buildFallbackInterviewBlueprint`) if
 * both attempts fail.
 *
 * `options.roundId` and `options.opportunity` drive the coverage plan (spec
 * §9.1): every blueprint this function returns -- whichever of the three
 * strategies above produced it -- carries `roundId`, a fixed `turnBudget` of
 * 8, and `targets` computed once from the same inputs, via `withCoveragePlan`
 * below. That single merge point is deliberate: it is the one place this
 * function guarantees the coverage-plan fields land, so no return path can
 * silently omit them.
 *
 * It is also the one place they are validated. `validateInterviewBlueprint`
 * is the only check that `targets` is non-empty, and `buildCoverageTargets`
 * legitimately returns an empty list for a profile with no competencies and
 * no anchored gaps -- exactly the sparse profile the discovery path serves.
 * `createSessionWithBlueprint` validates `blueprint.questions` rather than
 * `targets`, so an unvalidated empty plan used to insert an active session
 * with zero question rows and only then fail in `openingTurn`, orphaning it.
 * Throwing here, before any session exists, is the whole point.
 */
export async function generateInterviewBlueprint(
  profile: Pick<ProfileDraft, "role" | "seniority" | "summary" | "narrative" | "expertise" | "characteristics" | "competencies">,
  evidence: EvidenceItem[],
  options: { roundId: RoundId; opportunity: Pick<Opportunity, "gaps" | "jobDescription"> | null },
): Promise<InterviewBlueprint> {
  const createdAt = new Date().toISOString();
  const targets = buildCoverageTargets(profile, evidence, options.opportunity, options.roundId);
  const withCoveragePlan = (blueprint: InterviewBlueprint): InterviewBlueprint => validateInterviewBlueprint({
    ...blueprint,
    roundId: options.roundId,
    turnBudget: 8,
    targets,
  }, evidence);

  const readiness = assessProfileReadiness(evidence);
  if (!readiness.ready) {
    return withCoveragePlan(buildExperienceDiscoveryBlueprint(profile, evidence, readiness, new Date(createdAt)));
  }
  const competencyContext = profile.competencies.map((competency) => ({
    name: competency.name,
    relevance: competency.relevance,
  }));
  const prompt = (repair: boolean) => [
    "You are planning a software-engineering interview blueprint.",
    "Return only valid JSON.",
    "Use the exact five-question backbone in this order: introduction, experience, technical, architecture, behavioral.",
    "Every question must include objective, evidenceIds, expectedSignals, missingSignalPrompts, rubricCriteria, followUpLimit, prompt, difficulty, and optional competencyId/competencyName/sourceConfidence.",
    "Only reference evidence ids that appear below.",
    "Do not invent projects, technologies, or outcomes.",
    repair ? "The previous response failed validation. Repair it and satisfy every schema field exactly." : "",
    `Profile: ${JSON.stringify(profile)}`,
    `Competencies: ${JSON.stringify(competencyContext)}`,
    `Evidence: ${JSON.stringify(evidence)}`,
  ].filter(Boolean).join("\n");

  for (const repair of [false, true]) {
    const result = await modelJson("interview blueprint", prompt(repair), blueprintDraftSchema);
    if (!result) continue;
    try {
      return withCoveragePlan(normalizeBlueprint(result, createdAt));
    } catch {
      continue;
    }
  }

  return withCoveragePlan(buildFallbackInterviewBlueprint(
    {
      role: profile.role,
      seniority: profile.seniority,
      summary: profile.summary,
      narrative: profile.narrative,
      expertise: profile.expertise,
      characteristics: profile.characteristics,
      competencies: profile.competencies,
    },
    fallbackBlueprintCompetencies(profile),
    evidence,
    new Date(createdAt),
  ));
}

/**
 * The exact base question count for a practice plan's format, per the
 * release-2 controller ruling: plan-driven conversations support 1-5 base
 * questions (never the generic five-question backbone). `hands_on` never
 * calls `generatePracticeBlueprint` -- it starts via `handsOnExercise` and
 * `createHandsOnPracticeSession` instead -- so its count is 0 and unused.
 */
function baseQuestionCountFor(format: PracticeFormat): number {
  switch (format) {
    case "self_presentation": return 2;
    case "role_prep": return 4;
    case "full_simulation": return 5;
    case "hands_on": return 0;
    default: return 3;
  }
}

/**
 * The text to ground a reviewed observation on. A user correction always
 * supersedes the original AI-authored claim -- mirrors
 * `effectiveObservationText` in `src/lib/practice-recommendation.ts`,
 * duplicated locally (like `roleDescriptor`) so this module stays
 * independent of that one.
 */
function effectivePracticeObservationText(observation: CoachObservation): string {
  return observation.reviewState === "corrected" && observation.userCorrection?.trim()
    ? observation.userCorrection.trim()
    : observation.claim.trim();
}

function practiceOpportunityContext(opportunity: Opportunity | null): Record<string, unknown> | null {
  if (!opportunity) return null;
  return { company: opportunity.company, role: opportunity.role, jobDescription: opportunity.jobDescription };
}

function practiceBlueprintPrompt(
  profile: Profile,
  evidence: EvidenceItem[],
  plan: PracticePlan,
  context: PracticeBlueprintContext,
  baseQuestionCount: number,
  repair: boolean,
): string {
  const reviewedObservations = context.observations
    .filter((observation) => observation.reviewState === "confirmed" || observation.reviewState === "corrected")
    .map((observation) => ({
      type: observation.observationType,
      claim: effectivePracticeObservationText(observation),
      importance: observation.importance,
    }));
  const confirmedStories = context.stories
    .filter((story) => story.reviewState === "confirmed")
    .map((story) => ({
      title: story.title,
      situation: story.situation,
      responsibility: story.responsibility,
      problem: story.problem,
      actions: story.actions,
      alternatives: story.alternatives,
      tradeoffs: story.tradeoffs,
      ownership: story.ownership,
      outcome: story.outcome,
      lessons: story.lessons,
    }));

  return [
    "You are planning a practice interview blueprint for one specific practice plan.",
    "Return only valid JSON.",
    `Produce exactly ${baseQuestionCount} base question(s) unless the supplied candidate evidence cannot ground that many -- never produce more than ${baseQuestionCount}.`,
    "Every question must include sequence, category, objective, evidenceIds, expectedSignals, missingSignalPrompts, rubricCriteria, followUpLimit, prompt, difficulty, and optional competencyId/competencyName/sourceConfidence.",
    "Only reference evidence ids that appear in the supplied evidence below.",
    "Do not invent projects, technologies, or outcomes.",
    "Job requirements are targets to probe, not candidate evidence.",
    "Candidate factual claims must be grounded in supplied evidence or confirmed story facts.",
    "Do not invent company interview-process facts.",
    repair ? "The previous response failed validation. Repair it and satisfy every schema field exactly." : "",
    `Practice plan: ${JSON.stringify({
      format: plan.format,
      primaryFocus: plan.primaryFocus,
      secondaryFocus: plan.secondaryFocus,
      rationale: plan.rationale,
      successCriteria: plan.successCriteria,
    })}`,
    `Profile: ${JSON.stringify({
      role: profile.role,
      seniority: profile.seniority,
      summary: profile.summary,
      narrative: profile.narrative,
      expertise: profile.expertise,
      characteristics: profile.characteristics,
    })}`,
    `Evidence: ${JSON.stringify(evidence)}`,
    `Primary opportunity: ${JSON.stringify(practiceOpportunityContext(context.primaryOpportunity))}`,
    `Supporting opportunities: ${JSON.stringify(context.supportingOpportunities.map(practiceOpportunityContext))}`,
    `Reviewed observations: ${JSON.stringify(reviewedObservations)}`,
    `Confirmed stories: ${JSON.stringify(confirmedStories)}`,
  ].filter(Boolean).join("\n");
}

/**
 * Ensures an AI-generated practice blueprint stays within the plan's base
 * question budget and only references candidate-owned evidence. The sibling
 * validator to `validateInterviewBlueprint` (interview-planner.ts) for
 * plan-driven practice, not a relaxation of it: unlike the generic backbone,
 * a practice blueprint may use FEWER than `baseQuestionCount` questions when
 * grounding is thin, but must never exceed it, and its categories are not
 * fixed to a specific slot order.
 */
function validatePracticeBlueprint(
  blueprint: InterviewBlueprint,
  evidence: EvidenceItem[],
  baseQuestionCount: number,
): InterviewBlueprint {
  if (blueprint.questions.length < 1 || blueprint.questions.length > baseQuestionCount) {
    throw new Error(`Practice blueprint must contain between one and ${baseQuestionCount} base questions.`);
  }
  const knownEvidenceIds = new Set(evidence.map((item) => item.id));
  blueprint.questions.forEach((question, index) => {
    if (question.sequence !== index + 1 || question.isFollowUp) {
      throw new Error("Practice blueprint questions must be contiguous base questions.");
    }
    if (!question.objective.trim()) throw new Error("Practice blueprint questions need an objective.");
    if (!question.prompt!.trim()) throw new Error("Practice blueprint questions need prompt text.");
    if (!question.expectedSignals.length) throw new Error("Practice blueprint questions need expected signals.");
    if (!question.missingSignalPrompts.length) throw new Error("Practice blueprint questions need missing-signal prompts.");
    if (!question.rubricCriteria?.length) throw new Error("Practice blueprint questions need scoring criteria.");
    for (const evidenceId of question.evidenceIds) {
      if (!knownEvidenceIds.has(evidenceId)) {
        throw new Error(`Practice blueprint references unknown evidence ids: ${evidenceId}.`);
      }
    }
  });
  // Ruling R5: maxQuestions must leave follow-up headroom above the base
  // question count. The Task 2 migration clamps the persisted ceiling with
  // `greatest(v_count, least(8, max_questions))`, a floor -- a blueprint
  // whose maxQuestions equals its base count would make
  // `record_conversation_turn` refuse every follow-up.
  if (blueprint.maxQuestions <= blueprint.questions.length) {
    throw new Error("Practice blueprint must leave follow-up headroom above its base question count.");
  }
  return blueprint;
}

/**
 * Recomputes the top-level follow-up budget so ruling R5 holds regardless of
 * what the model (or the deterministic fallback) proposed: `maxFollowUps` is
 * clamped to at least 1 (never 0, which would leave no headroom) and at most
 * 3, and `maxQuestions` is derived from it rather than trusted verbatim.
 */
function finalizePracticeBlueprint(blueprint: InterviewBlueprint): InterviewBlueprint {
  const maxFollowUps = Math.max(1, Math.min(3, blueprint.maxFollowUps));
  const maxQuestions = Math.min(8, blueprint.questions.length + maxFollowUps);
  return { ...blueprint, maxFollowUps, maxQuestions };
}

const practiceFallbackCategoryOrder: QuestionCategory[] = ["introduction", "experience", "technical", "architecture", "behavioral"];

function practiceDifficulty(seniority: string | null): Difficulty {
  const value = (seniority ?? "").toLowerCase();
  if (/staff|principal|lead|advanced/.test(value)) return "advanced";
  if (/senior/.test(value)) return "senior";
  if (/junior|entry|graduate|foundational/.test(value)) return "foundational";
  return "intermediate";
}

function fallbackPracticeSignals(category: QuestionCategory): string[] {
  if (category === "introduction") return ["role summary", "recent ownership"];
  if (category === "technical") return ["decision", "trade-off", "constraint"];
  if (category === "architecture") return ["requirements", "constraints", "approach"];
  if (category === "behavioral") return ["collaboration", "decision", "impact"];
  return ["role", "decision", "impact"];
}

function fallbackPracticeMissingSignalPrompts(category: QuestionCategory, subject: string): string[] {
  if (category === "introduction") return [`Name the recent engineering area you owned in ${subject}.`];
  if (category === "technical") return ["What option did you reject and why?"];
  if (category === "architecture") return ["Which requirement or constraint changed the design?"];
  if (category === "behavioral") return ["Who did you need alignment from and how did you get it?"];
  return ["Name the measurable outcome or impact."];
}

function fallbackPracticeRubricCriteria(category: QuestionCategory, subject: string): string[] {
  if (category === "introduction") {
    return [
      "Establish the candidate's recent engineering ownership.",
      `Keep the summary grounded in ${subject}.`,
    ];
  }
  if (category === "technical") {
    return [
      `Name the technical decision being discussed in ${subject}.`,
      "Explain the constraint or rejected alternative.",
      "Describe the trade-off and result.",
    ];
  }
  if (category === "architecture") {
    return [
      `Explain the requirements or constraints that shaped ${subject}.`,
      "Describe the system-level decision or architecture choice.",
      "State the outcome or reliability impact.",
    ];
  }
  if (category === "behavioral") {
    return [
      `Name the collaboration challenge around ${subject}.`,
      "Describe how the team aligned on the decision.",
      "State what changed because of the collaboration.",
    ];
  }
  return [
    `Name the project or work example in ${subject}.`,
    "Describe the candidate's role and ownership.",
    "Explain the decision, trade-off, and outcome.",
  ];
}

/**
 * The most specific work anchor an evidence item can name, or `null` when the
 * item carries no anchor distinct from the practice focus. Extraction leaves
 * `projectOrEmployer` null often enough that falling straight through to
 * `plan.primaryFocus` produced questions and objectives naming the focus twice
 * ("Probe X using X"), so each weaker anchor is tried before giving up.
 */
function practiceEvidenceAnchor(item: EvidenceItem | null): string | null {
  if (!item) return null;
  const candidates = [item.projectOrEmployer, item.ownership, item.technologies[0], item.sourceExcerpt];
  for (const candidate of candidates) {
    const trimmed = candidate?.trim();
    if (trimmed) return trimmed;
  }
  return null;
}

/**
 * Fallback prompt for one practice question. `anchor` is null when no evidence
 * anchor is available; the focus-only wording then keeps the practice focus to
 * a single mention instead of repeating it as both subject and context.
 */
function fallbackPracticePrompt(category: QuestionCategory, anchor: string | null, plan: PracticePlan, role: string | null): string {
  if (category === "introduction") return `Give me a concise introduction to yourself and the ${roleDescriptor(role)} work relevant to ${plan.primaryFocus}.`;
  if (category === "technical") {
    return anchor
      ? `Walk me through a technical decision involving ${anchor}, focused on ${plan.primaryFocus}. What trade-offs did you consider?`
      : `Walk me through a technical decision behind ${plan.primaryFocus}. What trade-offs did you consider?`;
  }
  if (category === "architecture") {
    return anchor
      ? `Design an approach involving ${anchor} that addresses ${plan.primaryFocus}. Start with the requirements you would clarify.`
      : `Design an approach that addresses ${plan.primaryFocus}. Start with the requirements you would clarify.`;
  }
  if (category === "behavioral") {
    return anchor
      ? `Tell me about a collaboration challenge related to ${anchor}, connected to ${plan.primaryFocus}. How did you make progress?`
      : `Tell me about a collaboration challenge connected to ${plan.primaryFocus}. How did you make progress?`;
  }
  return anchor
    ? `Tell me about ${anchor}, in the context of ${plan.primaryFocus}. What was your role and impact?`
    : `Tell me about your work on ${plan.primaryFocus}. What was your role and impact?`;
}

/**
 * Builds a deterministic fallback practice blueprint when the model response
 * is absent or invalid, mirroring `buildFallbackInterviewBlueprint`'s
 * approach for the generic backbone. Uses exactly `baseQuestionCount`
 * questions (a leading slice of the same category order as the generic
 * backbone), grounds each non-introduction question in candidate evidence
 * (round-robin over the supplied items) when evidence exists, and is
 * explicitly marked limited grounding.
 */
function buildFallbackPracticeBlueprint(
  profile: Profile,
  evidence: EvidenceItem[],
  plan: PracticePlan,
  baseQuestionCount: number,
  createdAt: string,
  fallbackReason = "Gemini returned invalid practice blueprint JSON after one repair attempt.",
): InterviewBlueprint {
  const categoriesForCount = practiceFallbackCategoryOrder.slice(0, baseQuestionCount);
  const rankedEvidence = [...evidence].sort((left, right) => right.confidence - left.confidence);
  const difficulty = practiceDifficulty(profile.seniority);

  const questions: BlueprintQuestion[] = categoriesForCount.map((category, index) => {
    const sequence = index + 1;
    const item = category === "introduction" || !rankedEvidence.length
      ? null
      : rankedEvidence[index % rankedEvidence.length];
    const anchor = practiceEvidenceAnchor(item);
    const subject = anchor ?? plan.primaryFocus;
    return {
      id: `practice-blueprint-question-${sequence}`,
      sequence,
      category,
      competencyId: null,
      competencyName: plan.primaryFocus,
      difficulty,
      isFollowUp: false,
      prompt: fallbackPracticePrompt(category, anchor, plan, profile.role),
      answer: null,
      createdAt,
      objective: category === "introduction"
        ? `Establish recent engineering ownership relevant to ${plan.primaryFocus}.`
        : anchor
          ? `Probe ${plan.primaryFocus} using ${anchor}.`
          : `Probe ${plan.primaryFocus}.`,
      evidenceIds: item ? [item.id] : [],
      expectedSignals: fallbackPracticeSignals(category),
      missingSignalPrompts: fallbackPracticeMissingSignalPrompts(category, subject),
      rubricCriteria: fallbackPracticeRubricCriteria(category, subject),
      followUpLimit: category === "introduction" ? 0 : 1,
      sourceConfidence: item?.confidence ?? null,
      // A fallback practice question predates the director's intent/assistance pipeline.
      askedIntent: null,
      assistance: [],
      nonAnswer: false,
      setAsideAt: null,
      setAsideReason: null,
      nonAnswers: [],
    };
  });

  return finalizePracticeBlueprint({
    status: "limited-grounding",
    fallbackReason,
    maxFollowUps: 3,
    maxQuestions: 8,
    createdAt,
    questions,
    // Practice-plan blueprints never consume the round/coverage-target system.
    roundId: "tech-lead",
    turnBudget: defaultMaxQuestions,
    targets: [],
  });
}

/**
 * Generates a practice-plan-specific interview blueprint sized and focused
 * for `plan.format` -- the sibling generator to `generateInterviewBlueprint`
 * for plan-driven practice, not a replacement for it (that generator's exact
 * five-question backbone is untouched). Retries once on malformed or
 * unsupported model output, then falls back to a deterministic
 * limited-grounding plan built from `evidence` alone.
 *
 * Inputs: `profile` and `evidence` ground candidate factual claims;
 * `plan` supplies the format, focus, and success criteria that size and aim
 * the blueprint (see `baseQuestionCountFor`); `context` supplies the
 * opportunities that shape (never prove) what gets probed, plus the reviewed
 * observations and confirmed stories the blueprint may additionally ground
 * on. Only `confirmed`/`corrected` observations and `confirmed` stories
 * reach the prompt; a `corrected` observation's `userCorrection` is used in
 * place of its original `claim`.
 *
 * Side effects: none beyond the outbound Gemini request. Failure behavior:
 * never throws -- an unavailable or persistently invalid model response
 * always resolves to the deterministic fallback blueprint.
 *
 * Invariant (ruling R5): the returned blueprint always has
 * `maxQuestions > questions.length`, so a plan-driven session started from
 * it can always record at least one follow-up (see `finalizePracticeBlueprint`).
 */
export async function generatePracticeBlueprint(
  profile: Profile,
  evidence: EvidenceItem[],
  plan: PracticePlan,
  context: PracticeBlueprintContext,
): Promise<InterviewBlueprint> {
  const createdAt = new Date().toISOString();
  const baseQuestionCount = baseQuestionCountFor(plan.format);

  for (const repair of [false, true]) {
    const result = await modelJson(
      "practice blueprint",
      practiceBlueprintPrompt(profile, evidence, plan, context, baseQuestionCount, repair),
      practiceBlueprintDraftSchema,
    );
    if (!result) continue;
    try {
      const finalized = finalizePracticeBlueprint(normalizeBlueprint(result, createdAt));
      return validatePracticeBlueprint(finalized, evidence, baseQuestionCount);
    } catch {
      continue;
    }
  }

  return buildFallbackPracticeBlueprint(profile, evidence, plan, baseQuestionCount, createdAt);
}

function hasConcreteWorkAnchor(item: EvidenceItem): boolean {
  const sourceExcerpt = item.sourceExcerpt.trim();
  const structuredText = [item.ownership, item.decision, item.constraint, item.outcome]
    .filter((value): value is string => typeof value === "string" && value.trim().length > 0)
    .join(" ");
  const supportingText = [sourceExcerpt, structuredText].filter(Boolean).join(" ");
  const hasAction = workExampleVerbPattern.test(supportingText) || /\b(responsible for|owned|led|built|designed|implemented|created|refactored|migrated|shipped|deployed|improved|reduced|launched|maintained|developed|debugged|automated)\b/i.test(supportingText);
  const hasConcreteDetail = structuredText.length > 0 || /\b\d+%|\bmetric\b|\boutcome\b|\bimpact\b|\bconstraint\b|\btrade-?off\b/i.test(supportingText);
  return Boolean(sourceExcerpt) && item.technologies.length > 0 && hasAction && hasConcreteDetail;
}

function concreteEvidenceCount(evidence: EvidenceItem[]): number {
  return dedupeEvidenceItems(evidence).filter((item) => {
    const hasSpecifics = item.technologies.length > 0 || Boolean(item.ownership?.trim()) || Boolean(item.outcome?.trim()) || Boolean(item.decision?.trim());
    return hasConcreteWorkAnchor(item) && hasSpecifics;
  }).length;
}

/**
 * Deterministically scores how much source-backed detail is available for
 * interview grounding: concrete work examples, identifiable technologies, and
 * ownership or outcome signals. `ready === false` is advisory, not a gate --
 * `generateInterviewBlueprint` routes it to a discovery blueprint instead of
 * blocking the session (see `ProfileReadiness` in `@/lib/types`).
 */
export function assessProfileReadiness(evidence: EvidenceItem[]): ProfileReadiness {
  const missing = new Set<string>();
  if (concreteEvidenceCount(evidence) < 2) missing.add("two concrete engineering projects or work examples");
  if (!evidence.some((item) => item.technologies.length > 0)) missing.add("identifiable technologies");
  if (!evidence.some((item) => hasConcreteWorkAnchor(item))) missing.add("responsibilities or outcomes");
  return { ready: missing.size === 0, missing: [...missing] };
}

/** Extracts factual CV text with Gemini, enforcing hosted upload limits and actionable failures. */
export async function extractPdfText(file: File): Promise<string> {
  const apiKey = process.env.GEMINI_API_KEY;
  if (!apiKey) throw new Error("Add GEMINI_API_KEY to extract text from a PDF.");
  if (file.size > MAX_CV_PDF_BYTES) throw new Error("Keep CV PDFs under 4 MB so the upload fits Vercel's request limit.");
  const model = geminiModel();
  const data = Buffer.from(await file.arrayBuffer()).toString("base64");
  const response = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent`, {
    method: "POST", headers: { "Content-Type": "application/json", "x-goog-api-key": apiKey },
    body: JSON.stringify({
      contents: [{
        parts: [
          { text: "Extract the readable text from this CV. Preserve factual details, headings, job titles, dates, technologies, and achievements. Return only the extracted text." },
          { inlineData: { mimeType: "application/pdf", data } },
        ],
      }],
    }),
    signal: AbortSignal.timeout(60_000),
  });
  if (!response.ok) {
    const requestError = await geminiRequestError(response, "the PDF", model);
    console.warn("[gemini] request failed", {
      operation: "the PDF",
      state: requestError.state,
      status: requestError.status,
      model,
    });
    throw requestError;
  }
  const body = await response.json() as { candidates?: Array<{ content?: { parts?: Array<{ text?: string }> } }> };
  const text = body.candidates?.[0]?.content?.parts?.map((part) => part.text ?? "").join("").trim();
  if (!text || text.length < 80) throw new Error("Not enough readable text was found in that PDF. Paste a summary instead.");
  return text;
}

export async function analyzeProfile(cvText: string, coverLetter: string): Promise<ProfileDraft> {
  const result = await modelJson(
    "profile analysis",
    `You are a career-profile analyst. Extract a concise software-engineering profile from this CV and optional cover letter. Identify the most accurate engineering specialization when the evidence supports it, such as frontend, backend, mobile, infrastructure, security, data, or full-stack. Never invent facts. Return role, seniority, summary, narrative, expertise, characteristics, and competency names with professional relevance (0 to 1). Do not estimate ability, scores, confidence, or seniority beyond stated evidence.\nCV:\n${cvText}\nCover letter:\n${coverLetter}`,
    profileSchema,
  );
  return result ?? fallbackProfile(cvText, coverLetter);
}

function groundedQuestion(question: PlannedQuestion, blueprint: InterviewBlueprint | null): BlueprintQuestion {
  const rubric = blueprint?.questions.find((item) => item.id === question.id);
  if (rubric) return rubric;
  const objective = typeof question.objective === "string" && question.objective.trim().length > 0
    ? question.objective.trim()
    : question.competencyName
      ? `Probe ${question.competencyName} with concrete evidence.`
      : "Probe the candidate's recent engineering work.";
  return {
    ...question,
    objective,
    evidenceIds: question.evidenceIds ?? [],
    expectedSignals: question.expectedSignals?.length
      ? question.expectedSignals
      : question.competencyName
        ? [question.competencyName, "ownership", "impact"]
        : ["ownership", "impact"],
    missingSignalPrompts: question.missingSignalPrompts?.length
      ? question.missingSignalPrompts
      : question.competencyName
        ? [`Name one concrete example from ${question.competencyName}.`]
        : ["Name one concrete example from the work you owned."],
    rubricCriteria: question.rubricCriteria?.length
      ? question.rubricCriteria
      : question.competencyName
        ? [
          `Name one concrete example from ${question.competencyName}.`,
          "Describe the ownership or decision involved.",
          "Explain the outcome or trade-off.",
        ]
      : [
        "Name one concrete example from the work you owned.",
        "Describe the ownership or decision involved.",
        "Explain the outcome or trade-off.",
      ],
    followUpLimit: question.followUpLimit ?? 1,
    sourceConfidence: question.sourceConfidence ?? null,
    parentQuestionId: question.parentQuestionId ?? null,
  };
}

function normalizeGroundedEvaluation(question: PlannedQuestion, value: z.infer<typeof groundedEvaluationSchema>): GroundedEvaluation {
  return {
    score: value.score,
    questionId: question.id,
    competencyId: question.competencyId,
    competency: question.competencyName ?? value.competency ?? "Communication",
    relevance: value.relevance,
    dimensions: value.dimensions,
    strengths: value.strengths,
    needsWork: value.needsWork,
    missingPoints: value.missingPoints,
    betterStructure: value.betterStructure,
    improvedAnswer: value.improvedAnswer,
    supportedClaims: normalizeStrings(value.supportedClaims),
    expectedSignalsPresent: normalizeStrings(value.expectedSignalsPresent),
    unsupportedClaims: normalizeStrings(value.unsupportedClaims),
    dimensionReasons: value.dimensionReasons,
    answerTier: value.answerTier,
  };
}

/**
 * The assessor prompt. Scores privately against the rubric and classifies the
 * answer. It authors no candidate-facing text, so the rubric can appear here
 * safely -- that is the whole point of the split (spec §6.1).
 */
function assessorPrompt(
  question: BlueprintQuestion,
  profile: Pick<ProfileDraft, "role" | "seniority" | "expertise" | "narrative">,
  answer: string,
  transcript: string,
): string {
  return [
    "You are an experienced senior software-engineering interviewer.",
    "Privately evaluate the latest answer against the exact question rubric.",
    "Return only valid JSON.",
    "Ground the scoring in the exact question objective, expected signals, and the candidate's answer.",
    "Do not praise, coach, reveal scores, or invent facts.",
    "Classify the answer with `read`:",
    "  answered  - a genuine attempt that addressed the question",
    "  partial   - a genuine attempt that left the objective largely uncovered",
    "  evasive   - talked at length without engaging the question",
    "  stuck     - did not attempt the question: said they do not know, cannot",
    "              find words, are blanking, or asked to move on.",
    "`stuck` is about the absence of an attempt, never about a weak attempt.",
    "Separately, classify the answer's depth with `answerTier`:",
    "  incorrect  - the core technical claim is wrong",
    "  incomplete - correct as far as it goes, but skips a required part of the objective",
    "  shallow    - technically correct and complete, but lacks the trade-off reasoning or judgment a senior answer needs",
    "  strong     - correct, complete, and shows senior-level judgment and trade-off awareness",
    "Grade `answerTier` against this question's own difficulty and rubric criteria, not a fixed absolute bar.",
    hasSourceEvidenceTarget(question)
      ? ""
      : "Grounding rule: question.evidenceIds is empty, so this is a discovery/general objective. Treat first-person career details in the candidate's answer as newly supplied session evidence. Do not mark them unsupported merely because they were absent from the source profile. Never invent missing details; improved answers may only reuse facts actually supplied by the candidate or already grounded by the question context.",
    `Question: ${JSON.stringify(question)}`,
    `Rubric criteria: ${JSON.stringify(question.rubricCriteria ?? [])}`,
    `Profile: ${JSON.stringify(profile)}`,
    `Transcript: ${transcript}`,
    `Latest answer: ${answer}`,
  ].filter(Boolean).join("\n");
}

/**
 * Evaluates the candidate's answer against the persisted question rubric,
 * privately (spec §6.1) -- this is the assessor half of the turn, on its own
 * for callers that only need a score (for example hydrating historical
 * feedback) without running the full director/interviewer pipeline.
 */
export async function evaluateAnswer(
  question: PlannedQuestion,
  blueprint: InterviewBlueprint | null,
  profile: Pick<ProfileDraft, "role" | "seniority" | "expertise" | "narrative">,
  answer: string,
  transcript: string,
): Promise<GroundedEvaluation> {
  const rubric = groundedQuestion(question, blueprint);
  const result = await modelJson(
    "answer evaluation",
    assessorPrompt(rubric, profile, answer, transcript),
    assessorSchema,
  );
  return result
    ? validateGroundedModelEvaluation(rubric, answer, result.evaluation).evaluation
    : groundedEvaluationFor(rubric, answer);
}

export type SpeakContext = {
  round: InterviewRound;
  policy: ModePolicy;
  competencyName: string | null;
  evidence: EvidenceItem[];
  opportunity: Pick<Opportunity, "company" | "role" | "jobDescription"> | null;
  transcript: string;
  askedPrompts: string[];
  /** Strings the line must not echo; never themselves sent to the model. */
  forbiddenRubricText: string[];
};

/**
 * Structured evidence only. The interviewer refers to what a CV says; it never
 * recites it, and `sourceExcerpt` -- which carries raw CV text including
 * contact details -- is deliberately excluded (spec §11.2).
 */
function evidenceForSpeech(evidence: EvidenceItem[]): Array<Record<string, unknown>> {
  return evidence.slice(0, 6).map((item) => ({
    projectOrEmployer: item.projectOrEmployer,
    ownership: item.ownership,
    technologies: item.technologies,
    decision: item.decision,
    constraint: item.constraint,
    outcome: item.outcome,
  }));
}

function intentInstruction(intent: Intent, policy: ModePolicy): string {
  switch (intent.kind) {
    case "open":
      return "Open a new thread on the subject below. Ask what they worked on, in your own words.";
    case "probe":
      return `Press on "${intent.aspect}" in what they just said: "${intent.basis}". Ask for the missing specific.`;
    case "challenge":
      return `They claimed "${intent.claim}" without support. Ask how they know, without accusing them.`;
    case "rescue":
      return rescueInstruction(intent.style, intent.hook, policy);
    case "advance":
      return "Close the current thread briefly and open the new subject below.";
    case "hypothetical":
      return `Pose one short hypothetical grounded in what they described: "${intent.basis}".`;
    case "candidate-questions":
      return "Signal that you have covered what you wanted and invite their questions.";
    case "close":
      return "Close the conversation.";
  }
}

function rescueInstruction(style: RescueStyle, hook: string | null, policy: ModePolicy): string {
  const acknowledge = policy.acknowledgeStruggle
    ? "Acknowledge the difficulty in at most one short clause first. "
    : "Do not acknowledge the difficulty. ";
  switch (style) {
    case "narrow":
      return `${acknowledge}They could not answer. Ask a much smaller version of the same question.`;
    case "hook":
      return `${acknowledge}They could not answer. Hand them a concrete starting point${hook ? ` from their work on ${hook}` : ""} and ask them to start there.`;
    case "reframe":
      return `${acknowledge}They could not answer. Ask for the same material as a story about one specific occasion.`;
    case "park":
      return `${acknowledge}They could not answer. Say you will come back to it, and move to the new subject below.`;
  }
}

/**
 * Authors one line of interviewer speech for an already-decided intent.
 *
 * This call NEVER receives the objective, expected signals, rubric criteria, or
 * any score. That exclusion is structural, not filtered, and is the fix for the
 * rubric leak that commit 02ec2c1 worked around by removing model-authored
 * questions altogether.
 */
export async function speakIntent(intent: Intent, context: SpeakContext): Promise<string> {
  const prompt = [
    context.round.personaStake,
    `Your manner: ${context.round.register}`,
    `You never raise: ${context.round.outOfScope.join(", ")}.`,
    "Say one thing only, in at most two sentences, ending with exactly one question.",
    "Never quote a CV or job description. Never state the candidate's contact details.",
    context.policy.pushback === "firm"
      ? "Do not praise, encourage, or hint."
      : "You may acknowledge difficulty in one short clause. Never coach or hint at an answer.",
    `Your move: ${intentInstruction(intent, context.policy)}`,
    context.competencyName ? `Subject: ${context.competencyName}` : "",
    `What you know about their work: ${JSON.stringify(evidenceForSpeech(context.evidence))}`,
    context.opportunity
      ? `You are hiring for ${context.opportunity.role} at ${context.opportunity.company}. Role context: ${(context.opportunity.jobDescription ?? "").slice(0, 1200)}`
      : "",
    `Conversation so far:\n${context.transcript}`,
    context.askedPrompts.length
      ? `Already asked -- do not repeat or paraphrase these:\n${context.askedPrompts.join("\n")}`
      : "",
  ].filter(Boolean).join("\n");

  const lineContext = {
    forbiddenRubricText: context.forbiddenRubricText,
    askedPrompts: context.askedPrompts,
    policy: context.policy,
  };

  for (let attempt = 0; attempt < 2; attempt += 1) {
    const result = await modelJson(
      "interviewer line",
      attempt === 0 ? prompt : `${prompt}\nYour previous attempt broke a rule. Try again, shorter and more direct.`,
      interviewerLineSchema,
    );
    if (!result) break;
    const violation = validateInterviewerLine(result.line, lineContext);
    if (!violation) return result.line.trim();
    console.warn("[gemini] interviewer line rejected", { operation: "interviewer line", intent: intent.kind, violation });
  }

  return deterministicLine(intent, context.competencyName);
}

export type NextTurnInput = {
  profile: Pick<ProfileDraft, "role" | "seniority" | "expertise" | "narrative">;
  session: InterviewSession;
  answeredQuestion: PlannedQuestion;
  answer: string;
  blueprint: InterviewBlueprint;
  evidence: EvidenceItem[];
  opportunity: Pick<Opportunity, "company" | "role" | "jobDescription"> | null;
};

export type NextTurnResult = {
  /** Null when the candidate did not attempt the question (spec §3.4). */
  evaluation: GroundedEvaluation | null;
  nonAnswer: boolean;
  intent: Intent;
  prompt: string;
  assistance: AssistanceRecord | null;
  targetId: string | null;
  degraded: boolean;
  /** How this turn finishes the answered row without an answer, or null. */
  setAside: SetAsideReason | null;
};

function targetById(blueprint: InterviewBlueprint, id: string | null): CoverageTarget | null {
  return blueprint.targets.find((target) => target.id === id) ?? null;
}

function forbiddenRubricText(blueprint: InterviewBlueprint): string[] {
  return blueprint.targets.flatMap((target) => [target.objective, ...target.expectedSignals, ...target.rubricCriteria]);
}

/**
 * Prompts already put to the candidate, fed to `speakIntent` so the model
 * avoids repeating a question (spec §11.2).
 *
 * Restricted to rows this pipeline authored (`askedIntent !== null`) rather
 * than every persisted prompt: a pre-release row's prompt was authored by the
 * deleted `promptForPlan`, which interpolates a raw CV excerpt. Feeding that
 * text back into the interviewer prompt would leak CV/PII content -- exactly
 * what the assessor/interviewer split (Task 6) exists to keep out of this call
 * (controller ruling 10). `askedIntent` is a structural marker of "this
 * pipeline authored it", not a text filter, so it stays correct even if a
 * legacy prompt happens not to contain any excerpt-shaped text.
 */
function askedPromptsOf(session: InterviewSession): string[] {
  return session.questions
    .filter((question) => question.askedIntent !== null)
    .map((question) => question.prompt)
    .filter((prompt): prompt is string => Boolean(prompt));
}

/**
 * Runs one full turn: assess privately, decide deterministically, then speak.
 *
 * The order is load-bearing. The director never sees model-authored prose, and
 * the interviewer never sees the rubric.
 */
export async function nextTurn(input: NextTurnInput): Promise<NextTurnResult> {
  const { blueprint, session, answeredQuestion, answer } = input;
  const round = roundFor(blueprint.roundId);
  const policy = modePolicyFor(session.mode);
  const rubric = groundedQuestion(answeredQuestion, blueprint);
  const transcript = session.messages.map((message) => `${message.role}: ${message.content}`).join("\n");

  const assessment = await modelJson(
    "answer evaluation",
    assessorPrompt(rubric, input.profile, answer, transcript),
    assessorSchema,
  );
  let degraded = assessment === null;

  const read: AssessmentRead = assessment?.read ?? "answered";
  const nonAnswer = read === "stuck";
  const evaluation = nonAnswer
    ? null
    : assessment
      ? validateGroundedModelEvaluation(rubric, answer, assessment.evaluation).evaluation
      : groundedEvaluationFor(rubric, answer);

  const currentTargetId = answeredQuestion.askedIntent ? targetIdOf(answeredQuestion.askedIntent) : null;
  const states = deriveCoverageState(blueprint.targets, session.questions, session.evaluations);

  const decision = decideIntent({
    round,
    policy,
    states,
    currentTargetId,
    read,
    unsupportedClaims: evaluation?.unsupportedClaims ?? [],
    answer,
    turnsUsed: session.questions.filter((question) => question.answer !== null).length,
    turnBudget: blueprint.turnBudget,
    sessionRescues: rescuesSpentInSession(session.questions),
    canContinueCurrentTarget: canContinueOnAnsweredRow(session.questions, answeredQuestion, blueprint),
    // The director stays pure -- it never reads the clock itself (controller
    // ruling 5). This is the one call site allowed to.
    now: new Date().toISOString(),
  });

  const nextTargetId = targetIdOf(decision.intent);
  const nextTarget = targetById(blueprint, nextTargetId);

  const prompt = await speakIntent(decision.intent, {
    round,
    policy,
    competencyName: nextTarget?.competencyName ?? null,
    evidence: input.evidence,
    opportunity: input.opportunity,
    transcript,
    askedPrompts: askedPromptsOf(session),
    forbiddenRubricText: forbiddenRubricText(blueprint),
  });

  if (prompt === deterministicLine(decision.intent, nextTarget?.competencyName ?? null)) degraded = true;

  return {
    evaluation,
    nonAnswer,
    intent: decision.intent,
    prompt,
    assistance: decision.assistance,
    targetId: nextTargetId,
    degraded,
    setAside: decision.setAside,
  };
}

/** Authors the session's first question. Replaces `initialQuestion`. */
export async function openingTurn(input: Omit<NextTurnInput, "answeredQuestion" | "answer">): Promise<{
  intent: Intent;
  prompt: string;
  targetId: string;
}> {
  const { blueprint, session } = input;
  const round = roundFor(blueprint.roundId);
  const first = blueprint.targets[0];
  if (!first) throw new Error("An interview blueprint needs at least one coverage target.");
  const intent: Intent = { kind: "open", targetId: first.id };

  const prompt = await speakIntent(intent, {
    round,
    policy: modePolicyFor(session.mode),
    competencyName: first.competencyName,
    evidence: input.evidence,
    opportunity: input.opportunity,
    transcript: "",
    askedPrompts: [],
    forbiddenRubricText: forbiddenRubricText(blueprint),
  });

  return { intent, prompt, targetId: first.id };
}

// The hands-on exercise remains intentionally React-specific for now; the
// broader profile and interview flow are generalized across engineering roles,
// but this task still uses a single production-minded frontend exercise.
export function handsOnExercise(profile: Pick<Profile, "role">): HandsOnExercise {
  return { title: "Accessible product search", durationMinutes: 60, briefing: `You are joining a product team building a catalog experience. Implement a production-minded React + TypeScript search component appropriate for a ${profile.role ?? "frontend engineer"}. You may work from the starter and explain decisions as you go.`, requirements: ["Fetch matching products from /api/products?q=… after the user pauses typing.", "Show clear loading, empty, and recoverable error states.", "Prevent stale responses from replacing newer results.", "Make suggestions navigable with the keyboard and understandable to assistive technology.", "Keep component responsibilities and TypeScript models deliberate."], starterCode: handsOnStarter, interviewerOpening: "Start by reading the brief, then tell me which requirements you would clarify before you begin implementing." };
}

export async function handsOnCheckpoint(profile: Pick<Profile, "role" | "seniority">, session: InterviewSession, code: string, note: string) {
  const count = session.checkpoints.length;
  const transcript = session.messages.slice(-6).map((message) => `${message.role}: ${message.content}`).join("\n");
  const result = await modelJson("hands-on checkpoint", `You are a senior frontend interviewer observing a live coding exercise. Do not provide code or solve the exercise. Ask one short, probing interviewer question grounded in the candidate's latest code and note. ${count >= 1 ? "Introduce this new requirement once: the API may return 50,000 results, and keyboard navigation must remain smooth." : "Focus on their current reasoning."}\nProfile: ${JSON.stringify({ role: profile.role, seniority: profile.seniority })}\nRecent transcript:\n${transcript}\nCandidate note: ${note}\nLatest code:\n${code}`, z.object({ question: z.string() }));
  if (result) return result.question;
  if (count === 0) return "Talk me through the request lifecycle. What prevents a slow earlier response from overwriting the newest search?";
  if (count === 1) return "New constraint: results can reach 50,000 items. What would you change so keyboard navigation and rendering stay responsive?";
  return "Before we wrap, what would you test first, and which accessibility behavior would you validate manually?";
}

function scoreCode(code: string, patterns: RegExp[], baseline: number) { return Number(Math.min(9.5, baseline + patterns.filter((pattern) => pattern.test(code)).length * 0.7).toFixed(1)); }

export function evaluateHandsOn(session: InterviewSession) {
  const checkpoint = session.checkpoints.at(-1);
  const code = checkpoint?.code ?? (typeof session.exercise.starterCode === "string" ? session.exercise.starterCode : "");
  const implementation = scoreCode(code, [/useState/, /useEffect|fetch\(/, /AbortController|signal/, /catch|error/i], 4.4);
  const architecture = scoreCode(code, [/type\s+\w+|interface\s+\w+/, /function\s+\w+|const\s+\w+\s*=\s*\(/, /useRef|useMemo|useCallback/], 4.8);
  const accessibility = scoreCode(code, [/aria-/, /role=/, /onKeyDown/, /aria-activedescendant|aria-selected/], 4.0);
  const testing = scoreCode(code, [/describe\(|it\(|test\(|expect\(/, /userEvent|fireEvent/, /msw|mock/], 3.8);
  const communication = Number(Math.min(9, 5 + Math.min(3, (checkpoint?.note.trim().length ?? 0) / 100) + Math.min(1, session.checkpoints.length * 0.25)).toFixed(1));
  const makeEvaluation = (competency: string, score: number, strength: string, needsWork: string): Evaluation => ({
    competencyId: null,
    competency,
    score,
    dimensions: {},
    strengths: [strength],
    needsWork: [needsWork],
    missingPoints: [`Add one concrete example that shows ${competency.toLowerCase()} in action.`],
    betterStructure: ["Lead with the requirement, then explain the implementation choice and result."],
    improvedAnswer: `I would begin with the requirement, describe the implementation choice for ${competency.toLowerCase()}, and close with the result or trade-off.`,
  });
  const evaluations = [
    makeEvaluation("React architecture", architecture, architecture >= 6 ? "The component structure and types show a deliberate separation of concerns." : "You established a workable component starting point.", "Name state ownership and extraction boundaries explicitly as the feature grows."),
    makeEvaluation("TypeScript", architecture, "The solution keeps the domain model visible in the implementation.", "Use types to make loading, error, and selection states impossible to confuse."),
    makeEvaluation("Accessibility", accessibility, accessibility >= 6 ? "Keyboard and assistive-technology concerns appear in the implementation." : "The labelled input is a useful accessible starting point.", "Cover listbox semantics, focus movement, and announcing result changes before considering the work complete."),
    makeEvaluation("Testing", testing, testing >= 6 ? "The code indicates attention to observable behavior." : "You identified testing as a final validation step.", "Add tests for debounce/cancellation, keyboard selection, and failure states—not only the happy path."),
    makeEvaluation("Communication", communication, "Your checkpoint notes make your implementation reasoning inspectable.", "State the trade-off before implementation details when you narrate the next iteration."),
  ];
  const overallScore = Number((evaluations.reduce((total, item) => total + item.score, 0) / evaluations.length).toFixed(1));
  const weakest = [...evaluations].sort((a, b) => a.score - b.score)[0];
  return { evaluations, overallScore, summary: `Hiring signal: ${overallScore >= 7.5 ? "likely advance" : overallScore >= 6 ? "mixed—probe in a follow-up" : "not yet ready"}. Your implementation signal was ${implementation}/10. The clearest next improvement is ${weakest.competency.toLowerCase()}: ${weakest.needsWork[0]}` };
}

export function completeSession(session: InterviewSession) {
  const scores = session.evaluations.map((evaluation) => evaluation.score);
  const overallScore = scores.length ? Number((scores.reduce((sum, score) => sum + score, 0) / scores.length).toFixed(1)) : 0;
  const weakest = [...session.evaluations].sort((a, b) => a.score - b.score)[0];
  return { overallScore, summary: weakest ? `Your answers were strongest when grounded in experience. Focus next on ${weakest.competency.toLowerCase()}: make your structure and trade-offs explicit before discussing implementation.` : "Complete a few questions to receive personalized feedback." };
}

export function updateCompetencies(profile: Profile, evaluations: Evaluation[]): Profile {
  const competencies = [...profile.competencies];
  for (const evaluation of evaluations) {
    const index = competencies.findIndex((item) => item.id === evaluation.competencyId || item.name.toLowerCase() === evaluation.competency.toLowerCase());
    if (index >= 0) competencies[index] = applyEvaluation(competencies[index], evaluation, new Date().toISOString());
  }
  return { ...profile, competencies };
}
