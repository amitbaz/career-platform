import { describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));

import {
  addOpportunityNote,
  createOpportunity,
  getOpportunity,
  listOpportunities,
  listOpportunityEvents,
  scheduleOpportunityInterview,
  transitionOpportunity,
  updateOpportunityDetails,
} from "@/lib/repositories/opportunities";
import { RepositoryError } from "@/lib/repositories/profile";

type Row = Record<string, unknown>;
type RpcResult = { data: unknown; error: { code: string } | null };

const opportunityRow = (overrides: Row = {}): Row => ({
  id: "opp-1",
  user_id: "user-1",
  company: "Example",
  role: "Senior Frontend Engineer",
  status: "considering",
  location: "Remote",
  remote: true,
  job_url: "https://example.com/jobs/123",
  job_description: "Build things.",
  source_label: "Example careers page",
  source_system: "job-hunter",
  source_external_id: "job-123",
  match_score: 82,
  strengths: ["React"],
  gaps: ["Kubernetes"],
  notes: "Looks promising.",
  applied_at: null,
  next_interview_at: null,
  created_at: "2026-08-30T10:00:00.000Z",
  updated_at: "2026-08-30T10:00:00.000Z",
  ...overrides,
});

const eventRow = (overrides: Row = {}): Row => ({
  id: "event-1",
  user_id: "user-1",
  opportunity_id: "opp-1",
  event_type: "created",
  from_status: null,
  to_status: "considering",
  occurred_at: "2026-08-30T10:00:00.000Z",
  note: null,
  metadata: {},
  created_at: "2026-08-30T10:00:00.000Z",
  ...overrides,
});

/**
 * A single reusable chainable stub covering select/update/eq/order/maybeSingle.
 * When given a `capture`, every `[field, value]` pair passed to `.eq(...)`
 * on this builder is recorded, in call order, so tests can assert the
 * actual scoping used -- not just that `.eq` was called some number of
 * times. (An `.eq` stub that unconditionally returns `builder` can never
 * fail a scoping assertion, which is the defect this capture exists to
 * catch -- see `src/lib/repositories/practice-plans.test.ts`.)
 */
function tableStub(result: RpcResult, capture?: { eq?: Array<[string, unknown]> }) {
  const builder: Record<string, unknown> = {
    select: () => builder,
    update: () => builder,
    eq: (field: string, value: unknown) => {
      if (capture) (capture.eq ??= []).push([field, value]);
      return builder;
    },
    order: async () => result,
    maybeSingle: async () => result,
  };
  return builder;
}

function clientWithRow(
  row: Row | null,
  rpcResult: RpcResult = { data: null, error: null },
  capture?: { eq?: Array<[string, unknown]> },
) {
  const rpc = vi.fn(async () => rpcResult);
  const from = vi.fn(() => tableStub({ data: row, error: null }, capture));
  return { rpc, from, supabase: { rpc, from } };
}

