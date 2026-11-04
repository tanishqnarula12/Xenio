import { act, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { EventStream } from "../api/client";
import { ORB_STATES } from "../api/types";
import { Orb } from "../orb/Orb";
import { deriveOrbState, describe as describeState, friendlyTool, IDLE_VOICE, isAllowed, nextState, TRANSITIONS } from "../orb/machine";
import { lookFor } from "../orb/renderer";
import { assistant, FakeApi, renderApp } from "./fakes";

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
    expect(nextState("CONNECTING", "COMPLETED")).toBe("IDLE"); // a reload just after a turn: never stuck on "Connecting"
    expect(nextState("DISCONNECTED", "COMPLETED")).toBe("IDLE");
    expect(nextState("LISTENING", "COMPLETED")).toBe("COMPLETED"); // a quick command, its steps all arriving at once
    expect(nextState("LISTENING", "SPEAKING")).toBe("SPEAKING"); // and the answer is being spoken: never stuck on "Listening"
    expect(nextState("DISCONNECTED", "SPEAKING")).toBe("SPEAKING"); // only real audio says SPEAKING
    expect(nextState("THINKING", "COMPLETED")).toBe("COMPLETED");
    expect(nextState("IDLE", "SEARCHING")).toBe("SEARCHING"); // a reload mid-turn: show what is really happening
    expect(isAllowed("SPEAKING", "WAITING_APPROVAL")).toBe(true); // a decision can be needed at any time
    expect(isAllowed("IDLE", "COMPLETED")).toBe(false);
  });

  it("never stays behind: from any state, it shows what is true now", () => {
    const all = Object.keys(TRANSITIONS) as (keyof typeof TRANSITIONS)[];
    for (const from of all) for (const to of all) {
      const shown = nextState(from, to);
      expect(shown === to || (to === "COMPLETED" && shown === "IDLE"), `${from} -> ${to} showed ${shown}`).toBe(true);
    }
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

  it("says what XENIO's hands are really doing, and nothing when there is nothing to report", () => {
    const acting = assistant({ state: "EXECUTING", tool: "xenio_gui", action: { state: "acting", action: "click", step: 2, total: 4 } });
    expect(describeState("EXECUTING", acting).sentence).toBe("Acting: clicking (step 2 of 4).");
    const checking = assistant({ state: "EXECUTING", tool: "xenio_gui", action: { state: "verifying", action: "check" } });
    expect(describeState("EXECUTING", checking).label).toBe("Checking");
    const reading = assistant({ state: "SEARCHING", tool: "xenio_look_at_screen", action: { state: "looking", action: "look" } });
    expect(describeState("SEARCHING", reading).label).toBe("Reading the screen");
    const plain = assistant({ state: "EXECUTING", tool: "xenio_gui" });
    expect(describeState("EXECUTING", plain).sentence).toBe("Acting: using the keyboard and mouse.");
  });

  it("falls back to a still image without WebGL, and with reduced motion", () => {
    const { container } = render(<Orb state="IDLE" label="Ready" size={120} reducedMotion />);
    expect(container.querySelector(".orb__still")).not.toBeNull();
  });
});

describe("the orb on the page, from real events", () => {
  const state = (api: FakeApi, over: object) => api.emit("assistant.state", assistant(over) as unknown as Record<string, unknown>);

  it("follows a whole turn, and ends on Ready", async () => {
    const api = new FakeApi();
    await renderApp(api, "home");
    const orb = () => screen.getByRole("heading", { level: 1 }).textContent;
    await waitFor(() => expect(orb()).toBe("Ready"));
    for (const [over, words] of [
      [{ state: "PROCESSING", turn: "t1" }, "Working on it"], [{ state: "SEARCHING", turn: "t1", tool: "web_search" }, "Looking it up"], [{ state: "THINKING", turn: "t1" }, "Thinking"],
      [{ state: "EXECUTING", turn: "t1", tool: "xenio_gui" }, "Acting"], [{ state: "COMPLETED" }, "Done"], [{ state: "IDLE" }, "Ready"],
    ] as const) {
      act(() => state(api, over));
      expect(orb()).toBe(words);
    }
  });

  it("a quick command whose steps arrive together is not left on Listening", async () => {
    const api = new FakeApi();
    await renderApp(api, "home");
    const orb = () => screen.getByRole("heading", { level: 1 }).textContent;
    act(() => state(api, { state: "LISTENING", voice: "listening" }));
    expect(orb()).toBe("Listening");
    act(() => { // one read of the stream, one render
      state(api, { state: "PROCESSING", voice: "transcribing" });
      state(api, { state: "THINKING", turn: "t1" });
      state(api, { state: "SPEAKING", voice: "speaking" });
    });
    expect(orb()).toBe("Speaking");
    act(() => state(api, { state: "LISTENING", voice: "listening" }));
    act(() => {
      state(api, { state: "THINKING", turn: "t2" });
      state(api, { state: "COMPLETED" });
    });
    expect(orb()).toBe("Done");
    act(() => state(api, { state: "IDLE" }));
    expect(orb()).toBe("Ready");
  });

  it("names the new tools in plain words", () => {
    expect(friendlyTool("xenio_teach")).toBe("getting that ready");
    expect(friendlyTool("xenio_image")).toBe("making a picture");
    expect(friendlyTool("xenio_form")).toBe("filling the form");
  });
});

describe("a stream that goes silent", () => {
  it("is dropped and picked up again from where it stopped, so the orb cannot stay on an old state", async () => {
    vi.useFakeTimers();
    const calls: string[] = [];
    const never = new ReadableStream<Uint8Array>({ start() { /* connected, and then nothing at all */ } });
    const original = globalThis.fetch;
    globalThis.fetch = vi.fn(async (url: string, init?: RequestInit) => {
      calls.push(url);
      return await new Promise<Response>((resolve) => resolve(new Response(calls.length === 1 ? (init?.signal ? abortable(never, init.signal) : never) : new ReadableStream({ start() {} }), { status: 200 })));
    }) as never;
    const statuses: string[] = [];
    const stream = new EventStream("/events", "t", () => undefined, (s) => statuses.push(s));
    try {
      stream.start(41);
      await vi.advanceTimersByTimeAsync(10);
      expect(calls).toHaveLength(1);
      await vi.advanceTimersByTimeAsync(30_000);
      expect(calls).toHaveLength(1); // quiet, but not yet suspicious
      await vi.advanceTimersByTimeAsync(20_000);
      expect(calls).toHaveLength(2);
      expect(calls[1]).toContain("since=41");
      expect(statuses).toContain("retrying");
    } finally {
      stream.stop();
      globalThis.fetch = original;
      vi.useRealTimers();
    }
  });

  function abortable(body: ReadableStream<Uint8Array>, signal: AbortSignal): ReadableStream<Uint8Array> {
    const reader = body.getReader();
    return new ReadableStream({
      pull(control) { return reader.read().then(({ value, done }) => { if (done) control.close(); else control.enqueue(value); }); },
      start(control) { signal.addEventListener("abort", () => control.error(new DOMException("aborted", "AbortError"))); },
    });
  }
});
