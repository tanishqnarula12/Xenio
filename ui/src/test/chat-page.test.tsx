import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type { Message } from "../api/types";
import { approval, assistant, FakeApi, renderApp, snapshot } from "./fakes";

const said = (n: number, role: "user" | "assistant", text: string): Message => ({ id: `m${n}`, turn: `t${Math.ceil(n / 2)}`, role, text, at: "2026-11-03T10:00:00" } as unknown as Message);
const talk = [said(1, "user", "first question"), said(2, "assistant", "first answer"), said(3, "user", "second question"), said(4, "assistant", "second answer")];

describe("the Chat page", () => {
  it("is in the menu, right after Home, and has no orb", async () => {
    const { container } = await renderApp(new FakeApi(), "chat");
    const links = within(screen.getByRole("navigation", { name: "Mission Control" })).getAllByRole("link").map((a) => a.textContent);
    expect(links.indexOf("Chat")).toBe(links.indexOf("Home") + 1);
    expect(await screen.findByRole("heading", { name: "Chat" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Chat" })).toHaveAttribute("aria-current", "page");
    expect(await screen.findByText("What can I do for you?")).toBeInTheDocument();
    expect(container.querySelector("[data-state]")).toBeNull();
    expect(screen.queryByRole("img", { name: /^XENIO:/ })).toBeNull();
  });

  it("shows the whole conversation", async () => {
    const api = new FakeApi();
    api.snap = snapshot({ conversation: talk });
    await renderApp(api, "chat");
    for (const words of ["first question", "first answer", "second question", "second answer"]) expect(await screen.findByText(words)).toBeInTheDocument();
    expect(screen.queryByText("What can I do for you?")).toBeNull();
  });

  it("a typed message is answered on screen, not out loud", async () => {
    const api = new FakeApi();
    await renderApp(api, "chat");
    await screen.findByText("What can I do for you?");
    await userEvent.type(document.getElementById("composer-input") as HTMLTextAreaElement, "hello{Enter}");
    await waitFor(() => expect(api.chat).toHaveBeenCalled());
    expect(api.chat.mock.calls[0]!.slice(0, 3)).toEqual(["hello", "text", true]);
    expect(screen.getByText(/Replies stay on screen here/)).toBeInTheDocument();
  });

  it("says what XENIO is doing only while it is doing it", async () => {
    const api = new FakeApi();
    await renderApp(api, "chat");
    await screen.findByText("What can I do for you?");
    expect(screen.queryByRole("status")).toBeNull();
    act(() => api.emit("assistant.state", assistant({ state: "SEARCHING", turn: "t1", tool: "web_search" }) as unknown as Record<string, unknown>));
    expect(await screen.findByText("Looking it up: searching the web.")).toBeInTheDocument();
    act(() => api.emit("assistant.state", assistant({ state: "COMPLETED" }) as unknown as Record<string, unknown>));
    await waitFor(() => expect(screen.queryByText(/Looking it up/)).toBeNull());
  });

  it("asks for approval right there", async () => {
    const api = new FakeApi();
    api.snap = snapshot({ approvals: [approval()], assistant: assistant({ state: "WAITING_APPROVAL", approvals: 1, turn: "t1" }) });
    await renderApp(api, "chat");
    expect(await screen.findByRole("article", { name: /Approval needed: run: pip install requests/ })).toBeInTheDocument();
  });

  it("starts a new conversation, and leads back to the orb", async () => {
    const api = new FakeApi();
    await renderApp(api, "chat");
    await userEvent.click(await screen.findByRole("button", { name: "New conversation" }));
    expect(api.newConversation).toHaveBeenCalled();
    expect(screen.getByRole("link", { name: "Talk to XENIO, with the orb" })).toHaveAttribute("href", "#/home");
  });
});

describe("Home", () => {
  it("is still the orb, with only the latest exchange and a way to the full chat", async () => {
    const api = new FakeApi();
    api.snap = snapshot({ conversation: talk });
    const { container } = await renderApp(api, "home");
    expect(await screen.findByText("second answer")).toBeInTheDocument();
    expect(screen.getByText("second question")).toBeInTheDocument();
    expect(screen.queryByText("first answer")).toBeNull();
    expect(container.querySelector("[data-state]")).not.toBeNull();
    expect(screen.getByRole("link", { name: /Open the full chat/ })).toHaveAttribute("href", "#/chat");
  });
});
