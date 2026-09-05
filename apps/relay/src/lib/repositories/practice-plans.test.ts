import { describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));

import {
  createPracticePlan,
  getPracticePlan,
  listPracticePlans,
  setPracticePlanOpportunities,
  updatePracticePlan,
} from "@/lib/repositories/practice-plans";
import { RepositoryError } from "@/lib/repositories/profile";

type Row = Record<string, unknown>;
type QueryResult = { data: unknown; error: { code: string } | null };

const planRow = (overrides: Row = {}): Row => ({
  id: "plan-1",
  user_id: "user-1",
  status: "draft",
  primary_focus: "System design tradeoffs",
  secondary_focus: null,
  rationale: "Two recent interviews flagged shallow tradeoff discussion.",
  format: "targeted_drill",
  estimated_minutes: 30,
  success_criteria: ["Names at least two tradeoffs unprompted"],
  priority_score: null,
  priority_factors: {},
  generation_error: null,
  completed_at: null,
  created_at: "2026-08-30T10:00:00.000Z",
  updated_at: "2026-08-30T10:00:00.000Z",
  ...overrides,
});

const linkRow = (overrides: Row = {}): Row => ({
  user_id: "user-1",
  practice_plan_id: "plan-1",
  opportunity_id: "opp-1",
  relevance: "primary",
  created_at: "2026-08-30T10:00:00.000Z",
  ...overrides,
});

/**
 * A chainable table-stub builder. It is awaitable directly (implements
 * `.then`) so callers that never terminate with `.maybeSingle()` -- e.g.
 * `await supabase.from(...).delete().eq(...).eq(...)`, or a list query ending
 * in `.order(...).limit(...)` -- resolve to `result` just like
 * `.maybeSingle()` does. `capture.eq` records every `[field, value]` pair
 * passed to `.eq(...)` on this builder, in call order, so tests can assert
 * the actual scoping used -- not just that `.eq` was called some number of
 * times; `capture.limit` records the row cap a list query asked for.
 */
function tableStub(
  result: QueryResult,
  capture?: { insert?: Row | Row[]; update?: Row; eq?: Array<[string, unknown]>; limit?: number },
) {
  const builder: Record<string, unknown> = {
    insert: (row: Row | Row[]) => {
      if (capture) capture.insert = row;
      return builder;
    },
    update: (patch: Row) => {
      if (capture) capture.update = patch;
      return builder;
    },
    delete: () => builder,
    select: () => builder,
    eq: (field: string, value: unknown) => {
      if (capture) (capture.eq ??= []).push([field, value]);
      return builder;
    },
    order: () => builder,
    limit: (rows: number) => {
      if (capture) capture.limit = rows;
      return builder;
    },
    maybeSingle: async () => result,
    then: (resolve: (value: QueryResult) => void) => resolve(result),
  };
  return builder;
}

/** Routes `supabase.from(table)` to a per-table stub, keyed by table name. */
function makeSupabase(tables: Record<string, ReturnType<typeof tableStub>>) {
  const from = vi.fn((table: string) => tables[table]);
  return { from };
}

