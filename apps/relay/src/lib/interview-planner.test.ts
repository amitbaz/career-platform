import { describe, expect, it } from "vitest";
import {
  appendFollowUp,
  buildCoverageTargets,
  buildExperienceDiscoveryBlueprint,
  buildFallbackInterviewBlueprint,
  buildInterviewPlan,
  chooseDifficulty,
  validateInterviewBlueprint,
} from "@/lib/interview-planner";
import type {
  BlueprintQuestion,
  Competency,
  CoverageTarget,
  EvidenceItem,
  InterviewBlueprint,
  Opportunity,
  PlannedQuestion,
  ProfileDraft,
  QuestionCategory,
} from "@/lib/types";

const weakSystemDesign: Competency = {
  id: "system-design",
  name: "System design",
  relevance: 0.95,
  expectedLevel: "senior",
  estimatedLevel: "foundational",
  confidence: "low",
  lastPracticedAt: "2025-01-01T00:00:00.000Z",
  questionCount: 4,
  averageScore: 4,
  recentScore: 3,
  strengths: [],
  weaknesses: ["Clarify requirements before proposing architecture."],
};

const strongReact: Competency = {
  id: "react",
  name: "React",
  relevance: 0.9,
  expectedLevel: "senior",
  estimatedLevel: "senior",
  confidence: "high",
  lastPracticedAt: "2026-08-20T00:00:00.000Z",
  questionCount: 8,
  averageScore: 9,
  recentScore: 9,
  strengths: ["Explains state ownership clearly."],
  weaknesses: [],
};

const unassessedReact: Competency = {
  ...strongReact,
  id: "unassessed-react",
  estimatedLevel: null,
  confidence: null,
  questionCount: 0,
  averageScore: null,
  recentScore: null,
};

const evidence: EvidenceItem[] = [
  {
    id: "evidence-1",
    sourceKind: "cv",
    sourceExcerpt: "Led a React migration for checkout.",
    projectOrEmployer: "Checkout Platform",
    ownership: "Owned the frontend migration end to end.",
    technologies: ["React", "TypeScript"],
    decision: "Split a large route into smaller bundles.",
    constraint: "Tight launch window.",
    outcome: "Cut bundle size by 28%.",
    recency: "2025-02",
    confidence: 0.94,
  },
  {
    id: "evidence-2",
    sourceKind: "cv",
    sourceExcerpt: "Built observability for API regressions.",
    projectOrEmployer: "Reliability Tooling",
    ownership: "Designed the dashboard and alerting flow.",
    technologies: ["Next.js", "Postgres"],
    decision: "Added release health dashboards.",
    constraint: "Small team with limited bandwidth.",
    outcome: "Reduced incident triage time by 35%.",
    recency: "2024-11",
    confidence: 0.91,
  },
];

const profile: ProfileDraft = {
  role: "Frontend Engineer",
  seniority: "Senior",
  summary: "Frontend engineer focused on performance and delivery.",
  narrative: "Owns frontend platforms and reliability work.",
  expertise: ["React", "TypeScript", "Next.js"],
  characteristics: ["Pragmatic"],
  competencies: [
    { name: "React architecture", relevance: 1 },
    { name: "System design", relevance: 0.8 },
  ],
};

const sparseProfile: ProfileDraft = {
  role: "Frontend Engineer",
  seniority: "Senior",
  summary: "Frontend engineer",
  narrative: "Builds React product interfaces.",
  expertise: ["React", "TypeScript"],
  characteristics: ["Pragmatic"],
  competencies: [{ name: "React", relevance: 1 }],
};

// Shared fixtures for the coverage-target tests below. `sampleProfile`
// deliberately mirrors `profile`'s shape (role + competencies only) rather
// than reusing it directly, so it stays valid if `profile` grows fields
// `buildCoverageTargets` doesn't need.
function sampleProfile(): Pick<ProfileDraft, "role" | "competencies"> {
  return { role: profile.role, competencies: profile.competencies };
}

function sampleEvidence(): EvidenceItem[] {
  return evidence;
}

function sampleOpportunity(): Pick<Opportunity, "gaps" | "jobDescription"> {
  return { gaps: [], jobDescription: "Own platform reliability and testing culture." };
}

const backboneCategories: QuestionCategory[] = ["introduction", "experience", "technical", "architecture", "behavioral"];

