import { describe, expect, it } from "vitest";
import { OrbRenderer } from "../orb/renderer";

type Swarm = { ox: Float32Array; oy: Float32Array; active: boolean };
type Internals = { tick: (time: number, deltaMs: number) => void; swarm: Swarm };

function orb(reducedMotion = false) {
  const canvas = () => Object.assign(document.createElement("canvas"), { width: 232, height: 232 });
  const dots = canvas();
  Object.defineProperty(dots, "clientWidth", { value: 232 });
  return new OrbRenderer(canvas(), dots, { particles: 400, reducedMotion, interactive: false, vivid: true, scale: 0.72 });
}

describe("carrying the orb", () => {
  it("leaves each particle behind by its own weight, so it streams like stardust, then all of them come home", () => {
    const r = orb();
    const inside = r as unknown as Internals;
    for (let i = 0; i < 10; i++) { r.move(14, 0); inside.tick(0, 16); } // dragged to the right, fast
    const left = Array.from(inside.swarm.ox);
    const mean = left.reduce((a, b) => a + b, 0) / left.length;
    const spread = Math.sqrt(left.reduce((a, b) => a + (b - mean) ** 2, 0) / left.length);
    expect(mean).toBeLessThan(-10); // left behind, to the left
    expect(spread).toBeGreaterThan(5); // each by its own amount: a trail, not one shape moved along
    expect(Array.from(inside.swarm.oy).some((y) => Math.abs(y) > 0.5)).toBe(true); // and they swirl on the way back
    for (let i = 0; i < 400; i++) inside.tick(0, 16);
    expect(inside.swarm.active).toBe(false); // home, and still
    expect(Math.max(...Array.from(inside.swarm.ox).map(Math.abs))).toBe(0);
  });

  it("stays still when you ask for less motion", () => {
    const r = orb(true);
    const inside = r as unknown as Internals;
    for (let i = 0; i < 10; i++) { r.move(14, 0); inside.tick(0, 16); }
    expect(inside.swarm.active).toBe(false);
  });
});

describe("left alone a long while", () => {
  type Idle = Internals & { flourish: { kind: string } | null; lastFlourish: string | null };
  it("plays now and then on its own, a different little thing each time, and stops the moment something happens", () => {
    const r = orb();
    const inside = r as unknown as Idle;
    for (let i = 0; i < 600; i++) inside.tick(0, 16);  // ten seconds, not idle: nothing
    expect(inside.flourish).toBeNull();
    r.setIdle(true);
    const kinds: string[] = [];
    for (let i = 0; i < 60 * 120; i++) {  // two idle minutes
      inside.tick(0, 16);
      const kind = inside.flourish?.kind;
      if (kind && kinds.at(-1) !== kind) kinds.push(kind);
    }
    expect(kinds.length).toBeGreaterThanOrEqual(5);  // every 7-15 s
    expect(kinds.every((k, i) => i === 0 || k !== kinds[i - 1])).toBe(true);  // never the same twice in a row
    r.setIdle(false);
    expect(inside.flourish).toBeNull();
  });

  it("does nothing on its own when you ask for less motion", () => {
    const r = orb(true);
    const inside = r as unknown as Idle;
    r.setIdle(true);
    for (let i = 0; i < 60 * 30; i++) inside.tick(0, 16);
    expect(inside.flourish).toBeNull();
    expect(inside.swarm.active).toBe(false);
  });
});
