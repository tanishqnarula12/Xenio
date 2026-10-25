import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ORB_STATES } from "../api/types";
import { Orb } from "../orb/Orb";
import { deriveOrbState, describe as describeState, IDLE_VOICE, isAllowed, nextState, TRANSITIONS } from "../orb/machine";
import { lookFor } from "../orb/renderer";
import { assistant } from "./fakes";

describe("the orb's state machine", () => {
  it("knows every state, with words for it and a look of its own", () => {
    expect(ORB_STATES).toHaveLength(16);
    const looks = new Set<string>();
    for (const state of ORB_STATES) {
      const copy = describeState(state, assistant());
      expect(copy.label.length).toBeGreaterThan(2);
      expect(copy.sentence.length).toBeGreaterThan(2);
      expect(TRANSITIONS[state]).toBeDefined();
      const look = lookFor(state);
      looks.add(JSON.stringify([look.forms, look.color, look.pace, look.scan, look.breathe, look.jitter, look.radius]));
    }
    expect(looks.size).toBe(16); // every state is visually distinct
  });

  it("refuses the states that must be earned, but shows the backend's truth after a resync", () => {
    expect(nextState("IDLE", "COMPLETED")).toBe("IDLE"); // nothing was asked: never "done"
    expect(nextState("DISCONNECTED", "SPEAKING")).toBe("DISCONNECTED");
    expect(nextState("THINKING", "COMPLETED")).toBe("COMPLETED");
    expect(nextState("IDLE", "SEARCHING")).toBe("SEARCHING"); // a reload mid-turn: show what is really happening
    expect(isAllowed("SPEAKING", "WAITING_APPROVAL")).toBe(true); // a decision can be needed at any time
    expect(isAllowed("IDLE", "COMPLETED")).toBe(false);
  });

  it("derives the state from real inputs, in the right priority", () => {
    expect(deriveOrbState("unauthorized", assistant(), IDLE_VOICE)).toBe("DISCONNECTED");
    expect(deriveOrbState("connecting", null, IDLE_VOICE)).toBe("CONNECTING");
    expect(deriveOrbState("retrying", assistant(), IDLE_VOICE)).toBe("DISCONNECTED"); // an outage is never hidden
    expect(deriveOrbState("open", assistant({ state: "THINKING" }), { ...IDLE_VOICE, speaking: true })).toBe("SPEAKING");
    expect(deriveOrbState("open", assistant({ state: "WAITING_APPROVAL" }), { ...IDLE_VOICE, speaking: true })).toBe("WAITING_APPROVAL");
    expect(deriveOrbState("open", assistant(), { ...IDLE_VOICE, listening: true })).toBe("LISTENING");
    expect(deriveOrbState("open", assistant(), { ...IDLE_VOICE, transcribing: true })).toBe("PROCESSING");
    expect(deriveOrbState("open", assistant({ state: "ORCHESTRATING", background: { state: "ORCHESTRATING", runs: 1, agents: 3 } }), IDLE_VOICE))
      .toBe("ORCHESTRATING");
    expect(deriveOrbState("open", assistant(), IDLE_VOICE)).toBe("IDLE"); // nothing running: never shown as busy
  });

  it("describes background work with the backend's real numbers", () => {
    const a = assistant({ background: { state: "ORCHESTRATING", runs: 1, agents: 3 } });
    expect(describeState("ORCHESTRATING", a).sentence).toContain("3 agents");
    expect(describeState("ERROR", assistant({ error: "Hermes is unavailable" })).sentence).toBe("Hermes is unavailable");
  });
});

describe("the orb component", () => {
  it.each(ORB_STATES)("renders %s with its meaning in words", (state) => {
    render(<Orb state={state} label={describeState(state, assistant()).label} size={120} reducedMotion={false} />);
    const orb = screen.getByRole("img");
    expect(orb).toHaveAttribute("data-state", state);
    expect(orb).toHaveAccessibleName(`XENIO: ${describeState(state, assistant()).label}`);
  });

  it("falls back to a still image without WebGL, and with reduced motion", () => {
    const { container } = render(<Orb state="IDLE" label="Ready" size={120} reducedMotion />);
    expect(container.querySelector(".orb__still")).not.toBeNull();
  });
});