/** A minimal, valid legacy five-question backbone: no evidence anchoring, no prompt text. */
function sampleBlueprintQuestions(): BlueprintQuestion[] {
  return backboneCategories.map((category, index) => ({
    id: `sample-question-${index + 1}`,
    sequence: index + 1,
    category,
    competencyId: null,
    competencyName: null,
    difficulty: "senior",
    isFollowUp: false,
    prompt: null,
    answer: null,
    createdAt: "2026-08-29T00:00:00.000Z",
    objective: category === "introduction"
      ? "Establish recent engineering ownership."
      : "General objective: Establish real ownership and impact.",
    evidenceIds: [],
    expectedSignals: ["ownership"],
    missingSignalPrompts: ["Name the ownership decision."],
    rubricCriteria: ["Name a concrete example.", "Describe the ownership.", "Explain the outcome."],
    followUpLimit: 0,
    sourceConfidence: null,
    askedIntent: null,
    assistance: [],
    nonAnswer: false,
    setAsideAt: null,
    setAsideReason: null,
    nonAnswers: [],
  }));
}

const singleRequiredTarget: CoverageTarget = {
  id: "target-minimal",
  competencyId: null,
  competencyName: "React",
  category: "experience",
  evidenceIds: [],
  difficulty: "senior",
  objective: "Establish the candidate's ownership of React work.",
  expectedSignals: ["ownership", "outcome"],
  rubricCriteria: ["Name a concrete example.", "Describe the ownership.", "Explain the outcome."],
  required: true,
};

function sampleBlueprint(overrides: Partial<InterviewBlueprint> = {}): InterviewBlueprint {
  return {
    status: "grounded",
    fallbackReason: null,
    maxFollowUps: 3,
    maxQuestions: 8,
    createdAt: "2026-08-29T00:00:00.000Z",
    questions: sampleBlueprintQuestions(),
    roundId: "tech-lead",
    turnBudget: 8,
    targets: [singleRequiredTarget],
    ...overrides,
  };
}

