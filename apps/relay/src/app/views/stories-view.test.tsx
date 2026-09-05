import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi, type Mock } from "vitest";
import type { CareerStoryEvidence, CareerStorySummary } from "@/lib/types";
import { StoriesView } from "@/app/views/stories-view";

type StoriesViewMocks = {
  onCreate: Mock;
  onUpdate: Mock;
  onConfirm: Mock;
  onRetire: Mock;
  onAttachProfileEvidence: Mock;
};

function story(overrides: Partial<CareerStorySummary> = {}): CareerStorySummary {
  return {
    id: "story-1",
    userId: "user-1",
    title: "Checkout migration",
    situation: "Legacy checkout was blocking releases.",
    responsibility: null,
    problem: null,
    actions: "Phased the migration by route.",
    alternatives: null,
    tradeoffs: null,
    ownership: "Led the frontend workstream.",
    outcome: null,
    lessons: null,
    tags: ["react", "migration"],
    completeness: 0.5,
    reviewState: "draft",
    confirmedAt: null,
    createdAt: "2026-08-15T10:00:00.000Z",
    updatedAt: "2026-08-15T10:00:00.000Z",
    evidenceCount: 2,
    ...overrides,
  };
}

function renderStories(stories: CareerStorySummary[], overrides: Partial<StoriesViewMocks> = {}) {
  const defaults: StoriesViewMocks = {
    onCreate: vi.fn().mockResolvedValue(story()),
    onUpdate: vi.fn().mockResolvedValue(story()),
    onConfirm: vi.fn().mockResolvedValue(story({ reviewState: "confirmed" })),
    onRetire: vi.fn().mockResolvedValue(story({ reviewState: "retired" })),
    onAttachProfileEvidence: vi.fn().mockResolvedValue({
      id: "story-evidence-1",
      userId: "user-1",
      careerStoryId: "story-1",
      profileEvidenceId: "evidence-1",
      interviewQuestionId: null,
      note: null,
      createdAt: "2026-08-15T10:00:00.000Z",
    } satisfies CareerStoryEvidence),
  };
  const effective = { ...defaults, ...overrides };
  const view = render(
    <StoriesView
      stories={stories}
      profileEvidence={[
        {
          id: "evidence-1",
          sourceKind: "cv",
          sourceExcerpt: "Led the checkout migration from legacy React Router to App Router.",
          projectOrEmployer: "Checkout rewrite",
          ownership: "Frontend lead",
          technologies: ["React"],
          decision: null,
          constraint: null,
          outcome: null,
          recency: "2025",
          confidence: 0.9,
        },
      ]}
      busy={false}
      {...effective}
    />,
  );
  return {
    ...effective,
    rerenderWithStories: (next: CareerStorySummary[]) =>
      view.rerender(
        <StoriesView
          stories={next}
          profileEvidence={[]}
          busy={false}
          {...effective}
        />,
      ),
  };
}

