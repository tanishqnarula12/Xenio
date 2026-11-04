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

describe("your chats", () => {
  const today = new Date().toISOString().slice(0, 19);
  const daysAgo = (n: number) => new Date(Date.now() - n * 86_400_000).toISOString().slice(0, 19);
  const chat = (id: string, title: string, updated = today) => ({ id, title, created: updated, updated, messages: 2, imported: false });
  const list = () => within(screen.getByRole("complementary", { name: "Your chats" }));

  it("lists them newest first, by when, and marks the one on screen", async () => {
    const api = new FakeApi();
    api.chats.mockResolvedValue({ chats: [chat("chat_a", "Fix my notes"), chat("chat_b", "Teach me gravity", daysAgo(1)), chat("chat_c", "An old one", daysAgo(90))], current: "chat_a" });
    await renderApp(api, "chat");
    expect(await list().findByRole("button", { name: "Fix my notes" })).toHaveAttribute("aria-current", "true");
    expect(list().getByRole("button", { name: "Teach me gravity" })).not.toHaveAttribute("aria-current");
    for (const heading of ["Today", "Yesterday", "Earlier"]) expect(list().getByText(heading)).toBeInTheDocument();
    expect(list().getByText("Kept on this Mac only.")).toBeInTheDocument();
  });

  it("says so when there are none yet", async () => {
    await renderApp(new FakeApi(), "chat");
    expect(await list().findByText("Your chats will be kept here.")).toBeInTheDocument();
  });

  it("opens one, and its words arrive by the live event", async () => {
    const api = new FakeApi();
    api.chats.mockResolvedValue({ chats: [chat("chat_b", "Teach me gravity")], current: null });
    await renderApp(api, "chat");
    await userEvent.click(await list().findByRole("button", { name: "Teach me gravity" }));
    expect(api.changeChat).toHaveBeenCalledWith("chat_b", "open");
    act(() => api.emit("chat.opened", { id: "chat_b", conversation: [said(1, "user", "what is gravity"), said(2, "assistant", "a pull between masses")] }));
    expect(await screen.findByText("a pull between masses")).toBeInTheDocument();
    expect(screen.queryByText("What can I do for you?")).toBeNull();
  });

  it("does not switch chats while XENIO is answering", async () => {
    const api = new FakeApi();
    api.chats.mockResolvedValue({ chats: [chat("chat_b", "Teach me gravity")], current: null });
    api.snap = snapshot({ assistant: assistant({ state: "THINKING", turn: "t1" }) });
    await renderApp(api, "chat");
    await userEvent.click(await list().findByRole("button", { name: "Teach me gravity" }));
    expect(api.changeChat).not.toHaveBeenCalled();
  });

  it("a failure to open is said", async () => {
    const api = new FakeApi();
    api.chats.mockResolvedValue({ chats: [chat("chat_b", "Teach me gravity")], current: null });
    api.changeChat.mockRejectedValue(new Error("that chat is not there any more"));
    await renderApp(api, "chat");
    await userEvent.click(await list().findByRole("button", { name: "Teach me gravity" }));
    expect(await screen.findByText(/not there any more/)).toBeInTheDocument();
  });

  it("deletes only after asking", async () => {
    const api = new FakeApi();
    api.chats.mockResolvedValue({ chats: [chat("chat_b", "Teach me gravity")], current: null });
    await renderApp(api, "chat");
    await userEvent.click(await list().findByRole("button", { name: "Delete Teach me gravity" }));
    expect(api.changeChat).not.toHaveBeenCalled();
    await userEvent.click(list().getByRole("button", { name: "Keep" }));
    expect(api.changeChat).not.toHaveBeenCalled();
    await userEvent.click(list().getByRole("button", { name: "Delete Teach me gravity" }));
    await userEvent.click(list().getByRole("button", { name: "Delete" }));
    expect(api.changeChat).toHaveBeenCalledWith("chat_b", "delete");
  });

  it("deletes them all only after asking", async () => {
    const api = new FakeApi();
    await renderApp(api, "chat");
    await userEvent.click(await list().findByRole("button", { name: "Delete all chats" }));
    expect(api.deleteAllChats).not.toHaveBeenCalled();
    await userEvent.click(list().getByRole("button", { name: "Yes, delete all" }));
    expect(api.deleteAllChats).toHaveBeenCalled();
  });

  it("searches as you type", async () => {
    const api = new FakeApi();
    await renderApp(api, "chat");
    await userEvent.type(await screen.findByRole("searchbox", { name: "Search your chats" }), "zebra");
    await waitFor(() => expect(api.chats).toHaveBeenCalledWith("zebra", expect.anything()));
    expect(await list().findByText("No chat has those words.")).toBeInTheDocument();
  });

  it("starts a new chat from the list, and can be tucked away", async () => {
    const api = new FakeApi();
    await renderApp(api, "chat");
    await userEvent.click(await list().findByRole("button", { name: "New chat" }));
    expect(api.newConversation).toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Hide your chats" }));
    expect(screen.queryByRole("complementary", { name: "Your chats" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Show your chats" }));
    expect(screen.getByRole("complementary", { name: "Your chats" })).toBeInTheDocument();
  });

  it("refreshes when a reply ends or a chat is deleted", async () => {
    const api = new FakeApi();
    await renderApp(api, "chat");
    await list().findByText("Your chats will be kept here.");
    const before = api.chats.mock.calls.length;
    act(() => api.emit("chats.changed", {}));
    await waitFor(() => expect(api.chats.mock.calls.length).toBeGreaterThan(before));
  });
});

describe("what a reply offers next", () => {
  const offered = { ...said(2, "assistant", "Recursion is a function calling itself."), next: ["Walk through factorial(3)", "Show it as a diagram"] } as Message;

  it("shows as buttons under the newest reply, and a tap sends it, on screen only", async () => {
    const api = new FakeApi();
    api.snap = snapshot({ conversation: [said(1, "user", "what is recursion"), offered] });
    await renderApp(api, "chat");
    await userEvent.click(await screen.findByRole("button", { name: "Show it as a diagram" }));
    await waitFor(() => expect(api.chat).toHaveBeenCalled());
    expect(api.chat.mock.calls[0]!.slice(0, 3)).toEqual(["Show it as a diagram", "text", true]);
  });

  it("only under the newest one", async () => {
    const api = new FakeApi();
    api.snap = snapshot({ conversation: [said(1, "user", "q"), offered, said(3, "user", "another"), said(4, "assistant", "more")] });
    await renderApp(api, "chat");
    await screen.findByText("more");
    expect(screen.queryByRole("group", { name: "What next?" })).toBeNull();
  });

  it("goes away while XENIO is working", async () => {
    const api = new FakeApi();
    api.snap = snapshot({ conversation: [said(1, "user", "q"), offered] });
    await renderApp(api, "chat");
    await screen.findByRole("group", { name: "What next?" });
    act(() => api.emit("assistant.state", assistant({ state: "THINKING", turn: "t9" }) as unknown as Record<string, unknown>));
    await waitFor(() => expect(screen.queryByRole("group", { name: "What next?" })).toBeNull());
  });

  it("are spoken on Home, as its replies are", async () => {
    const api = new FakeApi();
    api.snap = snapshot({ conversation: [said(1, "user", "q"), offered] });
    await renderApp(api, "home");
    await userEvent.click(await screen.findByRole("button", { name: "Walk through factorial(3)" }));
    await waitFor(() => expect(api.chat).toHaveBeenCalled());
    expect(api.chat.mock.calls[0]![2]).toBeFalsy();
  });
});

describe("the Learn page, made simple", () => {
  it("is one box that takes you to the chat", async () => {
    const api = new FakeApi();
    await renderApp(api, "learn");
    const box = await screen.findByPlaceholderText(/What do you want to learn/);
    await userEvent.type(box, "gravity");
    await userEvent.click(screen.getByRole("button", { name: /Learn it in chat/ }));
    await waitFor(() => expect(api.chat).toHaveBeenCalled());
    expect(api.chat.mock.calls[0]!.slice(0, 3)).toEqual(["Teach me gravity", "text", true]);
    expect(window.location.hash).toBe("#/chat");
    expect(api.changeLearning).not.toHaveBeenCalled();
  });

  it("a whole lesson is still there, under more options, for when it is wanted", async () => {
    const api = new FakeApi();
    api.changeLearning.mockResolvedValue({ ok: true, lesson: 1 } as never);
    await renderApp(api, "learn");
    await userEvent.type(await screen.findByPlaceholderText(/What do you want to learn/), "gravity");
    await userEvent.click(screen.getByText("More options"));
    await userEvent.click(screen.getByRole("button", { name: /Write me a whole lesson instead/ }));
    await waitFor(() => expect(api.changeLearning).toHaveBeenCalledWith(expect.objectContaining({ action: "compose", topic: "gravity" })));
    expect(api.chat).not.toHaveBeenCalled();
  });
});