describe("adaptive interview planning", () => {
  it("builds a five-question backbone that prioritizes weak system design", () => {
    const plan = buildInterviewPlan([strongReact, weakSystemDesign], "Senior");

    expect(plan).toHaveLength(5);
    expect(plan.map((question) => question.category)).toEqual([
      "introduction", "experience", "technical", "architecture", "behavioral",
    ]);
    for (let index = 1; index < plan.length; index += 1) {
      const previous = plan[index - 1].competencyId;
      const current = plan[index].competencyId;
      if (previous !== null && current !== null) expect(current).not.toBe(previous);
    }
    expect(plan.find((question) => question.category === "architecture")?.competencyName).toBe("System design");
  });

  it("adjusts difficulty from evidence while preserving unassessed role seniority", () => {
    expect(chooseDifficulty(strongReact, "Senior")).toBe("advanced");
    expect(chooseDifficulty(unassessedReact, "Senior")).toBe("senior");
  });

  it("allows only three follow-ups beyond the backbone", () => {
    const plan = buildInterviewPlan([strongReact, weakSystemDesign], "Senior");
    const followUp: PlannedQuestion = {
      id: "follow-up",
      sequence: 6,
      category: "technical",
      competencyId: strongReact.id,
      competencyName: strongReact.name,
      difficulty: "advanced",
      isFollowUp: true,
      prompt: "What trade-off would change your implementation?",
      answer: null,
      createdAt: "2026-08-29T00:00:00.000Z",
      askedIntent: null,
      assistance: [],
      nonAnswer: false,
      setAsideAt: null,
      setAsideReason: null,
      nonAnswers: [],
    };

    const withOne = appendFollowUp(plan, followUp);
    const withTwo = appendFollowUp(withOne, followUp);
    const withThree = appendFollowUp(withTwo, followUp);

    expect(withThree).toHaveLength(8);
    expect(appendFollowUp(withThree, followUp)).toBe(withThree);
  });

  it("uses the injected current time when ranking practice recency", () => {
    const practicedLongAgo: Competency = {
      ...unassessedReact,
      id: "older-practice",
      name: "JavaScript",
      relevance: 0.72,
      confidence: "high",
      lastPracticedAt: "2026-01-01T00:00:00.000Z",
    };
    const practicedRecently: Competency = {
      ...unassessedReact,
      id: "recent-practice",
      name: "TypeScript",
      relevance: 0.9,
      confidence: "high",
      lastPracticedAt: "2026-12-31T00:00:00.000Z",
    };

    const plan = buildInterviewPlan(
      [practicedRecently, practicedLongAgo],
      "Senior",
      new Date("2027-01-01T00:00:00.000Z"),
    );

    expect(plan[1].competencyName).toBe("JavaScript");
  });

  it("validates a grounded five-question blueprint that only references known evidence", () => {
    const blueprint: InterviewBlueprint = {
      status: "grounded",
      fallbackReason: null,
      maxFollowUps: 3,
      maxQuestions: 8,
      createdAt: "2026-08-29T00:00:00.000Z",
      roundId: "tech-lead",
      turnBudget: 8,
      targets: [singleRequiredTarget],
      questions: [
        {
          id: "question-1",
          sequence: 1,
          category: "introduction",
          competencyId: null,
          competencyName: null,
          difficulty: "senior",
          isFollowUp: false,
          prompt: "Give me a concise introduction to yourself and the frontend work you have owned recently.",
          answer: null,
          createdAt: "2026-08-29T00:00:00.000Z",
          objective: "Establish recent engineering ownership.",
          evidenceIds: [],
          expectedSignals: ["role summary", "recent ownership"],
          missingSignalPrompts: ["Name the most recent engineering area you owned."],
          followUpLimit: 0,
          sourceConfidence: null,
          rubricCriteria: ["Establish the candidate's recent engineering ownership."],
          askedIntent: null,
          assistance: [],
          nonAnswer: false,
          setAsideAt: null,
          setAsideReason: null,
          nonAnswers: [],
        },
        {
          id: "question-2",
          sequence: 2,
          category: "experience",
          competencyId: "react",
          competencyName: "React",
          difficulty: "senior",
          isFollowUp: false,
          prompt: "Tell me about the Checkout Platform migration.",
          answer: null,
          createdAt: "2026-08-29T00:00:00.000Z",
          objective: "Probe the checkout migration ownership and impact.",
          evidenceIds: ["evidence-1"],
          expectedSignals: ["role", "trade-off", "outcome"],
          missingSignalPrompts: ["Name the launch trade-off you accepted."],
          followUpLimit: 1,
          sourceConfidence: 0.94,
          rubricCriteria: [
            "Name the project or work example.",
            "Describe the candidate's role and ownership.",
            "Explain the decision, trade-off, and outcome.",
          ],
          askedIntent: null,
          assistance: [],
          nonAnswer: false,
          setAsideAt: null,
          setAsideReason: null,
          nonAnswers: [],
        },
        {
          id: "question-3",
          sequence: 3,
          category: "technical",
          competencyId: "react",
          competencyName: "React",
          difficulty: "senior",
          isFollowUp: false,
          prompt: "Walk me through the route-splitting decision.",
          answer: null,
          createdAt: "2026-08-29T00:00:00.000Z",
          objective: "Probe the migration trade-off decision.",
          evidenceIds: ["evidence-1"],
          expectedSignals: ["decision", "constraint", "trade-off"],
          missingSignalPrompts: ["What trade-off did you reject?"],
          followUpLimit: 1,
          sourceConfidence: 0.94,
          rubricCriteria: [
            "Name the technical decision being discussed.",
            "Explain the constraint or rejected alternative.",
            "Describe the trade-off and result.",
          ],
          askedIntent: null,
          assistance: [],
          nonAnswer: false,
          setAsideAt: null,
          setAsideReason: null,
          nonAnswers: [],
        },
        {
          id: "question-4",
          sequence: 4,
          category: "architecture",
          competencyId: "system-design",
          competencyName: "System design",
          difficulty: "senior",
          isFollowUp: false,
          prompt: "How did you shape observability for API regressions?",
          answer: null,
          createdAt: "2026-08-29T00:00:00.000Z",
          objective: "Probe observability system design choices.",
          evidenceIds: ["evidence-2"],
          expectedSignals: ["requirements", "signal design", "constraint"],
          missingSignalPrompts: ["Which alert trade-off mattered most?"],
          followUpLimit: 1,
          sourceConfidence: 0.91,
          rubricCriteria: [
            "Explain the requirements or constraints that shaped the design.",
            "Describe the system-level decision or architecture choice.",
            "State the outcome or reliability impact.",
          ],
          askedIntent: null,
          assistance: [],
          nonAnswer: false,
          setAsideAt: null,
          setAsideReason: null,
          nonAnswers: [],
        },
        {
          id: "question-5",
          sequence: 5,
          category: "behavioral",
          competencyId: "system-design",
          competencyName: "System design",
          difficulty: "senior",
          isFollowUp: false,
          prompt: "How did you align the team on the release-health dashboards?",
          answer: null,
          createdAt: "2026-08-29T00:00:00.000Z",
          objective: "Probe cross-functional delivery around observability work.",
          evidenceIds: ["evidence-2"],
          expectedSignals: ["collaboration", "decision", "impact"],
          missingSignalPrompts: ["Who disagreed and how did you resolve it?"],
          followUpLimit: 0,
          sourceConfidence: 0.91,
          rubricCriteria: [
            "Name the collaboration challenge.",
            "Describe how the team aligned on the decision.",
            "State what changed because of the collaboration.",
          ],
          askedIntent: null,
          assistance: [],
          nonAnswer: false,
          setAsideAt: null,
          setAsideReason: null,
          nonAnswers: [],
        },
      ],
    };

    expect(validateInterviewBlueprint(blueprint, evidence)).toEqual(blueprint);
  });

  it("requires non-intro questions without evidence to be labeled as general objectives", () => {
    expect(() => validateInterviewBlueprint({
      status: "grounded",
      fallbackReason: null,
      maxFollowUps: 3,
      maxQuestions: 8,
      createdAt: "2026-08-29T00:00:00.000Z",
      roundId: "tech-lead",
      turnBudget: 8,
      targets: [singleRequiredTarget],
      questions: [
        {
          id: "question-1",
          sequence: 1,
          category: "introduction",
          competencyId: null,
          competencyName: null,
          difficulty: "senior",
          isFollowUp: false,
          prompt: "intro",
          answer: null,
          createdAt: "2026-08-29T00:00:00.000Z",
          objective: "Establish recent engineering ownership.",
          evidenceIds: [],
          expectedSignals: ["signal"],
          missingSignalPrompts: ["prompt"],
          followUpLimit: 0,
          sourceConfidence: null,
          rubricCriteria: ["Establish recent ownership."],
          askedIntent: null,
          assistance: [],
          nonAnswer: false,
          setAsideAt: null,
          setAsideReason: null,
          nonAnswers: [],
        },
        {
          id: "question-2",
          sequence: 2,
          category: "experience",
          competencyId: null,
          competencyName: "React",
          difficulty: "senior",
          isFollowUp: false,
          prompt: "experience",
          answer: null,
          createdAt: "2026-08-29T00:00:00.000Z",
          objective: "Probe the migration ownership and impact.",
          evidenceIds: [],
          expectedSignals: ["signal"],
          missingSignalPrompts: ["prompt"],
          rubricCriteria: ["Name the decision and impact."],
          followUpLimit: 1,
          sourceConfidence: 0.9,
          askedIntent: null,
          assistance: [],
          nonAnswer: false,
          setAsideAt: null,
          setAsideReason: null,
          nonAnswers: [],
        },
        {
          id: "question-3",
          sequence: 3,
          category: "technical",
          competencyId: null,
          competencyName: "React",
          difficulty: "senior",
          isFollowUp: false,
          prompt: "technical",
          answer: null,
          createdAt: "2026-08-29T00:00:00.000Z",
          objective: "General objective: Probe the migration trade-off decision.",
          evidenceIds: [],
          expectedSignals: ["signal"],
          missingSignalPrompts: ["prompt"],
          followUpLimit: 1,
          sourceConfidence: 0.9,
          rubricCriteria: ["Name the technical trade-off.", "Describe the outcome."],
          askedIntent: null,
          assistance: [],
          nonAnswer: false,
          setAsideAt: null,
          setAsideReason: null,
          nonAnswers: [],
        },
        {
          id: "question-4",
          sequence: 4,
          category: "architecture",
          competencyId: null,
          competencyName: "System design",
          difficulty: "senior",
          isFollowUp: false,
          prompt: "architecture",
          answer: null,
          createdAt: "2026-08-29T00:00:00.000Z",
          objective: "Probe the architecture decision.",
          evidenceIds: ["evidence-1"],
          expectedSignals: ["signal"],
          missingSignalPrompts: ["prompt"],
          followUpLimit: 1,
          sourceConfidence: 0.9,
          rubricCriteria: ["Name the constraint.", "Explain the architecture choice."],
          askedIntent: null,
          assistance: [],
          nonAnswer: false,
          setAsideAt: null,
          setAsideReason: null,
          nonAnswers: [],
        },
        {
          id: "question-5",
          sequence: 5,
          category: "behavioral",
          competencyId: null,
          competencyName: "System design",
          difficulty: "senior",
          isFollowUp: false,
          prompt: "behavioral",
          answer: null,
          createdAt: "2026-08-29T00:00:00.000Z",
          objective: "Probe the behavioral decision.",
          evidenceIds: ["evidence-2"],
          expectedSignals: ["signal"],
          missingSignalPrompts: ["prompt"],
          followUpLimit: 0,
          sourceConfidence: 0.9,
          rubricCriteria: ["Describe the team alignment.", "State the outcome."],
          askedIntent: null,
          assistance: [],
          nonAnswer: false,
          setAsideAt: null,
          setAsideReason: null,
          nonAnswers: [],
        },
      ],
    }, evidence)).toThrow("clearly labeled general objective");
  });

  it("rejects blueprint questions that reference missing evidence ids", () => {
    expect(() => validateInterviewBlueprint({
      status: "grounded",
      fallbackReason: null,
      maxFollowUps: 3,
      maxQuestions: 8,
      createdAt: "2026-08-29T00:00:00.000Z",
      roundId: "tech-lead",
      turnBudget: 8,
      targets: [singleRequiredTarget],
      questions: [
        {
          id: "question-1",
          sequence: 1,
          category: "introduction",
          competencyId: null,
          competencyName: null,
          difficulty: "senior",
          isFollowUp: false,
          prompt: "intro",
          answer: null,
          createdAt: "2026-08-29T00:00:00.000Z",
          objective: "intro",
          evidenceIds: [],
          expectedSignals: ["signal"],
          missingSignalPrompts: ["prompt"],
          rubricCriteria: ["Establish recent ownership."],
          followUpLimit: 0,
          sourceConfidence: null,
          askedIntent: null,
          assistance: [],
          nonAnswer: false,
          setAsideAt: null,
          setAsideReason: null,
          nonAnswers: [],
        },
        {
          id: "question-2",
          sequence: 2,
          category: "experience",
          competencyId: null,
          competencyName: "React",
          difficulty: "senior",
          isFollowUp: false,
          prompt: "experience",
          answer: null,
          createdAt: "2026-08-29T00:00:00.000Z",
          objective: "experience",
          evidenceIds: ["missing-evidence"],
          expectedSignals: ["signal"],
          missingSignalPrompts: ["prompt"],
          rubricCriteria: ["Name the decision and impact."],
          followUpLimit: 1,
          sourceConfidence: 0.9,
          askedIntent: null,
          assistance: [],
          nonAnswer: false,
          setAsideAt: null,
          setAsideReason: null,
          nonAnswers: [],
        },
        {
          id: "question-3",
          sequence: 3,
          category: "technical",
          competencyId: null,
          competencyName: "React",
          difficulty: "senior",
          isFollowUp: false,
          prompt: "technical",
          answer: null,
          createdAt: "2026-08-29T00:00:00.000Z",
          objective: "technical",
          evidenceIds: ["evidence-1"],
          expectedSignals: ["signal"],
          missingSignalPrompts: ["prompt"],
          rubricCriteria: ["Name the technical trade-off.", "Describe the outcome."],
          followUpLimit: 1,
          sourceConfidence: 0.9,
          askedIntent: null,
          assistance: [],
          nonAnswer: false,
          setAsideAt: null,
          setAsideReason: null,
          nonAnswers: [],
        },
        {
          id: "question-4",
          sequence: 4,
          category: "architecture",
          competencyId: null,
          competencyName: "System design",
          difficulty: "senior",
          isFollowUp: false,
          prompt: "architecture",
          answer: null,
          createdAt: "2026-08-29T00:00:00.000Z",
          objective: "architecture",
          evidenceIds: ["evidence-2"],
          expectedSignals: ["signal"],
          missingSignalPrompts: ["prompt"],
          rubricCriteria: ["Name the constraint.", "Explain the architecture choice."],
          followUpLimit: 1,
          sourceConfidence: 0.9,
          askedIntent: null,
          assistance: [],
          nonAnswer: false,
          setAsideAt: null,
          setAsideReason: null,
          nonAnswers: [],
        },
        {
          id: "question-5",
          sequence: 5,
          category: "behavioral",
          competencyId: null,
          competencyName: "System design",
          difficulty: "senior",
          isFollowUp: false,
          prompt: "behavioral",
          answer: null,
          createdAt: "2026-08-29T00:00:00.000Z",
          objective: "behavioral",
          evidenceIds: ["evidence-2"],
          expectedSignals: ["signal"],
          missingSignalPrompts: ["prompt"],
          rubricCriteria: ["Describe the team alignment.", "State the outcome."],
          followUpLimit: 0,
          sourceConfidence: 0.9,
          askedIntent: null,
          assistance: [],
          nonAnswer: false,
          setAsideAt: null,
          setAsideReason: null,
          nonAnswers: [],
        },
      ],
    }, evidence)).toThrow("unknown evidence");
  });

  it("builds a deterministic limited-grounding fallback blueprint with bounded follow-ups", () => {
    const blueprint = buildFallbackInterviewBlueprint(
      profile,
      [strongReact, weakSystemDesign],
      evidence,
      new Date("2026-08-29T00:00:00.000Z"),
    );

    expect(blueprint.status).toBe("limited-grounding");
    expect(blueprint.maxFollowUps).toBe(3);
    expect(blueprint.maxQuestions).toBe(8);
    expect(blueprint.questions).toHaveLength(5);
    expect(blueprint.questions[1]).toMatchObject({
      category: "experience",
      evidenceIds: ["evidence-1"],
      objective: "Probe ownership and impact in Checkout Platform.",
      rubricCriteria: expect.arrayContaining([
        expect.stringContaining("decision"),
      ]),
    });
    // buildFallbackInterviewBlueprint doesn't populate roundId/turnBudget/targets itself --
    // that merge happens once, at coach.ts's `generateInterviewBlueprint` boundary (spec
    // §9.1) -- so validate a coverage-plan-bearing copy rather than the raw fallback output.
    const grounded = { ...blueprint, roundId: "tech-lead" as const, turnBudget: 8, targets: [singleRequiredTarget] };
    expect(validateInterviewBlueprint(grounded, evidence)).toEqual(grounded);
  });

  it("matches fallback evidence to the selected competency instead of the evidence array position", () => {
    const reversedEvidence = [...evidence].reverse();
    const blueprint = buildFallbackInterviewBlueprint(
      profile,
      [strongReact, weakSystemDesign],
      reversedEvidence,
      new Date("2026-08-29T00:00:00.000Z"),
    );

    expect(blueprint.questions[1]).toMatchObject({
      category: "experience",
      competencyName: "React",
      evidenceIds: ["evidence-1"],
    });
    expect(blueprint.questions[3]).toMatchObject({
      category: "architecture",
      competencyName: "System design",
      evidenceIds: ["evidence-2"],
    });
  });

  it("builds the exact generic five-question backbone for a sparse profile", () => {
    const readiness = {
      ready: false,
      missing: [
        "two concrete engineering projects or work examples",
        "responsibilities or outcomes",
      ],
    };

    const result = buildExperienceDiscoveryBlueprint(
      {
        role: "Frontend Engineer",
        seniority: "Senior",
        summary: "Frontend engineer",
        narrative: "Builds React product interfaces.",
        expertise: ["React", "TypeScript"],
        characteristics: ["Pragmatic"],
        competencies: [{ name: "React", relevance: 1 }],
      },
      [],
      readiness,
      new Date("2026-09-01T12:00:00.000Z"),
    );

    expect(result.status).toBe("limited-grounding");
    expect(result.fallbackReason).toContain("limited concrete example detail");
    expect(result.questions.map((item) => item.category)).toEqual([
      "introduction",
      "experience",
      "technical",
      "architecture",
      "behavioral",
    ]);
    expect(result.questions).toHaveLength(5);
    expect(result.questions.every((item) => item.evidenceIds.length === 0)).toBe(true);
    // buildExperienceDiscoveryBlueprint doesn't populate roundId/turnBudget/targets itself --
    // see the comment on the fallback test above.
    const grounded = { ...result, roundId: "tech-lead" as const, turnBudget: 8, targets: [singleRequiredTarget] };
    expect(validateInterviewBlueprint(grounded, [])).toEqual(grounded);
  });

  it("never anchors discovery questions to evidence, even when a partial match exists", () => {
    // The evidence matcher (`scoreEvidenceForQuestion`) is permissive: any item with a
    // non-empty `projectOrEmployer` scores > 0 against nearly every non-introduction
    // question. On the commonest sparse shape -- one real project that failed the
    // two-example readiness threshold -- that would anchor every discovery question to
    // the same evidence id, contradicting their deliberately generic, evidence-free
    // wording (spec §6.1) and disabling the discovery-answer grounding protection
    // (`hasSourceEvidenceTarget`) exactly where it exists to help.
    const evidence = [{
      id: "evidence-1",
      sourceKind: "cv" as const,
      sourceExcerpt: "Worked on a React migration for checkout.",
      projectOrEmployer: "Checkout migration",
      ownership: null,
      technologies: ["React"],
      decision: null,
      constraint: null,
      outcome: null,
      recency: "2025",
      confidence: 0.82,
    }];

    const result = buildExperienceDiscoveryBlueprint(
      sparseProfile,
      evidence,
      {
        ready: false,
        missing: ["two concrete engineering projects or work examples", "responsibilities or outcomes"],
      },
      new Date("2026-09-01T12:00:00.000Z"),
    );

    for (const question of result.questions) {
      expect(question.evidenceIds).toEqual([]);
      expect(question.sourceConfidence).toBeNull();
    }
    const serialized = JSON.stringify(result.questions);
    expect(serialized).not.toContain("Checkout migration");
    expect(serialized).not.toContain("evidence-1");
    expect(serialized).not.toContain("30%");
    expect(serialized).not.toContain("led the migration");
    // buildExperienceDiscoveryBlueprint doesn't populate roundId/turnBudget/targets itself --
    // see the comment on the fallback test above.
    const grounded = { ...result, roundId: "tech-lead" as const, turnBudget: 8, targets: [singleRequiredTarget] };
    expect(validateInterviewBlueprint(grounded, evidence)).toEqual(grounded);
  });
});

