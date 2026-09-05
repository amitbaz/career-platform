import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const migrationPath = resolve(
  process.cwd(),
  "supabase/migrations/202608290002_complete_adaptive_interview_loop.sql",
);

describe("complete adaptive interview loop migration", () => {
  it("adds owned session evaluations protected by all four RLS operations", async () => {
    const migration = await readFile(migrationPath, "utf8");

    expect(migration).toMatch(/create table public\.session_evaluations[\s\S]*user_id uuid not null references auth\.users/);
    expect(migration).toContain("alter table public.session_evaluations enable row level security");
    for (const operation of ["select", "insert", "update", "delete"]) {
      expect(migration).toContain(`create policy ${operation}_own on public.session_evaluations`);
    }
  });

  it("uses caller-owned transactional RPCs for turns, hands-on completion, and profile replacement", async () => {
    const migration = await readFile(migrationPath, "utf8");

    for (const functionName of ["record_conversation_turn", "complete_hands_on_session", "save_profile_bundle"]) {
      expect(migration).toContain(`function public.${functionName}`);
    }
    expect(migration.match(/auth\.uid\(\)/g)?.length).toBeGreaterThanOrEqual(7);
    expect(migration).toContain("and q.user_id = v_user_id");
    expect(migration).toContain("and user_id = v_user_id");
  });
});
