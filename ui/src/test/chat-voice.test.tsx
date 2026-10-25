import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import { assistant, FakeApi, renderApp, snapshot } from "./fakes";

afterEach(() => vi.unstubAllGlobals());

describe("chat", () => {
  it("sends typed text, and Enter sends while Shift+Enter starts a new line", async () => {
    const api = new FakeApi();
    await renderApp(api);
    const box = await screen.findByLabelText("Message XENIO");
    await userEvent.type(box, "Open Spotify{Shift>}{Enter}{/Shift}please{Enter}");
    await waitFor(() => expect(api.chat).toHaveBeenCalledWith("Open Spotify\nplease", "text", false));
  });

  it("never sends an empty message", async () => {
    const api = new FakeApi();
    await renderApp(api);
    const send = await screen.findByRole("button", { name: "Send" });
    expect(send).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Message XENIO"), "   {Enter}");
    expect(api.chat).not.toHaveBeenCalled();
  });

  it("shows the streamed answer and what XENIO really did", async () => {
    const api = new FakeApi();
    await renderApp(api);
    act(() => {
      api.emit("chat.user", { id: "t1-u", turn: "t1", role: "user", text: "make a folder", at: "" });
      api.emit("chat.chunk", { turn: "t1", text: "Creating it" });
    });
    expect(await screen.findByText("Creating it")).toBeInTheDocument();
    act(() => api.emit("chat.done", { id: "t1-a", turn: "t1", role: "assistant", text: "The folder could not be created.", ok: true,
      actions: ["[FAILED] xenio_create_directory: Projects - permission denied"], sources: [], at: "" }));
    expect(await screen.findByText("Failed")).toBeInTheDocument();
    expect(screen.getByText(/permission denied/)).toBeInTheDocument();
  });

  it("offers Stop while XENIO answers, and stopping cancels the turn", async () => {
    const api = new FakeApi();
    api.snap = snapshot({ assistant: assistant({ state: "THINKING", turn: "t1" }) });
    await renderApp(api);
    await userEvent.click(await screen.findByRole("button", { name: "Stop XENIO" }));
    expect(api.cancel).toHaveBeenCalled();
  });

  it("explains a refusal from XENIO instead of failing silently", async () => {
    const api = new FakeApi();
    api.chat.mockRejectedValueOnce(new ApiError("XENIO is still answering: stop it or wait for the answer", 409));
    await renderApp(api);
    await userEvent.type(await screen.findByLabelText("Message XENIO"), "hello{Enter}");
    expect(await screen.findByText(/still answering/)).toBeInTheDocument();
  });

  it("shows a backend outage instead of a calm orb", async () => {
    const api = new FakeApi();
    await renderApp(api);
    act(() => api.status.forEach((s) => s("retrying")));
    expect((await screen.findAllByText("Reconnecting to XENIO")).length).toBeGreaterThan(0);
    expect(screen.getAllByRole("img", { name: /XENIO: Not connected/ }).length).toBeGreaterThan(0);
  });
});

describe("voice", () => {
  it("keeps typing usable when the microphone is blocked", async () => {
    const api = new FakeApi();
    vi.stubGlobal("AudioContext", class { state = "running"; resume = vi.fn(); });
    Object.defineProperty(navigator, "mediaDevices", { configurable: true,
      value: { getUserMedia: vi.fn(async () => { throw new DOMException("denied", "NotAllowedError"); }) } });
    await renderApp(api);
    await userEvent.click(await screen.findByRole("button", { name: "Speak to XENIO" }));
    expect(await screen.findByText(/Microphone access is blocked/)).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Message XENIO"), "type instead{Enter}");
    await waitFor(() => expect(api.chat).toHaveBeenCalledWith("type instead", "text", false));
  });

  it("says when there is no microphone", async () => {
    const api = new FakeApi();
    vi.stubGlobal("AudioContext", class { state = "running"; resume = vi.fn(); });
    Object.defineProperty(navigator, "mediaDevices", { configurable: true,
      value: { getUserMedia: vi.fn(async () => { throw new DOMException("none", "NotFoundError"); }) } });
    await renderApp(api);
    await userEvent.click(await screen.findByRole("button", { name: "Speak to XENIO" }));
    expect(await screen.findByText(/No microphone was found/)).toBeInTheDocument();
  });

  it("disables the microphone, with the reason, when speech recognition is not set up", async () => {
    const api = new FakeApi();
    api.snap = snapshot({ voice: { stt: false, tts: false } });
    await renderApp(api);
    expect(await screen.findByRole("button", { name: "Voice input is not set up" })).toBeDisabled();
    expect(screen.getByLabelText("Message XENIO")).toBeEnabled();
  });

  it("keeps the answer as text when XENIO's voice fails", async () => {
    const api = new FakeApi();
    api.speak.mockRejectedValueOnce(new ApiError("speech output failed: TTS returned 500", 502));
    const { app } = await renderApp(api);
    app.store.set({ settings: { ...(await api.settings()), speak_replies: true } });
    act(() => api.emit("chat.done", { id: "t1-a", turn: "t1", role: "assistant", text: "It is 10 o'clock.", ok: true, actions: [], sources: [], at: "" }));
    expect(await screen.findByText("It is 10 o'clock.")).toBeInTheDocument();
    expect(await screen.findByText(/speech output failed/)).toBeInTheDocument();
    expect(app.store.get().voice.speaking).toBe(false); // never "speaking" without audio
  });

  it("reports speech recognition failures and keeps the request unsent", async () => {
    const api = new FakeApi();
    api.transcribe.mockRejectedValueOnce(new ApiError("speech recognition failed: timeout", 504));
    const { app } = await renderApp(api);
    // a finished recording, as the microphone code hands it over
    const recording = { level: () => 0, stop: () => {}, cancel: () => {}, done: Promise.resolve(new Blob(["RIFF"], { type: "audio/wav" })) };
    app.recorder = async () => recording;
    await act(async () => { await app.toggleListening(); });
    expect(await screen.findByText(/speech recognition failed/)).toBeInTheDocument();
    expect(api.chat).not.toHaveBeenCalled();
  });
});
