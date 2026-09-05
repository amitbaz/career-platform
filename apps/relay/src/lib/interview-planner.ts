import { roundFor } from "@/lib/interview-rounds";
import type {
  BlueprintQuestion,
  Competency,
  CompetencyScope,
  CoverageTarget,
  Difficulty,
  EvidenceItem,
  InterviewBlueprint,
  Opportunity,
  PlannedQuestion,
  ProfileDraft,
  ProfileReadiness,
  QuestionCategory,
  RoundId,
} from "@/lib/types";

const difficulties: Difficulty[] = ["foundational", "intermediate", "senior", "advanced"];
const categories: QuestionCategory[] = ["introduction", "experience", "technical", "architecture", "behavioral"];
// Same order as `categories` today, kept as its own const because the discovery backbone is
// independently pinned by `validateInterviewBlueprint`/the discovery tests -- it is free to
// diverge from `categories` if the legacy plan's ordering ever changes.
const discoveryCategories: QuestionCategory[] = ["introduction", "experience", "technical", "architecture", "behavioral"];
const plannerTimestamp = "1970-01-01T00:00:00.000Z";
const defaultMaxFollowUps = 3;
export const defaultMaxQuestions = 8;

function normalizedSeniority(seniority: string): Difficulty {
  const value = seniority.toLowerCase();
  if (/staff|principal|lead|advanced/.test(value)) return "advanced";
  if (/senior/.test(value)) return "senior";
  if (/junior|entry|graduate|foundational/.test(value)) return "foundational";
  return "intermediate";
}

function boundedDifficulty(index: number): Difficulty {
  return difficulties[Math.max(0, Math.min(difficulties.length - 1, index))];
}

function weakness(competency: Competency): number {
  if (!competency.estimatedLevel) return 0;
  return Math.max(
    0,
    (difficulties.indexOf(competency.expectedLevel) - difficulties.indexOf(competency.estimatedLevel)) /
      (difficulties.length - 1),
  );
}

function uncertainty(competency: Competency): number {
  if (competency.confidence === null) return 1;
  if (competency.confidence === "low") return 0.75;
  if (competency.confidence === "medium") return 0.4;
  return 0;
}

function staleness(competency: Competency, referenceTime: number): number {
  if (!competency.lastPracticedAt) return 1;
  const timestamp = Date.parse(competency.lastPracticedAt);
  if (Number.isNaN(timestamp)) return 1;
  return Math.max(0, Math.min(1, (referenceTime - timestamp) / (365 * 24 * 60 * 60 * 1000)));
}

function priority(competency: Competency, referenceTime: number): number {
  return competency.relevance * 0.45
    + weakness(competency) * 0.25
    + uncertainty(competency) * 0.2
    + staleness(competency, referenceTime) * 0.1;
}

function matchesCategory(competency: Competency, category: QuestionCategory): boolean {
  const name = competency.name.toLowerCase();
  if (category === "architecture") return /architecture|system\s*design/.test(name);
  if (category === "technical") return !/architecture|system\s*design|communication|behavior/.test(name);
  if (category === "behavioral") return /communication|collaboration|leadership|behavior/.test(name);
  return true;
}

function selectCompetency(
  competencies: Competency[],
  category: QuestionCategory,
  previousCompetencyId: string | null,
  referenceTime: number,
): Competency | null {
  const eligible = competencies.filter((competency) => competency.id !== previousCompetencyId);
  const categoryMatches = eligible.filter((competency) => matchesCategory(competency, category));
  const candidates = categoryMatches.length ? categoryMatches : eligible;

  return [...candidates].sort((left, right) => priority(right, referenceTime) - priority(left, referenceTime) || left.id.localeCompare(right.id))[0] ?? null;
}

function categorySignals(category: QuestionCategory): string[] {
  if (category === "introduction") return ["role summary", "recent ownership"];
  if (category === "experience") return ["role", "decision", "impact"];
  if (category === "technical") return ["decision", "trade-off", "constraint"];
  if (category === "architecture") return ["requirements", "constraints", "approach"];
  return ["collaboration", "decision", "impact"];
}

