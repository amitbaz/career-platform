import { describe, expect, it } from "vitest";
import { recommendPractice } from "@/lib/practice-recommendation";
import { calculateReadiness } from "@/lib/readiness";
import type {
  CareerStory,
  CoachObservation,
  Competency,
  InterviewSession,
  Opportunity,
  OpportunityStatus,
  PracticeRecommendationInput,
  ReadinessEvidence,
  ReadinessModel,
} from "@/lib/types";

const now = new Date("2026-08-31T08:00:00.000Z");

const opportunity: Opportunity = {
  id: "opportunity-1",
  userId: "user-1",
  company: "Example Co",
  role: "Staff Engineer",
  status: "considering",
  location: null,
  remote: null,
  jobUrl: null,
  jobDescription: null,
  sourceLabel: null,
  sourceSystem: null,
  sourceExternalId: null,
  matchScore: null,
  strengths: [],
  gaps: [],
  notes: null,
  appliedAt: null,
  nextInterviewAt: null,
  createdAt: "2026-08-01T00:00:00.000Z",
  updatedAt: "2026-08-01T00:00:00.000Z",
};

const observation: CoachObservation = {
  id: "observation-1",
  userId: "user-1",
  observationType: "knowledge_gap",
  claim: "Skips discussing trade-offs.",
  confidence: 0.7,
  importance: 0.7,
  trend: "unresolved",
  reviewState: "unreviewed",
  userCorrection: null,
  firstSeenAt: "2026-08-01T00:00:00.000Z",
  lastSeenAt: "2026-08-20T00:00:00.000Z",
  confirmedAt: null,
  correctedAt: null,
  dismissedAt: null,
  createdAt: "2026-08-01T00:00:00.000Z",
  updatedAt: "2026-08-20T00:00:00.000Z",
};

const story = (overrides: Partial<CareerStory> = {}): CareerStory => ({
  id: "story-1",
  userId: "user-1",
  title: "Migrated the billing pipeline",
  situation: null,
  responsibility: null,
  problem: null,
  actions: null,
  alternatives: null,
  tradeoffs: null,
  ownership: null,
  outcome: null,
  lessons: null,
  tags: [],
  completeness: 0.5,
  reviewState: "confirmed",
  confirmedAt: "2026-08-10T00:00:00.000Z",
  createdAt: "2026-08-01T00:00:00.000Z",
  updatedAt: "2026-08-10T00:00:00.000Z",
  ...overrides,
});

const competency = (overrides: Partial<Competency> = {}): Competency => ({
  id: "competency-1",
  name: "Architecture framing",
  relevance: 1,
  expectedLevel: "senior",
  estimatedLevel: "senior",
  confidence: "medium",
  lastPracticedAt: "2026-08-20T00:00:00.000Z",
  questionCount: 3,
  averageScore: 6,
  recentScore: 6,
  strengths: [],
  weaknesses: [],
  ...overrides,
});

/** One graded answer, weighted at close to 1 (real mode, no assistance, recorded right at `now`). */
const evidence = (overrides: Partial<ReadinessEvidence> = {}): ReadinessEvidence => ({
  questionEvaluationId: "eval-1",
  sessionId: "session-1",
  recordedAt: "2026-08-31T08:00:00.000Z",
  score: 8,
  competencyId: "competency-1",
  competencyName: "Backend systems",
  category: "technical",
  relevance: 1,
  mode: "real",
  degraded: false,
  assistanceCount: 0,
  ...overrides,
});

/** An empty, all-`unresolved` readiness model: no dimension has any confidence, so branch 5 never fires. */
const emptyReadiness: ReadinessModel = calculateReadiness([], now);

/**
 * Four strong `backend` evidence rows (real mode, no assistance, scored 9)
 * plus two weak `system-design` rows (scored 2) -- enough weight for both
 * dimensions to carry non-null confidence, with `system-design` clearly the
 * weaker of the two.
 */
