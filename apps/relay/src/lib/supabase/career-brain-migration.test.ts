import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const migrationPath = resolve(
  process.cwd(),
  "supabase/migrations/202608300001_stable_profile_evidence.sql",
);

const opportunitiesMigrationPath = resolve(
  process.cwd(),
  "supabase/migrations/202608300002_opportunities.sql",
);

const careerStoriesMigrationPath = resolve(
  process.cwd(),
  "supabase/migrations/202608300003_career_stories.sql",
);

const coachObservationsMigrationPath = resolve(
  process.cwd(),
  "supabase/migrations/202608300004_coach_observations.sql",
);

const practicePlansMigrationPath = resolve(
  process.cwd(),
  "supabase/migrations/202608300005_practice_plans.sql",
);

const sessionCareerContextMigrationPath = resolve(
  process.cwd(),
  "supabase/migrations/202608300006_session_career_context.sql",
);

describe("stable profile evidence migration", () => {
  it("adds the durable identity columns to profile_evidence", async () => {
    const migration = await readFile(migrationPath, "utf8");

    expect(migration).toContain("add column evidence_key text");
    expect(migration).toContain("add column is_active boolean not null default true");
    expect(migration).toContain("add column retired_at timestamptz");
  });

  it("backfills a unique legacy key for existing rows before enforcing not null", async () => {
    const migration = await readFile(migrationPath, "utf8");

    expect(migration).toContain("'legacy:' || id::text");
    expect(migration).toContain("alter column evidence_key set not null");
  });

  it("enforces one evidence identity per user", async () => {
    const migration = await readFile(migrationPath, "utf8");

    expect(migration).toContain("unique (user_id, evidence_key)");
  });

  it("reconciles evidence by stable key instead of deleting and reinserting it", async () => {
    const migration = await readFile(migrationPath, "utf8");

    expect(migration).toContain("function public.save_profile_bundle");
    expect(migration).toContain("is_active = false");
    expect(migration).toContain("on conflict (user_id, evidence_key) do update");
    expect(migration).not.toContain("delete from public.profile_evidence");
  });
});

