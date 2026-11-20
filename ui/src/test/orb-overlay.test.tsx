import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
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

  it("summoned, Chat opens a glass pill under the orb; the answer previews there, and the preview opens the whole chat", async () => {
    const api = new FakeApi();
    api.snap = engineSnap();
    await renderOrb(api);
    act(() => window.xenioNative!.receive({ type: "summon", via: "triple_space", at: Date.now() }));
    await waitFor(() => expect(last("show")).toMatchObject({ width: 232, height: 222 }));  // the orb on its own
    expect(api.voiceListen).not.toHaveBeenCalled();
    await userEvent.click(await screen.findByRole("button", { name: "Chat" }));
    const pill = await screen.findByRole("textbox", { name: "Ask XENIO" });
    await waitFor(() => expect(pill).toHaveFocus());
    expect(last("chat")).toEqual({ type: "chat", on: true });  // the panel takes the keyboard
    expect(last("size")).toMatchObject({ width: 340 });
    expect(last("openApp")).toBeUndefined();  // no big window
    await userEvent.type(pill, "what is on my calendar{Enter}");
    expect(api.chat).toHaveBeenCalledWith("what is on my calendar", "text", true);  // typed answers stay quiet
    act(() => {
      api.emit("chat.user", { id: "t1-u", turn: "t1", role: "user", text: "what is on my calendar", at: "" });
      api.emit("chat.done", { id: "t1-a", turn: "t1", role: "assistant", text: "Standup at 10.", ok: true, actions: [], sources: [], at: "" });
    });
    const preview = await screen.findByRole("button", { name: "Open the whole chat", description: undefined }).catch(() => screen.getAllByRole("button", { name: "Open the whole chat" })[0]!);
    expect(await screen.findByText("Standup at 10.")).toBeInTheDocument();
    await userEvent.click(screen.getByText("Standup at 10."));
    const whole = await screen.findByRole("textbox", { name: "Message XENIO" });
    expect(whole).toBeInTheDocument();
    expect(preview).toBeTruthy();
    await waitFor(() => expect(last("size")).toMatchObject({ width: 420, height: 580 }));
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(last("chat")).toEqual({ type: "chat", on: false }));
  });

  it("Esc closes the pill, and the orb stays", async () => {
    const api = new FakeApi();
    api.snap = engineSnap();
    await renderOrb(api);
    act(() => window.xenioNative!.receive({ type: "summon", via: "triple_space", at: Date.now() }));
    await userEvent.click(await screen.findByRole("button", { name: "Chat" }));
    const glass = () => (last("glass") as { shapes: { w: number; h: number }[]; instant?: boolean });
    await waitFor(() => expect(glass().shapes[0]).toMatchObject({ r: 26 }));  // the deck's glass became the pill (no layout here: its size reads 0)
    await userEvent.type(await screen.findByRole("textbox", { name: "Ask XENIO" }), "{Escape}");
    expect(glass().shapes).toEqual([expect.objectContaining({ w: 40, h: 6 })]);  // first it melts back into the handle,
    expect(last("size")).toMatchObject({ width: 340 });  // while the window keeps its size
    await waitFor(() => expect(screen.queryByRole("textbox", { name: "Ask XENIO" })).toBeNull());
    expect(screen.getByRole("button", { name: "Chat" })).toBeInTheDocument();  // back to the orb and its deck
    await waitFor(() => expect(last("size")).toMatchObject({ width: 232, anchor: "top-center" }));
    expect(glass()).toMatchObject({ instant: true });  // the glass moves with the narrower window, in the same frame
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
    await waitFor(() => expect(last("show")).toMatchObject({ width: 232, height: 222 }));
    expect(last("surface")).toEqual({ type: "surface", glass: true });  // no card: the orb floats
  });

  it("talking to the floating orb keeps it the orb: what you say and its answers build up in one thread under it", async () => {
    const api = new FakeApi();
    api.snap = engineSnap();
    await renderOrb(api);
    act(() => window.xenioNative!.receive({ type: "summon", via: "triple_space", at: Date.now() }));
    await screen.findByRole("button", { name: "Chat" });
    const say = (n: number, words: string, answer: string) => act(() => {
      api.emit("assistant.state", { ...assistant({ state: "THINKING" }), turn: `v${n}` });
      api.emit("chat.user", { id: `v${n}-u`, turn: `v${n}`, role: "user", text: words, at: "" });
      api.emit("chat.done", { id: `v${n}-a`, turn: `v${n}`, role: "assistant", text: answer, ok: true, actions: [], sources: [], at: "" });
      api.emit("assistant.state", { ...assistant({ state: "IDLE" }), turn: null });
    });
    say(1, "play some lofi", "Playing lofi beats.");
    expect(await screen.findByText("Playing lofi beats.")).toBeInTheDocument();
    say(2, "a bit louder", "Volume up.");
    expect(await screen.findByText("Volume up.")).toBeInTheDocument();
    expect(screen.getByText("Playing lofi beats.")).toBeInTheDocument();  // the same thread, not a new one each time
    expect(screen.getByText("a bit louder")).toBeInTheDocument();
    expect(document.querySelector(".orb-overlay")).toBeNull();  // never the old card
    await userEvent.click(screen.getByRole("button", { name: "Continue here" }));  // and you can carry on typing in it
    expect(await screen.findByRole("textbox", { name: "Ask XENIO" })).toBeInTheDocument();
    expect(screen.getByText("Volume up.")).toBeInTheDocument();
  });

  it("Esc keeps what you were typing for when you open the pill again", async () => {
    const api = new FakeApi();
    api.snap = engineSnap();
    await renderOrb(api);
    act(() => window.xenioNative!.receive({ type: "summon", via: "triple_space", at: Date.now() }));
    await userEvent.click(await screen.findByRole("button", { name: "Chat" }));
    await userEvent.type(await screen.findByRole("textbox", { name: "Ask XENIO" }), "half a thought{Escape}");
    await waitFor(() => expect(screen.queryByRole("textbox", { name: "Ask XENIO" })).toBeNull());
    await userEvent.click(screen.getByRole("button", { name: "Chat" }));
    expect(await screen.findByRole("textbox", { name: "Ask XENIO" })).toHaveValue("half a thought");
  });

  it("moves the window only when its size changes, not on every streamed word", async () => {
    const api = new FakeApi();
    api.snap = engineSnap();
    await renderOrb(api);
    act(() => window.xenioNative!.receive({ type: "summon", via: "triple_space", at: Date.now() }));
    await screen.findByRole("button", { name: "Chat" });
    const sizes = () => posted.filter((m) => m.type === "size" || m.type === "show").length;
    const before = sizes();
    for (let i = 0; i < 5; i++) act(() => window.xenioNative!.receive({ type: "hover", on: i % 2 === 0 }));  // renders, same size
    expect(sizes()).toBe(before);
  });

  it("the whole chat resizes from its corner grip, keeps that size, and its glass stays inside the window's edge", async () => {
    const api = new FakeApi();
    api.snap = engineSnap();
    await renderOrb(api);
    act(() => window.xenioNative!.receive({ type: "summon", via: "triple_space", at: Date.now() }));
    await userEvent.click(await screen.findByRole("button", { name: "Chat" }));
    await userEvent.click(await screen.findByRole("button", { name: "Open the whole chat" }));
    await screen.findByRole("textbox", { name: "Message XENIO" });
    const glass = () => (last("glass") as { shapes: { x: number; y: number; w: number; h: number }[] }).shapes[0]!;
    await waitFor(() => expect(glass()).toMatchObject({ x: 10, y: 10, w: 400, h: 560 }));  // rounded inside, never cut square
    const grip = document.querySelector(".qc-grip")!;
    fireEvent.pointerDown(grip);
    expect(last("resizeStart")).toMatchObject({ minWidth: 360, minHeight: 420 });
    act(() => window.xenioNative!.receive({ type: "resized", width: 600, height: 700 }));
    act(() => window.xenioNative!.receive({ type: "resized", width: 640, height: 760, done: true }));
    await waitFor(() => expect(glass()).toMatchObject({ w: 620, h: 740 }));
    expect(JSON.parse(localStorage.getItem("xenio.orb.chatSize")!)).toEqual({ w: 640, h: 760 });
    act(() => window.xenioNative!.receive({ type: "resized", width: 100, height: 5000, done: true }));  // never past its limits
    await waitFor(() => expect(last("size")).toMatchObject({ width: 360, height: 1100 }));
    localStorage.removeItem("xenio.orb.chatSize");
  });

  it("Esc in the whole chat draws it back into the orb, and the floating orb it came from stays", async () => {
    const api = new FakeApi();
    api.snap = engineSnap();
    await renderOrb(api);
    act(() => window.xenioNative!.receive({ type: "summon", via: "triple_space", at: Date.now() }));
    await userEvent.click(await screen.findByRole("button", { name: "Chat" }));
    await userEvent.click(await screen.findByRole("button", { name: "Open the whole chat" }));
    await screen.findByRole("textbox", { name: "Message XENIO" });
    const before = posted.length;
    await userEvent.keyboard("{Escape}");
    expect(document.querySelector(".qc-frame--closing")).not.toBeNull();  // first it shrinks,
    const shape = (last("glass") as { shapes: { w: number; h: number; r: number }[] }).shapes[0]!;
    expect(shape.w).toBe(shape.h);  // its glass drawing in to a circle where the orb is
    expect(shape.r).toBe(shape.w / 2);
    await waitFor(() => expect(screen.getByRole("button", { name: "Chat" })).toBeInTheDocument());  // then the orb, still there
    expect(posted.slice(before).some((m) => m.type === "hide")).toBe(false);  // never gone, not even for a moment
  });

  it("floats as a pet: real glass behind a deck that shows only on hover, a handle otherwise, and it stays until hidden", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const api = new FakeApi();
    api.snap = engineSnap();
    await renderOrb(api);
    act(() => window.xenioNative!.receive({ type: "summon", via: "triple_space", at: Date.now() }));
    await screen.findByRole("button", { name: "Chat" });
    await waitFor(() => expect(last("surface")).toEqual({ type: "surface", glass: true }));
    const shape = () => (last("glass") as { shapes: { w: number; h: number }[] }).shapes[0];
    expect(shape()).toMatchObject({ w: 40, h: 6 });  // a glass handle: the deck waits for the pointer
    act(() => window.xenioNative!.receive({ type: "hover", on: true }));
    await waitFor(() => expect(shape()!.w).toBe(220));
    act(() => window.xenioNative!.receive({ type: "hover", on: false }));
    act(() => { vi.advanceTimersByTime(120); });
    expect(shape()!.w).toBe(220);  // passing over the orb does not flicker the deck: it waits a moment
    act(() => window.xenioNative!.receive({ type: "hover", on: true }));  // and coming back keeps it open
    act(() => window.xenioNative!.receive({ type: "hover", on: false }));
    act(() => { vi.advanceTimersByTime(300); });
    await waitFor(() => expect(shape()).toMatchObject({ w: 40, h: 6 }));
    expect([...posted].reverse().find((m) => m.type === "size" || m.type === "show")).toMatchObject({ anchor: "top-center" });  // the window stays put around the orb
    const before = posted.length;
    act(() => { vi.advanceTimersByTime(60_000); });  // a minute later it is still there: only you hide it
    expect(posted.slice(before).some((m) => m.type === "hide")).toBe(false);
    await userEvent.click(screen.getByRole("button", { name: "Hide the orb" }));
    await waitFor(() => expect(last("hide")).toBeTruthy());
    vi.useRealTimers();
  });

  it("stays on screen while XENIO's window is minimized or closed, and comes back after being hidden when it goes away again", async () => {
    const api = new FakeApi();
    await renderOrb(api);
    act(() => window.xenioNative!.receive({ type: "app", away: true }));
    await waitFor(() => expect(last("show")).toMatchObject({ width: 232, height: 222 }));
    await userEvent.click(screen.getByRole("button", { name: "Hide the orb" }));
    await waitFor(() => expect(last("hide")).toBeTruthy());
    act(() => window.xenioNative!.receive({ type: "app", away: false }));
    act(() => window.xenioNative!.receive({ type: "app", away: true }));
    await screen.findByRole("button", { name: "Chat" });
  });

  it("music mode: the orb listens to what the Mac plays only when asked, says plainly when it may not, and stops when hidden", async () => {
    const api = new FakeApi();
    api.snap = engineSnap();
    const { store } = await renderOrb(api);
    act(() => window.xenioNative!.receive({ type: "summon", via: "triple_space", at: Date.now() }));
    const button = await screen.findByRole("button", { name: "Music mode" });
    expect(button).toHaveAttribute("aria-pressed", "false");
    await userEvent.click(button);
    expect(last("music")).toEqual({ type: "music", on: true });
    act(() => window.xenioNative!.receive({ type: "music", on: true }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Music mode" })).toHaveAttribute("aria-pressed", "true"));
    expect(() => act(() => window.xenioNative!.receive({ type: "musicLevel", level: 0.7, bass: 0.9, beat: true }))).not.toThrow();
    await userEvent.click(screen.getByRole("button", { name: "Hide the orb" }));
    expect(last("music")).toEqual({ type: "music", on: false });  // hidden: nothing to listen for
    act(() => window.xenioNative!.receive({ type: "music", on: false, error: "The user declined TCCs for application, window, display capture" }));
    const notice = store.get().notices.at(-1);
    expect(notice?.title).toBe("Music mode is off");
    expect(notice?.message).toMatch(/Screen & System Audio Recording/);  // which setting, in plain words
  });

  it("passes the drag on to the orb, so its particles can trail it", async () => {
    const api = new FakeApi();
    api.snap = engineSnap();
    await renderOrb(api);
    act(() => window.xenioNative!.receive({ type: "summon", via: "triple_space", at: Date.now() }));
    await screen.findByRole("button", { name: "Chat" });
    expect(() => act(() => window.xenioNative!.receive({ type: "moved", dx: 30, dy: -12 }))).not.toThrow();
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
