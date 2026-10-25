import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { DemoApi } from "../api/demo";
import { RichText, safeUrl } from "../components/RichText";
import { AppController } from "../state/controller";
import { AppProvider } from "../state/context";
import { Store } from "../state/store";
import { App } from "../App";
import { FakeApi, renderApp } from "./fakes";

describe("security", () => {
  it("renders outside text as text: no markup runs, only http(s) links", () => {
    const { container } = render(<RichText text={'<img src=x onerror="alert(1)"> **bold** [x](javascript:alert(1)) https://example.com/a and javascript:alert(2)'} />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.textContent).toContain('<img src=x onerror="alert(1)">');
    const links = [...container.querySelectorAll("a")];
    expect(links.map((a) => a.getAttribute("href"))).toEqual(["https://example.com/a"]);
    expect(links[0]).toHaveAttribute("rel", "noopener noreferrer");
    expect(safeUrl("javascript:alert(1)")).toBeNull();
    expect(safeUrl("data:text/html,hi")).toBeNull();
  });

  it("never shows the session token on the page", async () => {
    window.history.replaceState(null, "", "/#token=TOKENTOKENTOKENTOKENTOKEN");
    const { readToken } = await import("../api/client");
    const token = readToken()!;
    const api = new FakeApi();
    await renderApp(api);
    expect(document.body.innerHTML).not.toContain(token);
    expect(window.location.href).not.toContain(token);
  });

  it("labels demo data everywhere and never as live", async () => {
    const api = new DemoApi();
    const store = new Store();
    const app = new AppController(api, store);
    window.location.hash = "#/home";
    await app.init();
    render(<AppProvider store={store} app={app}><App /></AppProvider>);
    expect(await screen.findByText(/Demo data: this page is not connected to XENIO/)).toBeInTheDocument();
    expect(screen.getByText("Demo data")).toBeInTheDocument();
    expect(screen.queryByText("Connected")).toBeNull();
    expect((await api.snapshot()).demo).toBe(true);
  });

  it("has no way to approve without the backend", async () => {
    const api = new FakeApi();
    await renderApp(api, "approvals");
    // nothing pending: no approve control exists at all
    expect(await screen.findByText("Nothing is waiting for your decision.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Allow once" })).toBeNull();
  });
});

describe("accessibility", () => {
  it("keeps new words in view without scrolling the page away", async () => {
    const api = new FakeApi();
    await renderApp(api, "home");
    const scrollTo = vi.spyOn(window, "scrollTo");
    const into = vi.spyOn(Element.prototype, "scrollIntoView");
    act(() => api.emit("chat.user", { id: "t9-u", turn: "t9", role: "user", text: "hello", at: "" }));
    act(() => api.emit("chat.chunk", { turn: "t9", text: "Hi there" }));
    expect(await screen.findByText("Hi there")).toBeInTheDocument();
    expect(scrollTo).not.toHaveBeenCalled();
    expect(into).not.toHaveBeenCalled();
  });

  it("marks the current page and moves between pages by keyboard", async () => {
    await renderApp(new FakeApi(), "agents");
    const nav = screen.getByRole("navigation", { name: "Mission Control" });
    expect(screen.getByRole("link", { name: "Agents" })).toHaveAttribute("aria-current", "page");
    expect(nav.querySelectorAll("a[href^='#/']").length).toBe(13); // wordmark + 12 pages
  });

  it("opens the assistant from the orb, closes on Escape and gives focus back", async () => {
    await renderApp(new FakeApi(), "agents");
    const orb = screen.getByRole("button", { name: /Open XENIO/ });
    await userEvent.click(orb);
    const panel = screen.getByRole("dialog", { name: "XENIO" });
    await waitFor(() => expect(panel).not.toHaveAttribute("hidden"));
    expect(orb).toHaveAttribute("aria-expanded", "true");
    await userEvent.keyboard("{Escape}");
    expect(orb).toHaveAttribute("aria-expanded", "false");
    expect(orb).toHaveFocus();
  });

  it("follows the reduced-motion setting", async () => {
    const { app } = await renderApp(new FakeApi());
    act(() => app.store.set({ settings: { motion: "reduced", speak_replies: false, orb_detail: "auto", notifications: true, transcript_lines: 50 } }));
    await waitFor(() => expect(document.documentElement.dataset.motion).toBe("reduced"));
    act(() => app.store.set({ settings: { motion: "full", speak_replies: false, orb_detail: "auto", notifications: true, transcript_lines: 50 } }));
    await waitFor(() => expect(document.documentElement.dataset.motion).toBe("full"));
  });

  it("announces status changes and never relies on colour alone", async () => {
    const api = new FakeApi();
    await renderApp(api, "home");
    expect(screen.getByRole("heading", { level: 1, name: "Ready" })).toHaveAttribute("aria-live", "polite");
    act(() => api.emit("assistant.state", { state: "SEARCHING", connection: "connected", paused: false, approvals: 0, turn: "t1",
      tool: "web_search", error: null, background: { state: null, runs: 0, agents: 0 } }));
    expect(await screen.findByRole("heading", { level: 1, name: "Looking it up" })).toBeInTheDocument();
    expect(screen.getByText("Looking it up: searching the web.")).toBeInTheDocument();
  });
});
