import { act, fireEvent, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { workingLabel } from "../components/Conversation";
import { assistant, FakeApi, renderApp, snapshot } from "./fakes";

const tool = (over = {}) => ({ turn: "t1", id: "c1", title: "web_search: best laptops", status: "in_progress", ...over }) as never;

describe("while XENIO answers", () => {
  it("says what it is really doing: the tool it is running, then Writing, never a made-up step", () => {
    expect(workingLabel([], false)).toBe("Thinking");
    expect(workingLabel([tool()], false)).toBe("Searching the web");
    expect(workingLabel([tool({ status: "completed" })], true)).toBe("Writing");
    expect(workingLabel([tool({ status: "completed" }), tool({ id: "c2", title: "xenio_calendar_events", status: "in_progress" })], true)).toBe("Checking your calendar");
  });

  it("shows that line live, and a new answer settles in while older ones stay still", async () => {
    const api = new FakeApi();
    await renderApp(api);
    act(() => {
      api.emit("chat.user", { id: "t1-u", turn: "t1", role: "user", text: "find laptops", at: "" });
      api.emit("chat.tool", { turn: "t1", id: "c1", title: "web_search: laptops", status: "in_progress" });
    });
    expect((await screen.findAllByText("Searching the web")).length).toBeGreaterThan(0);
    expect(screen.getAllByRole("status").some((el) => el.textContent?.includes("Searching the web"))).toBe(true);
    act(() => api.emit("chat.done", { id: "t1-a", turn: "t1", role: "assistant", text: "Here are three.", ok: true, actions: [], sources: [], at: "" }));
    const answer = (await screen.findByText("Here are three.")).closest(".msg");
    expect(answer).toHaveClass("msg--arrived");
  });

  it("an empty Enter nudges the bar instead of doing nothing, and sends nothing", async () => {
    const api = new FakeApi();
    await renderApp(api);
    const box = await screen.findByLabelText("Message XENIO");
    await userEvent.type(box, "{Enter}");
    expect(box.closest(".composer__row")).toHaveAttribute("data-nudge");
    expect(api.chat).not.toHaveBeenCalled();
    fireEvent.change(box, { target: { value: "hi" } });
    expect(screen.getByRole("button", { name: "Send" })).toHaveAttribute("data-ready");
  });
});

describe("the orb and the chat move together", () => {
  it("each piece of the answer swells the orb, and it settles once the words stop", async () => {
    const api = new FakeApi();
    const { app } = await renderApp(api);
    const now = vi.spyOn(performance, "now");
    now.mockReturnValue(10_000);
    act(() => api.emit("chat.chunk", { turn: "t1", text: "Here is a fairly long piece of the answer, arriving now." }));
    const fresh = app.writingLevel();
    now.mockReturnValue(10_400);
    const later = app.writingLevel();
    now.mockReturnValue(11_500);
    expect(fresh).toBeGreaterThan(0.4);
    expect(later).toBeLessThan(fresh);
    expect(app.writingLevel()).toBe(0);
    now.mockRestore();
  });

  it("while words arrive, the orb and the chat both say Writing", async () => {
    const api = new FakeApi();
    api.snap = snapshot({ assistant: assistant({ state: "THINKING", turn: "t1" }) });
    await renderApp(api, "chat");
    act(() => {
      api.emit("chat.user", { id: "t1-u", turn: "t1", role: "user", text: "tell me", at: "" });
      api.emit("chat.chunk", { turn: "t1", text: "Sure, " });
    });
    await screen.findByText("Sure,"); // the answer is on screen before its line is looked for
    const status = (await screen.findAllByRole("status")).find((el) => el.classList.contains("working"));
    expect(status?.textContent).toContain("Writing");
  });

  it("every answer carries XENIO's mark, and Copy copies it as written", async () => {
    const writeText = vi.fn(async () => undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    const api = new FakeApi();
    await renderApp(api, "chat");
    act(() => {
      api.emit("chat.user", { id: "t1-u", turn: "t1", role: "user", text: "hi", at: "" });
      api.emit("chat.done", { id: "t1-a", turn: "t1", role: "assistant", text: "Hello **there**.", ok: true, actions: [], sources: [], at: "" });
    });
    const answer = (await screen.findByText("there")).closest(".msg");
    expect(answer?.querySelector(".msg__mark svg")).not.toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Copy the answer" }));
    expect(writeText).toHaveBeenCalledWith("Hello **there**.");
    expect(await screen.findByRole("button", { name: "Copied" })).toBeInTheDocument();
  });
});
