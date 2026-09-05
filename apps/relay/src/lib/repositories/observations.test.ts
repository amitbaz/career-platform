import { describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));

import {
  attachObservationEvidence,
  createCoachObservation,
  getCoachObservation,
  listCoachObservations,
  listObservationEvidence,
  reviewCoachObservation,
} from "@/lib/repositories/observations";

type Row = Record<string, unknown>;
type QueryResult = { data: unknown; error: { code: string } | null };

const observationRow = (overrides: Row = {}): Row => ({
  id: "obs-1",
  user_id: "user-1",
  observation_type: "weakness",
  claim: "I skip tradeoffs.",
  confidence: 0.7,
  importance: 0.6,
  trend: "unresolved",
  review_state: "unreviewed",
  user_correction: null,
  first_seen_at: "2026-08-30T10:00:00.000Z",
  last_seen_at: "2026-08-30T10:00:00.000Z",
  confirmed_at: null,
  corrected_at: null,
  dismissed_at: null,
  created_at: "2026-08-30T10:00:00.000Z",
  updated_at: "2026-08-30T10:00:00.000Z",
  ...overrides,
});

const evidenceRow = (overrides: Row = {}): Row => ({
  id: "evidence-link-1",
  user_id: "user-1",
  observation_id: "obs-1",
  profile_evidence_id: "evidence-1",
  question_evaluation_id: null,
  career_story_id: null,
  opportunity_event_id: null,
  evidence_role: "supporting",
  weight: 1,
  reason: "Backs the claim",
  created_at: "2026-08-30T10:00:00.000Z",
  ...overrides,
});

/** A single reusable chainable stub covering insert/select/update/eq/order/maybeSingle. */
function tableStub(
  result: QueryResult,
  capture?: { insert?: Row; update?: Row; eq?: Array<[string, unknown]> },
) {
  const builder: Record<string, unknown> = {
    insert: (row: Row) => {
      if (capture) capture.insert = row;
      return builder;
    },
    update: (patch: Row) => {
      if (capture) capture.update = patch;
      return builder;
    },
    select: () => builder,
    eq: (field: string, value: unknown) => {
      if (capture) (capture.eq ??= []).push([field, value]);
      return builder;
    },
    order: async () => result,
    maybeSingle: async () => result,
  };
  return builder;
}

