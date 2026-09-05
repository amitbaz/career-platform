import { describe, expect, it, vi } from "vitest";
import { resolveObservationEvidence } from "@/lib/coach-memory";
import type { ObservationEvidence } from "@/lib/types";

type Row = Record<string, unknown>;

/**
 * A minimal fake covering exactly the `.from(table).select(...).eq(...).in(...)`
 * shape `coach-memory.ts` issues: `eq` filters are applied first (so
 * ownership scoping is enforced the same way Postgres RLS/`.eq("user_id", …)`
 * would), then `in` narrows to the requested ids and resolves the query.
 */
function fakeSupabase(tables: Record<string, Row[]>) {
  const from = vi.fn((table: string) => {
    const rows = tables[table] ?? [];
    const filters: Array<[string, unknown]> = [];
    const builder = {
      select: () => builder,
      eq: (field: string, value: unknown) => {
        filters.push([field, value]);
        return builder;
      },
      in: (field: string, values: unknown[]) => Promise.resolve({
        data: rows.filter((row) => filters.every(([f, v]) => row[f] === v) && values.includes(row[field])),
        error: null,
      }),
    };
    return builder;
  });
  return { from };
}

function evidenceRow(overrides: Partial<ObservationEvidence> = {}): ObservationEvidence {
  return {
    id: "evidence-link-1",
    userId: "user-1",
    observationId: "obs-1",
    profileEvidenceId: null,
    questionEvaluationId: null,
    careerStoryId: null,
    opportunityEventId: null,
    evidenceRole: "supporting",
    weight: 1,
    reason: "Backs the claim",
    createdAt: "2026-08-30T10:00:00.000Z",
    ...overrides,
  };
}

describe("resolveObservationEvidence", () => {
  it("resolves profile evidence to a project/employer label and excerpt summary", async () => {
    const supabase = fakeSupabase({
      profile_evidence: [{
        id: "pe-1",
        user_id: "user-1",
        project_or_employer: "Acme",
        source_excerpt: "Led the checkout migration with zero downtime.",
      }],
    });

    const [display] = await resolveObservationEvidence(supabase as never, "user-1", [
      evidenceRow({ profileEvidenceId: "pe-1", evidenceRole: "supporting", reason: "Backs the claim" }),
    ]);

    expect(display).toEqual({
      kind: "profile_evidence",
      label: "Acme",
      summary: "Led the checkout migration with zero downtime.",
      role: "supporting",
      reason: "Backs the claim",
    });
  });

  it("resolves a question evaluation to the question prompt plus a strengths/weaknesses summary", async () => {
    const supabase = fakeSupabase({
      question_evaluations: [{
        id: "qe-1",
        user_id: "user-1",
        question_id: "q-1",
        strengths: ["Clear structure"],
        weaknesses: ["Missed the tradeoff"],
      }],
      interview_questions: [{ id: "q-1", user_id: "user-1", prompt: "Tell me about a system you scaled." }],
    });

    const [display] = await resolveObservationEvidence(supabase as never, "user-1", [
      evidenceRow({ questionEvaluationId: "qe-1" }),
    ]);

    expect(display.kind).toBe("question_evaluation");
    expect(display.label).toBe("Tell me about a system you scaled.");
    expect(display.summary).toBe("Strengths: Clear structure. Needs work: Missed the tradeoff.");
  });

  it("resolves a career story to its title", async () => {
    const supabase = fakeSupabase({
      career_stories: [{ id: "story-1", user_id: "user-1", title: "Led the checkout migration" }],
    });

    const [display] = await resolveObservationEvidence(supabase as never, "user-1", [
      evidenceRow({ careerStoryId: "story-1" }),
    ]);

    expect(display.kind).toBe("career_story");
    expect(display.label).toBe("Led the checkout migration");
    expect(display.summary).toBe("Led the checkout migration");
  });

  it("resolves an opportunity event to a company/role label and a note-backed description", async () => {
    const supabase = fakeSupabase({
      opportunity_events: [{
        id: "event-1",
        user_id: "user-1",
        opportunity_id: "opp-1",
        event_type: "note",
        note: "Recruiter flagged strong system design signal.",
      }],
      opportunities: [{ id: "opp-1", user_id: "user-1", company: "Globex", role: "Staff Engineer" }],
    });

    const [display] = await resolveObservationEvidence(supabase as never, "user-1", [
      evidenceRow({ opportunityEventId: "event-1" }),
    ]);

    expect(display.kind).toBe("opportunity_event");
    expect(display.label).toBe("Globex · Staff Engineer");
    expect(display.summary).toBe("Recruiter flagged strong system design signal.");
  });

  it("falls back to a deterministic description when an opportunity event has no note", async () => {
    const supabase = fakeSupabase({
      opportunity_events: [{
        id: "event-1",
        user_id: "user-1",
        opportunity_id: "opp-1",
        event_type: "status_changed",
        from_status: "applied",
        to_status: "interviewing",
        note: null,
      }],
      opportunities: [{ id: "opp-1", user_id: "user-1", company: "Globex", role: "Staff Engineer" }],
    });

    const [display] = await resolveObservationEvidence(supabase as never, "user-1", [
      evidenceRow({ opportunityEventId: "event-1" }),
    ]);

    expect(display.summary).toBe("Status changed from applied to interviewing.");
  });

  it("omits evidence whose source row is not owned by the caller, never surfacing a raw id", async () => {
    const supabase = fakeSupabase({
      profile_evidence: [{
        id: "pe-1",
        user_id: "someone-else",
        project_or_employer: "Acme",
        source_excerpt: "Not this user's evidence.",
      }],
    });

    const displays = await resolveObservationEvidence(supabase as never, "user-1", [
      evidenceRow({ profileEvidenceId: "pe-1" }),
    ]);

    expect(displays).toEqual([]);
  });

  it("omits evidence whose source row no longer exists", async () => {
    const supabase = fakeSupabase({});

    const displays = await resolveObservationEvidence(supabase as never, "user-1", [
      evidenceRow({ careerStoryId: "missing-story" }),
    ]);

    expect(displays).toEqual([]);
  });

  it("returns an empty list without querying anything for zero evidence rows", async () => {
    const supabase = fakeSupabase({});

    const displays = await resolveObservationEvidence(supabase as never, "user-1", []);

    expect(displays).toEqual([]);
    expect(supabase.from).not.toHaveBeenCalled();
  });
});
