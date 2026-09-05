"use client";

import { useState } from "react";
import type { AssistanceRecord, EvidenceItem, InterviewSession } from "@/lib/types";

const evaluationDimensionLabels = [
  ["correctness", "Correctness"],
  ["depth", "Depth"],
  ["clarity", "Clarity"],
  ["structure", "Structure"],
  ["practicalExperience", "Practical experience"],
  ["tradeOffAwareness", "Trade-off awareness"],
  ["communication", "Communication"],
  ["confidence", "Confidence"],
  ["relevance", "Relevance"],
] as const;

type EvaluationDimensionKey = keyof InterviewSession["evaluations"][number]["dimensions"];

function slugToken(value: string | null | undefined, fallback: string): string {
  const normalized = (value ?? "")
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9_-]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return normalized || fallback;
}

function evaluationKey(session: InterviewSession, index: number): string {
  const evaluation = session.evaluations[index];
  const scope = evaluation.questionId
    ? `question-${slugToken(evaluation.questionId, `question-${index}`)}`
    : `${slugToken(evaluation.competencyId, "general")}-${slugToken(evaluation.competency, `evaluation-${index}`)}`;
  return `${scope}-${index}`;
}

function joinOrFallback(values: string[], fallback: string): string {
  return values.length > 0 ? values.join(" · ") : fallback;
}

function evidenceLabel(item: EvidenceItem): string {
  const summary = [
    item.projectOrEmployer?.trim(),
    item.ownership?.trim(),
    item.outcome?.trim(),
  ].filter((value): value is string => Boolean(value));

  return summary.length > 0 ? summary.join(" · ") : item.sourceExcerpt.trim();
}

/**
 * Describes the rescues that produced a Coach-mode score, so the score is
 * never shown alone (spec §8.4) -- a bare number flatters the candidate when
 * it was actually reached with help. Spells out "one"/"two" since those are
 * the only counts common enough to read oddly as digits; anything higher
 * falls back to the digit form rather than spelling out an unbounded range.
 */
function assistanceLabel(assistance: AssistanceRecord[]): string | null {
  if (assistance.length === 0) return null;
  const count = assistance.length === 1 ? "one rescue" : assistance.length === 2 ? "two rescues" : `${assistance.length} rescues`;
  return `reached after ${count}`;
}

function startViewTransition(update: () => void) {
  const documentWithTransition = document as Document & { startViewTransition?: (callback: () => void) => void };
  if (documentWithTransition.startViewTransition) {
    documentWithTransition.startViewTransition(update);
    return;
  }
  update();
}

/**
 * Renders per-evaluation interview coaching with accessible disclosure controls and
 * question-linked details when question evidence exists for that evaluation.
 */
