import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const sql = readFileSync("supabase/migrations/202609010001_adaptive_interviewer.sql", "utf8");

describe("adaptive interviewer migration", () => {
  it("adds the session round and mode columns with backward-compatible defaults", () => {
    expect(sql).toMatch(/add column round_id text not null default 'tech-lead'/);
    expect(sql).toMatch(/add column mode text not null default 'real'/);
  });

  it("adds the session degraded flag", () => {
    expect(sql).toMatch(/add column degraded boolean not null default false/);
    expect(sql).toMatch(/p_degraded boolean/);
  });

  it("adds the question intent, assistance, and non-answer columns", () => {
    expect(sql).toMatch(/add column asked_intent jsonb/);
    expect(sql).toMatch(/add column assistance jsonb not null default '\[\]'::jsonb/);
    expect(sql).toMatch(/add column non_answer boolean not null default false/);
  });

  it("drops the not-null constraint on question prompts", () => {
    expect(sql).toMatch(/alter column prompt drop not null/);
  });

  it("replaces record_conversation_turn with the intent parameters", () => {
    expect(sql).toMatch(/create or replace function public\.record_conversation_turn/);
    expect(sql).toMatch(/p_asked_intent jsonb/);
    expect(sql).toMatch(/p_assistance jsonb/);
    expect(sql).toMatch(/p_non_answer boolean/);
  });

  it("keeps every function security invoker and re-grants the new signature", () => {
    expect(sql).not.toMatch(/security definer/);
    expect(sql.match(/security invoker/g)?.length).toBeGreaterThanOrEqual(2);
    expect(sql).toMatch(/grant execute on function public\.record_conversation_turn\([^)]*boolean[^)]*\) to authenticated/);
  });

  it("does not add an opportunity_id column that already exists", () => {
    expect(sql).not.toMatch(/add column opportunity_id/);
  });
});