describe("opportunity lifecycle migration", () => {
  it("constrains opportunities.status and opportunity_events.event_type to the spec'd values", async () => {
    const migration = await readFile(opportunitiesMigrationPath, "utf8");

    expect(migration).toContain(
      "check (status in ('considering', 'applied', 'interviewing', 'offer', 'rejected', 'withdrawn', 'closed'))",
    );
    expect(migration).toContain(
      "check (event_type in ('created', 'status_changed', 'interview_scheduled', 'interview_completed', 'note', 'source_updated'))",
    );
  });

  it("constrains match_score to the 0..100 range", async () => {
    const migration = await readFile(opportunitiesMigrationPath, "utf8");

    expect(migration).toContain("check (match_score is null or match_score between 0 and 100)");
  });

  it("gives both tables unique (id, user_id) for composite ownership foreign keys", async () => {
    const migration = await readFile(opportunitiesMigrationPath, "utf8");

    const uniqueIdUserId = migration.match(/unique \(id, user_id\)/g) ?? [];
    expect(uniqueIdUserId).toHaveLength(2);
  });

  it("prevents duplicate external identities per user with a partial unique index", async () => {
    const migration = await readFile(opportunitiesMigrationPath, "utf8");

    expect(migration).toContain(
      "create unique index opportunities_user_source_identity_key\n  on public.opportunities (user_id, source_system, source_external_id)\n  where source_system is not null and source_external_id is not null;",
    );
  });

  it("keeps the opportunity_events foreign key ownership-preserving via the composite key", async () => {
    const migration = await readFile(opportunitiesMigrationPath, "utf8");

    expect(migration).toContain(
      "foreign key (opportunity_id, user_id) references public.opportunities (id, user_id) on delete cascade",
    );
  });

  it("gives opportunities full own-row CRUD RLS policies", async () => {
    const migration = await readFile(opportunitiesMigrationPath, "utf8");

    expect(migration).toContain("create policy select_own on public.opportunities for select using (auth.uid() = user_id);");
    expect(migration).toContain("create policy insert_own on public.opportunities for insert with check (auth.uid() = user_id);");
    expect(migration).toContain("create policy update_own on public.opportunities for update using (auth.uid() = user_id) with check (auth.uid() = user_id);");
    expect(migration).toContain("create policy delete_own on public.opportunities for delete using (auth.uid() = user_id);");
  });

  it("gives opportunity_events only select/insert RLS policies -- no update or delete", async () => {
    const migration = await readFile(opportunitiesMigrationPath, "utf8");

    expect(migration).toContain("create policy select_own on public.opportunity_events for select using (auth.uid() = user_id);");
    expect(migration).toContain("create policy insert_own on public.opportunity_events for insert with check (auth.uid() = user_id);");
    expect(migration).not.toContain("for update on public.opportunity_events");
    expect(migration).not.toContain("for delete on public.opportunity_events");
    expect(migration).not.toMatch(/on public\.opportunity_events for (update|delete)/);
  });

  it("creates all three lifecycle functions as security invoker with no caller-supplied user_id", async () => {
    const migration = await readFile(opportunitiesMigrationPath, "utf8");

    for (const name of ["create_opportunity", "transition_opportunity", "schedule_opportunity_interview"]) {
      const definition = migration.split(`function public.${name}(`)[1];
      expect(definition, `${name} definition`).toBeDefined();
      expect(definition).toContain("security invoker");
      expect(definition.split("$$;")[0]).not.toMatch(/p_user_id/);
    }
    expect(migration).toContain("v_user_id uuid := auth.uid();");
  });

  it("creates the opportunity through create_opportunity as a single 'considering' created event", async () => {
    const migration = await readFile(opportunitiesMigrationPath, "utf8");

    const definition = migration.split("function public.create_opportunity(")[1].split("$$;")[0];
    expect(definition).toContain("'considering'");
    expect((definition.match(/insert into public\.opportunity_events/g) ?? [])).toHaveLength(1);
    expect(definition).toContain("'created', null, 'considering'");
  });

  it("locks and verifies the owned opportunity before transitioning status", async () => {
    const migration = await readFile(opportunitiesMigrationPath, "utf8");

    const definition = migration.split("function public.transition_opportunity(")[1].split("$$;")[0];
    expect(definition).toContain("for update");
    expect(definition).toContain("raise exception 'Owned opportunity was not found' using errcode = 'P0002';");
    expect(definition).toContain("applied_at = case");
    expect(definition).toContain("when p_to_status = 'applied' then coalesce(applied_at, p_occurred_at, now())");
  });

  it("only moves a pre-interview opportunity into interviewing when scheduling an interview", async () => {
    const migration = await readFile(opportunitiesMigrationPath, "utf8");

    const definition = migration.split("function public.schedule_opportunity_interview(")[1].split("$$;")[0];
    expect(definition).toContain("when v_opportunity.status in ('considering', 'applied') then 'interviewing'");
    expect(definition).toContain("else v_opportunity.status");
    expect(definition).toContain("next_interview_at = p_interview_at");
    expect(definition).toContain("'interview_scheduled'");
  });
});

