import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const sql = readFileSync("supabase/migrations/202609020001_coverage_target_required.sql", "utf8");

describe("coverage target required migration", () => {
  it("adds the question required column with a backward-compatible default", () => {
    expect(sql).toMatch(/add column required boolean not null default true/);
  });

  it("replaces create_conversation_session_with_blueprint to persist required", () => {
    expect(sql).toMatch(/create or replace function public\.create_conversation_session_with_blueprint/);
    expect(sql).toMatch(/required boolean\s*\n\s*\)/);
    expect(sql).toMatch(/coalesce\(target\.required, true\)/);
  });

  it("keeps the function security invoker and re-grants its unchanged signature", () => {
    expect(sql).not.toMatch(/security definer/);
    expect(sql).toMatch(/security invoker/);
    expect(sql).toMatch(/grant execute on function public\.create_conversation_session_with_blueprint\(jsonb\) to authenticated/);
  });
});
