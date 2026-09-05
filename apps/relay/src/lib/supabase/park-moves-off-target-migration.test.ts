import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const sql = readFileSync("supabase/migrations/202609040001_park_moves_off_target.sql", "utf8");

describe("park moves off target migration", () => {
  it("adds the set-aside marker and the unscored-exchange log", () => {
    expect(sql).toMatch(/add column set_aside_at timestamptz/);
    expect(sql).toMatch(/add column set_aside_reason text/);
    expect(sql).toMatch(/add column non_answers jsonb not null default '\[\]'::jsonb/);
  });

  it("constrains the set-aside reason to the two the director can produce", () => {
    expect(sql).toMatch(/set_aside_reason in \('parked', 'rescue-budget-spent'\)/);
  });

  it("drops the old turn function before replacing it with the wider signature", () => {
    expect(sql).toMatch(/drop function if exists public\.record_conversation_turn\(uuid, text, numeric/);
    expect(sql).toMatch(/p_set_aside_reason text\s*\n\s*\)/);
  });

  it("appends the unscored exchange instead of overwriting it", () => {
    expect(sql).toMatch(/non_answers = case\s*\n\s*when p_non_answer then coalesce\(non_answers, '\[\]'::jsonb\) \|\|/);
  });

  it("clears the set-aside marker when a row is asked again", () => {
    expect(sql).toMatch(/set_aside_at = null,\s*\n\s*set_aside_reason = null/);
  });

  it("sets the set-aside marker on the answered row instead of just persisting the reason", () => {
    expect(sql).toMatch(/set_aside_at = case when p_set_aside_reason is null then set_aside_at else now\(\) end/);
    expect(sql).toMatch(/set_aside_reason = coalesce\(p_set_aside_reason, set_aside_reason\)/);
  });

  it("keeps the function security invoker and grants the new signature", () => {
    expect(sql).not.toMatch(/security definer/);
    expect(sql).toMatch(/security invoker/);
    expect(sql).toMatch(/grant execute on function public\.record_conversation_turn\(uuid, text, numeric, jsonb, jsonb, jsonb, jsonb, jsonb, text, numeric, jsonb, jsonb, jsonb, jsonb, uuid, text, jsonb, jsonb, jsonb, boolean, boolean, text\) to authenticated/);
  });

  it("revokes public execute on the full 22-argument signature before granting it", () => {
    expect(sql).toMatch(/revoke all on function public\.record_conversation_turn\(uuid, text, numeric, jsonb, jsonb, jsonb, jsonb, jsonb, text, numeric, jsonb, jsonb, jsonb, jsonb, uuid, text, jsonb, jsonb, jsonb, boolean, boolean, text\) from public/);
  });
});