const backendAndSystemDesignReadiness: ReadinessModel = calculateReadiness([
  ...Array.from({ length: 4 }, (_, index) => evidence({
    questionEvaluationId: `backend-${index}`,
    competencyName: "Backend systems",
    score: 9,
  })),
  ...Array.from({ length: 2 }, (_, index) => evidence({
    questionEvaluationId: `system-design-${index}`,
    competencyName: "System design fundamentals",
    score: 2,
  })),
], now);

const completedSession: InterviewSession = {
  id: "session-1",
  userId: "user-1",
  kind: "conversation",
  roundId: "tech-lead",
  mode: "real",
  degraded: false,
  status: "complete",
  startedAt: "2026-08-20T09:00:00.000Z",
  completedAt: "2026-08-20T10:00:00.000Z",
  exercise: {},
  resultSummary: {},
  overallScore: 7,
  questions: [],
  checkpoints: [],
  evaluations: [],
  messages: [],
  createdAt: "2026-08-20T09:00:00.000Z",
  updatedAt: "2026-08-20T10:00:00.000Z",
  practicePlanId: null,
  opportunityId: null,
};

// A neutral baseline: one completed session (so branch 7 does not fire), no
// readiness evidence at all (so branch 5 does not fire), and no other
// signals, so the default falls through to the branch-8 fallback unless a
// test overrides fields to exercise an earlier branch.
const baseInput: PracticeRecommendationInput = {
  opportunities: [],
  observations: [],
  stories: [],
  readiness: emptyReadiness,
  competencies: [],
  recentSessions: [completedSession],
  recentPlans: [],
  now,
};