describe("career story provenance migration", () => {
  it("requires exactly one typed evidence source on career_story_evidence", async () => {
    const migration = await readFile(careerStoriesMigrationPath, "utf8");

    expect(migration).toContain("check (num_nonnulls(profile_evidence_id, interview_question_id) = 1)");
  });

  it("gives career_stories unique (id, user_id) for the composite foreign key Task 4 needs", async () => {
    const migration = await readFile(careerStoriesMigrationPath, "utf8");

    const createTable = migration.split("create table public.career_stories (")[1].split(");")[0];
    expect(createTable).toContain("unique (id, user_id)");
  });

  it("does not re-add unique (id, user_id) to career_story_evidence", async () => {
    const migration = await readFile(careerStoriesMigrationPath, "utf8");

    const createTable = migration
      .split("create table public.career_story_evidence (")[1]
      .split("create index")[0];
    expect(createTable).not.toContain("unique (id, user_id)");
  });

  it("uses the exact same-user composite foreign keys the brief specifies", async () => {
    const migration = await readFile(careerStoriesMigrationPath, "utf8");

    expect(migration).toContain(
      "foreign key (career_story_id, user_id)\n    references public.career_stories (id, user_id) on delete cascade,",
    );
    expect(migration).toContain(
      "foreign key (profile_evidence_id, user_id)\n    references public.profile_evidence (id, user_id),",
    );
    expect(migration).toContain(
      "foreign key (interview_question_id, user_id)\n    references public.interview_questions (id, user_id)",
    );
  });

  it("does not cascade the evidence-parent foreign keys, only the career_story_id link", async () => {
    const migration = await readFile(careerStoriesMigrationPath, "utf8");

    const createTable = migration
      .split("create table public.career_story_evidence (")[1]
      .split("create index")[0];
    const foreignKeys = createTable.split("foreign key")[0].length < createTable.length
      ? createTable.slice(createTable.indexOf("foreign key"))
      : "";

    // Exactly one "on delete cascade" among this table's foreign keys -- the
    // career_story_id link -- so the two evidence-parent foreign keys
    // (profile_evidence, interview_questions) fall back to restrictive
    // default delete behavior and cannot silently disappear.
    expect((foreignKeys.match(/on delete cascade/g) ?? [])).toHaveLength(1);
    expect(createTable.split("foreign key (career_story_id")[1]).toContain("on delete cascade");
    expect(createTable.split("foreign key (profile_evidence_id")[1].split("foreign key (interview_question_id")[0])
      .not.toContain("on delete");
    expect(createTable.split("foreign key (interview_question_id")[1]).not.toContain("on delete");
  });

  it("constrains completeness to 0..1 and review_state to the spec'd values", async () => {
    const migration = await readFile(careerStoriesMigrationPath, "utf8");

    expect(migration).toContain("check (completeness between 0 and 1)");
    expect(migration).toContain("check (review_state in ('draft', 'confirmed', 'retired'))");
  });

  it("gives career_stories full own-row CRUD RLS policies", async () => {
    const migration = await readFile(careerStoriesMigrationPath, "utf8");

    expect(migration).toContain("create policy select_own on public.career_stories for select using (auth.uid() = user_id);");
    expect(migration).toContain("create policy insert_own on public.career_stories for insert with check (auth.uid() = user_id);");
    expect(migration).toContain("create policy update_own on public.career_stories for update using (auth.uid() = user_id) with check (auth.uid() = user_id);");
    expect(migration).toContain("create policy delete_own on public.career_stories for delete using (auth.uid() = user_id);");
  });

  it("gives career_story_evidence only select/insert RLS policies -- no update or delete", async () => {
    const migration = await readFile(careerStoriesMigrationPath, "utf8");

    expect(migration).toContain("create policy select_own on public.career_story_evidence for select using (auth.uid() = user_id);");
    expect(migration).toContain("create policy insert_own on public.career_story_evidence for insert with check (auth.uid() = user_id);");
    expect(migration).not.toMatch(/on public\.career_story_evidence for (update|delete)/);
  });
});

