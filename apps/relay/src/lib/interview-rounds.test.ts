import { describe, expect, it } from "vitest";
import { IMPLEMENTED_ROUNDS, modePolicyFor, roundFor } from "@/lib/interview-rounds";

describe("roundFor", () => {
  it("returns the tech lead round with its full repertoire", () => {
    const round = roundFor("tech-lead");
    expect(round.id).toBe("tech-lead");
    expect(round.moves).toEqual(
      expect.arrayContaining(["open", "probe", "challenge", "rescue", "advance", "hypothetical", "candidate-questions", "close"]),
    );
    expect(round.opening).toBe("open");
    expect(round.closing).toBe("candidate-questions");
  });

  it("keeps salary and live coding out of scope for the tech lead round", () => {
    const round = roundFor("tech-lead");
    expect(round.outOfScope).toEqual(
      expect.arrayContaining(["salary", "notice period", "live coding"]),
    );
  });

  it("gives every round a persona stake that names a motivation", () => {
    for (const id of IMPLEMENTED_ROUNDS) {
      expect(roundFor(id).personaStake.length).toBeGreaterThan(40);
    }
  });
});

describe("modePolicyFor", () => {
  it("lets coach mode rescue more often and park", () => {
    const coach = modePolicyFor("coach");
    expect(coach.rescuesPerQuestion).toBe(2);
    expect(coach.rescuesPerSession).toBe(5);
    expect(coach.rescueStyles).toEqual(["narrow", "hook", "reframe", "park"]);
    expect(coach.parkAndReturn).toBe(true);
    expect(coach.acknowledgeStruggle).toBe(true);
  });

  it("limits real mode to a single narrowing rescue and no parking", () => {
    const real = modePolicyFor("real");
    expect(real.rescuesPerQuestion).toBe(1);
    expect(real.rescuesPerSession).toBe(2);
    expect(real.rescueStyles).toEqual(["narrow"]);
    expect(real.parkAndReturn).toBe(false);
    expect(real.acknowledgeStruggle).toBe(false);
    expect(real.pushback).toBe("firm");
  });
});
