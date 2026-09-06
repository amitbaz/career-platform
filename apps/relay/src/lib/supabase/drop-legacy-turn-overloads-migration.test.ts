import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { supabaseMigration } from "@/test/supabase-paths";

const sql = readFileSync(
  supabaseMigration("202609060001_drop_legacy_record_conversation_turn_overloads.sql"),
  "utf8",
);

/** Argument lists of the three overloads left behind by pre-202609040001 migrations. */
const legacySignatures = {
  "9-argument (202608290002)": "uuid, text, numeric, jsonb, jsonb, jsonb, uuid, text, jsonb",
  "12-argument (202608290003)": "uuid, text, numeric, jsonb, jsonb, jsonb, jsonb, jsonb, text, uuid, text, jsonb",
  "17-argument (202608290007)": "uuid, text, numeric, jsonb, jsonb, jsonb, jsonb, jsonb, text, numeric, jsonb, jsonb, jsonb, jsonb, uuid, text, jsonb",
};

/** Collapses the migration's wrapped argument lists onto one line each for matching. */
const normalized = sql.replace(/\s+/g, " ");

describe("drop legacy turn overloads migration", () => {
  for (const [name, args] of Object.entries(legacySignatures)) {
    it(`drops the ${name} overload`, () => {
      expect(normalized).toContain(`drop function if exists public.record_conversation_turn( ${args} )`);
    });
  }

  it("leaves the current 22-argument signature alone", () => {
    const current = "uuid, text, numeric, jsonb, jsonb, jsonb, jsonb, jsonb, text, numeric, jsonb, jsonb, jsonb, jsonb, uuid, text, jsonb, jsonb, jsonb, boolean, boolean, text";
    expect(normalized).not.toContain(`public.record_conversation_turn( ${current} )`);
  });

  it("only drops, so a mistake cannot silently redefine the function", () => {
    expect(sql).not.toMatch(/create or replace function/i);
    expect(sql.match(/drop function/gi)).toHaveLength(3);
  });
});