describe("opportunities repository", () => {
  it("creates an opportunity through the transactional create RPC", async () => {
    const { rpc, supabase } = clientWithRow(opportunityRow(), {
      data: [{ opportunity_id: "opp-1" }],
      error: null,
    });

    await createOpportunity(supabase as never, "user-1", {
      company: "Example",
      role: "Senior Frontend Engineer",
      sourceSystem: "job-hunter",
      sourceExternalId: "job-123",
    });

    expect(rpc).toHaveBeenCalledWith("create_opportunity", expect.objectContaining({
      p_company: "Example",
      p_role: "Senior Frontend Engineer",
      p_source_system: "job-hunter",
      p_source_external_id: "job-123",
    }));
  });

  it("reloads and maps the opportunity created by the RPC", async () => {
    const { supabase } = clientWithRow(opportunityRow(), {
      data: [{ opportunity_id: "opp-1" }],
      error: null,
    });

    const opportunity = await createOpportunity(supabase as never, "user-1", {
      company: "Example",
      role: "Senior Frontend Engineer",
    });

    expect(opportunity).toEqual(expect.objectContaining({
      id: "opp-1",
      userId: "user-1",
      company: "Example",
      role: "Senior Frontend Engineer",
      status: "considering",
      sourceSystem: "job-hunter",
      sourceExternalId: "job-123",
      matchScore: 82,
      strengths: ["React"],
      gaps: ["Kubernetes"],
    }));
  });

  it("fails with NO_OWNED_ROW when the create RPC returns no opportunity id", async () => {
    const { supabase } = clientWithRow(null, { data: null, error: null });

    await expect(createOpportunity(supabase as never, "user-1", {
      company: "Example",
      role: "Senior Frontend Engineer",
    })).rejects.toMatchObject({ code: "NO_OWNED_ROW" });
  });

  it("uses the transactional status RPC instead of direct status update", async () => {
    const { rpc, supabase } = clientWithRow(opportunityRow({ status: "applied" }), {
      data: [{ opportunity_id: "opp-1" }],
      error: null,
    });

    await transitionOpportunity(supabase as never, "user-1", "opp-1", "applied", {
      occurredAt: "2026-08-30T20:00:00.000Z",
      note: "Applied from company site",
    });

    expect(rpc).toHaveBeenCalledWith("transition_opportunity", expect.objectContaining({
      p_opportunity_id: "opp-1",
      p_to_status: "applied",
    }));
  });

  it("reloads the opportunity after a status transition", async () => {
    const { supabase } = clientWithRow(opportunityRow({ status: "applied", applied_at: "2026-08-30T20:00:00.000Z" }), {
      data: [{ opportunity_id: "opp-1" }],
      error: null,
    });

    const opportunity = await transitionOpportunity(supabase as never, "user-1", "opp-1", "applied");

    expect(opportunity.status).toBe("applied");
    expect(opportunity.appliedAt).toBe("2026-08-30T20:00:00.000Z");
  });

  it("fails with a repository error when the transition RPC reports the row was not owned", async () => {
    const { supabase } = clientWithRow(opportunityRow(), {
      data: null,
      error: { code: "P0002" },
    });

    await expect(transitionOpportunity(supabase as never, "user-1", "opp-1", "applied"))
      .rejects.toBeInstanceOf(RepositoryError);
  });

  it("uses the interview scheduling RPC for next_interview_at", async () => {
    const { rpc, supabase } = clientWithRow(
      opportunityRow({ status: "interviewing", next_interview_at: "2026-09-03T10:00:00.000Z" }),
      { data: [{ opportunity_id: "opp-1" }], error: null },
    );

    await scheduleOpportunityInterview(
      supabase as never,
      "user-1",
      "opp-1",
      "2026-09-03T10:00:00.000Z",
    );

    expect(rpc).toHaveBeenCalledWith("schedule_opportunity_interview", expect.objectContaining({
      p_opportunity_id: "opp-1",
      p_interview_at: "2026-09-03T10:00:00.000Z",
    }));
  });

  it("reloads the opportunity after scheduling an interview", async () => {
    const { supabase } = clientWithRow(
      opportunityRow({ status: "interviewing", next_interview_at: "2026-09-03T10:00:00.000Z" }),
      { data: [{ opportunity_id: "opp-1" }], error: null },
    );

    const opportunity = await scheduleOpportunityInterview(supabase as never, "user-1", "opp-1", "2026-09-03T10:00:00.000Z");

    expect(opportunity.nextInterviewAt).toBe("2026-09-03T10:00:00.000Z");
    expect(opportunity.status).toBe("interviewing");
  });

  it("loads a single owned opportunity scoped by user id", async () => {
    const capture: { eq?: Array<[string, unknown]> } = {};
    const { from, supabase } = clientWithRow(opportunityRow(), { data: null, error: null }, capture);

    const opportunity = await getOpportunity(supabase as never, "user-1", "opp-1");

    expect(from).toHaveBeenCalledWith("opportunities");
    expect(opportunity?.company).toBe("Example");
    // Must be scoped by BOTH the opportunity id and the requesting user id --
    // an `.eq` stub that swallows its arguments could never fail this.
    expect(capture.eq).toEqual([["id", "opp-1"], ["user_id", "user-1"]]);
  });

  it("returns null when the opportunity is not found", async () => {
    const { supabase } = clientWithRow(null);

    const opportunity = await getOpportunity(supabase as never, "user-1", "missing");

    expect(opportunity).toBeNull();
  });

  it("lists opportunities mapped from snake_case rows, scoped by user id", async () => {
    const capture: { eq?: Array<[string, unknown]> } = {};
    const rpc = vi.fn();
    const from = vi.fn(() => {
      const builder: Record<string, unknown> = {
        select: () => builder,
        eq: (field: string, value: unknown) => {
          (capture.eq ??= []).push([field, value]);
          return builder;
        },
        order: async () => ({ data: [opportunityRow(), opportunityRow({ id: "opp-2" })], error: null }),
      };
      return builder;
    });

    const opportunities = await listOpportunities({ rpc, from } as never, "user-1");

    expect(opportunities).toHaveLength(2);
    expect(opportunities[0]).toEqual(expect.objectContaining({ id: "opp-1", company: "Example" }));
    // Must be scoped by the requesting user id -- an `.eq` stub that
    // swallows its arguments could never fail this.
    expect(capture.eq).toEqual([["user_id", "user-1"]]);
  });

  it("updates descriptive opportunity details without touching lifecycle fields, scoped by user id", async () => {
    let capturedPatch: Row | undefined;
    const capture: { eq?: Array<[string, unknown]> } = {};
    const rpc = vi.fn();
    const from = vi.fn(() => {
      const builder: Record<string, unknown> = {
        update: (patch: Row) => {
          capturedPatch = patch;
          return builder;
        },
        eq: (field: string, value: unknown) => {
          (capture.eq ??= []).push([field, value]);
          return builder;
        },
        select: () => builder,
        maybeSingle: async () => ({ data: opportunityRow({ notes: "Updated notes" }), error: null }),
      };
      return builder;
    });

    const opportunity = await updateOpportunityDetails({ rpc, from } as never, "user-1", "opp-1", {
      notes: "Updated notes",
    });

    expect(opportunity.notes).toBe("Updated notes");
    expect(capturedPatch).toBeDefined();
    expect(capturedPatch).not.toHaveProperty("status");
    expect(capturedPatch).not.toHaveProperty("applied_at");
    expect(capturedPatch).not.toHaveProperty("next_interview_at");
    // Must be scoped by BOTH the opportunity id and the requesting user id --
    // an `.eq` stub that swallows its arguments could never fail this.
    expect(capture.eq).toEqual([["id", "opp-1"], ["user_id", "user-1"]]);
  });

  it("lists an opportunity's append-only event history, scoped by user id", async () => {
    const capture: { eq?: Array<[string, unknown]> } = {};
    const rpc = vi.fn();
    const from = vi.fn(() => {
      const builder: Record<string, unknown> = {
        select: () => builder,
        eq: (field: string, value: unknown) => {
          (capture.eq ??= []).push([field, value]);
          return builder;
        },
        order: async () => ({ data: [eventRow()], error: null }),
      };
      return builder;
    });

    const events = await listOpportunityEvents({ rpc, from } as never, "user-1", "opp-1");

    expect(events).toEqual([expect.objectContaining({
      id: "event-1",
      opportunityId: "opp-1",
      eventType: "created",
      toStatus: "considering",
    })]);
    // Must be scoped by BOTH the requesting user id and the opportunity id --
    // an `.eq` stub that swallows its arguments could never fail this.
    expect(capture.eq).toEqual([["user_id", "user-1"], ["opportunity_id", "opp-1"]]);
  });
});