describe("coach observation repository", () => {
  it("creates a coach observation and maps structured fields", async () => {
    const capture: { insert?: Row } = {};
    const from = vi.fn(() => tableStub({ data: observationRow(), error: null }, capture));
    const supabase = { from };

    const observation = await createCoachObservation(supabase as never, "user-1", {
      observationType: "weakness",
      claim: "I skip tradeoffs.",
      confidence: 0.7,
      importance: 0.6,
    });

    expect(from).toHaveBeenCalledWith("coach_observations");
    expect(capture.insert).toEqual(expect.objectContaining({
      user_id: "user-1",
      observation_type: "weakness",
      claim: "I skip tradeoffs.",
      confidence: 0.7,
      importance: 0.6,
    }));
    expect(observation).toEqual(expect.objectContaining({
      id: "obs-1",
      userId: "user-1",
      observationType: "weakness",
      claim: "I skip tradeoffs.",
      reviewState: "unreviewed",
    }));
  });

  it("loads a single owned coach observation scoped by user id", async () => {
    const capture: { eq?: Array<[string, unknown]> } = {};
    const from = vi.fn(() => tableStub({ data: observationRow(), error: null }, capture));
    const supabase = { from };

    const observation = await getCoachObservation(supabase as never, "user-1", "obs-1");

    expect(from).toHaveBeenCalledWith("coach_observations");
    expect(observation?.claim).toBe("I skip tradeoffs.");
    expect(capture.eq).toEqual(expect.arrayContaining([
      ["id", "obs-1"],
      ["user_id", "user-1"],
    ]));
  });

  it("returns null when the coach observation is not found", async () => {
    const from = vi.fn(() => tableStub({ data: null, error: null }));
    const supabase = { from };

    const observation = await getCoachObservation(supabase as never, "user-1", "missing");

    expect(observation).toBeNull();
  });

  it("lists coach observations mapped from snake_case rows, scoped by user id", async () => {
    const capture: { eq?: Array<[string, unknown]> } = {};
    const from = vi.fn(() => tableStub({
      data: [observationRow(), observationRow({ id: "obs-2" })],
      error: null,
    }, capture));
    const supabase = { from };

    const observations = await listCoachObservations(supabase as never, "user-1");

    expect(observations).toHaveLength(2);
    expect(observations[0]).toEqual(expect.objectContaining({ id: "obs-1", claim: "I skip tradeoffs." }));
    expect(capture.eq).toEqual(expect.arrayContaining([["user_id", "user-1"]]));
  });

  it("keeps the original claim when the user corrects an observation", async () => {
    const capture: { update?: Row } = {};
    const from = vi.fn(() => tableStub({
      data: observationRow({
        review_state: "corrected",
        user_correction: "I explain tradeoffs well; I need to make ownership more explicit.",
        corrected_at: "2026-08-30T11:00:00.000Z",
      }),
      error: null,
    }, capture));
    const supabase = { from };

    const result = await reviewCoachObservation(supabase as never, "user-1", "obs-1", {
      state: "corrected",
      correction: "I explain tradeoffs well; I need to make ownership more explicit.",
    });

    expect(capture.update).toEqual(expect.objectContaining({
      review_state: "corrected",
      user_correction: "I explain tradeoffs well; I need to make ownership more explicit.",
      corrected_at: expect.any(String),
      confirmed_at: null,
      dismissed_at: null,
    }));
    expect(result.claim).toBe("I skip tradeoffs.");
  });

  it("confirms an observation, setting confirmed_at and clearing correction/dismissal timestamps", async () => {
    const capture: { update?: Row } = {};
    const from = vi.fn(() => tableStub({
      data: observationRow({ review_state: "confirmed", confirmed_at: "2026-08-30T11:00:00.000Z" }),
      error: null,
    }, capture));
    const supabase = { from };

    const result = await reviewCoachObservation(supabase as never, "user-1", "obs-1", { state: "confirmed" });

    expect(capture.update).toEqual(expect.objectContaining({
      review_state: "confirmed",
      confirmed_at: expect.any(String),
      corrected_at: null,
      dismissed_at: null,
      user_correction: null,
    }));
    expect(result.reviewState).toBe("confirmed");
  });

  it("dismisses an observation without touching the original claim", async () => {
    const capture: { update?: Row } = {};
    const from = vi.fn(() => tableStub({
      data: observationRow({ review_state: "dismissed", dismissed_at: "2026-08-30T11:00:00.000Z" }),
      error: null,
    }, capture));
    const supabase = { from };

    const result = await reviewCoachObservation(supabase as never, "user-1", "obs-1", { state: "dismissed" });

    expect(capture.update).toEqual(expect.objectContaining({
      review_state: "dismissed",
      dismissed_at: expect.any(String),
      confirmed_at: null,
      corrected_at: null,
      user_correction: null,
    }));
    expect(result.claim).toBe("I skip tradeoffs.");
  });

  it("scopes the review update by user id", async () => {
    const capture: { eq?: Array<[string, unknown]> } = {};
    const from = vi.fn(() => tableStub({ data: observationRow({ review_state: "confirmed" }), error: null }, capture));
    const supabase = { from };

    await reviewCoachObservation(supabase as never, "user-1", "obs-1", { state: "confirmed" });

    expect(capture.eq).toEqual(expect.arrayContaining([
      ["id", "obs-1"],
      ["user_id", "user-1"],
    ]));
  });

  it("attaches supporting evidence with exactly one typed source", async () => {
    const capture: { insert?: Row } = {};
    const from = vi.fn(() => tableStub({ data: evidenceRow(), error: null }, capture));
    const supabase = { from };

    const evidence = await attachObservationEvidence(
      supabase as never,
      "user-1",
      "obs-1",
      { kind: "profile_evidence", profileEvidenceId: "evidence-1" },
    );

    expect(from).toHaveBeenCalledWith("observation_evidence");
    expect(capture.insert).toEqual(expect.objectContaining({
      user_id: "user-1",
      observation_id: "obs-1",
      profile_evidence_id: "evidence-1",
      question_evaluation_id: null,
      career_story_id: null,
      opportunity_event_id: null,
      evidence_role: "supporting",
    }));
    expect(evidence.evidenceRole).toBe("supporting");
  });

  it("attaches contradicting evidence from a question evaluation", async () => {
    const capture: { insert?: Row } = {};
    const from = vi.fn(() => tableStub({
      data: evidenceRow({
        profile_evidence_id: null,
        question_evaluation_id: "qe-1",
        evidence_role: "contradicting",
      }),
      error: null,
    }, capture));
    const supabase = { from };

    const evidence = await attachObservationEvidence(
      supabase as never,
      "user-1",
      "obs-1",
      { kind: "question_evaluation", questionEvaluationId: "qe-1" },
      { role: "contradicting" },
    );

    expect(capture.insert).toEqual(expect.objectContaining({
      profile_evidence_id: null,
      question_evaluation_id: "qe-1",
      career_story_id: null,
      opportunity_event_id: null,
      evidence_role: "contradicting",
    }));
    expect(evidence.evidenceRole).toBe("contradicting");
  });

  it("attaches context evidence from a career story", async () => {
    const capture: { insert?: Row } = {};
    const from = vi.fn(() => tableStub({
      data: evidenceRow({
        profile_evidence_id: null,
        career_story_id: "story-1",
        evidence_role: "context",
      }),
      error: null,
    }, capture));
    const supabase = { from };

    const evidence = await attachObservationEvidence(
      supabase as never,
      "user-1",
      "obs-1",
      { kind: "career_story", careerStoryId: "story-1" },
      { role: "context", reason: "Related story" },
    );

    expect(capture.insert).toEqual(expect.objectContaining({
      profile_evidence_id: null,
      question_evaluation_id: null,
      career_story_id: "story-1",
      opportunity_event_id: null,
      evidence_role: "context",
      reason: "Related story",
    }));
    expect(evidence.evidenceRole).toBe("context");
  });

  it("attaches evidence from an opportunity event", async () => {
    const capture: { insert?: Row } = {};
    const from = vi.fn(() => tableStub({
      data: evidenceRow({
        profile_evidence_id: null,
        opportunity_event_id: "event-1",
      }),
      error: null,
    }, capture));
    const supabase = { from };

    const evidence = await attachObservationEvidence(
      supabase as never,
      "user-1",
      "obs-1",
      { kind: "opportunity_event", opportunityEventId: "event-1" },
    );

    expect(capture.insert).toEqual(expect.objectContaining({
      profile_evidence_id: null,
      question_evaluation_id: null,
      career_story_id: null,
      opportunity_event_id: "event-1",
    }));
    expect(evidence.opportunityEventId).toBe("event-1");
  });

  it("lists an observation's evidence scoped by user and observation", async () => {
    const capture: { eq?: Array<[string, unknown]> } = {};
    const from = vi.fn(() => tableStub({ data: [evidenceRow()], error: null }, capture));
    const supabase = { from };

    const evidence = await listObservationEvidence(supabase as never, "user-1", "obs-1");

    expect(from).toHaveBeenCalledWith("observation_evidence");
    expect(evidence).toEqual([expect.objectContaining({
      id: "evidence-link-1",
      observationId: "obs-1",
      profileEvidenceId: "evidence-1",
    })]);
    expect(capture.eq).toEqual(expect.arrayContaining([
      ["user_id", "user-1"],
      ["observation_id", "obs-1"],
    ]));
  });
});