export function ResultsFeedbackCards({
  session,
  evidence = [],
}: {
  session: InterviewSession;
  evidence?: EvidenceItem[];
}) {
  const answeredQuestions = session.questions.filter((question) => Boolean(question.answer));
  const answeredQuestionsById = new Map(answeredQuestions.map((question) => [question.id, question]));
  // Unlike `answeredQuestionsById`, this covers every question regardless of
  // answer status -- a non-answer question is never in `answeredQuestions`
  // (it has no `answer`), but its score row still needs its `nonAnswer` and
  // `assistance` fields to render "Not attempted" instead of a bare score.
  const questionsById = new Map(session.questions.map((question) => [question.id, question]));
  const evidenceById = new Map(evidence.map((item) => [item.id, item]));
  const [expandedEvaluationKey, setExpandedEvaluationKey] = useState<string | null>(null);

  return (
    <div className="mt-6 grid gap-4 md:grid-cols-2">
      {session.evaluations.map((evaluation, index) => {
        const currentKey = evaluationKey(session, index);
        const expanded = expandedEvaluationKey === currentKey;
        const buttonId = `${currentKey}-toggle`;
        const regionId = `${currentKey}-details`;
        const answeredQuestion = evaluation.questionId
          ? answeredQuestionsById.get(evaluation.questionId) ?? null
          : answeredQuestions[index] ?? null;
        const linkedQuestion = evaluation.questionId
          ? questionsById.get(evaluation.questionId) ?? null
          : answeredQuestions[index] ?? null;
        const dimensions = evaluationDimensionLabels.filter(([key]) => {
          const value = evaluation.dimensions[key as EvaluationDimensionKey];
          return typeof value === "number" && Number.isFinite(value);
        });
        const evidenceTargets = (session.blueprint?.questions.find((question) => question.id === evaluation.questionId)?.evidenceIds ?? [])
          .map((id) => evidenceById.get(id))
          .filter((item): item is EvidenceItem => Boolean(item))
          .map(evidenceLabel);

        return (
          <article
            key={currentKey}
            className="rounded-2xl border border-[var(--line)] bg-[var(--paper)] p-5"
            style={{ viewTransitionName: `evaluation-card-${currentKey}` }}
          >
            <div className="flex items-start justify-between gap-3">
              <div>
                <h2 className="font-semibold">{evaluation.competency}</h2>
                <p className="mt-1 text-sm leading-6 text-[var(--ink-muted)]">{evaluation.strengths[0] ?? evaluation.needsWork[0] ?? "Open the details to review the full coaching."}</p>
              </div>
              <span className="shrink-0 text-right font-semibold text-[var(--pine)]">
                {linkedQuestion?.nonAnswer ? "Not attempted" : (
                  <>
                    {evaluation.score.toFixed(1)}/10
                    {linkedQuestion && assistanceLabel(linkedQuestion.assistance) && (
                      <span className="mt-1 block text-xs font-normal text-[var(--ink-muted)]">{assistanceLabel(linkedQuestion.assistance)}</span>
                    )}
                  </>
                )}
              </span>
            </div>
            <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
              <div className="flex flex-wrap gap-4 text-sm">
                {evaluation.strengths[0] && <div><p className="font-semibold text-[#416151]">Good</p><p className="leading-6 text-[var(--ink-muted)]">{evaluation.strengths[0]}</p></div>}
                {evaluation.needsWork[0] && <div><p className="font-semibold text-[#8e5e20]">Try next</p><p className="leading-6 text-[var(--ink-muted)]">{evaluation.needsWork[0]}</p></div>}
              </div>
              <button
                type="button"
                id={buttonId}
                aria-controls={regionId}
                aria-expanded={expanded}
                aria-label={`${evaluation.competency} feedback`}
                onClick={() => startViewTransition(() => setExpandedEvaluationKey((current) => current === currentKey ? null : currentKey))}
                className="inline-flex items-center gap-2 rounded-full border border-[var(--line)] px-4 py-2 text-sm font-semibold text-[var(--pine)]"
              >
                {expanded ? "Hide details" : "Show details"}
                <span
                  aria-hidden="true"
                  className={`text-base transition-transform duration-200 motion-reduce:transition-none ${expanded ? "rotate-45" : "rotate-0"}`}
                >
                  +
                </span>
              </button>
            </div>
            {expanded && (
              <div
                id={regionId}
                role="region"
                aria-labelledby={buttonId}
                className="mt-5 space-y-5 border-t border-[var(--line)] pt-5 text-sm"
              >
                {answeredQuestion && (
                  <div className="space-y-3">
                    <div>
                      <p className="font-semibold text-[var(--ink-muted)]">Question</p>
                      <p className="mt-1 leading-6">{answeredQuestion.prompt}</p>
                    </div>
                    <div>
                      <p className="font-semibold text-[var(--ink-muted)]">Your answer</p>
                      <p className="mt-1 leading-6">{answeredQuestion.answer}</p>
                    </div>
                  </div>
                )}
                {session.blueprint?.status === "limited-grounding" && (
                  <div className="rounded-2xl border border-[#e4c9a0] bg-[#fff6eb] px-4 py-3 text-[#8e5e20]">
                    <p className="text-xs font-semibold uppercase tracking-[.12em]">Broader practice</p>
                    <p className="mt-1 leading-6">
                      {session.blueprint.fallbackReason ?? "This session is using broader questions because Relay has less source grounding for this practice than usual."}
                    </p>
                  </div>
                )}
                {evaluation.questionId && (
                  <div className="space-y-3 rounded-2xl border border-[var(--line)] bg-[#f8f7f2] px-4 py-4">
                    <div>
                      <p className="font-semibold text-[var(--ink-muted)]">Question objective</p>
                      <p className="mt-1 leading-6">{session.blueprint?.questions.find((question) => question.id === evaluation.questionId)?.objective ?? "Ground the answer in the exact question that was asked."}</p>
                    </div>
                    <div>
                      <p className="font-semibold text-[var(--ink-muted)]">Evidence target</p>
                      {evidenceTargets.length > 0 ? (
                        <ul className="mt-3 space-y-2 leading-6 text-[var(--ink-muted)]">
                          {evidenceTargets.map((target) => <li key={target} className="rounded-2xl bg-white px-3 py-3">{target}</li>)}
                        </ul>
                      ) : (
                        <p className="mt-1 leading-6">{joinOrFallback([], "No explicit evidence target recorded.")}</p>
                      )}
                    </div>
                    <div>
                      <p className="font-semibold text-[var(--ink-muted)]">Expected signals</p>
                      <p className="mt-1 leading-6">
                        {joinOrFallback(
                          session.blueprint?.questions.find((question) => question.id === evaluation.questionId)?.expectedSignals ?? [],
                          "No expected signals recorded.",
                        )}
                      </p>
                    </div>
                  </div>
                )}
                {"relevance" in evaluation && typeof evaluation.relevance === "number" && (
                  <div className="rounded-2xl border border-[var(--line)] bg-white px-4 py-4">
                    <p className="font-semibold text-[var(--ink-muted)]">Relevance</p>
                    <p className="mt-1 text-lg font-semibold text-[var(--pine)]">{evaluation.relevance.toFixed(1)}/10</p>
                  </div>
                )}
                {"supportedClaims" in evaluation && (evaluation.supportedClaims?.length ?? 0) > 0 && (
                  <div>
                    <p className="font-semibold text-[var(--ink-muted)]">Supported claims</p>
                    <ul className="mt-3 space-y-2 leading-6 text-[var(--ink-muted)]">
                      {evaluation.supportedClaims?.map((claim) => <li key={claim} className="rounded-2xl bg-[#eef3e7] px-3 py-3 text-[#38502e]">{claim}</li>)}
                    </ul>
                  </div>
                )}
                {"expectedSignalsPresent" in evaluation && (evaluation.expectedSignalsPresent?.length ?? 0) > 0 && (
                  <div>
                    <p className="font-semibold text-[var(--ink-muted)]">Expected signals present</p>
                    <ul className="mt-3 space-y-2 leading-6 text-[var(--ink-muted)]">
                      {evaluation.expectedSignalsPresent?.map((signal) => <li key={signal} className="rounded-2xl bg-[#eef3e7] px-3 py-3 text-[#38502e]">{signal}</li>)}
                    </ul>
                  </div>
                )}
                {"unsupportedClaims" in evaluation && (evaluation.unsupportedClaims?.length ?? 0) > 0 && (
                  <div>
                    <p className="font-semibold text-[var(--ink-muted)]">Unsupported claims</p>
                    <ul className="mt-3 space-y-2 leading-6 text-[var(--ink-muted)]">
                      {evaluation.unsupportedClaims?.map((claim) => <li key={claim} className="rounded-2xl bg-[#fff6eb] px-3 py-3 text-[#8e5e20]">{claim}</li>)}
                    </ul>
                  </div>
                )}
                {"dimensionReasons" in evaluation && evaluation.dimensionReasons && (
                  <div>
                    <p className="font-semibold text-[var(--ink-muted)]">Dimension reasons</p>
                    <ul className="mt-3 space-y-2 leading-6 text-[var(--ink-muted)]">
                      {Object.entries(evaluation.dimensionReasons).map(([dimension, reason]) => <li key={dimension} className="rounded-2xl bg-[#f4f1eb] px-3 py-3">{reason}</li>)}
                    </ul>
                  </div>
                )}
                {dimensions.length > 0 && (
                  <div>
                    <p className="font-semibold text-[var(--ink-muted)]">Dimensions</p>
                    <div className="mt-3 grid gap-2 sm:grid-cols-2">
                      {dimensions.map(([key, label]) => (
                        <div key={key} className="rounded-2xl bg-[#eef3e7] px-3 py-3">
                          <p className="text-xs font-semibold uppercase tracking-[.12em] text-[#537053]">{label}</p>
                          <p className="mt-1 font-semibold text-[#1d332b]">{evaluation.dimensions[key as EvaluationDimensionKey]}/10</p>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
                {evaluation.strengths.length > 0 && (
                  <div>
                    <p className="font-semibold text-[var(--ink-muted)]">Strengths</p>
                    <ul className="mt-3 space-y-2 leading-6 text-[var(--ink-muted)]">
                      {evaluation.strengths.map((strength) => <li key={strength} className="rounded-2xl bg-[#eef3e7] px-3 py-3 text-[#38502e]">{strength}</li>)}
                    </ul>
                  </div>
                )}
                {evaluation.missingPoints.length > 0 && (
                  <div>
                    <p className="font-semibold text-[var(--ink-muted)]">Missing points</p>
                    <ul className="mt-3 space-y-2 leading-6 text-[var(--ink-muted)]">
                      {evaluation.missingPoints.map((point) => <li key={point} className="rounded-2xl bg-[#fff6eb] px-3 py-3 text-[#8e5e20]">{point}</li>)}
                    </ul>
                  </div>
                )}
                {evaluation.betterStructure.length > 0 && (
                  <div>
                    <p className="font-semibold text-[var(--ink-muted)]">Better structure</p>
                    <ul className="mt-3 space-y-2 leading-6 text-[var(--ink-muted)]">
                      {evaluation.betterStructure.map((item) => <li key={item} className="rounded-2xl bg-white px-3 py-3">{item}</li>)}
                    </ul>
                  </div>
                )}
                {evaluation.improvedAnswer && (
                  <div>
                    <p className="font-semibold text-[var(--ink-muted)]">Improved answer</p>
                    <p className="mt-3 rounded-2xl bg-[#f4f1eb] px-4 py-4 leading-6">{evaluation.improvedAnswer}</p>
                  </div>
                )}
              </div>
            )}
          </article>
        );
      })}
    </div>
  );
}
