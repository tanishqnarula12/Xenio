import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { ApiError } from "../api/client";
import { layout } from "../components/DependencyGraph";
import { milestones } from "../pages/Orchestration";
import { availability } from "../pages/Tools";
import { agent, approval, FakeApi, renderApp, run, toSummary } from "./fakes";

describe("agents", () => {
  it("shows each role's real state", async () => {
    const api = new FakeApi();
    api.agents.mockResolvedValue({ available: true, agents: [
      agent({ id: "frontend", name: "Frontend engineer", state: "working", current: [{ run: "run-1", task: "frontend", title: "Show the date", status: "running", failure: null }] }),
      agent({ id: "tester", name: "Tester", state: "blocked", blocked: [{ run: "run-1", task: "tests", title: "Test it", status: "blocked", failure: "waits for backend" }] }),
      agent({ id: "docs", name: "Documentation writer", state: "idle" }),
    ] });
    await renderApp(api, "agents");
    expect(await screen.findByText("1 of 3 agents working right now.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Frontend engineer/ })).toHaveTextContent("Show the date");
    expect(screen.getByRole("button", { name: /Tester/ })).toHaveTextContent("Waiting: Test it");
    expect(screen.getByRole("button", { name: /Tester/ })).toHaveTextContent("Blocked");
    expect(screen.getByRole("button", { name: /Documentation writer/ })).toHaveTextContent("Idle");
    expect(screen.getByRole("button", { name: /Frontend engineer/ })).toHaveTextContent("Working");
  });

  it("says when nobody is working, and when the team is off", async () => {
    const api = new FakeApi();
    api.agents.mockResolvedValue({ available: true, agents: [agent()] });
    await renderApp(api, "agents");
    expect(await screen.findByText("No agent is working right now.")).toBeInTheDocument();
    api.agents.mockResolvedValue({ available: false, reason: "the engineering team is switched off (XENIO_ENGINEERING=false)", agents: [] });
    await renderApp(api, "agents");
    expect(await screen.findByText(/switched off/)).toBeInTheDocument();
  });
});

describe("orchestration", () => {
  it("lays tasks out by what they wait for", () => {
    const placed = layout(run().tasks);
    const col = (id: string) => placed.find((p) => p.task.id === id)!.col;
    expect([col("backend"), col("frontend"), col("tests"), col("review")]).toEqual([0, 1, 1, 2]);
  });

  it("counts progress from real task states, never a percentage", async () => {
    expect(milestones(run().tasks)).toBe("1 of 4 tasks done · 2 running");
    const api = new FakeApi();
    api.runs.mockResolvedValue({ available: true, runs: [toSummary(run())] });
    await renderApp(api, "orchestration");
    expect(await screen.findByText(/1 of 4 tasks done · 2 running · 2 agents working now/)).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/\d+\s?%/);
    expect(screen.getByRole("table", { name: "Tasks of this run" })).toBeInTheDocument();
  });

  it("never shows a failed or unverified run as a success", async () => {
    const api = new FakeApi();
    const failed = run({ status: "failed", outcome: "The tests failed after the change.", running: [],
      tasks: run().tasks.map((t) => (t.id === "tests" ? { ...t, status: "failed", failure: "2 tests failed" } : t)) });
    api.runs.mockResolvedValue({ available: true, runs: [toSummary(failed)] });
    api.run.mockResolvedValue(failed);
    await renderApp(api, "orchestration");
    expect(await screen.findByText("2 tests failed")).toBeInTheDocument();
    expect(screen.queryByText("Verified by XENIO's own checks")).toBeNull();
    const done = run({ status: "done", verified: false, running: [] });
    api.runs.mockResolvedValue({ available: true, runs: [toSummary(done)] });
    api.run.mockResolvedValue(done);
    await renderApp(api, "orchestration");
    expect(await screen.findByText("Finished, but not verified")).toBeInTheDocument();
  });

  it("explains an empty history", async () => {
    await renderApp(new FakeApi(), "orchestration");
    expect(await screen.findByText(/No engineering runs yet/)).toBeInTheDocument();
  });
});

describe("schedules", () => {
  it("shows Hermes' real jobs with their on/off state", async () => {
    const api = new FakeApi();
    api.schedules.mockResolvedValue({ available: true, ticking: true, jobs: [
      { id: "j1", name: "Inbox check", schedule: "every day at 08:00", enabled: true, next_run: "2026-10-19T08:00:00", last_run: null,
        last_status: null, last_error: null, task: "anything urgent?", runs: [] },
      { id: "j2", name: "Weekly report", schedule: "mondays", enabled: false, next_run: null, last_run: "2026-10-12T09:00:00",
        last_status: "error", last_error: "model unavailable", task: "summarise", runs: [{ at: "2026-10-12T09-00", result: "failed", message: "model unavailable" }] },
    ] });
    await renderApp(api, "schedules");
    expect(await screen.findByText("Inbox check")).toBeInTheDocument();
    expect(screen.getByText("On")).toBeInTheDocument();
    expect(screen.getByText("Off")).toBeInTheDocument();
    expect(screen.getByText("Last run failed")).toBeInTheDocument();
  });

  it("has an empty state and survives a failed connection", async () => {
    const api = new FakeApi();
    await renderApp(api, "schedules");
    expect(await screen.findByText(/No scheduled checks/)).toBeInTheDocument();
    api.schedules.mockRejectedValue(new ApiError("XENIO is not reachable", 0));
    await renderApp(api, "schedules");
    expect(await screen.findByText(/could not be loaded: XENIO is not reachable/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Try again/ })).toBeInTheDocument();
  });
});

