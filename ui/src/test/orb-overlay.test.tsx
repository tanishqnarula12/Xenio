import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { OrbOverlay } from "../overlay/OrbOverlay";
import { AppController } from "../state/controller";
import { AppProvider } from "../state/context";
import { Store } from "../state/store";
import { approval, assistant, FakeApi, run, snapshot, toSummary } from "./fakes";

let posted: Record<string, unknown>[] = [];

beforeEach(() => {
  posted = [];
  window.webkit = { messageHandlers: { xenio: { postMessage: (m) => posted.push(m as unknown as Record<string, unknown>) } } };
});
afterEach(() => {
  delete window.webkit;
  delete window.xenioNative;
});

async function renderOrb(api: FakeApi) {
  const store = new Store();
  const app = new AppController(api, store);
  app.silent = true;
  await app.init();
  render(<AppProvider store={store} app={app}><OrbOverlay /></AppProvider>);
  return { store, app };
}

const engineSnap = (patch = {}) => snapshot({ voice: { stt: true, tts: true, engine: true, wake: true, wake_phrase: "hey xenio" }, ...patch });
const last = (type: string) => [...posted].reverse().find((m) => m.type === type);

describe("the floating orb", () => {
  it("stays tucked away while nothing happens, and tells the helper its settings", async () => {
    await renderOrb(new FakeApi());
    expect(posted.some((m) => m.type === "ready")).toBe(true);
    expect(last("hide")).toBeTruthy();
    expect(last("show")).toBeUndefined();
    await waitFor(() => expect(last("config")).toEqual({ type: "config", shortcut: "triple_space", interval_ms: 450, location: false }));
  });

  it("appears when XENIO hears its name, and reports how fast", async () => {
    const api = new FakeApi();
    api.snap = engineSnap();
    await renderOrb(api);
    act(() => api.emit("assistant.state", { ...assistant({ state: "LISTENING" }), voice: "listening", voice_since: Date.now() - 120 }));
    await waitFor(() => expect(last("show")).toMatchObject({ width: 400 }));
    expect(await screen.findByText("Listening")).toBeInTheDocument();
    const [name, ms] = api.overlayMetric.mock.calls[0]!;
    expect(name).toBe("listening_to_orb_ms");
    expect(ms).toBeGreaterThanOrEqual(120);
  });

  it("listens when summoned, if the user chose talking", async () => {
    const api = new FakeApi();
    api.snap = engineSnap();
    api.settings.mockResolvedValue({ motion: "system", speak_replies: false, orb_detail: "auto", notifications: true,
      transcript_lines: 50, orb_summon_mode: "talk" } as never);
    await renderOrb(api);
    act(() => window.xenioNative!.receive({ type: "summon", via: "triple_space", at: Date.now() - 40 }));
    await waitFor(() => expect(api.voiceListen).toHaveBeenCalled());
    await waitFor(() => expect(last("show")).toBeTruthy());
    expect(api.overlayMetric).toHaveBeenCalledWith("summon_to_orb_ms", expect.any(Number));
  });

  it("asks Chat or Voice when summoned: Chat opens the quick chat right there, and typed answers stay quiet", async () => {
    const api = new FakeApi();
    api.snap = engineSnap();
    await renderOrb(api);
    act(() => window.xenioNative!.receive({ type: "summon", via: "triple_space", at: Date.now() }));
    const chat = await screen.findByRole("button", { name: "Chat" });
    await waitFor(() => expect(last("show")).toMatchObject({ width: 330 }));
    expect(api.voiceListen).not.toHaveBeenCalled();
    await userEvent.click(chat);
    const box = await screen.findByRole("textbox", { name: "Message XENIO" });
    await waitFor(() => expect(box).toHaveFocus());
    expect(last("chat")).toEqual({ type: "chat", on: true });  // the panel takes the keyboard
    expect(last("size")).toMatchObject({ width: 420, height: 580 });
    expect(last("openApp")).toBeUndefined();  // no big window
    await userEvent.type(box, "what is on my calendar{Enter}");
    expect(api.chat).toHaveBeenCalledWith("what is on my calendar", "text", true);
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(last("chat")).toEqual({ type: "chat", on: false }));
    await waitFor(() => expect(last("hide")).toBeTruthy());
    expect(screen.queryByRole("textbox")).toBeNull();
  });

  it("asks Chat or Voice when summoned: Voice listens", async () => {
    const api = new FakeApi();
    api.snap = engineSnap();
    await renderOrb(api);
    act(() => window.xenioNative!.receive({ type: "summon", via: "hotkey", at: Date.now() }));
    await userEvent.click(await screen.findByRole("button", { name: "Voice" }));
    expect(api.voiceListen).toHaveBeenCalled();
  });

  it("goes back and forth between the chat and voice, on the same conversation", async () => {
    const api = new FakeApi();
    api.snap = engineSnap({ conversation: [{ id: "u1", turn: "t0", role: "user", text: "remind me at 6 to call mom", via: "voice", at: "" },
      { id: "a1", turn: "t0", role: "assistant", text: "Okay, I'll remind you at 6 PM.", ok: true, actions: [], sources: [], at: "" }] });
    api.settings.mockResolvedValue({ motion: "system", speak_replies: false, orb_detail: "auto", notifications: true,
      transcript_lines: 50, orb_summon_mode: "chat" } as never);
    await renderOrb(api);
    await waitFor(() => expect(last("config")).toBeTruthy());
    act(() => window.xenioNative!.receive({ type: "summon", via: "triple_space", at: Date.now() }));
    expect(await screen.findByText("Okay, I'll remind you at 6 PM.")).toBeInTheDocument();  // what was said aloud
    await userEvent.click(screen.getByRole("button", { name: "Speak to XENIO" }));  // chat -> voice
    expect(api.voiceListen).toHaveBeenCalled();
    act(() => window.xenioNative!.receive({ type: "closeChat" }));  // Command-W
    await waitFor(() => expect(screen.queryByRole("textbox")).toBeNull());
    act(() => api.emit("assistant.state", { ...assistant({ state: "LISTENING" }), voice: "listening", voice_since: Date.now() }));
    await userEvent.click(await screen.findByRole("button", { name: "Open the chat" }));  // voice -> chat
    expect(await screen.findByRole("textbox", { name: "Message XENIO" })).toBeInTheDocument();
  });

  it("opens Mission Control from the chat, at the page a link points to", async () => {
    const api = new FakeApi();
    api.runs.mockResolvedValue({ available: true, runs: [toSummary(run({ id: "run-7", status: "planned", request: "Build a snake game" }))] });
    await renderOrb(api);
    act(() => window.xenioNative!.receive({ type: "summon", via: "xenio", at: Date.now(), mode: "chat" }));
    await userEvent.click(await screen.findByRole("button", { name: "Open Mission Control" }));
    expect(last("openApp")).toEqual({ type: "openApp" });
    await userEvent.click(await screen.findByRole("link", { name: "See the plan" }));
    expect(last("openApp")).toEqual({ type: "openApp", route: "/orchestration/run-7" });
  });

  it("asks for approval right in the orb", async () => {
    const api = new FakeApi();
    api.snap = engineSnap({ approvals: [approval({ id: "abc123", description: "create snake/game.js", confined: true })] });
    await renderOrb(api);
    await userEvent.click(await screen.findByRole("button", { name: "Allow for this task" }));
    expect(api.decide).toHaveBeenCalledWith("abc123", "approve_task");
  });

  it("relays the helper's status and the Mac's location to XENIO", async () => {
    const api = new FakeApi();
    await renderOrb(api);
    act(() => window.xenioNative!.receive({ type: "status", shortcut: "triple_space", shortcut_status: "ok", accessibility: true, screens: 2 }));
    expect(api.overlayStatus).toHaveBeenCalledWith({ shortcut: "triple_space", shortcut_status: "ok", accessibility: true, screens: 2 });
    act(() => window.xenioNative!.receive({ type: "location", lat: 28.61, lon: 77.21, accuracy_m: 900, label: "New Delhi, India" }));
    expect(api.saveLocation).toHaveBeenCalledWith({ label: "New Delhi, India", lat: 28.61, lon: 77.21, source: "device", accuracy_m: 900 });
  });

  it("can always show a small orb", async () => {
    const api = new FakeApi();
    api.settings.mockResolvedValue({ motion: "system", speak_replies: false, orb_detail: "auto", notifications: true,
      transcript_lines: 50, orb_overlay_mode: "always" } as never);
    await renderOrb(api);
    await waitFor(() => expect(last("show")).toMatchObject({ width: 96, height: 96 }));
  });

  it("never plays audio itself", async () => {
    const api = new FakeApi();
    api.settings.mockResolvedValue({ motion: "system", speak_replies: true, orb_detail: "auto", notifications: true, transcript_lines: 50 });
    await renderOrb(api);
    act(() => api.emit("chat.done", { id: "t1-a", turn: "t1", role: "assistant", text: "Hello.", ok: true, actions: [], sources: [], at: "" }));
    await new Promise((r) => setTimeout(r, 20));
    expect(api.speak).not.toHaveBeenCalled();
    vi.restoreAllMocks();
  });
});
