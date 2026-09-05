import { describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));

import {
  attachCareerStoryEvidence,
  createCareerStory,
  getCareerStory,
  listCareerStories,
  listCareerStoryEvidence,
  updateCareerStory,
} from "@/lib/repositories/stories";

type Row = Record<string, unknown>;
type QueryResult = { data: unknown; error: { code: string } | null };

const storyRow = (overrides: Row = {}): Row => ({
  id: "story-1",
  user_id: "user-1",
  title: "Migrated the legacy payments service",
  situation: "The payments service was on an unsupported runtime.",
  responsibility: "I owned the migration end to end.",
  problem: "Security patches were no longer available.",
  actions: "Planned a phased cutover and led the rollout.",
  alternatives: "Considered a full rewrite instead of migration.",
  tradeoffs: "Chose incremental migration to reduce risk.",
  ownership: "Sole engineer on the project.",
  outcome: "Zero downtime cutover, retired the legacy runtime.",
  lessons: "Phased rollouts de-risk large migrations.",
  tags: ["migration", "ownership"],
  completeness: 0.8,
  review_state: "draft",
  confirmed_at: null,
  created_at: "2026-08-30T10:00:00.000Z",
  updated_at: "2026-08-30T10:00:00.000Z",
  ...overrides,
});

const evidenceRow = (overrides: Row = {}): Row => ({
  id: "evidence-link-1",
  user_id: "user-1",
  career_story_id: "story-1",
  profile_evidence_id: "evidence-1",
  interview_question_id: null,
  note: "Supports the migration outcome",
  created_at: "2026-08-30T10:00:00.000Z",
  ...overrides,
});

/** A single reusable chainable stub covering insert/select/update/eq/order/maybeSingle. */
function tableStub(
  result: QueryResult,
  capture?: { insert?: Row; update?: Row; eq?: Array<[string, unknown]> },
) {
  const builder: Record<string, unknown> = {
    insert: (row: Row) => {
      if (capture) capture.insert = row;
      return builder;
    },
    update: (patch: Row) => {
      if (capture) capture.update = patch;
      return builder;
    },
    select: () => builder,
    eq: (field: string, value: unknown) => {
      if (capture) (capture.eq ??= []).push([field, value]);
      return builder;
    },
    order: async () => result,
    maybeSingle: async () => result,
  };
  return builder;
}

describe("career story repository", () => {
  it("creates a career story and maps structured fields and tags", async () => {
    const capture: { insert?: Row } = {};
    const from = vi.fn(() => tableStub({ data: storyRow(), error: null }, capture));
    const supabase = { from };

    const story = await createCareerStory(supabase as never, "user-1", {
      title: "Migrated the legacy payments service",
      situation: "The payments service was on an unsupported runtime.",
      tags: ["migration", "ownership"],
    });

    expect(from).toHaveBeenCalledWith("career_stories");
    expect(capture.insert).toEqual(expect.objectContaining({
      user_id: "user-1",
      title: "Migrated the legacy payments service",
      tags: ["migration", "ownership"],
    }));
    expect(story).toEqual(expect.objectContaining({
      id: "story-1",
      userId: "user-1",
      title: "Migrated the legacy payments service",
      tags: ["migration", "ownership"],
      completeness: 0.8,
      reviewState: "draft",
    }));
  });

  it("loads a single owned career story scoped by user id", async () => {
    const capture: { eq?: Array<[string, unknown]> } = {};
    const from = vi.fn(() => tableStub({ data: storyRow(), error: null }, capture));
    const supabase = { from };

    const story = await getCareerStory(supabase as never, "user-1", "story-1");

    expect(from).toHaveBeenCalledWith("career_stories");
    expect(story?.title).toBe("Migrated the legacy payments service");
    expect(capture.eq).toEqual(expect.arrayContaining([
      ["id", "story-1"],
      ["user_id", "user-1"],
    ]));
  });

  it("returns null when the career story is not found", async () => {
    const from = vi.fn(() => tableStub({ data: null, error: null }));
    const supabase = { from };

    const story = await getCareerStory(supabase as never, "user-1", "missing");

    expect(story).toBeNull();
  });

  it("lists career stories mapped from snake_case rows", async () => {
    const from = vi.fn(() => tableStub({
      data: [storyRow(), storyRow({ id: "story-2" })],
      error: null,
    }));
    const supabase = { from };

    const stories = await listCareerStories(supabase as never, "user-1");

    expect(stories).toHaveLength(2);
    expect(stories[0]).toEqual(expect.objectContaining({ id: "story-1", title: "Migrated the legacy payments service" }));
  });

  it("updates only the provided career story fields", async () => {
    const capture: { update?: Row } = {};
    const from = vi.fn(() => tableStub({ data: storyRow({ outcome: "Updated outcome" }), error: null }, capture));
    const supabase = { from };

    const story = await updateCareerStory(supabase as never, "user-1", "story-1", {
      outcome: "Updated outcome",
    });

    expect(story.outcome).toBe("Updated outcome");
    expect(capture.update).toBeDefined();
    expect(capture.update).toHaveProperty("outcome", "Updated outcome");
    expect(capture.update).not.toHaveProperty("title");
    expect(capture.update).not.toHaveProperty("tags");
  });

  it("persists profile evidence provenance with exactly one source", async () => {
    const capture: { insert?: Row } = {};
    const builder = tableStub({ data: evidenceRow(), error: null }, capture);
    const insert = vi.fn((row: Row) => {
      capture.insert = row;
      return builder;
    });
    const from = vi.fn(() => ({ ...builder, insert }));
    const supabase = { from };

    await attachCareerStoryEvidence(
      supabase as never,
      "user-1",
      "story-1",
      { kind: "profile_evidence", profileEvidenceId: "evidence-1" },
      "Supports the migration outcome",
    );

    expect(insert).toHaveBeenCalledWith(expect.objectContaining({
      user_id: "user-1",
      career_story_id: "story-1",
      profile_evidence_id: "evidence-1",
      interview_question_id: null,
    }));
  });

  it("persists interview question provenance with exactly one source", async () => {
    const capture: { insert?: Row } = {};
    const from = vi.fn(() => tableStub({
      data: evidenceRow({ profile_evidence_id: null, interview_question_id: "question-1" }),
      error: null,
    }, capture));
    const supabase = { from };

    const evidence = await attachCareerStoryEvidence(
      supabase as never,
      "user-1",
      "story-1",
      { kind: "interview_question", interviewQuestionId: "question-1" },
    );

    expect(capture.insert).toEqual(expect.objectContaining({
      user_id: "user-1",
      career_story_id: "story-1",
      profile_evidence_id: null,
      interview_question_id: "question-1",
    }));
    expect(evidence).toEqual(expect.objectContaining({
      profileEvidenceId: null,
      interviewQuestionId: "question-1",
    }));
  });

  it("lists a career story's evidence scoped by user and story", async () => {
    const capture: { eq?: Array<[string, unknown]> } = {};
    const from = vi.fn(() => tableStub({ data: [evidenceRow()], error: null }, capture));
    const supabase = { from };

    const evidence = await listCareerStoryEvidence(supabase as never, "user-1", "story-1");

    expect(from).toHaveBeenCalledWith("career_story_evidence");
    expect(evidence).toEqual([expect.objectContaining({
      id: "evidence-link-1",
      careerStoryId: "story-1",
      profileEvidenceId: "evidence-1",
      interviewQuestionId: null,
      note: "Supports the migration outcome",
    })]);
    expect(capture.eq).toEqual(expect.arrayContaining([
      ["user_id", "user-1"],
      ["career_story_id", "story-1"],
    ]));
  });
});
