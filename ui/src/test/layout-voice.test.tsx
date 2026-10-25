import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { getLayout, reloadLayout } from "../state/layout";
import { assistant, FakeApi, renderApp, snapshot } from "./fakes";

beforeEach(() => {
  localStorage.clear();
  reloadLayout();
});

describe("layout", () => {
  it("collapses the sidebar to an icon rail and remembers it", async () => {
    const { container } = await renderApp(new FakeApi());
    await userEvent.click(await screen.findByRole("button", { name: "Collapse the sidebar" }));
    expect(container.querySelector(".shell")).toHaveClass("shell--rail");
    expect(JSON.parse(localStorage.getItem("xenio.layout") ?? "{}").sidebar).toBe("rail");
    // the pages stay reachable by name in the rail
    expect(screen.getByRole("link", { name: "Settings" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Expand the sidebar" }));
    expect(container.querySelector(".shell")).not.toHaveClass("shell--rail");
  });

  it("hides the side panel to focus on the conversation, and brings it back", async () => {
    const { container } = await renderApp(new FakeApi());
    expect(await screen.findByRole("complementary", { name: "At a glance" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Hide the side panel \(focus/ }));
    expect(screen.queryByRole("complementary", { name: "At a glance" })).not.toBeInTheDocument();
    expect(container.querySelector(".home")).toHaveClass("home--focus");
    expect(screen.getByLabelText("Message XENIO")).toBeInTheDocument(); // the conversation stays
    await userEvent.click(screen.getByRole("button", { name: "Show the side panel" }));
    expect(screen.getByRole("complementary", { name: "At a glance" })).toBeInTheDocument();
  });

  it("resizes the side panel from the keyboard within its limits", async () => {
    await renderApp(new FakeApi());
    const splitter = await screen.findByRole("separator", { name: "Resize the side panel" });
    splitter.focus();
    await userEvent.keyboard("{ArrowLeft}{ArrowLeft}");
    expect(getLayout().sideWidth).toBe(372);
    await userEvent.keyboard("{End}");
    expect(getLayout().sideWidth).toBe(280);
    expect(splitter).toHaveAttribute("aria-valuenow", "280");
  });

  it("minimises side sections to one line, and opens them again", async () => {
    const api = new FakeApi();
    api.health.mockResolvedValue({ checks: [{ name: "Hermes", status: "OK", detail: "ok" }, { name: "TTS", status: "WARN", detail: "x" }],
      checked_at: "", platform: "darwin" });
    await renderApp(api);
    const side = await screen.findByRole("complementary", { name: "At a glance" });
    const system = within(side).getByRole("button", { name: "System" });
    expect(system).toHaveAttribute("aria-expanded", "false"); // minimised by default: the panel is not crowded
    expect(await within(side).findByText("1 of 2 parts working")).toBeInTheDocument();
    await userEvent.click(system);
    expect(system).toHaveAttribute("aria-expanded", "true");
    expect(await within(side).findByText(/TTS: x/)).toBeInTheDocument();
    expect(getLayout().folded).not.toContain("home-system");
    await userEvent.click(within(side).getByRole("button", { name: "Running now" }));
    expect(within(side).getByRole("button", { name: "Running now" })).toHaveAttribute("aria-expanded", "false");
  });

  it("makes the orb bigger or smaller, within limits", async () => {
    await renderApp(new FakeApi());
    const bigger = await screen.findByRole("button", { name: "Bigger orb" });
    await userEvent.click(bigger);
    expect(getLayout().orbStep).toBe(4);
    expect(bigger).toBeDisabled();
    for (let i = 0; i < 4; i++) await userEvent.click(screen.getByRole("button", { name: "Smaller orb" }));
    expect(getLayout().orbStep).toBe(0);
    expect(screen.getByRole("button", { name: "Smaller orb" })).toBeDisabled();
  });

  it("survives damaged or missing storage", () => {
    localStorage.setItem("xenio.layout", "{not json");
    reloadLayout();
    expect(getLayout().side).toBe(true);
    localStorage.setItem("xenio.layout", JSON.stringify({ sideWidth: 99999, orbStep: -3, sidebar: "weird", folded: [1, "home-x"] }));
    reloadLayout();
    expect(getLayout()).toMatchObject({ sideWidth: 560, orbStep: 0, sidebar: "open", folded: ["home-x"] });
  });
});

describe("XENIO's talk engine", () => {
  const engineSnap = () => snapshot({ voice: { stt: true, tts: true, engine: true, wake: true, wake_phrase: "hey xenio", problem: null } });

  it("the microphone button asks XENIO to listen on the Mac instead of recording in the browser", async () => {
    const api = new FakeApi();
    api.snap = engineSnap();
    const { app } = await renderApp(api);
    const recorder = vi.fn();
    app.recorder = recorder as unknown as typeof app.recorder;
    await userEvent.click(await screen.findByRole("button", { name: "Speak to XENIO" }));
    expect(api.voiceListen).toHaveBeenCalled();
    expect(recorder).not.toHaveBeenCalled();
    act(() => api.emit("assistant.state", { ...assistant({ state: "LISTENING" }), voice: "listening" }));
    const stop = await screen.findByRole("button", { name: "Stop listening and send" });
    expect(stop).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(stop);
    expect(api.voiceFinish).toHaveBeenCalled();
    await waitFor(() => expect(api.voiceLevel).toHaveBeenCalled()); // the orb follows the real microphone level
  });

  it("does not speak in the browser when XENIO speaks for itself", async () => {
    const api = new FakeApi();
    api.snap = engineSnap();
    api.settings.mockResolvedValue({ motion: "system", speak_replies: true, orb_detail: "auto", notifications: true, transcript_lines: 50 });
    await renderApp(api);
    act(() => api.emit("chat.done", { id: "t1-a", turn: "t1", role: "assistant", text: "Hello there.", ok: true, actions: [], sources: [], at: "" }));
    await screen.findByText("Hello there.");
    expect(api.speak).not.toHaveBeenCalled();
  });

  it("stops XENIO's own voice from the speaker button", async () => {
    const api = new FakeApi();
    api.snap = engineSnap();
    await renderApp(api);
    act(() => api.emit("assistant.state", { ...assistant({ state: "SPEAKING" }), voice: "speaking" }));
    await userEvent.click(await screen.findByRole("button", { name: "Stop speaking" }));
    expect(api.cancel).toHaveBeenCalled();
  });

  it("says how to wake XENIO, and follows the engine going away", async () => {
    const api = new FakeApi();
    api.snap = engineSnap();
    await renderApp(api);
    const main = await screen.findByRole("main");
    expect(await within(main).findByText("Say “Hey Xenio” to talk")).toBeInTheDocument();
    act(() => api.emit("voice.changed", { stt: true, tts: true, engine: false, wake: false, wake_phrase: "hey xenio", problem: "the microphone stopped" }));
    await waitFor(() => expect(within(main).queryByText("Say “Hey Xenio” to talk")).not.toBeInTheDocument());
  });

  it("falls back to the browser's microphone when the engine is not running", async () => {
    const api = new FakeApi();
    const { app } = await renderApp(api);
    const recorder = vi.fn(async () => { throw new Error("no microphone in tests"); });
    app.recorder = recorder as unknown as typeof app.recorder;
    await userEvent.click(await screen.findByRole("button", { name: "Speak to XENIO" }));
    expect(recorder).toHaveBeenCalled();
    expect(api.voiceListen).not.toHaveBeenCalled();
  });
});
