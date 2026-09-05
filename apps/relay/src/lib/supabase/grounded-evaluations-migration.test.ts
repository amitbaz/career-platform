import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const migrationPath = resolve(
  process.cwd(),
  "supabase/migrations/202608290007_grounded_evaluations.sql",
);

describe("grounded interview evaluation migration", () => {
  it("adds grounded evaluation columns to question and session feedback tables", async () => {
    const migration = await readFile(migrationPath, "utf8");

    expect(migration).toContain("add column relevance numeric");
    expect(migration).toContain("add column supported_claims jsonb");
    expect(migration).toContain("add column expected_signals_present jsonb");
    expect(migration).toContain("add column unsupported_claims jsonb");
    expect(migration).toContain("add column dimension_reasons jsonb");
  });

  it("extends the interview persistence functions with grounded evaluation payloads", async () => {
    const migration = await readFile(migrationPath, "utf8");

    expect(migration).toContain("function public.record_interview_evidence");
    expect(migration).toContain("function public.record_conversation_turn");
    expect(migration).toContain("p_relevance numeric");
    expect(migration).toContain("p_supported_claims jsonb");
    expect(migration).toContain("p_expected_signals_present jsonb");
    expect(migration).toContain("p_unsupported_claims jsonb");
    expect(migration).toContain("p_dimension_reasons jsonb");
  });
});
