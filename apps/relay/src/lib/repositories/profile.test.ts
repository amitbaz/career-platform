import { describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));

import { competencyScopeFor, evidenceKeyFor, getProfile, profileScopeRows, saveProfile } from "@/lib/repositories/profile";
import type { EvidenceItem } from "@/lib/types";

/** Builds a chainable Supabase query spy: `.eq(...)` repeats, `.order(...)` terminates with `data`. */
function makeEvidenceQuery(data: unknown[] = []) {
  const query: { eq: ReturnType<typeof vi.fn>; order: ReturnType<typeof vi.fn> } = {
    eq: vi.fn(),
    order: vi.fn(async () => ({ data, error: null })),
  };
  query.eq.mockImplementation(() => query);
  return query;
}

describe("evidenceKeyFor", () => {
  it("creates a stable evidence key that ignores confidence and temporary ids", () => {
    const base: EvidenceItem = {
      id: "temporary-1",
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
    };

    expect(evidenceKeyFor(base)).toBe(evidenceKeyFor({
      ...base,
      id: "temporary-99",
      confidence: 0.61,
    }));
    expect(evidenceKeyFor(base)).not.toBe(evidenceKeyFor({
      ...base,
      outcome: "Cut bundle size by 35%.",
    }));
  });
});

describe("competencyScopeFor", () => {
  it("keeps expertise at full relevance and adds generic engineering scope without evidence", () => {
    expect(competencyScopeFor(["React", "TypeScript"])).toEqual(expect.arrayContaining([
      { name: "React", relevance: 1 },
      { name: "TypeScript", relevance: 1 },
      { name: "Architecture and system design", relevance: 0.9 },
      { name: "Accessibility and user impact", relevance: 0.9 },
      { name: "Testing and quality", relevance: 0.7 },
    ]));
  });

  it("maps backend expertise onto the shared engineering taxonomy", () => {
    expect(competencyScopeFor(["Backend platform"])).toEqual(expect.arrayContaining([
      { name: "Architecture and system design", relevance: 0.9 },
      { name: "Debugging and reliability", relevance: 0.9 },
    ]));
  });

  it("normalizes generated scope instead of persisting model relevance or assessment evidence", () => {
    const rows = profileScopeRows("user-1", {
      role: "Frontend Engineer",
      seniority: "Senior",
      summary: "",
      narrative: "",
      expertise: ["React"],
      characteristics: [],
      competencies: [{ name: "Invented competency", relevance: 0.01 }],
    });

    expect(rows).toContainEqual(expect.objectContaining({
      user_id: "user-1",
      name: "Architecture and system design",
      relevance: 0.9,
      expected_level: "senior",
    }));
    expect(rows).not.toContainEqual(expect.objectContaining({ name: "Invented competency" }));
    expect(rows.every((row) => !("estimated_level" in row) && !("average_score" in row))).toBe(true);
  });

  it("saves profile, sources, and active scope through one ownership-scoped RPC", async () => {
    const calls: Array<{ name: string; payload: Record<string, unknown> }> = [];
    const profileRow = {
      user_id: "user-1", role: "Frontend Engineer", seniority: "Senior", summary: "", narrative: "",
      expertise: ["React"], characteristics: [], created_at: "created", updated_at: "updated",
    };
    const evidenceQuery = makeEvidenceQuery([]);
    const supabase = {
      rpc: async (name: string, payload: Record<string, unknown>) => {
        calls.push({ name, payload });
        return { data: null, error: null };
      },
      from: (table: string) => {
        if (table === "profiles") return {
          select: () => ({ eq: () => ({ maybeSingle: async () => ({ data: profileRow, error: null }) }) }),
        };
        if (table === "source_documents") return {
          select: () => ({ eq: async () => ({ data: [], error: null }) }),
        };
        if (table === "profile_evidence") return {
          select: () => evidenceQuery,
        };
        return {
          select: () => ({ eq: () => ({ eq: () => ({ order: async () => ({ data: [], error: null }) }) }) }),
        };
      },
    };

    await saveProfile(supabase as never, "user-1", {
      role: "Frontend Engineer", seniority: "Senior", summary: "", narrative: "", expertise: ["React"], characteristics: [],
      competencies: [{ name: "Model supplied but ignored", relevance: 0.01 }],
    }, { cvText: "CV", coverLetter: "" }, [
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
    ], {
      ready: true,
      missing: [],
    });

    expect(calls).toHaveLength(1);
    expect(calls[0].name).toBe("save_profile_bundle");
    expect(calls[0].payload.p_profile_ready).toBe(true);
    expect(calls[0].payload.p_profile_missing).toEqual([]);
    expect(calls[0].payload.p_scope).toEqual(expect.arrayContaining([
      expect.objectContaining({ name: "Architecture and system design", relevance: 0.9 }),
    ]));
    expect(calls[0].payload.p_evidence).toHaveLength(1);
    expect((calls[0].payload.p_evidence as Array<Record<string, unknown>>)[0]).toMatchObject({
      evidence_key: expect.stringMatching(/^[0-9a-f]{64}$/),
      id: "evidence-1",
      source_excerpt: "Led a React migration for checkout.",
      project_or_employer: "Checkout Platform",
      ownership: "Owned the frontend migration end to end.",
      technologies: ["React", "TypeScript"],
    });
    expect((calls[0].payload.p_scope as Array<Record<string, unknown>>)).not.toContainEqual(
      expect.objectContaining({ name: "Model supplied but ignored" }),
    );
    expect(evidenceQuery.eq).toHaveBeenCalledWith("user_id", "user-1");
    expect(evidenceQuery.eq).toHaveBeenCalledWith("is_active", true);
  });

  it("hydrates evidence and readiness from the owned profile bundle, excluding retired rows", async () => {
    const profileRow = {
      user_id: "user-1", role: "Frontend Engineer", seniority: "Senior", summary: "", narrative: "",
      expertise: ["React"], characteristics: [], profile_ready: true, profile_missing: ["technologies"],
      created_at: "created", updated_at: "updated",
    };
    // The mock only ever returns the active row: getProfile's own `is_active` filter is
    // asserted below via the spy, since a real backend performs the exclusion server-side.
    const evidenceQuery = makeEvidenceQuery([{
      id: "evidence-1",
      user_id: "user-1",
      source_kind: "cv",
      source_excerpt: "Led a React migration for checkout.",
      project_or_employer: "Checkout Platform",
      ownership: "Owned the frontend migration end to end.",
      technologies: ["React", "TypeScript"],
      decision: "Split a large route into smaller bundles.",
      constraint_text: "Tight launch window.",
      outcome: "Cut bundle size by 28%.",
      recency: "2025-02",
      confidence: 0.94,
      is_active: true,
    }]);
    const supabase = {
      from: (table: string) => {
        if (table === "profiles") return {
          select: () => ({ eq: () => ({ maybeSingle: async () => ({ data: profileRow, error: null }) }) }),
        };
        if (table === "source_documents") return {
          select: () => ({ eq: async () => ({ data: [], error: null }) }),
        };
        if (table === "profile_evidence") return {
          select: () => evidenceQuery,
        };
        return {
          select: () => ({ eq: () => ({ eq: () => ({ order: async () => ({ data: [], error: null }) }) }) }),
        };
      },
    };

    const profile = await getProfile(supabase as never, "user-1");

    expect(profile).toEqual(expect.objectContaining({
      userId: "user-1",
      readiness: {
        ready: true,
        missing: ["technologies"],
      },
      evidence: [expect.objectContaining({
        id: "evidence-1",
        sourceExcerpt: "Led a React migration for checkout.",
      })],
    }));
    expect(profile?.evidence).toHaveLength(1);
    expect(evidenceQuery.eq).toHaveBeenCalledWith("user_id", "user-1");
    expect(evidenceQuery.eq).toHaveBeenCalledWith("is_active", true);
  });
});
