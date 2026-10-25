import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { reloadLayout } from "../state/layout";
import { approval, FakeApi, renderApp, run, toSummary } from "./fakes";

beforeEach(() => {
  localStorage.clear();
  reloadLayout();
});

describe("deciding from Home", () => {
  it("keeps questions in view above the message box, not inside the scrolled conversation", async () => {
    const api = new FakeApi();
    api.snap = { ...api.snap, approvals: [approval({ description: "create snake/game.js in the XENIO workspace", confined: true })],
      conversation: Array.from({ length: 30 }, (_, i) => ({ id: `m${i}`, turn: `t${i}`, role: "user" as const, text: `message ${i}`, at: "" })) };
    const { container } = await renderApp(api);
    const card = await screen.findByRole("article", { name: /Approval needed: create snake/ });
    expect(card.closest(".decision-tray")).not.toBeNull();
    expect(card.closest(".home-chat__scroll")).toBeNull();
    expect(container.querySelector(".decision-tray")!.nextElementSibling).toHaveClass("composer");
  });

  it("offers 'allow for this task' only when XENIO says the action stays in its workspace", async () => {
    const api = new FakeApi();
    api.snap = { ...api.snap, approvals: [approval({ id: "aaa111", description: "create snake/game.js", confined: true }),
      approval({ id: "bbb222", description: "change ~/Desktop/notes.txt", confined: false })] };
    await renderApp(api);
    const inside = await screen.findByRole("article", { name: /create snake/ });
    const outside = screen.getByRole("article", { name: /Desktop/ });
    expect(within(outside).queryByRole("button", { name: "Allow for this task" })).toBeNull();
    await userEvent.click(within(inside).getByRole("button", { name: "Allow for this task" }));
    expect(api.decide).toHaveBeenCalledWith("aaa111", "approve_task", "");
  });

  it("shows when XENIO works on its own, and ends it", async () => {
    const api = new FakeApi();
    const until = new Date(Date.now() + 20 * 60_000).toISOString();
    api.snap = { ...api.snap, grant: { active: true, until, seconds_left: 1200, covered: 3, reason: "create snake/game.js" } };
    await renderApp(api);
    expect(await screen.findByText(/Working on its own in XENIO's workspace · 20 min left · 3 steps without asking/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "End" }));
    expect(api.endGrant).toHaveBeenCalled();
    act(() => api.emit("grant.changed", { grant: null }));
    await waitFor(() => expect(screen.queryByText(/Working on its own/)).not.toBeInTheDocument());
  });

  it("starts an engineering plan from Home", async () => {
    const api = new FakeApi();
    api.runs.mockResolvedValue({ available: true, runs: [toSummary(run({ status: "planned", request: "Build a snake game" }))] });
    await renderApp(api);
    const card = await screen.findByRole("article", { name: /Plan ready: Build a snake game/ });
    await userEvent.click(within(card).getByRole("button", { name: "Start it" }));
    expect(api.runAction).toHaveBeenCalledWith("run-1", "approve");
  });
});

describe("reminders and projects", () => {
  it("sets and removes reminders", async () => {
    const api = new FakeApi();
    api.schedules.mockResolvedValue({ available: true, jobs: [], ticking: true, reminders: [
      { id: "abc123", kind: "reminder", text: "call mom", schedule: null, enabled: true, next_run: "2026-10-23T17:00:00",
        last_run: null, last_status: null, done: false }] });
    await renderApp(api, "schedules");
    await userEvent.type(await screen.findByLabelText("Remind me to"), "drink water");
    await userEvent.type(screen.getByLabelText("When"), "in 20 minutes");
    await userEvent.click(screen.getByRole("button", { name: "Set reminder" }));
    expect(api.createReminder).toHaveBeenCalledWith("drink water", "in 20 minutes");
    await userEvent.click(await screen.findByRole("button", { name: "Remove the reminder: call mom" }));
    expect(api.deleteReminder).toHaveBeenCalledWith("abc123");
  });

  it("lists what XENIO built in its workspace", async () => {
    const api = new FakeApi();
    api.projects.mockResolvedValue({ projects: [], current: null, workspace_path: "/Users/me/Xenio", workspace: [
      { name: "snake", path: "/Users/me/Xenio/snake", kind: "Web page", git: false, files: 3, updated: "2026-10-19T13:37:00",
        runs: 1, last_run: { id: "run-1", status: "done", request: "Build a snake game" } }] });
    await renderApp(api, "tasks");
    expect(await screen.findByText("snake")).toBeInTheDocument();
    expect(screen.getByText(/last team run: Build a snake game/)).toBeInTheDocument();
  });

  it("saves the location the user types", async () => {
    const api = new FakeApi();
    await renderApp(api, "settings");
    await userEvent.type(await screen.findByLabelText("Where you are (city or area)"), "New Delhi, India");
    await userEvent.click(within(screen.getByRole("region", { name: "Location" })).getByRole("button", { name: "Save" }));
    expect(api.saveLocation).toHaveBeenCalledWith({ label: "New Delhi, India" });
  });
});