describe("approvals", () => {
  it("sends only the user's own decision, and shows XENIO's refusal", async () => {
    const api = new FakeApi();
    api.snap = { ...api.snap, approvals: [approval()] };
    await renderApp(api, "approvals");
    const card = await screen.findByRole("article", { name: /run: pip install requests/ });
    expect(api.decide).not.toHaveBeenCalled(); // showing a question never answers it
    await userEvent.click(within(card).getByRole("button", { name: "Decline" }));
    expect(api.decide).toHaveBeenCalledWith("abc123", "deny", "");
    api.decide.mockRejectedValueOnce(new ApiError("this approval is no longer open (answered, expired or unknown)", 404));
    await userEvent.click(within(card).getByRole("button", { name: "Allow once" }));
    expect(await within(card).findByText(/no longer open/)).toBeInTheDocument();
  });

  it("needs the word confirm for a high-risk action", async () => {
    const api = new FakeApi();
    api.snap = { ...api.snap, approvals: [approval({ id: "def456", risk: "HIGH_RISK", strict: true, description: "permanently delete ~/old" })] };
    await renderApp(api, "approvals");
    const main = within(screen.getByRole("main"));
    const allow = await main.findByRole("button", { name: "Allow once" });
    expect(allow).toBeDisabled();
    await userEvent.type(main.getByLabelText(/to allow this high-risk action/), "confirm");
    expect(allow).toBeEnabled();
    await userEvent.click(allow);
    expect(api.decide).toHaveBeenCalledWith("def456", "approve", "confirm");
  });

  it("removes an answered question when XENIO says it is settled", async () => {
    const api = new FakeApi();
    api.snap = { ...api.snap, approvals: [approval()] };
    await renderApp(api, "approvals");
    await screen.findByRole("article");
    act(() => api.emit("approval.settled", { id: "abc123", status: "expired" }));
    await waitFor(() => expect(screen.queryByRole("article")).toBeNull());
    expect(screen.getByText("Nothing is waiting for your decision.")).toBeInTheDocument();
  });
});

describe("integrations", () => {
  it("shows working, broken and unchecked services", async () => {
    const api = new FakeApi();
    api.health.mockResolvedValue({ checked_at: "2026-10-18T10:00:00", platform: "darwin", checks: [
      { name: "Hermes", status: "OK", detail: "connected" }, { name: "Model", status: "OK", detail: "free model" },
      { name: "Google", status: "WARN", detail: "not connected: run --connect-google" },
      { name: "Desktop", status: "FAIL", detail: "macOS has not given XENIO Accessibility access" },
    ] });
    await renderApp(api, "integrations");
    const hermes = (await screen.findByText("Hermes Agent")).closest("li")!;
    expect(within(hermes).getByText("Working")).toBeInTheDocument();
    const google = screen.getByText("Google Calendar and Gmail").closest("li")!;
    expect(within(google).getByText("Needs attention")).toBeInTheDocument();
    expect(within(google).getByText(/Needs: python -m app.main --connect-google/)).toBeInTheDocument();
    const desktop = screen.getByText("Desktop control").closest("li")!;
    expect(within(desktop).getByText("Not working")).toBeInTheDocument();
    const github = screen.getByText("GitHub").closest("li")!;
    expect(within(github).getByText("Not checked")).toBeInTheDocument(); // no check: no claim
  });

  it("does not claim a capability that has no check, and names missing permissions", () => {
    expect(availability("Desktop", {}, "darwin").label).toBe("Not checked");
    expect(availability("Desktop", {}, "linux").label).toBe("Not supported on this platform");
    expect(availability("Desktop", { Desktop: { name: "Desktop", status: "WARN", detail: "needs the Accessibility permission" } }, "darwin").label)
      .toBe("Needs a macOS permission");
  });

  it("reports a failed health check", async () => {
    const api = new FakeApi();
    api.health.mockRejectedValue(new ApiError("XENIO did not answer in time", 504));
    await renderApp(api, "system");
    expect(await screen.findByText(/could not be loaded: XENIO did not answer in time/)).toBeInTheDocument();
    expect(screen.getByText("Not measured by XENIO")).toBeInTheDocument(); // no invented CPU or uptime
  });
});

describe("tasks", () => {
  it("adds, completes and - only after a confirmation - deletes", async () => {
    const api = new FakeApi();
    api.tasks.mockResolvedValue({ tasks: [{ id: 1, title: "Renew passport", status: "pending", priority: "high", source: "user", created: "", updated: "" }], counts: {} });
    await renderApp(api, "tasks");
    await userEvent.type(await screen.findByLabelText("New task"), "Call the bank");
    await userEvent.click(screen.getByRole("button", { name: "Add task" }));
    expect(api.createTask).toHaveBeenCalledWith({ title: "Call the bank", priority: "normal", due: undefined });
    await userEvent.click(await screen.findByLabelText("Renew passport"));
    expect(api.updateTask).toHaveBeenCalledWith(1, "complete");
    await userEvent.click(screen.getByRole("button", { name: 'Delete "Renew passport"' }));
    const dialog = screen.getByRole("alertdialog");
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toHaveFocus(); // the safe choice first
    await userEvent.keyboard("{Escape}");
    expect(api.deleteTask).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: 'Delete "Renew passport"' }));
    await userEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Delete task" }));
    expect(api.deleteTask).toHaveBeenCalledWith(1);
  });
});
