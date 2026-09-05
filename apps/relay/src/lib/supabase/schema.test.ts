import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const migrationPath = resolve(
  process.cwd(),
  "supabase/migrations/202608290001_adaptive_interview_foundation.sql",
);
const competencyMigrationPath = resolve(
  process.cwd(),
  "supabase/migrations/202608290009_competency_names.sql",
);

describe("adaptive interview foundation schema", () => {
  it("requires child records to reference parents owned by the same user", async () => {
    const migration = await readFile(migrationPath, "utf8");

    expect(migration).toContain("foreign key (session_id, user_id) references public.interview_sessions (id, user_id)");
    expect(migration).toContain("foreign key (competency_id, user_id) references public.competencies (id, user_id)");
    expect(migration).toContain("foreign key (question_id, user_id) references public.interview_questions (id, user_id)");
  });

  it("adds competency names in a later additive migration", async () => {
    const migration = await readFile(competencyMigrationPath, "utf8");

    expect(migration).toContain("alter table public.interview_questions");
    expect(migration).toContain("add column competency_name text");
    expect(migration).toContain("create or replace function public.create_conversation_session_with_plan(p_plan jsonb)");
    expect(migration).toContain("create or replace function public.create_conversation_session_with_blueprint(p_blueprint jsonb)");
  });
});