describe("practice plan repository", () => {
  it("creates a practice plan and maps structured fields, with no opportunities yet", async () => {
    const capture: { insert?: Row } = {};
    const supabase = makeSupabase({
      practice_plans: tableStub({ data: planRow(), error: null }, capture),
    });

    const plan = await createPracticePlan(supabase as never, "user-1", {
      primaryFocus: "System design tradeoffs",
      rationale: "Two recent interviews flagged shallow tradeoff discussion.",
      format: "targeted_drill",
      estimatedMinutes: 30,
      successCriteria: ["Names at least two tradeoffs unprompted"],
    });

    expect(supabase.from).toHaveBeenCalledWith("practice_plans");
    expect(capture.insert).toEqual(expect.objectContaining({
      user_id: "user-1",
      primary_focus: "System design tradeoffs",
      format: "targeted_drill",
      estimated_minutes: 30,
    }));
    expect(plan).toEqual(expect.objectContaining({
      id: "plan-1",
      userId: "user-1",
      status: "draft",
      primaryFocus: "System design tradeoffs",
      format: "targeted_drill",
      opportunities: [],
    }));
  });

  it("loads a single owned practice plan scoped by user id, hydrated with its linked opportunities", async () => {
    const planCapture: { eq?: Array<[string, unknown]> } = {};
    const linkCapture: { eq?: Array<[string, unknown]> } = {};
    const supabase = makeSupabase({
      practice_plans: tableStub({ data: planRow(), error: null }, planCapture),
      practice_plan_opportunities: tableStub({ data: [linkRow()], error: null }, linkCapture),
    });

    const plan = await getPracticePlan(supabase as never, "user-1", "plan-1");

    expect(plan?.primaryFocus).toBe("System design tradeoffs");
    expect(plan?.opportunities).toEqual([expect.objectContaining({
      opportunityId: "opp-1",
      relevance: "primary",
    })]);
    expect(planCapture.eq).toEqual(expect.arrayContaining([
      ["id", "plan-1"],
      ["user_id", "user-1"],
    ]));
    expect(linkCapture.eq).toEqual(expect.arrayContaining([
      ["user_id", "user-1"],
      ["practice_plan_id", "plan-1"],
    ]));
  });

  it("returns null when the practice plan is not found", async () => {
    const supabase = makeSupabase({
      practice_plans: tableStub({ data: null, error: null }),
    });

    const plan = await getPracticePlan(supabase as never, "user-1", "missing");

    expect(plan).toBeNull();
  });

  it("lists practice plans scoped by user id, each hydrated with its linked opportunities", async () => {
    const listCapture: { eq?: Array<[string, unknown]>; limit?: number } = {};
    const supabase = makeSupabase({
      practice_plans: tableStub({
        data: [planRow(), planRow({ id: "plan-2" })],
        error: null,
      }, listCapture),
      practice_plan_opportunities: tableStub({ data: [linkRow()], error: null }),
    });

    const plans = await listPracticePlans(supabase as never, "user-1");

    expect(plans).toHaveLength(2);
    expect(plans[0]).toEqual(expect.objectContaining({ id: "plan-1" }));
    expect(plans[0].opportunities).toEqual([expect.objectContaining({ opportunityId: "opp-1" })]);
    expect(listCapture.eq).toEqual(expect.arrayContaining([["user_id", "user-1"]]));
    // Each plan costs an extra link query, so the list must never be unbounded.
    expect(listCapture.limit).toBe(20);
  });

  it("lists at most the number of plans the caller asks for", async () => {
    const listCapture: { limit?: number } = {};
    const supabase = makeSupabase({
      practice_plans: tableStub({ data: [planRow()], error: null }, listCapture),
      practice_plan_opportunities: tableStub({ data: [], error: null }),
    });

    await listPracticePlans(supabase as never, "user-1", { limit: 5 });

    expect(listCapture.limit).toBe(5);
  });

  it("updates only the provided fields of an owned practice plan, scoped by user id", async () => {
    const capture: { update?: Row; eq?: Array<[string, unknown]> } = {};
    const supabase = makeSupabase({
      practice_plans: tableStub({
        data: planRow({ status: "ready", primary_focus: "Behavioral storytelling" }),
        error: null,
      }, capture),
      practice_plan_opportunities: tableStub({ data: [], error: null }),
    });

    const plan = await updatePracticePlan(supabase as never, "user-1", "plan-1", {
      status: "ready",
      primaryFocus: "Behavioral storytelling",
    });

    expect(capture.update).toEqual(expect.objectContaining({
      status: "ready",
      primary_focus: "Behavioral storytelling",
      updated_at: expect.any(String),
    }));
    expect(capture.update).not.toHaveProperty("format");
    expect(capture.eq).toEqual(expect.arrayContaining([
      ["id", "plan-1"],
      ["user_id", "user-1"],
    ]));
    expect(plan.status).toBe("ready");
  });

  it("scopes a conditional update to the expected status so it cannot overwrite a newer one", async () => {
    const capture: { eq?: Array<[string, unknown]> } = {};
    const supabase = makeSupabase({
      practice_plans: tableStub({ data: planRow({ status: "failed" }), error: null }, capture),
      practice_plan_opportunities: tableStub({ data: [], error: null }),
    });

    const plan = await updatePracticePlan(
      supabase as never,
      "user-1",
      "plan-1",
      { status: "failed", generationError: "Could not prepare this practice session." },
      { expectedStatus: "ready" },
    );

    expect(capture.eq).toEqual(expect.arrayContaining([
      ["id", "plan-1"],
      ["user_id", "user-1"],
      ["status", "ready"],
    ]));
    expect(plan.status).toBe("failed");
  });

  it("reports no owned row when the conditional update matches nothing", async () => {
    const supabase = makeSupabase({
      practice_plans: tableStub({ data: null, error: null }),
      practice_plan_opportunities: tableStub({ data: [], error: null }),
    });

    await expect(
      updatePracticePlan(supabase as never, "user-1", "plan-1", { status: "failed" }, { expectedStatus: "ready" }),
    ).rejects.toMatchObject({ code: "NO_OWNED_ROW" });
  });

  it("persists one primary and multiple supporting opportunities", async () => {
    const insertCapture: { insert?: Row | Row[] } = {};
    const supabase = makeSupabase({
      practice_plan_opportunities: tableStub({
        data: [
          linkRow({ opportunity_id: "opp-1", relevance: "primary" }),
          linkRow({ opportunity_id: "opp-2", relevance: "supporting" }),
        ],
        error: null,
      }, insertCapture),
      practice_plans: tableStub({ data: planRow(), error: null }),
    });

    const plan = await setPracticePlanOpportunities(supabase as never, "user-1", "plan-1", [
      { opportunityId: "opp-1", relevance: "primary" },
      { opportunityId: "opp-2", relevance: "supporting" },
    ]);

    expect(insertCapture.insert).toEqual([
      { user_id: "user-1", practice_plan_id: "plan-1", opportunity_id: "opp-1", relevance: "primary" },
      { user_id: "user-1", practice_plan_id: "plan-1", opportunity_id: "opp-2", relevance: "supporting" },
    ]);
    expect(plan.opportunities).toHaveLength(2);
    expect(plan.opportunities.filter((link) => link.relevance === "primary")).toHaveLength(1);
  });

  it("rejects more than one primary opportunity before writing anything", async () => {
    const deleteCapture: { eq?: Array<[string, unknown]> } = {};
    const supabase = makeSupabase({
      practice_plan_opportunities: tableStub({ data: [], error: null }, deleteCapture),
      practice_plans: tableStub({ data: planRow(), error: null }),
    });

    await expect(
      setPracticePlanOpportunities(supabase as never, "user-1", "plan-1", [
        { opportunityId: "opp-1", relevance: "primary" },
        { opportunityId: "opp-2", relevance: "primary" },
      ]),
    ).rejects.toMatchObject(new RepositoryError(
      "A practice plan can have only one primary opportunity.",
      "INVALID_PLAN_CONTEXT",
    ));

    // The one-primary rule is checked before any write -- delete must never
    // have been called for a rejected request.
    expect(deleteCapture.eq).toBeUndefined();
  });

  it("deletes existing links scoped by BOTH user id and practice plan id, never by plan id alone", async () => {
    const deleteCapture: { eq?: Array<[string, unknown]> } = {};
    const supabase = makeSupabase({
      practice_plan_opportunities: tableStub({ data: [], error: null }, deleteCapture),
      practice_plans: tableStub({ data: planRow(), error: null }),
    });

    await setPracticePlanOpportunities(supabase as never, "user-1", "plan-1", []);

    expect(deleteCapture.eq).toEqual([
      ["user_id", "user-1"],
      ["practice_plan_id", "plan-1"],
    ]);
  });

  it("clears all links when replaced with an empty list, without inserting anything", async () => {
    const capture: { insert?: Row | Row[] } = {};
    const supabase = makeSupabase({
      practice_plan_opportunities: tableStub({ data: [], error: null }, capture),
      practice_plans: tableStub({ data: planRow(), error: null }),
    });

    const plan = await setPracticePlanOpportunities(supabase as never, "user-1", "plan-1", []);

    expect(capture.insert).toBeUndefined();
    expect(plan.opportunities).toEqual([]);
  });
});
