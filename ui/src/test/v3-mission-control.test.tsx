import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type { ContextBrief, XenioEvent } from "../api/types";
import { elapsed } from "../pages/Agents";
import { lifecycle } from "../pages/Orchestration";
import { Store } from "../state/store";
import { agent, approval, FakeApi, renderApp, run, snapshot, toSummary } from "./fakes";

const ev = (id: string, type: string, data: Record<string, unknown>, at = "2026-10-24T10:00:00"): XenioEvent =>
  ({ v: 1, seq: 0, id, type, at, data });

describe("memory", () => {
  it("lets the user correct, delete and add entries, by id, with Hermes' real usage", async () => {
    const api = new FakeApi();
    api.memory.mockResolvedValue({ available: true, editable: true, folder: "/h/memories",
      usage: { user: { used: 40, limit: 1375 }, memory: { used: 0, limit: 2200 } },
      entries: [{ id: "u1", store: "user", text: "Prefers short answers" }] });
    await renderApp(api, "memory");
    expect(await screen.findByText("Prefers short answers")).toBeInTheDocument();
    expect(screen.getByText("40 of 1,375 characters")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Correct this memory" }));
    const box = screen.getByRole("textbox", { name: "Corrected text" });
    await userEvent.clear(box);
    await userEvent.type(box, "Prefers short answers in the morning");
    await userEvent.click(screen.getByRole("button", { name: /Save/ }));
    expect(api.changeMemory).toHaveBeenCalledWith({ action: "correct", id: "u1", text: "Prefers short answers in the morning" });

    await userEvent.click(screen.getByRole("button", { name: "Delete this memory" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Delete this memory?" });
    expect(api.changeMemory).toHaveBeenCalledTimes(1);  // nothing deleted before the confirmation
    await userEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
    expect(api.changeMemory).toHaveBeenLastCalledWith({ action: "delete", id: "u1" });

    await userEvent.click(screen.getAllByRole("button", { name: /Add/ })[1]!);
    await userEvent.type(screen.getByRole("textbox", { name: "New memory" }), "The home server is atlas");
    await userEvent.click(screen.getByRole("button", { name: "Remember it" }));
    expect(api.changeMemory).toHaveBeenLastCalledWith({ action: "add", store: "memory", text: "The home server is atlas" });
  });

  it("shows XENIO's refusal instead of pretending it saved", async () => {
    const api = new FakeApi();
    api.memory.mockResolvedValue({ available: true, editable: true, entries: [] });
    api.changeMemory.mockRejectedValue(new Error("that looks like a password, key or other secret"));
    await renderApp(api, "memory");
    await userEvent.click((await screen.findAllByRole("button", { name: /Add/ }))[0]!);
    await userEvent.type(screen.getByRole("textbox", { name: "New memory" }), "my password is hunter2");
    await userEvent.click(screen.getByRole("button", { name: "Remember it" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("password, key or other secret");
    expect(screen.getByText("What is not memory")).toBeInTheDocument();
  });
});

describe("activity", () => {
  it("filters on the server and pages back without duplicates", async () => {
    const api = new FakeApi();
    api.activity.mockImplementation(async (_l?: number, _s?: AbortSignal, offset = 0, kind = "all") => kind === "system"
      ? { events: [ev("s1", "system.event", { what: "XENIO started", tone: "info" })], more: false, next_offset: 100 }
      : offset === 0
        ? { events: [ev("a2", "task.changed", { id: 7, change: "complete" }), ev("a1", "memory.changed", { store: "user", action: "deleted" })], more: true, next_offset: 2 }
        : { events: [ev("a1", "memory.changed", { store: "user", action: "deleted" }), ev("a0", "settings.changed", { changed: ["text_size"] })], more: false, next_offset: 4 });
    await renderApp(api, "activity");
    expect(await screen.findByText("Task #7 marked done")).toBeInTheDocument();
    expect(screen.getByText("Memory (about you): an entry was deleted by you")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Show older activity" }));
    expect(await screen.findByText("Settings changed: text size")).toBeInTheDocument();
    expect(screen.getAllByText("Memory (about you): an entry was deleted by you")).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "Show older activity" })).toBeNull();
    await userEvent.click(screen.getByRole("radio", { name: "System" }));
    expect(await screen.findByText("XENIO started")).toBeInTheDocument();
    expect(api.activity).toHaveBeenLastCalledWith(100, expect.anything(), 0, "system");
  });

  it("shows tool calls with what really became of them", async () => {
    const api = new FakeApi();
    api.audit.mockResolvedValue({ calls: [
      { tool: "terminal", at: "2026-10-24T10:00:00", status: "allowed", level: "READ", summary: "run: ls", detail: "" },
      { tool: "xenio_delete_path", at: "2026-10-24T10:00:01", status: "blocked", level: "HIGH_RISK", summary: "delete /", detail: "" },
      { tool: "xenio_gui", at: "2026-10-24T10:00:02", status: "completed", level: "CONFIRM_REQUIRED", summary: "click Send", detail: "", ms: 800 },
    ] });
    await renderApp(api, "activity");
    await userEvent.click(await screen.findByRole("radio", { name: "Tool calls" }));
    expect(await screen.findByText("Allowed to run")).toBeInTheDocument();
    expect(screen.getByText(/is not proof that they worked/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("radio", { name: "Blocked, failed or declined" }));
    expect(screen.getByText("Blocked by XENIO")).toBeInTheDocument();
    expect(screen.queryByText("click Send")).toBeNull();
  });
});

describe("approvals", () => {
  it("say why, what they reach, who asked and what a yes allows - and can stop the request", async () => {
    const api = new FakeApi();
    api.snap = snapshot({ approvals: [approval({ id: "q1", description: "run: pip install requests", why: "Asked because it installs or runs software that comes from outside.",
      targets: ["command: pip install requests"], requested_by: "the backend agent, engineering task deps (run 1024-1000-abcd)",
      scope: "A yes allows only this action; XENIO asks again next time." })] });
    await renderApp(api, "approvals");
    const card = await screen.findByRole("article", { name: /pip install requests/ });
    expect(card).toHaveTextContent("Asked because it installs or runs software that comes from outside.");
    expect(card).toHaveTextContent("command: pip install requests");
    expect(card).toHaveTextContent("the backend agent, engineering task deps");
    expect(card).toHaveTextContent("A yes allows only this action");
    await userEvent.click(within(card).getByRole("button", { name: "Decline and stop" }));
    expect(api.decide).toHaveBeenCalledWith("q1", "deny");
    await waitFor(() => expect(api.cancel).toHaveBeenCalled());
  });

  it("disappear everywhere once XENIO settles them", async () => {
    const api = new FakeApi();
    api.snap = snapshot({ approvals: [approval({ id: "q2", description: "delete old.txt" })] });
    const { store } = await renderApp(api, "approvals");
    expect(await screen.findByRole("article", { name: /delete old.txt/ })).toBeInTheDocument();
    act(() => api.emit("approval.settled", { id: "q2", status: "approved", description: "delete old.txt" }));  // answered in the orb
    await waitFor(() => expect(screen.queryByRole("article", { name: /delete old.txt/ })).toBeNull());
    expect(store.get().approvals).toEqual([]);
  });
});

describe("orchestration", () => {
  it("shows only the lifecycle stages a run really went through", () => {
    const team = run({ status: "planned", events: [{ time: "", kind: "planned", text: "Plan ready" }] });
    const stages = lifecycle(team);
    expect(stages.map((s) => s.id)).toEqual(["request", "plan", "goahead", "execute", "result"]);
    expect(stages.find((s) => s.id === "goahead")!.state).toBe("current");
    const explain = lifecycle(run({ mode: "explain", status: "done", tasks: [], events: [] }));
    expect(explain.map((s) => s.id)).not.toContain("execute");
    const done = lifecycle(run({ status: "done", verified: true, mode: "change", running: [],
      tasks: run().tasks.map((t) => ({ ...t, status: "succeeded" as const })),
      final_tests: [{ command: "pytest", status: "passed", passed: 12, failed: 0, errors: 0, skipped: 0, seconds: 3, sandboxed: true, detail: "" }],
      events: [{ time: "", kind: "planned", text: "" }, { time: "", kind: "tested", text: "" }] }));
    expect(done.find((s) => s.id === "test")).toMatchObject({ state: "done", note: "12 passed, 0 failed" });
    expect(done.find((s) => s.id === "apply")).toMatchObject({ state: "pending", note: "not yet: it asks you first" });
    const unverified = lifecycle(run({ status: "done", verified: false, running: [], events: [{ time: "", kind: "verifying", text: "" }],
      final_tests: [{ command: "(none)", status: "not_run", passed: 0, failed: 0, errors: 0, skipped: 0, seconds: 0, sandboxed: false, detail: "" }] }));
    expect(unverified.find((s) => s.id === "test")).toMatchObject({ state: "skipped", note: "no tests ran" });  // never a green check
    expect(unverified.find((s) => s.id === "result")).toMatchObject({ state: "warning", note: "not verified" });
  });

  it("inspects a task, its reviews and its changes", async () => {
    const api = new FakeApi();
    const r = run({ status: "done", running: [], diff_stat: " game.js | 2 +-", reviews: { security: {
      summary: "Mostly fine.", findings: [{ severity: "high", issue: "Score is never saved", file: "db.js", line: 3, fix: "Write it" }],
      acceptance: [{ criterion: "Scores persist", met: false, why: "stub" }] } } });
    api.runs.mockResolvedValue({ available: true, runs: [toSummary(r)] });
    api.run.mockResolvedValue(r);
    api.runDiff.mockResolvedValue({ run: r.id, files: [{ status: "M", path: "game.js" }], diff: "diff --git a/game.js b/game.js\n-old\n+new" });
    await renderApp(api, "orchestration");
    await screen.findByText("Score is never saved", undefined, { timeout: 4000 });  // a big page: role queries are slow in jsdom
    await userEvent.click(screen.getByRole("button", { name: "Details of Show the date" }));
    const detail = screen.getByRole("region", { name: "Task: Show the date" });
    expect(detail).toHaveTextContent("frontend");
    expect(screen.getByText("Score is never saved")).toBeInTheDocument();
    expect(screen.getByText("Not met")).toBeInTheDocument();
    expect(screen.getByText(/They are not in your files unless you apply them/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Show the changes" }));
    expect(await screen.findByLabelText("The changes, as a diff")).toHaveTextContent("+new");
  });

  it("asks the engineering team from Mission Control, plan first", async () => {
    const api = new FakeApi();
    api.runs.mockResolvedValue({ available: true, runs: [] });
    await renderApp(api, "orchestration");
    await userEvent.click(await screen.findByRole("button", { name: "Ask the team" }));
    await userEvent.type(screen.getByLabelText("What should the team do?"), "add a high-score table");
    await userEvent.type(screen.getByLabelText("Project (optional)"), "snake");
    await userEvent.selectOptions(screen.getByLabelText("Kind of work"), "plan");
    await userEvent.click(screen.getByRole("button", { name: "Plan it" }));
    expect(api.startRun).toHaveBeenCalledWith({ request: "add a high-score table", project: "snake", path: "", mode: "plan" });
    expect(await screen.findByText("Planning in the background")).toBeInTheDocument();
  });
});

describe("agents", () => {
  it("say how long they have worked, what happened last, and can be stopped", async () => {
    expect(elapsed(new Date(Date.now() - 4 * 60_000).toISOString())).toBe("for 4 min");
    expect(elapsed(null)).toBeNull();
    const api = new FakeApi();
    api.agents.mockResolvedValue({ available: true, agents: [agent({ id: "backend", name: "Backend engineer", state: "working",
      current: [{ run: "run-1", task: "backend", title: "Store the date", status: "running", failure: null,
        since: new Date(Date.now() - 125_000).toISOString(), model: "free-model", latest: { text: "Tester finished", at: "2026-10-24T10:00:00" } }] })] });
    await renderApp(api, "agents");
    const row = await screen.findByRole("button", { name: /Backend engineer/ });
    expect(row).toHaveTextContent("for 2 min");
    await userEvent.click(row);
    expect(screen.getByText(/Latest: Tester finished/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Stop this run…" }));
    await userEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Stop the run" }));
    expect(api.runAction).toHaveBeenCalledWith("run-1", "cancel");
  });
});

describe("skills", () => {
  it("switch off only after a confirmation, never the essential one", async () => {
    const api = new FakeApi();
    api.tools.mockResolvedValue({ tools: [], health: {}, skills: [
      { name: "apple-notes", category: "apple", description: "Notes", enabled: true, available: false, availability: "needs memo (not installed)", version: "1.0.1" },
      { name: "hermes-agent", category: "agents", description: "Hermes", enabled: true, essential: true, available: true },
    ] });
    await renderApp(api, "tools");
    expect(await screen.findByText("needs memo (not installed)")).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "hermes-agent: on" })).toBeDisabled();
    await userEvent.click(screen.getByRole("switch", { name: "apple-notes: on" }));
    await userEvent.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Switch off" }));
    expect(api.setSkill).toHaveBeenCalledWith("apple-notes", false);
    await userEvent.click(screen.getByRole("radio", { name: /Need setup/ }));
    expect(screen.queryByText("Hermes")).toBeNull();
  });
});

describe("home", () => {
  it("puts what needs the user first, and where the work stands", async () => {
    const api = new FakeApi();
    api.snap = snapshot({ approvals: [approval({ id: "q9", description: "push the branch" })] });
    api.runs.mockResolvedValue({ available: true, runs: [toSummary(run({ id: "run-9", status: "failed", request: "Fix the login", updated: new Date().toISOString().slice(0, 19) }))] });
    const brief: ContextBrief = { focus: { kind: "run", name: "run-9", why: "x" }, ambiguous: false, candidates: [], project: null,
      work: [{ run: "run-9", request: "Fix the login", project: "crm", folder: "/x", status: "failed", updated: "2026-10-24T10:00:00",
        verified: false, progress: "1 of 2 tasks done", tests: null, outcome: null, applied: false, pushed: false,
        next: "it failed: explain why (below) and offer a fix or a new run" }],
      tasks: [], fix_next: [{ what: "tests: 2 failed", source: "tests", run: "run-9" }], problems: [], not_included: [], ms: 2 };
    api.context.mockResolvedValue(brief);
    await renderApp(api, "home");
    const attention = await screen.findByRole("region", { name: "Needs your attention" });
    expect(attention).toHaveTextContent("1 question waiting for your answer");
    await waitFor(() => expect(attention).toHaveTextContent("Engineering run failed: Fix the login"));
    expect(await screen.findByText("Next: it failed: explain why (below) and offer a fix or a new run")).toBeInTheDocument();
    expect(screen.getByText("1 thing left to fix, from the tests and reviews.")).toBeInTheDocument();
  });

  it("asks which work to continue when several fit", async () => {
    const api = new FakeApi();
    api.context.mockResolvedValue({ focus: null, ambiguous: true, candidates: ["crm", "snake"], project: null, work: [], tasks: [],
      fix_next: [], problems: [], not_included: [], ms: 1 });
    await renderApp(api, "home");
    expect(await screen.findByText("Recent work on crm, snake. Tell XENIO which one to continue.")).toBeInTheDocument();
  });
});

describe("mission control and the orb are one product", () => {
  it("says when live updates stop instead of showing old state as current", async () => {
    const api = new FakeApi();
    await renderApp(api, "tasks");
    expect(screen.queryByText(/Live updates paused/)).toBeNull();
    act(() => api.status.forEach((s) => s("retrying")));
    expect(await screen.findByText(/Live updates paused: reconnecting to XENIO/)).toBeInTheDocument();
    act(() => api.status.forEach((s) => s("open")));
    await waitFor(() => expect(screen.queryByText(/Live updates paused/)).toBeNull());
  });

  it("opens the floating orb's quick chat from Mission Control", async () => {
    const api = new FakeApi();
    api.snap = snapshot({ overlay: { supported: true, running: true, pid: 1, problem: null, status: {} } as never });
    await renderApp(api, "tasks");
    await userEvent.click(await screen.findByRole("button", { name: "Quick chat (the floating orb's chat)" }));
    expect(api.summonOrb).toHaveBeenCalledWith("chat");
  });

  it("keeps settings events and new topics apart", () => {
    const store = new Store();
    store.apply(ev("x1", "settings.changed", { motion: "full", text_size: "large", changed: ["text_size"] }));
    expect(store.get().settings).toEqual({ motion: "full", text_size: "large" });
    const before = store.get().revision.memory;
    store.apply(ev("x2", "memory.changed", { store: "user", action: "deleted" }));
    expect(store.get().revision.memory).toBe(before + 1);
  });

  it("re-reads what a turn may have changed inside Hermes (tasks, reminders, memory)", async () => {
    const api = new FakeApi();
    api.tasks.mockResolvedValue({ tasks: [], counts: {} });
    await renderApp(api, "tasks");
    await waitFor(() => expect(api.tasks).toHaveBeenCalled());
    const before = api.tasks.mock.calls.length;
    api.tasks.mockResolvedValue({ tasks: [{ id: 2, title: "check the V3 quick chat", status: "pending", priority: "normal", source: "user",
      created: "", updated: "" }], counts: { open: 1 } });
    act(() => api.emit("chat.done", { id: "t9-a", turn: "t9", role: "assistant", text: "Added to your tasks", ok: true, actions: [], sources: [], at: "" }));
    await waitFor(() => expect(api.tasks.mock.calls.length).toBeGreaterThan(before));
    expect(await screen.findByText("check the V3 quick chat")).toBeInTheDocument();
  });

  it("applies the text size and saves the activity retention", async () => {
    const api = new FakeApi();
    await renderApp(api, "settings");
    await userEvent.selectOptions(await screen.findByLabelText("Keep the activity history for"), "30");
    expect(api.saveSettings).toHaveBeenCalledWith({ activity_retention_days: 30 });
    await userEvent.click(screen.getByRole("radio", { name: "Larger text across Mission Control" }));
    expect(api.saveSettings).toHaveBeenLastCalledWith({ text_size: "large" });
    await waitFor(() => expect(document.documentElement.dataset.textSize).toBe("large"));
  });
});
