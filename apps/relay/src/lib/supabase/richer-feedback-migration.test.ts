import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const migrationPath = resolve(
  process.cwd(),
  "supabase/migrations/202608290003_richer_feedback.sql",
);

describe("richer feedback migration", () => {
  it("adds additive coaching columns to question and session evaluations", async () => {
    const migration = await readFile(migrationPath, "utf8");

    expect(migration).toContain("alter table public.question_evaluations");
    expect(migration).toContain("add column missing_points jsonb");
    expect(migration).toContain("add column better_structure jsonb");
    expect(migration).toContain("add column improved_answer text");
    expect(migration).toContain("alter table public.session_evaluations");
  });

  it("extends both transactional persistence functions with richer coaching fields", async () => {
    const migration = await readFile(migrationPath, "utf8");

    expect(migration).toContain("function public.record_interview_evidence");
    expect(migration).toContain("function public.complete_hands_on_session");
    expect(migration).toContain("p_missing_points jsonb");
    expect(migration).toContain("p_better_structure jsonb");
    expect(migration).toContain("p_improved_answer text");
    expect(migration).toContain("coalesce(p_missing_points, '[]'::jsonb)");
    expect(migration).toContain("coalesce(v_evaluation -> 'missing_points', '[]'::jsonb)");
  });
});
