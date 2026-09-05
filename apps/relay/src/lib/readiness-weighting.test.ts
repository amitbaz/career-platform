import { describe, expect, it } from "vitest";
import {
  MINIMUM_RECENCY_FACTOR,
  MINIMUM_RELEVANCE,
  RECENCY_HALF_LIFE_DAYS,
  evidenceStrength,
  recencyFactor,
  relevanceFactor,
} from "@/lib/readiness-weighting";

const conditions = (overrides: Partial<Parameters<typeof evidenceStrength>[0]> = {}) => ({
  mode: "real" as const,
  degraded: false,
  assistanceCount: 0,
  ...overrides,
});

describe("evidenceStrength", () => {
  it("gives full strength to an unassisted answer under real interview conditions", () => {
    expect(evidenceStrength(conditions())).toBe(1);
  });

  it("materially discounts a coach-mode answer against a real-mode one", () => {
    expect(evidenceStrength(conditions({ mode: "coach" }))).toBeLessThan(
      evidenceStrength(conditions({ mode: "real" })) / 2,
    );
  });

  it("discounts each rescue the interviewer had to give", () => {
    const none = evidenceStrength(conditions({ assistanceCount: 0 }));
    const one = evidenceStrength(conditions({ assistanceCount: 1 }));
    const two = evidenceStrength(conditions({ assistanceCount: 2 }));
    expect(one).toBeLessThan(none);
    expect(two).toBeLessThan(one);
  });

  it("does not discount further beyond two rescues", () => {
    expect(evidenceStrength(conditions({ assistanceCount: 5 }))).toBe(
      evidenceStrength(conditions({ assistanceCount: 2 })),
    );
  });

  it("halves a degraded session, which ran on the fallback interviewer", () => {
    expect(evidenceStrength(conditions({ degraded: true }))).toBe(0.5);
  });

  it("never returns a negative or above-one strength", () => {
    const worst = evidenceStrength(conditions({ mode: "coach", degraded: true, assistanceCount: 9 }));
    expect(worst).toBeGreaterThan(0);
    expect(worst).toBeLessThan(0.1);
  });
});

describe("recencyFactor", () => {
  const now = new Date("2026-09-04T12:00:00.000Z");
  const daysAgo = (days: number) => new Date(now.getTime() - days * 86_400_000).toISOString();

  it("gives evidence recorded right now its full weight", () => {
    expect(recencyFactor(daysAgo(0), now)).toBe(1);
  });

  it("halves the weight after exactly one half-life", () => {
    expect(recencyFactor(daysAgo(RECENCY_HALF_LIFE_DAYS), now)).toBeCloseTo(0.5, 3);
  });

  it("quarters the weight after two half-lives", () => {
    expect(recencyFactor(daysAgo(RECENCY_HALF_LIFE_DAYS * 2), now)).toBeCloseTo(0.25, 3);
  });

  it("never lets old evidence reach zero", () => {
    expect(recencyFactor(daysAgo(5000), now)).toBe(MINIMUM_RECENCY_FACTOR);
  });

  it("treats an unparseable timestamp as the oldest possible evidence", () => {
    expect(recencyFactor("not-a-date", now)).toBe(MINIMUM_RECENCY_FACTOR);
  });

  it("does not reward a timestamp in the future", () => {
    expect(recencyFactor(new Date(now.getTime() + 86_400_000).toISOString(), now)).toBe(1);
  });
});

describe("relevanceFactor", () => {
  it("passes an in-range relevance through untouched", () => {
    expect(relevanceFactor(0.7)).toBe(0.7);
    expect(relevanceFactor(1)).toBe(1);
  });

  it("never lets an unset relevance annihilate its evidence", () => {
    // `competencies.relevance` is `not null default 0`, and the competency
    // upsert coalesces a scope with no relevance to 0, so 0 means "nobody set
    // this" far more often than "provably worthless". Zero weight would erase
    // the evidence entirely.
    expect(relevanceFactor(0)).toBe(MINIMUM_RELEVANCE);
    expect(MINIMUM_RELEVANCE).toBeGreaterThan(0);
  });

  it("floors rather than promotes, so low relevance still counts least", () => {
    expect(relevanceFactor(0)).toBeLessThan(relevanceFactor(1));
  });

  it("clamps a relevance outside the 0-1 range", () => {
    expect(relevanceFactor(4)).toBe(1);
    expect(relevanceFactor(-2)).toBe(MINIMUM_RELEVANCE);
  });

  it("treats a non-numeric relevance as the lowest possible", () => {
    expect(relevanceFactor(Number.NaN)).toBe(MINIMUM_RELEVANCE);
  });
});