describe("coach observation migration", () => {
  it("adds unique (id, user_id) to question_evaluations before referencing it", async () => {
    const migration = await readFile(coachObservationsMigrationPath, "utf8");

    const alterIndex = migration.indexOf(
      "alter table public.question_evaluations\n  add constraint question_evaluations_id_user_key unique (id, user_id);",
    );
    const fkIndex = migration.indexOf("foreign key (question_evaluation_id, user_id)");
    expect(alterIndex).toBeGreaterThan(-1);
    expect(fkIndex).toBeGreaterThan(-1);
    expect(alterIndex).toBeLessThan(fkIndex);
  });

  it("constrains observation_type, trend, and review_state to the spec'd values", async () => {
    const migration = await readFile(coachObservationsMigrationPath, "utf8");

    expect(migration).toContain(
      "check (observation_type in (\n    'strength', 'weakness', 'answer_habit', 'knowledge_gap',\n    'story_gap', 'story_strength', 'delivery_pattern', 'other'\n  ))",
    );
    expect(migration).toContain("check (trend in ('unresolved', 'improving', 'stable', 'worsening'))");
    expect(migration).toContain("check (review_state in ('unreviewed', 'confirmed', 'corrected', 'dismissed'))");
  });

  it("constrains confidence, importance, and weight to 0..1", async () => {
    const migration = await readFile(coachObservationsMigrationPath, "utf8");

    expect(migration).toContain("check (confidence between 0 and 1)");
    expect(migration).toContain("check (importance between 0 and 1)");
    expect(migration).toContain("check (weight between 0 and 1)");
  });

  it("constrains evidence_role to the spec'd values", async () => {
    const migration = await readFile(coachObservationsMigrationPath, "utf8");

    expect(migration).toContain("check (evidence_role in ('supporting', 'contradicting', 'context'))");
  });

  it("requires exactly one of the four typed evidence sources on observation_evidence", async () => {
    const migration = await readFile(coachObservationsMigrationPath, "utf8");

    expect(migration).toContain(
      "check (num_nonnulls(\n    profile_evidence_id,\n    question_evaluation_id,\n    career_story_id,\n    opportunity_event_id\n  ) = 1)",
    );
  });

  it("gives coach_observations unique (id, user_id) for the composite foreign key observation_evidence needs", async () => {
    const migration = await readFile(coachObservationsMigrationPath, "utf8");

    const createTable = migration.split("create table public.coach_observations (")[1].split(");")[0];
    expect(createTable).toContain("unique (id, user_id)");
  });

  it("does not re-add unique (id, user_id) to observation_evidence", async () => {
    const migration = await readFile(coachObservationsMigrationPath, "utf8");

    const createTable = migration
      .split("create table public.observation_evidence (")[1]
      .split("create index")[0];
    expect(createTable).not.toContain("unique (id, user_id)");
  });

  it("uses the exact same-user composite foreign keys the brief specifies", async () => {
    const migration = await readFile(coachObservationsMigrationPath, "utf8");

    expect(migration).toContain(
      "foreign key (observation_id, user_id)\n    references public.coach_observations (id, user_id) on delete cascade,",
    );
    expect(migration).toContain(
      "foreign key (profile_evidence_id, user_id)\n    references public.profile_evidence (id, user_id),",
    );
    expect(migration).toContain(
      "foreign key (question_evaluation_id, user_id)\n    references public.question_evaluations (id, user_id),",
    );
    expect(migration).toContain(
      "foreign key (career_story_id, user_id)\n    references public.career_stories (id, user_id),",
    );
    expect(migration).toContain(
      "foreign key (opportunity_event_id, user_id)\n    references public.opportunity_events (id, user_id)",
    );
  });

  it("does not cascade the evidence-parent foreign keys, only the observation_id link", async () => {
    const migration = await readFile(coachObservationsMigrationPath, "utf8");

    const createTable = migration
      .split("create table public.observation_evidence (")[1]
      .split("create index")[0];
    const foreignKeys = createTable.slice(createTable.indexOf("foreign key"));

    // Exactly one "on delete cascade" among this table's foreign keys --
    // the observation_id link -- so the four evidence-parent foreign keys
    // (profile_evidence, question_evaluations, career_stories,
    // opportunity_events) fall back to restrictive default delete behavior
    // and cannot silently disappear.
    expect((foreignKeys.match(/on delete cascade/g) ?? [])).toHaveLength(1);
    expect(createTable.split("foreign key (observation_id")[1]).toContain("on delete cascade");
    expect(createTable.split("foreign key (profile_evidence_id")[1].split("foreign key (question_evaluation_id")[0])
      .not.toContain("on delete");
    expect(createTable.split("foreign key (question_evaluation_id")[1].split("foreign key (career_story_id")[0])
      .not.toContain("on delete");
    expect(createTable.split("foreign key (career_story_id")[1].split("foreign key (opportunity_event_id")[0])
      .not.toContain("on delete");
    expect(createTable.split("foreign key (opportunity_event_id")[1]).not.toContain("on delete");
  });

  it("gives coach_observations full own-row CRUD RLS policies", async () => {
    const migration = await readFile(coachObservationsMigrationPath, "utf8");

    expect(migration).toContain("create policy select_own on public.coach_observations for select using (auth.uid() = user_id);");
    expect(migration).toContain("create policy insert_own on public.coach_observations for insert with check (auth.uid() = user_id);");
    expect(migration).toContain("create policy update_own on public.coach_observations for update using (auth.uid() = user_id) with check (auth.uid() = user_id);");
    expect(migration).toContain("create policy delete_own on public.coach_observations for delete using (auth.uid() = user_id);");
  });

  it("gives observation_evidence only select/insert RLS policies -- no update or delete", async () => {
    const migration = await readFile(coachObservationsMigrationPath, "utf8");

    expect(migration).toContain("create policy select_own on public.observation_evidence for select using (auth.uid() = user_id);");
    expect(migration).toContain("create policy insert_own on public.observation_evidence for insert with check (auth.uid() = user_id);");
    expect(migration).not.toMatch(/on public\.observation_evidence for (update|delete)/);
  });
});

