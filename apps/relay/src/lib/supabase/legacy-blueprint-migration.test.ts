import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const migrationPath = resolve(
  process.cwd(),
  "supabase/migrations/202608290008_legacy_blueprint_state.sql",
);

describe("legacy blueprint state migration", () => {
  it("backfills legacy conversation sessions to limited-grounding when no persisted blueprint exists", async () => {
    const migration = await readFile(migrationPath, "utf8");

    expect(migration).toContain("update public.interview_sessions");
    expect(migration).toContain("blueprint_status = 'limited-grounding'");
    expect(migration).toContain("Legacy session created before grounded blueprints were persisted.");
  });
});