describe("buildCoverageTargets", () => {
  it("produces targets with rubric material and no prompt text", () => {
    const targets = buildCoverageTargets(sampleProfile(), sampleEvidence(), null, "tech-lead");
    expect(targets.length).toBeGreaterThan(0);
    for (const target of targets) {
      expect(target.objective.length).toBeGreaterThan(0);
      expect(target.rubricCriteria.length).toBeGreaterThan(0);
      // Task 3 ledger item: an empty `expectedSignals` can never reach
      // "satisfied" in `deriveCoverageState` (src/lib/interview-coverage.ts),
      // which would strand the target open for the rest of the round.
      expect(target.expectedSignals.length).toBeGreaterThan(0);
      expect(target).not.toHaveProperty("prompt");
    }
  });

  it("makes every opportunity gap a required target when anchored", () => {
    const opportunity = { ...sampleOpportunity(), gaps: ["Testing strategy", "Observability"] };
    const targets = buildCoverageTargets(sampleProfile(), sampleEvidence(), opportunity, "tech-lead");
    const required = targets.filter((target) => target.required).map((target) => target.competencyName);
    expect(required).toEqual(expect.arrayContaining(["Testing strategy", "Observability"]));
  });

  it("gives every target a unique id", () => {
    const targets = buildCoverageTargets(sampleProfile(), sampleEvidence(), null, "tech-lead");
    expect(new Set(targets.map((target) => target.id)).size).toBe(targets.length);
  });
});

describe("validateInterviewBlueprint (coverage targets)", () => {
  it("accepts a blueprint whose questions have no prompt text", () => {
    const blueprint = sampleBlueprint({ targets: buildCoverageTargets(sampleProfile(), sampleEvidence(), null, "tech-lead") });
    expect(() => validateInterviewBlueprint(blueprint)).not.toThrow();
  });

  it("rejects a blueprint with no required target", () => {
    const targets = buildCoverageTargets(sampleProfile(), sampleEvidence(), null, "tech-lead")
      .map((target) => ({ ...target, required: false }));
    expect(() => validateInterviewBlueprint(sampleBlueprint({ targets }))).toThrow(/required/i);
  });
});