describe("StoriesView", () => {
  it("shows an honest empty state when there are no stories", () => {
    renderStories([]);

    expect(screen.getByText(/haven.t added any career stories yet/i)).toBeInTheDocument();
  });

  it("creates a story with the drafted fields", async () => {
    const { onCreate } = renderStories([]);

    fireEvent.click(screen.getByRole("button", { name: "New story" }));
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Checkout migration" } });
    fireEvent.change(screen.getByLabelText("Situation"), { target: { value: "Legacy checkout was blocking releases." } });
    fireEvent.change(screen.getByLabelText("Actions / decisions"), { target: { value: "Phased the migration by route." } });
    fireEvent.change(screen.getByLabelText(/Tags/), { target: { value: "react, migration" } });
    fireEvent.click(screen.getByRole("button", { name: "Save story" }));

    await waitFor(() => expect(onCreate).toHaveBeenCalledTimes(1));
    expect(onCreate.mock.calls[0][0]).toMatchObject({
      title: "Checkout migration",
      situation: "Legacy checkout was blocking releases.",
      actions: "Phased the migration by route.",
      tags: ["react", "migration"],
    });
  });

  it("edits a selected story's fields", async () => {
    const { onUpdate } = renderStories([story()]);

    fireEvent.click(screen.getByRole("button", { name: /Checkout migration/ }));
    fireEvent.click(screen.getByRole("button", { name: "Edit story" }));
    fireEvent.change(screen.getByLabelText("Outcome"), { target: { value: "Rolled out with zero downtime." } });
    fireEvent.click(screen.getByRole("button", { name: "Save story" }));

    await waitFor(() => expect(onUpdate).toHaveBeenCalledTimes(1));
    expect(onUpdate.mock.calls[0][0]).toBe("story-1");
    expect(onUpdate.mock.calls[0][1]).toMatchObject({ outcome: "Rolled out with zero downtime." });
  });

  // Story completeness (`careerStoryCompleteness` in src/lib/career-story.ts) is
  // computed server-side as coveredDimensions/6 across six FACTUAL dimensions --
  // never delivery/answer quality (design section 4.3). This proves the view
  // presents it as factual coverage and never claims to grade how the story was
  // told: a wrong implementation that instead showed "quality"/"delivery"
  // wording, or omitted the "6" denominator, would fail this.
  it("presents completeness as factual coverage, never delivery quality", () => {
    renderStories([story({ completeness: 4 / 6 })]);

    fireEvent.click(screen.getByRole("button", { name: /Checkout migration/ }));

    expect(screen.getByText("4 of 6 factual dimensions covered")).toBeInTheDocument();
    expect(screen.getByText(/factual coverage only/i)).toBeInTheDocument();
    expect(screen.queryByText(/quality/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/how well/i)).not.toBeInTheDocument();
  });

  it("confirms a draft story", async () => {
    const { onConfirm } = renderStories([story()]);

    fireEvent.click(screen.getByRole("button", { name: /Checkout migration/ }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm story" }));

    await waitFor(() => expect(onConfirm).toHaveBeenCalledWith("story-1"));
  });

  it("does not offer to confirm an already-confirmed story", () => {
    renderStories([story({ reviewState: "confirmed", confirmedAt: "2026-08-16T10:00:00.000Z" })]);

    fireEvent.click(screen.getByRole("button", { name: /Checkout migration/ }));

    expect(screen.queryByRole("button", { name: "Confirm story" })).not.toBeInTheDocument();
  });

  // Retiring is a state change, not a delete (design section 4.3): the row
  // survives, but the DEFAULT list must stop showing it (R16 -- the route
  // returns retired stories too, the view is what filters). A wrong
  // implementation that renders every story regardless of reviewState would
  // still show "Checkout migration" after this rerender, so this fails it.
  it("removes a retired story from the default list", async () => {
    const { onRetire, rerenderWithStories } = renderStories([story()]);

    fireEvent.click(screen.getByRole("button", { name: /Checkout migration/ }));
    fireEvent.click(screen.getByRole("button", { name: "Retire story" }));

    await waitFor(() => expect(onRetire).toHaveBeenCalledWith("story-1"));

    rerenderWithStories([story({ reviewState: "retired" })]);

    expect(screen.queryByRole("button", { name: "Checkout migration" })).not.toBeInTheDocument();
    expect(screen.getByText(/haven.t added any career stories yet/i)).toBeInTheDocument();
  });

  it("shows the attached provenance count", () => {
    renderStories([story({ evidenceCount: 3 })]);

    fireEvent.click(screen.getByRole("button", { name: /Checkout migration/ }));

    expect(screen.getByText("3 attached evidence items")).toBeInTheDocument();
  });

  it("attaches current profile evidence to a story", async () => {
    const { onAttachProfileEvidence } = renderStories([story()]);

    fireEvent.click(screen.getByRole("button", { name: /Checkout migration/ }));
    fireEvent.change(screen.getByLabelText("Attach profile evidence"), { target: { value: "evidence-1" } });
    fireEvent.click(screen.getByRole("button", { name: "Attach evidence" }));

    await waitFor(() => expect(onAttachProfileEvidence).toHaveBeenCalledWith("story-1", "evidence-1", undefined));
  });
});