describe("recommendPractice", () => {
  it("prioritizes an interview in three days over a generic weakness", () => {
    const result = recommendPractice({
      ...baseInput,
      now: new Date("2026-08-31T08:00:00Z"),
      opportunities: [{
        ...opportunity,
        status: "interviewing",
        nextInterviewAt: "2026-09-03T10:00:00Z",
      }],
      readiness: backendAndSystemDesignReadiness,
    });
    expect(result).toMatchObject({ format: "role_prep", primaryOpportunityId: opportunity.id });
    expect(result.signals[0]).toMatchObject({ kind: "upcoming_interview", detail: "Example Co · in 3 days" });
    expect(result.signals[0].detail).not.toContain(opportunity.id);
    expect(result.rationale.length).toBeGreaterThan(0);
    expect(result.rationale).toContain("Example Co");
  });

  it("uses corrected observation text", () => {
    const result = recommendPractice({
      ...baseInput,
      observations: [{
        ...observation,
        reviewState: "corrected",
        importance: 0.9,
        userCorrection: "Make ownership explicit.",
      }],
    });
    expect(result.primaryFocus).toContain("Make ownership explicit");
  });

  it("recommends role prep for any interviewing opportunity without a near-term date", () => {
    const result = recommendPractice({
      ...baseInput,
      opportunities: [{ ...opportunity, status: "interviewing", nextInterviewAt: null }],
    });
    expect(result).toMatchObject({ format: "role_prep", primaryOpportunityId: opportunity.id });
    expect(result.signals[0].kind).toBe("interviewing_opportunity");
  });

  it("selects a confirmed high-importance observation and maps story_gap to story_work", () => {
    const result = recommendPractice({
      ...baseInput,
      observations: [{
        ...observation,
        observationType: "story_gap",
        reviewState: "confirmed",
        importance: 0.8,
      }],
    });
    expect(result.format).toBe("story_work");
    expect(result.estimatedMinutes).toBe(12);
  });

  it("never lets a dismissed observation drive the recommendation", () => {
    const result = recommendPractice({
      ...baseInput,
      observations: [{ ...observation, reviewState: "dismissed", importance: 0.95 }],
    });
    expect(result.format).toBe("full_simulation");
  });

  it("never lets an unreviewed observation drive the recommendation", () => {
    const result = recommendPractice({
      ...baseInput,
      observations: [{ ...observation, reviewState: "unreviewed", importance: 0.95 }],
    });
    expect(result.format).toBe("full_simulation");
  });

  /**
   * A confirmed strength is not a practice target. Without a type filter the
   * catch-all in `formatForObservation` routes it to `targeted_drill`, and the
   * user is told to "Work on:" something they already do well.
   */
  it.each(["strength", "story_strength"] as const)(
    "never lets a confirmed %s drive the recommendation",
    (observationType) => {
      const result = recommendPractice({
        ...baseInput,
        observations: [{
          ...observation,
          observationType,
          reviewState: "confirmed",
          importance: 0.95,
          claim: "You consistently give clear architecture answers",
        }],
      });

      expect(result.format).toBe("full_simulation");
      expect(result.primaryFocus).not.toContain("You consistently give clear architecture answers");
    },
  );

  it("still selects a lower-importance reviewed weakness over a confirmed strength", () => {
    const result = recommendPractice({
      ...baseInput,
      observations: [
        { ...observation, id: "observation-strength", observationType: "strength", reviewState: "confirmed", importance: 0.95 },
        {
          ...observation,
          id: "observation-weakness",
          observationType: "weakness",
          reviewState: "confirmed",
          importance: 0.7,
          claim: "You skip the trade-off when you close an answer",
        },
      ],
    });

    expect(result.format).toBe("targeted_drill");
    expect(result.primaryFocus).toContain("You skip the trade-off when you close an answer");
  });

  it("maps a reviewed answer_habit/delivery_pattern observation to technical_communication by default", () => {
    const result = recommendPractice({
      ...baseInput,
      observations: [{
        ...observation,
        observationType: "answer_habit",
        reviewState: "confirmed",
        importance: 0.7,
        claim: "Rambles before getting to the point.",
      }],
    });
    expect(result.format).toBe("technical_communication");
  });

  it("maps a reviewed answer_habit/delivery_pattern observation to behavioral when clearly behavioral", () => {
    const result = recommendPractice({
      ...baseInput,
      observations: [{
        ...observation,
        observationType: "delivery_pattern",
        reviewState: "confirmed",
        importance: 0.7,
        claim: "Avoids naming their role in team conflict stories.",
      }],
    });
    expect(result.format).toBe("behavioral");
  });

  it("recommends story_work for an active application with zero confirmed stories", () => {
    const result = recommendPractice({
      ...baseInput,
      opportunities: [{ ...opportunity, status: "applied", appliedAt: "2026-08-15T00:00:00.000Z" }],
      stories: [story({ reviewState: "draft" })],
    });
    expect(result).toMatchObject({ format: "story_work", primaryOpportunityId: opportunity.id });
    expect(result.signals[0]).toMatchObject({ kind: "story_bank_gap", detail: "no confirmed stories yet" });
  });

  it("recommends practice for the weakest readiness dimension, mapped to the matching competency, when no higher tier applies", () => {
    const result = recommendPractice({
      ...baseInput,
      readiness: backendAndSystemDesignReadiness,
      competencies: [
        competency({ id: "backend-1", name: "Backend systems", averageScore: 9 }),
        competency({ id: "system-design-1", name: "System design fundamentals", averageScore: 2 }),
      ],
    });
    expect(result.format).toBe("targeted_drill");
    expect(result.primaryFocus).toContain("System design fundamentals");
  });

  it("falls back to the humanized dimension name when no competency maps to the weakest dimension", () => {
    const result = recommendPractice({
      ...baseInput,
      readiness: backendAndSystemDesignReadiness,
      competencies: [],
    });
    expect(result.format).toBe("targeted_drill");
    expect(result.primaryFocus).toContain("system design");
  });

  /**
   * `backendOnlyReadiness` has evidence ONLY for `backend` (strong, score
   * 90) -- every other dimension, including ones that would otherwise look
   * "weak" at a null/zero score, has `confidence: null` because Relay has
   * never actually observed them. A dimension with no evidence is an
   * unknown, not a weakness (issue #14): the selector must still pick the
   * one CONFIDENT dimension it has, not fall through past it in search of a
   * lower number that was never really measured.
   */
  it("ignores dimensions with no evidence (unresolved confidence) when picking the weakest", () => {
    const backendOnlyReadiness = calculateReadiness(
      Array.from({ length: 4 }, (_, index) => evidence({ questionEvaluationId: `backend-${index}`, score: 9 })),
      now,
    );
    expect(backendOnlyReadiness.dimensions.filter((dimension) => dimension.confidence !== null)).toHaveLength(1);

    const result = recommendPractice({
      ...baseInput,
      readiness: backendOnlyReadiness,
      competencies: [competency({ id: "backend-1", name: "Backend systems", averageScore: 9 })],
    });

    expect(result.format).toBe("targeted_drill");
    expect(result.primaryFocus).toContain("Backend systems");
  });

  it("recommends role prep for an applied opportunity once stories and readiness are covered", () => {
    const result = recommendPractice({
      ...baseInput,
      opportunities: [{ ...opportunity, status: "applied" }],
      stories: [story({ reviewState: "confirmed" })],
    });
    expect(result).toMatchObject({ format: "role_prep", primaryOpportunityId: opportunity.id });
    expect(result.signals[0].kind).toBe("applied_opportunity");
  });

  it("falls back to self-presentation for a first-time user with no completed sessions", () => {
    const result = recommendPractice({ ...baseInput, recentSessions: [] });
    expect(result.format).toBe("self_presentation");
    expect(result.estimatedMinutes).toBe(10);
  });

  it("falls back to a full simulation when no other signal applies", () => {
    const result = recommendPractice(baseInput);
    expect(result.format).toBe("full_simulation");
    expect(result.estimatedMinutes).toBe(30);
    expect(result.primaryOpportunityId).toBeNull();
  });

  it("introduces backend/full-stack practice once another dimension has evidence but backend has none", () => {
    const frontendOnlyReadiness = calculateReadiness(
      Array.from({ length: 4 }, (_, index) => evidence({
        questionEvaluationId: `frontend-${index}`,
        competencyName: "React architecture",
        score: 8,
      })),
      now,
    );
    const result = recommendPractice({ ...baseInput, readiness: frontendOnlyReadiness });
    expect(result.format).toBe("targeted_drill");
    expect(result.primaryFocus).toContain("backend");
    expect(result.secondaryFocus).toContain("frontend-leaning");
    expect(result.signals[0]).toMatchObject({ kind: "coverage_gap" });
  });

  it("does not introduce backend once backend itself has any evidence, even as the weakest dimension", () => {
    const result = recommendPractice({ ...baseInput, readiness: backendAndSystemDesignReadiness });
    expect(result.signals[0].kind).toBe("progress_weakness");
  });

  it("never lets a terminal opportunity create urgency even with a near-term interview date", () => {
    const terminalStatuses: OpportunityStatus[] = ["offer", "rejected", "withdrawn", "closed"];
    for (const status of terminalStatuses) {
      const result = recommendPractice({
        ...baseInput,
        opportunities: [{ ...opportunity, status, nextInterviewAt: "2026-09-03T10:00:00Z" }],
      });
      expect(result.format).toBe("full_simulation");
      expect(result.primaryOpportunityId).toBeNull();
    }
  });

  it("produces a deterministic, sorted supportingOpportunityIds excluding the primary and any terminal opportunity", () => {
    const primaryOpportunity = { ...opportunity, id: "opportunity-b", status: "interviewing" as const, nextInterviewAt: null };
    const supportingOpportunity = { ...opportunity, id: "opportunity-a", status: "considering" as const };
    const terminalOpportunity = { ...opportunity, id: "opportunity-c", status: "rejected" as const };
    const result = recommendPractice({
      ...baseInput,
      opportunities: [primaryOpportunity, supportingOpportunity, terminalOpportunity],
    });
    expect(result.primaryOpportunityId).toBe("opportunity-b");
    expect(result.supportingOpportunityIds).toEqual(["opportunity-a"]);
  });
});