describe("addOpportunityNote", () => {
  /** Captures the single row handed to `.insert(...)` on the events table. */
  function insertClient(result: RpcResult = { data: eventRow({ event_type: "note", note: "Recruiter call went well" }), error: null }) {
    let insertedRow: Row | undefined;
    const insert = vi.fn((row: Row) => {
      insertedRow = row;
      return builder;
    });
    const builder: Record<string, unknown> = {
      insert,
      select: () => builder,
      maybeSingle: async () => result,
    };
    const from = vi.fn(() => builder);
    return { from, insert, supabase: { rpc: vi.fn(), from }, inserted: () => insertedRow };
  }

  it("creates exactly one owned append-only note event", async () => {
    const { from, insert, supabase, inserted } = insertClient();

    const event = await addOpportunityNote(supabase as never, "user-1", "opp-1", "Recruiter call went well");

    expect(from).toHaveBeenCalledWith("opportunity_events");
    expect(insert).toHaveBeenCalledTimes(1);
    expect(inserted()).toEqual({
      user_id: "user-1",
      opportunity_id: "opp-1",
      event_type: "note",
      note: "Recruiter call went well",
      metadata: {},
    });
    expect(event).toEqual(expect.objectContaining({ eventType: "note", note: "Recruiter call went well" }));
  });

  it("trims the stored note", async () => {
    const { supabase, inserted } = insertClient();

    await addOpportunityNote(supabase as never, "user-1", "opp-1", "  Recruiter call went well  ");

    expect(inserted()).toEqual(expect.objectContaining({ note: "Recruiter call went well" }));
  });

  it("rejects a blank note without touching the database", async () => {
    const { from, supabase } = insertClient();

    await expect(addOpportunityNote(supabase as never, "user-1", "opp-1", "   "))
      .rejects.toBeInstanceOf(RepositoryError);
    expect(from).not.toHaveBeenCalled();
  });

  it("never writes a lifecycle status onto the note event", async () => {
    const { supabase, inserted } = insertClient();

    await addOpportunityNote(supabase as never, "user-1", "opp-1", "Recruiter call went well");

    expect(inserted()).not.toHaveProperty("from_status");
    expect(inserted()).not.toHaveProperty("to_status");
  });

  it("fails with a repository error when the insert returns no owned row", async () => {
    const { supabase } = insertClient({ data: null, error: null });

    await expect(addOpportunityNote(supabase as never, "user-1", "opp-1", "Recruiter call went well"))
      .rejects.toMatchObject({ code: "NO_OWNED_ROW" });
  });
});