describe("practice plan migration", () => {
  it("constrains status and format to the spec'd values", async () => {
    const migration = await readFile(practicePlansMigrationPath, "utf8");

    expect(migration).toContain(
      "check (status in ('draft', 'ready', 'started', 'completed', 'cancelled', 'failed'))",
    );
    expect(migration).toContain(
      "check (format in (\n    'targeted_drill', 'story_work', 'self_presentation', 'behavioral',\n    'technical_communication', 'role_prep', 'full_simulation', 'hands_on'\n  ))",
    );
  });

  it("constrains relevance to the spec'd values", async () => {
    const migration = await readFile(practicePlansMigrationPath, "utf8");

    expect(migration).toContain("check (relevance in ('primary', 'supporting'))");
  });

  it("constrains estimated_minutes to the 1..180 range when present", async () => {
    const migration = await readFile(practicePlansMigrationPath, "utf8");

    expect(migration).toContain("check (estimated_minutes is null or estimated_minutes between 1 and 180)");
  });

  it("gives practice_plans unique (id, user_id) for the composite foreign key practice_plan_opportunities needs", async () => {
    const migration = await readFile(practicePlansMigrationPath, "utf8");

    const createTable = migration.split("create table public.practice_plans (")[1].split(");")[0];
    expect(createTable).toContain("unique (id, user_id)");
  });

  it("uses the exact same-user composite foreign keys the brief specifies", async () => {
    const migration = await readFile(practicePlansMigrationPath, "utf8");

    expect(migration).toContain(
      "foreign key (practice_plan_id, user_id)\n    references public.practice_plans (id, user_id) on delete cascade,",
    );
    expect(migration).toContain(
      "foreign key (opportunity_id, user_id)\n    references public.opportunities (id, user_id) on delete cascade",
    );
  });

  it("keys practice_plan_opportunities on (practice_plan_id, opportunity_id)", async () => {
    const migration = await readFile(practicePlansMigrationPath, "utf8");

    expect(migration).toContain("primary key (practice_plan_id, opportunity_id)");
  });

  it("enforces at most one primary opportunity per plan with a partial unique index", async () => {
    const migration = await readFile(practicePlansMigrationPath, "utf8");

    expect(migration).toContain(
      "create unique index practice_plan_one_primary_opportunity_idx\non public.practice_plan_opportunities (practice_plan_id)\nwhere relevance = 'primary';",
    );
  });

  it("gives practice_plans full own-row CRUD RLS policies", async () => {
    const migration = await readFile(practicePlansMigrationPath, "utf8");

    expect(migration).toContain("create policy select_own on public.practice_plans for select using (auth.uid() = user_id);");
    expect(migration).toContain("create policy insert_own on public.practice_plans for insert with check (auth.uid() = user_id);");
    expect(migration).toContain("create policy update_own on public.practice_plans for update using (auth.uid() = user_id) with check (auth.uid() = user_id);");
    expect(migration).toContain("create policy delete_own on public.practice_plans for delete using (auth.uid() = user_id);");
  });

  it("gives practice_plan_opportunities select/insert/delete RLS policies -- but no update policy", async () => {
    const migration = await readFile(practicePlansMigrationPath, "utf8");

    expect(migration).toContain("create policy select_own on public.practice_plan_opportunities for select using (auth.uid() = user_id);");
    expect(migration).toContain("create policy insert_own on public.practice_plan_opportunities for insert with check (auth.uid() = user_id);");
    expect(migration).toContain("create policy delete_own on public.practice_plan_opportunities for delete using (auth.uid() = user_id);");
    expect(migration).not.toMatch(/on public\.practice_plan_opportunities for update/);
  });
});