function categoryMissingSignalPrompts(category: QuestionCategory, subject: string): string[] {
  if (category === "introduction") return [`Name the recent engineering area you owned in ${subject}.`];
  if (category === "experience") return ["Name the measurable outcome or impact."];
  if (category === "technical") return ["What option did you reject and why?"];
  if (category === "architecture") return ["Which requirement or constraint changed the design?"];
  return ["Who did you need alignment from and how did you get it?"];
}

function categoryRubricCriteria(category: QuestionCategory, subject: string): string[] {
  if (category === "introduction") {
    return [
      "Establish the candidate's recent engineering ownership.",
      `Keep the summary grounded in ${subject}.`,
      "Do not drift into unrelated background details.",
    ];
  }
  if (category === "experience") {
    return [
      `Name the project or work example in ${subject}.`,
      "Describe the candidate's role and ownership.",
      "Explain the decision, trade-off, and outcome.",
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
  return [
    `Name the collaboration challenge around ${subject}.`,
    "Describe how the team aligned on the decision.",
    "State what changed because of the collaboration.",
  ];
}

function fallbackCandidateCompetencies(
  category: QuestionCategory,
  competencies: Competency[],
): Competency[] {
  if (category === "introduction") return [];
  if (category === "experience" || category === "technical") {
    return competencies.filter((competency) => !/architecture|system\s*design|communication|behavior/i.test(competency.name));
  }
  if (category === "architecture") {
    return competencies.filter((competency) => /architecture|system\s*design/i.test(competency.name));
  }
  if (category === "behavioral") {
    const behavioral = competencies.filter((competency) => /communication|collaboration|leadership|behavior/i.test(competency.name));
    if (behavioral.length) return behavioral;
    const architecture = competencies.filter((competency) => /architecture|system\s*design/i.test(competency.name));
    if (architecture.length) return architecture;
  }
  return competencies;
}

function normalizeTokens(value: string): string[] {
  return value
    .toLowerCase()
    .split(/[^a-z0-9+.#-]+/)
    .map((token) => token.replace(/^[^a-z0-9]+|[^a-z0-9]+$/g, ""))
    .filter((token) => token.length > 2);
}

function evidenceSignals(item: EvidenceItem): string {
  return [
    item.sourceExcerpt,
    item.projectOrEmployer ?? "",
    item.ownership ?? "",
    item.decision ?? "",
    item.constraint ?? "",
    item.outcome ?? "",
    item.technologies.join(" "),
  ].join(" ").toLowerCase();
}

function categoryEvidenceKeywords(category: QuestionCategory): string[] {
  if (category === "introduction") return [];
  if (category === "experience") return ["project", "migration", "ownership", "impact", "launch", "delivery", "checkout"];
  if (category === "technical") return ["technical", "implementation", "trade", "performance", "bundle", "api", "route", "state", "react"];
  if (category === "architecture" || category === "system-design") return ["architecture", "system", "design", "scalability", "reliability", "observability", "dashboard", "release", "incident"];
  return ["collaboration", "alignment", "stakeholder", "team", "decision", "conflict", "impact"];
}

function scoreEvidenceForQuestion(question: PlannedQuestion, item: EvidenceItem): number {
  if (question.category === "introduction") return 0;
  const signals = evidenceSignals(item);
  const competencyName = question.competencyName?.toLowerCase() ?? "";
  const questionTokens = normalizeTokens([
    question.competencyName ?? "",
    question.prompt,
    question.category,
  ].join(" "));
  const competencyTokens = normalizeTokens(question.competencyName ?? "");
  const categoryKeywords = categoryEvidenceKeywords(question.category);

  let score = 0;
  for (const token of competencyTokens) {
    if (signals.includes(token)) score += 4;
  }
  for (const token of questionTokens) {
    if (signals.includes(token)) score += 2;
  }
  for (const token of categoryKeywords) {
    if (signals.includes(token)) score += 1;
  }
  if (competencyName && item.technologies.some((technology) => competencyName.includes(technology.toLowerCase()) || technology.toLowerCase().includes(competencyName))) {
    score += 3;
  }
  if (item.projectOrEmployer?.trim()) score += 1;
  if (item.ownership?.trim() || item.decision?.trim() || item.constraint?.trim() || item.outcome?.trim()) score += 1;
  return score;
}

function scoreCompetencyForCategory(
  category: QuestionCategory,
  competency: Competency,
  evidence: EvidenceItem[],
): number {
  const question: PlannedQuestion = {
    id: `fallback-${category}-${competency.id}`,
    sequence: 0,
    category,
    competencyId: competency.id,
    competencyName: competency.name,
    difficulty: chooseDifficulty(competency, competency.expectedLevel),
    isFollowUp: false,
    // Prompt text is authored live by the interviewer call (spec §9.1); this
    // synthetic question exists only to score evidence relevance, never to
    // be shown, so it carries no prompt.
    prompt: null,
    answer: null,
    createdAt: plannerTimestamp,
    // Synthetic scoring-only question: never asked, so it never went through
    // the director's intent pipeline.
    askedIntent: null,
    assistance: [],
    nonAnswer: false,
    setAsideAt: null,
    setAsideReason: null,
    nonAnswers: [],
  };
  const evidenceScore = evidence.reduce((best, item) => Math.max(best, scoreEvidenceForQuestion(question, item)), 0);
  return evidenceScore + competency.relevance;
}

function fallbackQuestionPlan(
  category: QuestionCategory,
  sequence: number,
  competencies: Competency[],
  seniority: string,
  evidence: EvidenceItem[],
): PlannedQuestion {
  const candidates = fallbackCandidateCompetencies(category, competencies);
  const selected = [...candidates].sort((left, right) => {
    const scoreDelta = scoreCompetencyForCategory(category, right, evidence)
      - scoreCompetencyForCategory(category, left, evidence);
    if (scoreDelta !== 0) return scoreDelta;
    return left.id.localeCompare(right.id);
  })[0] ?? null;

  return {
    id: `planned-${sequence}-${selected?.id ?? category}`,
    sequence,
    category,
    competencyId: selected?.id ?? null,
    competencyName: selected?.name ?? null,
    difficulty: selected ? chooseDifficulty(selected, seniority) : normalizedSeniority(seniority),
    isFollowUp: false,
    // Prompt text is authored live by the interviewer call, not pre-written here (spec §9.1).
    prompt: null,
    answer: null,
    createdAt: plannerTimestamp,
    // Not yet asked, so it predates the director's intent/assistance pipeline.
    askedIntent: null,
    assistance: [],
    nonAnswer: false,
    setAsideAt: null,
    setAsideReason: null,
    nonAnswers: [],
  };
}

function blueprintObjective(category: QuestionCategory, competencyName: string | null, item: EvidenceItem | null): string {
  const subject = item?.projectOrEmployer ?? competencyName ?? "recent engineering work";
  const prefix = item || category === "introduction" ? "Probe" : "General objective: Probe";
  if (category === "introduction") return "Establish recent engineering ownership.";
  if (category === "experience") return `${prefix} ownership and impact in ${subject}.`;
  if (category === "technical") return `${prefix} the core technical decision behind ${subject}.`;
  if (category === "architecture") return `${prefix} system design choices around ${subject}.`;
  return `${prefix} collaboration and delivery around ${subject}.`;
}

function evidenceForQuestion(question: PlannedQuestion, evidence: EvidenceItem[]): EvidenceItem | null {
  if (!evidence.length || question.category === "introduction") return null;
  let bestEvidence: EvidenceItem | null = null;
  let bestScore = 0;
  for (const item of evidence) {
    const score = scoreEvidenceForQuestion(question, item);
    if (score > bestScore) {
      bestScore = score;
      bestEvidence = item;
    }
  }
  return bestScore > 0 ? bestEvidence : null;
}

function normalizeFollowUpLimit(value: number): number {
  return Math.max(0, Math.min(defaultMaxFollowUps, Math.trunc(value)));
}

function defaultBlueprintQuestion(
  planned: PlannedQuestion,
  item: EvidenceItem | null,
): BlueprintQuestion {
  const evidenceIds = item ? [item.id] : [];
  const competencyName = planned.competencyName;
  return {
    // `planned.prompt` is already null: prompt text is authored live by the
    // interviewer call, not pre-written here (spec §9.1).
    ...planned,
    objective: blueprintObjective(planned.category, competencyName, item),
    evidenceIds,
    expectedSignals: categorySignals(planned.category),
    missingSignalPrompts: categoryMissingSignalPrompts(planned.category, item?.projectOrEmployer ?? competencyName ?? "that work"),
    rubricCriteria: categoryRubricCriteria(planned.category, item?.projectOrEmployer ?? competencyName ?? "that work"),
    followUpLimit: planned.category === "introduction" || planned.category === "behavioral" ? 0 : 1,
    sourceConfidence: item?.confidence ?? null,
  };
}

export function chooseDifficulty(competency: Competency, seniority: string): Difficulty {
  if (!competency.estimatedLevel) return normalizedSeniority(seniority);

  const estimatedIndex = difficulties.indexOf(competency.estimatedLevel);
  if (competency.confidence === "low") return boundedDifficulty(estimatedIndex - 1);
  if (competency.confidence === "high" || (competency.averageScore ?? 0) >= 8) {
    return boundedDifficulty(estimatedIndex + 1);
  }
  return competency.estimatedLevel;
}

/** Builds the legacy five-question backbone using evidence priority relative to the injected current time. */
export function buildInterviewPlan(
  competencies: Competency[],
  seniority: string,
  now: Date = new Date(),
): PlannedQuestion[] {
  let previousCompetencyId: string | null = null;
  const referenceTime = Number.isNaN(now.getTime()) ? Date.now() : now.getTime();

  return categories.map((category, index) => {
    const competency = category === "introduction"
      ? null
      : selectCompetency(competencies, category, previousCompetencyId, referenceTime);
    previousCompetencyId = competency?.id ?? previousCompetencyId;

    const sequence = index + 1;
    return {
      id: `planned-${sequence}-${competency?.id ?? category}`,
      sequence,
      category,
      competencyId: competency?.id ?? null,
      competencyName: competency?.name ?? null,
      difficulty: competency ? chooseDifficulty(competency, seniority) : normalizedSeniority(seniority),
      isFollowUp: false,
      // Prompt text is authored live by the interviewer call, not pre-written here (spec §9.1).
      prompt: null,
      answer: null,
      createdAt: plannerTimestamp,
      // Not yet asked, so it predates the director's intent/assistance pipeline.
      askedIntent: null,
      assistance: [],
      nonAnswer: false,
      setAsideAt: null,
      setAsideReason: null,
      nonAnswers: [],
    };
  });
}

/**
 * Ensures an AI-generated blueprint uses the exact legacy backbone and only
 * owned evidence ids, and that its coverage targets (spec §9.1) are usable by
 * the director: at least one, each with rubric material, unique ids, and at
 * least one marked `required` so a round always has an opening move.
 *
 * `evidence` defaults to `[]` so callers validating a blueprint purely for its
 * coverage-target shape (no legacy evidence-anchored questions in play) don't
 * need to thread it through.
 */
export function validateInterviewBlueprint(
  blueprint: InterviewBlueprint,
  evidence: EvidenceItem[] = [],
): InterviewBlueprint {
  if (blueprint.questions.length !== categories.length) {
    throw new Error("Interview blueprint must contain the exact five-question backbone.");
  }
  if (blueprint.maxFollowUps > defaultMaxFollowUps) {
    throw new Error("Interview blueprint exceeds the follow-up limit.");
  }
  if (blueprint.maxQuestions > defaultMaxQuestions) {
    throw new Error("Interview blueprint exceeds the total-question limit.");
  }

  const knownEvidenceIds = new Set(evidence.map((item) => item.id));
  let totalFollowUpBudget = 0;
  for (const [index, question] of blueprint.questions.entries()) {
    if (question.sequence !== index + 1 || question.category !== categories[index] || question.isFollowUp) {
      throw new Error("Interview blueprint must preserve the exact five-question backbone.");
    }
    if (!question.objective.trim()) throw new Error("Interview blueprint questions need an objective.");
    if (!question.expectedSignals.length) throw new Error("Interview blueprint questions need expected signals.");
    if (!question.missingSignalPrompts.length) throw new Error("Interview blueprint questions need missing-signal prompts.");
    if (!question.rubricCriteria?.length) throw new Error("Interview blueprint questions need scoring criteria.");
    if (normalizeFollowUpLimit(question.followUpLimit) !== question.followUpLimit) {
      throw new Error("Interview blueprint follow-up limits must stay between 0 and 3.");
    }
    if (question.category !== "introduction"
      && question.evidenceIds.length === 0
      && !question.objective.startsWith("General objective:")) {
      throw new Error("Interview blueprint questions without evidence need a clearly labeled general objective.");
    }
    totalFollowUpBudget += question.followUpLimit;
    for (const evidenceId of question.evidenceIds) {
      if (!knownEvidenceIds.has(evidenceId)) {
        throw new Error(`Interview blueprint references unknown evidence ids: ${evidenceId}.`);
      }
    }
  }
  if (totalFollowUpBudget > blueprint.maxFollowUps || blueprint.questions.length + totalFollowUpBudget > blueprint.maxQuestions) {
    throw new Error("Interview blueprint exceeds the total follow-up budget.");
  }

  if (blueprint.targets.length === 0) throw new Error("An interview blueprint needs at least one coverage target.");
  if (!blueprint.targets.some((target) => target.required)) {
    throw new Error("An interview blueprint needs at least one required coverage target.");
  }
  if (new Set(blueprint.targets.map((target) => target.id)).size !== blueprint.targets.length) {
    throw new Error("Interview blueprint coverage targets need unique ids.");
  }
  for (const target of blueprint.targets) {
    if (!target.rubricCriteria.length) throw new Error("Every coverage target needs rubric criteria.");
  }

  return blueprint;
}

/**
 * Builds what the round must find out, not what it will say. Prompt text is
 * authored live by the interviewer call (spec §9.1).
 *
 * When anchored to an opportunity, every gap becomes a required target: a tech
 * lead's real agenda is the places the candidate looks thin against the spec,
 * and that list is already computed.
 *
 * Every target carries a 3-element `expectedSignals` array. This is load-bearing,
 * not incidental: `deriveCoverageState`'s `statusFor` (`src/lib/interview-coverage.ts`)
 * can never mark a target `satisfied` when `expectedSignals` is empty, so an
 * empty array would strand that target open for the rest of the round.
 */
export function buildCoverageTargets(
  profile: Pick<ProfileDraft, "role" | "competencies">,
  evidence: EvidenceItem[],
  opportunity: Pick<Opportunity, "gaps" | "jobDescription"> | null,
  roundId: RoundId,
): CoverageTarget[] {
  const round = roundFor(roundId);
  const gapTargets: CoverageTarget[] = (opportunity?.gaps ?? []).map((gap, index) => ({
    id: `gap-${index}`,
    competencyId: null,
    competencyName: gap,
    category: "experience",
    evidenceIds: [],
    difficulty: "senior",
    objective: `Establish whether the candidate has real experience with ${gap}.`,
    expectedSignals: [gap, "ownership", "outcome"],
    rubricCriteria: [
      `Name a concrete example involving ${gap}.`,
      "Describe the decision they personally made.",
      "Explain the outcome or trade-off.",
    ],
    required: true,
  }));

  const competencyTargets: CoverageTarget[] = [...profile.competencies]
    .sort((left, right) => right.relevance - left.relevance)
    .slice(0, Math.max(1, 5 - gapTargets.length))
    .map((competency, index) => ({
      id: `competency-${index}`,
      // ProfileDraft's competency scope carries only a name and relevance, no
      // stable id, so this stays null until a real Competency is threaded through.
      competencyId: null,
      competencyName: competency.name,
      category: "experience",
      evidenceIds: evidence
        .filter((item) => item.technologies.some((tech) => competency.name.toLowerCase().includes(tech.toLowerCase())))
        .map((item) => item.id),
      difficulty: "senior",
      objective: `Establish the candidate's real ownership within ${competency.name}.`,
      expectedSignals: [competency.name, "ownership", "outcome"],
      rubricCriteria: [
        `Name a concrete example from ${competency.name}.`,
        "Describe the ownership or decision involved.",
        "Explain the outcome or trade-off.",
      ],
      required: index === 0,
    }));

  const all = [...gapTargets, ...competencyTargets];
  // A round with no repertoire for a category cannot cover it; drop rather than
  // plan something the director may never issue.
  return round.moves.includes("open") ? all : [];
}

/**
 * Builds a deterministic fallback blueprint when the model response is absent
 * or invalid. The resulting plan remains evidence-backed where possible and is
 * explicitly marked as limited grounding.
 */
export function buildFallbackInterviewBlueprint(
  profile: ProfileDraft,
  competencies: Competency[],
  evidence: EvidenceItem[],
  now: Date = new Date(),
  fallbackReason = "Gemini returned invalid blueprint JSON after one repair attempt.",
): InterviewBlueprint {
  const seniority = profile.seniority ?? "Intermediate";
  const plan = categories.map((category, index) => fallbackQuestionPlan(
    category,
    index + 1,
    competencies,
    seniority,
    evidence,
  ));
  return {
    status: "limited-grounding",
    fallbackReason,
    maxFollowUps: defaultMaxFollowUps,
    maxQuestions: defaultMaxQuestions,
    createdAt: now.toISOString(),
    questions: plan.map((question) => defaultBlueprintQuestion(question, evidenceForQuestion(question, evidence))),
    // `generateInterviewBlueprint`'s `withCoveragePlan` is the single merge
    // point that overwrites roundId/turnBudget/targets with the real
    // round-derived values before this blueprint reaches a caller; these are
    // inert placeholders that satisfy the type only.
    roundId: "tech-lead",
    turnBudget: defaultMaxQuestions,
    targets: [],
  };
}

/** Adds one persisted follow-up while preserving the total eight-question cap. */
export function appendFollowUp(plan: PlannedQuestion[], followUp: PlannedQuestion): PlannedQuestion[] {
  if (plan.length >= defaultMaxQuestions) return plan;

  return [...plan, {
    ...followUp,
    sequence: plan.length + 1,
    isFollowUp: true,
  }];
}

/** Picks the highest-relevance competency scope name to anchor discovery prompts, or null when the profile lists none. */
function selectDiscoveryScopeName(competencies: CompetencyScope[]): string | null {
  if (!competencies.length) return null;
  return [...competencies].sort((left, right) => right.relevance - left.relevance || left.name.localeCompare(right.name))[0].name;
}

/**
 * Discovery-oriented objective text. Every non-introduction question carries
 * the "General objective:" prefix `validateInterviewBlueprint` requires for
 * evidence-free questions -- discovery questions are always evidence-free
 * (see `buildExperienceDiscoveryBlueprint`), so there is no evidence-anchored
 * variant to branch on here.
 */
function discoveryObjective(category: QuestionCategory): string {
  if (category === "introduction") return "Learn about the candidate's recent focus and background.";
  const prefix = "General objective: Surface";
  if (category === "experience") return `${prefix} one concrete example of real work the candidate can describe in detail.`;
  if (category === "technical") return `${prefix} a real technical problem or decision the candidate can walk through.`;
  if (category === "architecture") return `${prefix} the requirements and constraints behind a real system or feature.`;
  return `${prefix} how the candidate handled collaboration, ambiguity, or delivery pressure.`;
}

function discoveryMissingSignalPrompts(category: QuestionCategory): string[] {
  if (category === "introduction") return ["Name the kind of work you have mainly been doing recently."];
  if (category === "experience") return ["Name the specific project, team, or task this was part of."];
  if (category === "technical") return ["Name the concrete problem, option, or constraint you dealt with."];
  if (category === "architecture") return ["Name the requirement or constraint that mattered most."];
  return ["Name who was involved and what changed as a result."];
}

/**
 * Discovery-oriented rubric criteria. Unlike `categoryRubricCriteria`, these
 * name no project or subject -- discovery questions never carry an evidence
 * anchor, so a rubric criterion naming a project the prompt never mentioned
 * would mislead both the evaluator and the transcript UI (see the final
 * review finding this fixes).
 */
function discoveryRubricCriteria(category: QuestionCategory): string[] {
  if (category === "introduction") {
    return [
      "Establish what kind of engineering work the candidate has mainly been doing recently.",
      "Keep the summary grounded in what the candidate actually says.",
      "Do not drift into unrelated background details.",
    ];
  }
  if (category === "experience") {
    return [
      "Surface one real piece of work the candidate can describe from memory.",
      "Describe the candidate's personal responsibility in it.",
      "Explain what happened and why it mattered, using only what the candidate said.",
    ];
  }
  if (category === "technical") {
    return [
      "Surface a real technical problem or decision the candidate can walk through.",
      "Explain the option, constraint, or trade-off the candidate considered.",
      "Describe the result, using only what the candidate said.",
    ];
  }
  if (category === "architecture") {
    return [
      "Surface the requirements or constraints behind a real system or feature.",
      "Describe how the technical approach took shape.",
      "State the outcome, using only what the candidate said.",
    ];
  }
  return [
    "Surface a real collaboration, ambiguity, or delivery-pressure challenge.",
    "Describe what the candidate did about it.",
    "State what changed as a result, using only what the candidate said.",
  ];
}

/**
 * Builds the deterministic five-question discovery backbone for a profile
 * whose source evidence is too sparse for grounded, evidence-anchored
 * questions (`assessProfileReadiness` returned `ready: false`).
 *
 * Discovery questions never anchor to `evidence`, even when a partial match
 * scores > 0 against `scoreEvidenceForQuestion`. That matcher is permissive
 * enough that the commonest sparse shape -- one real project that failed the
 * two-example readiness threshold -- would score positively against nearly
 * every non-introduction question, anchoring all four of them to the same
 * evidence id despite prompts that never mention it (spec §6.1's discovery
 * prompts are deliberately generic). Keeping that anchor would also silently
 * disable `hasSourceEvidenceTarget`'s discovery-answer grounding protection
 * for exactly the questions it exists to protect. So every discovery
 * question gets `evidenceIds: []`, `sourceConfidence: null`, and rubric
 * criteria that name no project -- see spec §6.2: "Questions without a safe
 * source anchor use a general objective and `evidenceIds: []`."
 */
export function buildExperienceDiscoveryBlueprint(
  profile: Pick<ProfileDraft,
    | "role"
    | "seniority"
    | "summary"
    | "narrative"
    | "expertise"
    | "characteristics"
    | "competencies"
  >,
  evidence: EvidenceItem[],
  readiness: ProfileReadiness,
  now?: Date,
): InterviewBlueprint {
  const createdAt = (now ?? new Date()).toISOString();
  const selectedScopeName = selectDiscoveryScopeName(profile.competencies);

  const questions: BlueprintQuestion[] = discoveryCategories.map((category, index) => {
    const sequence = index + 1;
    return {
      id: `discovery-${sequence}-${category}`,
      sequence,
      category,
      competencyId: null,
      competencyName: category === "introduction" ? null : selectedScopeName,
      difficulty: normalizedSeniority(profile.seniority ?? ""),
      isFollowUp: false,
      // Prompt text is authored live by the interviewer call (spec §9.1).
      prompt: null,
      answer: null,
      createdAt,
      objective: discoveryObjective(category),
      evidenceIds: [],
      expectedSignals: categorySignals(category),
      missingSignalPrompts: discoveryMissingSignalPrompts(category),
      rubricCriteria: discoveryRubricCriteria(category),
      followUpLimit: category === "introduction" || category === "behavioral" ? 0 : 1,
      sourceConfidence: null,
      // Not yet asked, so it predates the director's intent/assistance pipeline.
      askedIntent: null,
      assistance: [],
      nonAnswer: false,
      setAsideAt: null,
      setAsideReason: null,
      nonAnswers: [],
    };
  });

  return {
    status: "limited-grounding",
    fallbackReason: readiness.missing.length > 0
      ? `Your source profile has limited concrete example detail (${readiness.missing.join(", ")}), so this session starts broader and helps you uncover real projects, ownership, decisions, and outcomes as you answer.`
      : "Your source profile has limited concrete example detail, so this session starts broader and helps you uncover real engineering examples as you answer.",
    maxFollowUps: defaultMaxFollowUps,
    maxQuestions: defaultMaxQuestions,
    createdAt,
    questions,
    // `generateInterviewBlueprint`'s `withCoveragePlan` is the single merge
    // point that overwrites roundId/turnBudget/targets with the real
    // round-derived values before this blueprint reaches a caller; these are
    // inert placeholders that satisfy the type only.
    roundId: "tech-lead",
    turnBudget: defaultMaxQuestions,
    targets: [],
  };
}