// No live Supabase target exists in this environment (no `supabase` CLI, no
// credentials), so `supabase db push` and manual DB verification cannot run.
// This block statically verifies the migration's invariants instead --
// both columns nullable, both composite same-user foreign keys using the
// required `on delete set null (column)` form, and the user/context
// indexes -- in place of Task 6 brief Step 7.
describe("session career context migration", () => {
  it("adds practice_plan_id and opportunity_id as nullable uuid columns", async () => {
    const migration = await readFile(sessionCareerContextMigrationPath, "utf8");

    expect(migration).toContain("add column practice_plan_id uuid,");
    expect(migration).toContain("add column opportunity_id uuid,");
    // Neither column may be constrained not null -- historical sessions
    // must remain valid without a backfill guess.
    expect(migration).not.toMatch(/practice_plan_id uuid not null/);
    expect(migration).not.toMatch(/opportunity_id uuid not null/);
  });

  it("uses the exact same-user composite foreign keys, with the required column-list on delete set null form", async () => {
    const migration = await readFile(sessionCareerContextMigrationPath, "utf8");

    // interview_sessions.user_id is not null, so a plain composite
    // `on delete set null` would try to null user_id too and fail at
    // runtime -- the column-list form nulls only the Career Brain
    // reference and leaves ownership intact. This is deliberate, not a
    // typo; see the migration's header comment.
    expect(migration).toContain(
      "foreign key (practice_plan_id, user_id)\n    references public.practice_plans (id, user_id) on delete set null (practice_plan_id),",
    );
    expect(migration).toContain(
      "foreign key (opportunity_id, user_id)\n    references public.opportunities (id, user_id) on delete set null (opportunity_id);",
    );
  });

  it("does not re-create the practice_plans or opportunities tables -- Tasks 5 and 2 already added them", async () => {
    const migration = await readFile(sessionCareerContextMigrationPath, "utf8");

    expect(migration).not.toMatch(/create table public\.(practice_plans|opportunities)/);
  });

  it("adds cheap user/context indexes for later Release 2 queries", async () => {
    const migration = await readFile(sessionCareerContextMigrationPath, "utf8");

    expect(migration).toContain(
      "create index interview_sessions_user_opportunity_idx\n  on public.interview_sessions (user_id, opportunity_id, created_at desc);",
    );
    expect(migration).toContain(
      "create index interview_sessions_user_plan_idx\n  on public.interview_sessions (user_id, practice_plan_id);",
    );
  });
});
