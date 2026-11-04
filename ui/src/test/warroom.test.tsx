import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { FoundryMember, FoundryNode, FoundryOverview, FoundryState } from "../api/types";
import { describe as describeEvent, logoFor, placeNodes, roundsOf } from "../pages/WarRoom";
import { FakeApi, renderApp } from "./fakes";

const perf = { runs: 0, succeeded: 0, failed: 0, workflows: 0, average_score: null, seconds: 0, last: "" };
const member = (over: Partial<FoundryMember> = {}): FoundryMember => ({ agent: "backend", name: "Backend engineer", role: "backend", reused: true, why: "the API", work: "build", kind: "permanent", source: "builtin", model: "", state: "idle", task: "", since: "", tools_used: 0, permissions: { tools: [], terminal: "write", writes: true, writes_code: true }, skills: [], performance: perf, ...over });
const node = (over: Partial<FoundryNode> = {}): FoundryNode => ({ id: "task-api", type: "task", title: "Reset endpoint", status: "pending", agent: "backend", deps: [], inputs: [], outputs: {}, error: "", started: "", finished: "", priority: 0, writes: true, detail: "", payload: { task: "api" }, budget: {}, retries: 0, max_retries: 0, history: [], ...over });
const AT = "2026-11-04T10:00:00";

function state(over: Partial<FoundryState["workflow"]> = {}, more: Partial<FoundryState> = {}): FoundryState {
  return { workflow: { id: "w1", goal: "Build a gym app", status: "running", created: AT, updated: AT, outcome: "", attention: "", milestone: 1, autonomy: "supervisor", project: "/x", delivered: "", summary: "", paused: false, pause_reason: "", constraints: [], require_evidence: false, run_id: "r1", ...over },
    team: [member(), member({ agent: "reviewer", name: "Code reviewer", state: "working", task: "Reviewing", model: "glm-5-2", permissions: { tools: [], terminal: "read", writes: false, writes_code: false } })], graph: [node()], counts: {}, messages: [], decisions: [], interventions: [], loop: [], events: [], live: true, seq: 1, ...more };
}

const message = (over = {}) => ({ id: "m1", seq: 1, workflow: "w1", sender: "backend", recipient: "team", type: "PROPOSAL", content: "Use PostgreSQL", task: "", correlation: "c", reply_to: "", evidence: [], artifacts: [], model: "glm-5-2", at: AT, ...over });
const overview = (over: Partial<FoundryOverview> = {}): FoundryOverview => ({ available: true, can_think: true, workflows: [], agents: [], skills: [], seats: ["glm-5-2"], ...over });

async function workflow(s: FoundryState, api = new FakeApi()) {
  api.foundryState = vi.fn(async () => s);
  await renderApp(api, "warroom/w1");
  await screen.findByText(s.workflow.goal);
  return api;
}

describe("the command", () => {
  it("one sentence starts the team, as a supervisor unless you ask otherwise", async () => {
    const api = new FakeApi();
    await renderApp(api, "warroom");
    fireEvent.change(await screen.findByLabelText("What should the team build?"), { target: { value: "Build a gym app" } });
    fireEvent.click(screen.getByRole("button", { name: "Start" }));
    await waitFor(() => expect(api.foundryDo).toHaveBeenCalledWith("start", expect.objectContaining({ goal: "Build a gym app", autonomy: "supervisor" })));
    await waitFor(() => expect(window.location.hash).toContain("warroom/w1"));
  });

  it("you can still ask to be asked", async () => {
    const api = new FakeApi();
    await renderApp(api, "warroom");
    fireEvent.change(await screen.findByLabelText("What should the team build?"), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Options" }));
    fireEvent.click(screen.getByLabelText(/Ask me before building/));
    fireEvent.click(screen.getByRole("button", { name: "Start" }));
    await waitFor(() => expect(api.foundryDo).toHaveBeenCalledWith("start", expect.objectContaining({ autonomy: "careful" })));
  });

  it("an empty command cannot be sent", async () => {
    await renderApp(new FakeApi(), "warroom");
    expect(await screen.findByRole("button", { name: "Start" })).toBeDisabled();
  });

  it("says plainly when the team cannot think", async () => {
    const api = new FakeApi();
    api.foundry = vi.fn(async () => overview({ can_think: false }));
    await renderApp(api, "warroom");
    expect(await screen.findByText(/needs a Nabh key/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start" })).toBeDisabled();
  });

  it("shows when it is switched off, with no fake page", async () => {
    const api = new FakeApi();
    api.foundry = vi.fn(async () => overview({ available: false, reason: "the engineering team is switched off" }));
    await renderApp(api, "warroom");
    expect(await screen.findByText("the engineering team is switched off")).toBeInTheDocument();
    expect(screen.queryByLabelText("What should the team build?")).toBeNull();
  });

  it("a server error is shown, not swallowed", async () => {
    const api = new FakeApi();
    api.foundryDo = vi.fn(async () => { throw new Error("the team needs a key"); });
    await renderApp(api, "warroom");
    fireEvent.change(await screen.findByLabelText("What should the team build?"), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Start" }));
    expect(await screen.findByText("the team needs a key")).toBeInTheDocument();
  });
});

describe("stopping everything", () => {
  const line = { id: "w1", goal: "A running thing", status: "running", created: AT, updated: AT, outcome: "", attention: "", milestone: 1, autonomy: "supervisor", project: "", delivered: "", summary: "", paused: false, pause_reason: "" };

  it("is one button away while anything runs, and asks once", async () => {
    const api = new FakeApi();
    api.foundry = vi.fn(async () => overview({ workflows: [line] }));
    await renderApp(api, "warroom");
    fireEvent.click(await screen.findByRole("button", { name: "Stop everything" }));
    expect(api.foundryDo).not.toHaveBeenCalled();
    const dialog = await screen.findByRole("alertdialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Stop everything" }));
    await waitFor(() => expect(api.foundryDo).toHaveBeenCalledWith("stop_all", {}));
  });

  it("is not there when nothing runs", async () => {
    const api = new FakeApi();
    api.foundry = vi.fn(async () => overview({ workflows: [{ ...line, status: "done" }] }));
    await renderApp(api, "warroom");
    await screen.findByText("A running thing");
    expect(screen.queryByRole("button", { name: "Stop everything" })).toBeNull();
  });
});

describe("a running workflow", () => {
  it("shows who is doing what, from the real state", async () => {
    await workflow(state());
    expect(screen.getByText("Working on Reviewing")).toBeInTheDocument();
    expect(screen.getByText(/glm-5-2/)).toBeInTheDocument();
    expect(screen.getByText(/read-only/)).toBeInTheDocument();
  });

  it("shows the conversation with what each message is", async () => {
    await workflow(state({}, { messages: [message(), message({ id: "m2", seq: 2, sender: "reviewer", type: "CHALLENGE", content: "That will not scale", recipient: "backend", evidence: ["db.py:3"] })] }));
    expect(screen.getByText("Use PostgreSQL")).toBeInTheDocument();
    expect(screen.getByText("proposes")).toBeInTheDocument();
    expect(screen.getByText("challenges")).toBeInTheDocument();
    expect(screen.getByText("db.py:3")).toBeInTheDocument();
  });

  it("filters the conversation by person and by kind", async () => {
    const many = Array.from({ length: 8 }, (_, i) => message({ id: `m${i}`, seq: i, content: `from backend ${i}` }));
    await workflow(state({}, { messages: [...many, message({ id: "x", sender: "reviewer", type: "REVIEW", content: "the review" })] }));
    fireEvent.change(screen.getByLabelText("Who"), { target: { value: "reviewer" } });
    expect(screen.getByText("the review")).toBeInTheDocument();
    expect(screen.queryByText("from backend 1")).toBeNull();
    fireEvent.change(screen.getByLabelText("Kind"), { target: { value: "PROPOSAL" } });
    expect(screen.getByText("Nothing matches those filters.")).toBeInTheDocument();
  });

  it("what you say goes to the team, or to one agent", async () => {
    const api = await workflow(state());
    fireEvent.change(screen.getByLabelText("Your message"), { target: { value: "Stop. Use PostgreSQL." } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(api.foundryDo).toHaveBeenCalledWith("message", { workflow: "w1", text: "Stop. Use PostgreSQL.", to: "team" }));
    fireEvent.change(screen.getByLabelText("Say it to"), { target: { value: "reviewer" } });
    fireEvent.change(screen.getByLabelText("Your message"), { target: { value: "Why?" } });
    fireEvent.keyDown(screen.getByLabelText("Your message"), { key: "Enter" });
    await waitFor(() => expect(api.foundryDo).toHaveBeenCalledWith("message", { workflow: "w1", text: "Why?", to: "reviewer" }));
  });

  it("pause is real, and becomes resume when paused", async () => {
    const api = await workflow(state());
    fireEvent.click(screen.getByRole("button", { name: /Pause/ }));
    await waitFor(() => expect(api.foundryDo).toHaveBeenCalledWith("pause", { workflow: "w1" }));
    await act(async () => { api.foundryState = vi.fn(async () => state({ paused: true, status: "paused" })); });
  });

  it("resume when paused", async () => {
    const api = await workflow(state({ paused: true, status: "paused" }));
    fireEvent.click(screen.getByRole("button", { name: /Resume/ }));
    await waitFor(() => expect(api.foundryDo).toHaveBeenCalledWith("resume", { workflow: "w1" }));
  });

  it("stop asks once, then stops", async () => {
    const api = await workflow(state());
    fireEvent.click(screen.getByRole("button", { name: /^Stop$/ }));
    expect(api.foundryDo).not.toHaveBeenCalled();
    fireEvent.click(await screen.findByRole("button", { name: "Stop it" }));
    await waitFor(() => expect(api.foundryDo).toHaveBeenCalledWith("stop", { workflow: "w1" }));
  });

  it("what needs you is at the top with the real buttons", async () => {
    const api = await workflow(state({ status: "waiting", attention: "Start building this plan?" }, { graph: [node({ id: "go-ahead", type: "approval", title: "Your go-ahead", status: "waiting" })] }));
    expect(screen.getByText("Start building this plan?")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(api.foundryDo).toHaveBeenCalledWith("approve", { workflow: "w1", node: "go-ahead" }));
  });

  it("running out of budget offers more", async () => {
    const api = await workflow(state({ status: "paused", paused: true, pause_reason: "out of budget: 200 calls", attention: "The budget is used up." }));
    fireEvent.click(screen.getByRole("button", { name: "Add more budget" }));
    await waitFor(() => expect(api.foundryDo).toHaveBeenCalledWith("extend_budget", { workflow: "w1" }));
  });

  it("out of credit says what to do, and Resume is what does it", async () => {
    const api = await workflow(state({ status: "paused", paused: true, pause_reason: "out of credit: too low", attention: "The account paying for the models is out of credit. Nothing is lost." }));
    expect(screen.getByText(/out of credit. Nothing is lost/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add more budget" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Resume/ }));
    await waitFor(() => expect(api.foundryDo).toHaveBeenCalledWith("resume", { workflow: "w1" }));
  });

  it("shows what the work has cost so far, as an estimate", async () => {
    await workflow(state({ budget: { calls: 12, max_calls: 800, tokens_estimated: 1_400_000, max_tokens: 6_000_000, seconds: 600, max_seconds: 28800 } }));
    expect(screen.getByText("1.4M of 6.0M tokens")).toBeInTheDocument();
    expect(screen.getByText(/12 model calls · 10 min/)).toBeInTheDocument();
  });

  it("a requirement you add goes to the team and can be removed", async () => {
    const api = await workflow(state({ constraints: ["Use PostgreSQL"] }));
    fireEvent.change(screen.getByLabelText("Add a requirement"), { target: { value: "No Docker" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() => expect(api.foundryDo).toHaveBeenCalledWith("constraints", { workflow: "w1", constraints: ["Use PostgreSQL", "No Docker"] }));
    fireEvent.click(screen.getByRole("button", { name: "Remove Use PostgreSQL" }));
    await waitFor(() => expect(api.foundryDo).toHaveBeenCalledWith("constraints", { workflow: "w1", constraints: [] }));
  });

  it("you can require evidence", async () => {
    const api = await workflow(state());
    fireEvent.click(screen.getByLabelText(/need evidence/));
    await waitFor(() => expect(api.foundryDo).toHaveBeenCalledWith("require_evidence", { workflow: "w1", required: true }));
  });

  it("you can ask someone for a review", async () => {
    const api = await workflow(state());
    fireEvent.click(screen.getByRole("button", { name: "Ask Code reviewer for a review" }));
    fireEvent.change(screen.getByLabelText(/What should Code reviewer review/), { target: { value: "the schema" } });
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));
    await waitFor(() => expect(api.foundryDo).toHaveBeenCalledWith("review", { workflow: "w1", agent: "reviewer", text: "the schema" }));
  });

  it("what you did, and what it did, stays on screen", async () => {
    await workflow(state({}, { interventions: [{ id: "i", kind: "message", text: "Use PostgreSQL", to: "team", effect: ["reopened the decision"], at: AT }] }));
    expect(screen.getByText("reopened the decision")).toBeInTheDocument();
  });

  it("a stopped workflow cannot be paused, but you can still tell it what to do next", async () => {
    await workflow(state({ status: "stopped", outcome: "Stopped: you stopped it." }));
    expect(screen.getByText("Stopped: you stopped it.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Pause/ })).toBeNull();
    expect(screen.getByLabelText("Your message")).toBeInTheDocument();
  });

  it("shows what it has spent and which models carried it", async () => {
    await workflow(state({ budget: { calls: 9, max_calls: 800, tokens_estimated: 120_000, max_tokens: 1_500_000, seconds: 60, max_seconds: 28800 }, model_use: { "glm-5-2": 80_000, "kimi-k2-7-code": 40_000 } }));
    expect(screen.getByText("120k of 1.5M tokens")).toBeInTheDocument();
    expect(screen.getByText("glm-5-2 80k")).toBeInTheDocument();
    expect(screen.getByText("kimi-k2-7-code 40k")).toBeInTheDocument();
  });

  it("approvals come to you right here", async () => {
    const api = new FakeApi();
    api.snap = { ...api.snap, approvals: [{ id: "a1", action: "x.y", description: "run something risky", risk: "CONFIRM_REQUIRED", strict: false, source: "xenio", requested_at: AT, expires_at: new Date(Date.now() + 60_000).toISOString(), status: "pending" }] } as never;
    await workflow(state(), api);
    const here = await screen.findByLabelText("Waiting for your yes");
    expect(within(here).getByText("XENIO wants to run something risky.")).toBeInTheDocument();
    expect(within(here).getByRole("button", { name: /Approve|Allow|Yes/i })).toBeInTheDocument();
  });

  it("no approvals, nothing in the way", async () => {
    await workflow(state());
    expect(screen.queryByLabelText("Waiting for your yes")).toBeNull();
  });
});

describe("every agent has a little logo", () => {
  it.each([["backend", "Backend engineer", "⚙️"], ["frontend", "Frontend engineer", "🎨"], ["tester", "Test engineer", "🧪"], ["security", "Security reviewer", "🛡️"], ["reviewer", "Code reviewer", "🔍"], ["docs", "Documentation engineer", "📚"], ["planner", "Planner", "🧭"], ["db", "Database Architect", "🗄️"], ["pay", "Payments Integration Engineer", "💳"], ["ops", "DevOps Engineer", "🚀"], ["rec", "Recommendation Engineer", "🧠"], ["x", "Something Else", "🤖"]])("%s", (id, name, logo) => {
    expect(logoFor(id, name)).toBe(logo);
  });
});

describe("what it built, running", () => {
  it("shows it right there, with its address", async () => {
    await workflow(state({ status: "done", outcome: "Verified." }, { preview: { state: "running", url: "http://127.0.0.1:5555/" } }));
    expect(screen.getByText("It’s running")).toBeInTheDocument();
    const frame = screen.getByTitle("The app the team built");
    expect(frame).toHaveAttribute("src", "http://127.0.0.1:5555/");
    expect(frame.getAttribute("sandbox")).toContain("allow-scripts");
    expect(frame.getAttribute("sandbox")).not.toContain("allow-top-navigation");
    expect(screen.getByRole("link", { name: /127\.0\.0\.1:5555/ })).toHaveAttribute("target", "_blank");
  });

  it("you can stop it and start it again", async () => {
    const api = await workflow(state({ status: "done", outcome: "x" }, { preview: { state: "running", url: "http://127.0.0.1:5555/" } }));
    fireEvent.click(screen.getByRole("button", { name: "Stop it" }));
    await waitFor(() => expect(api.foundryDo).toHaveBeenCalledWith("preview_stop", { workflow: "w1" }));
    fireEvent.click(screen.getByRole("button", { name: "Start it again" }));
    await waitFor(() => expect(api.foundryDo).toHaveBeenCalledWith("preview_start", { workflow: "w1" }));
  });

  it("when it is not running, one button runs it", async () => {
    const api = await workflow(state({ status: "done", outcome: "x" }, { preview: { state: "stopped" } }));
    fireEvent.click(screen.getByRole("button", { name: /Run it/ }));
    await waitFor(() => expect(api.foundryDo).toHaveBeenCalledWith("preview_start", { workflow: "w1" }));
  });

  it("when it would not start, it says why next to the button, and the team can be asked to fix it", async () => {
    const api = await workflow(state({ status: "done", outcome: "x" }, { preview: { state: "failed", detail: "it stopped as soon as it started", log: "Traceback: boom" } }));
    expect(screen.getByText(/It would not start: it stopped as soon as it started/)).toBeInTheDocument();
    expect(screen.getByText("Traceback: boom")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(api.foundryDo).toHaveBeenCalledWith("preview_start", { workflow: "w1" }));
    fireEvent.click(screen.getByRole("button", { name: "Ask the team to fix it" }));
    await waitFor(() => expect(api.foundryDo).toHaveBeenCalledWith("change", { workflow: "w1", text: expect.stringContaining("Fix it so it starts") }));
  });

  it("finished work opens on what it made, and the message box stays in every tab", async () => {
    await workflow(state({ status: "done", outcome: "All verified." }));
    expect(screen.getByRole("tab", { name: "Result" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("All verified.")).toBeInTheDocument();
    for (const name of [/Conversation/, /Decisions/, /Graph/, /Timeline/]) {
      fireEvent.click(screen.getByRole("tab", { name }));
      expect(screen.getByLabelText("Your message")).toBeInTheDocument();
    }
  });

  it("work still in progress opens on the conversation, with no result tab", async () => {
    await workflow(state());
    expect(screen.getByRole("tab", { name: /Conversation/ })).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByRole("tab", { name: /Result|Running/ })).toBeNull();
  });

  it("what you say after trying it becomes a change for the team", async () => {
    const api = await workflow(state({ status: "done", outcome: "x", delivered: "/p" }));
    expect(screen.getByPlaceholderText(/Tell the team what to change/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Say it to")).toBeNull();
    fireEvent.change(screen.getByLabelText("Your message"), { target: { value: "Make the button blue" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(api.foundryDo).toHaveBeenCalledWith("change", { workflow: "w1", text: "Make the button blue" }));
  });
});

describe("deleting work", () => {
  const line = (over = {}) => ({ id: "w1", goal: "An old draft", status: "stopped", created: AT, updated: AT, outcome: "", attention: "", milestone: 1, autonomy: "supervisor", project: "", delivered: "", summary: "", paused: false, pause_reason: "", ...over });

  it("from the list, asking first", async () => {
    const api = new FakeApi();
    api.foundry = vi.fn(async () => overview({ workflows: [line()] }));
    await renderApp(api, "warroom");
    fireEvent.click(await screen.findByRole("button", { name: "Delete: An old draft" }));
    expect(api.foundryDo).not.toHaveBeenCalled();
    fireEvent.click(await screen.findByRole("button", { name: "Delete it" }));
    await waitFor(() => expect(api.foundryDo).toHaveBeenCalledWith("delete", { workflow: "w1" }));
  });

  it("not while it is running", async () => {
    const api = new FakeApi();
    api.foundry = vi.fn(async () => overview({ workflows: [line({ status: "running" })] }));
    await renderApp(api, "warroom");
    await screen.findByText("An old draft");
    expect(screen.queryByRole("button", { name: "Delete: An old draft" })).toBeNull();
  });

  it("from the work itself", async () => {
    const api = await workflow(state({ status: "failed", outcome: "x" }));
    fireEvent.click(screen.getByRole("button", { name: /Delete this work/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Delete it" }));
    await waitFor(() => expect(api.foundryDo).toHaveBeenCalledWith("delete", { workflow: "w1" }));
  });
});

describe("the graph stays readable", () => {
  it("test and fix rounds are shown as rounds, not more boxes", async () => {
    const graph = [node({ id: "loop", type: "loop", title: "Test, review, fix", agent: "" }), node({ id: "test-1", type: "testing", title: "XENIO runs the tests", agent: "", status: "done", payload: { managed: true }, outputs: { tests: [{ status: "passed", passed: 12, failed: 1 }] } }), node({ id: "review-1", type: "review", title: "Independent review", agent: "reviewer", status: "done", payload: { managed: true }, outputs: { issues: ["one"] } }), node({ id: "task-fix-1-backend", type: "task", title: "Fix what the checks found", agent: "backend", status: "done", payload: { managed: true } })];
    await workflow(state({}, { graph }));
    fireEvent.click(screen.getByRole("tab", { name: /Graph/ }));
    expect(screen.queryByRole("button", { name: /XENIO runs the tests/ })).toBeNull();
    const rounds = screen.getByLabelText("Rounds of testing and fixing");
    expect(within(rounds).getByText(/12 passed, 1 failed/)).toBeInTheDocument();
    expect(within(rounds).getByText(/1 to fix/)).toBeInTheDocument();
    expect(within(rounds).getByText(/Backend engineer: done/)).toBeInTheDocument();
    expect(roundsOf(graph)).toHaveLength(1);
  });
});

describe("the result", () => {
  it("a project XENIO made says where it is", async () => {
    await workflow(state({ status: "done", outcome: "Verified.", delivered: "/Users/you/Xenio/projects/gym" }));
    expect(screen.getByText("/Users/you/Xenio/projects/gym")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Apply to my files" })).toBeNull();
  });

  it("work on your own project stays apart until you apply it, through the existing confirmations", async () => {
    const api = new FakeApi();
    api.runAction = vi.fn(async () => ({}));
    await workflow(state({ status: "done", outcome: "Verified." }), api);
    fireEvent.click(screen.getByRole("button", { name: "Apply to my files" }));
    await waitFor(() => expect(api.runAction).toHaveBeenCalledWith("r1", "apply"));
  });
});

const decision = (over = {}) => ({ id: "d1", workflow: "w1", question: "PostgreSQL or MongoDB?", status: "decided", participants: [], human: [], constraints: [], rounds: [{ number: 1, responses: [{ from: "reviewer", name: "Code reviewer", to: "backend", stance: "challenge", point: "scale", severity: "high", evidence: [] }] }],
  options: [{ agent: "backend", name: "Backend engineer", model: "glm-5-2", position: "PostgreSQL", reasoning: "r", evidence: [{ text: "db.py:1", checked: true }, { text: "it is popular", checked: false }], tradeoffs: [], risks: ["ops"], validation: [], confidence: 0.8, history: ["Mongo", "PostgreSQL"], why_changed: "the challenge", label: "A" }, { agent: "reviewer", name: "Code reviewer", model: "kimi", position: "MongoDB", reasoning: "r", evidence: [], tradeoffs: [], risks: [], validation: [], confidence: 0.5, history: ["MongoDB"], label: "B" }],
  selected: "A", decision: "Use PostgreSQL", reasons: ["relational data"], confidence: 0.9, decided_by: "team", dissent: [{ agent: "reviewer", name: "Code reviewer", position: "MongoDB", why: "r", why_not_chosen: "weak evidence" }], ...over });

describe("the decision room", () => {
  const open = () => fireEvent.click(screen.getByRole("tab", { name: /Decisions/ }));

  it("shows the options, what was checked, who changed their mind, and who still disagrees", async () => {
    await workflow(state({}, { decisions: [decision()] as never }));
    open();
    expect(screen.getByText("Use PostgreSQL")).toBeInTheDocument();
    expect(screen.getByText("checked")).toBeInTheDocument();
    expect(screen.getByText("claimed")).toBeInTheDocument();
    expect(screen.getByText(/Changed position: the challenge/)).toBeInTheDocument();
    expect(screen.getByText(/Dissent, kept on record/)).toBeInTheDocument();
    expect(screen.getByText(/weak evidence/)).toBeInTheDocument();
    expect(screen.getByText("1 challenge raised")).toBeInTheDocument();
  });

  it("flags a judgment call", async () => {
    await workflow(state({}, { decisions: [decision({ provisional: true, why_provisional: ["confidence is only 40%"] })] as never }));
    open();
    expect(screen.getByText(/A judgment call/)).toBeInTheDocument();
  });

  it("when it is yours, you can choose an option or say what you want", async () => {
    const api = await workflow(state({ status: "waiting", attention: "Which one?" }, { decisions: [decision({ status: "needs_human", question_for_human: "Which one?", why_human: ["confidence is only 30%"], decision: "x" })] as never }));
    fireEvent.click(screen.getByRole("button", { name: "See the decision" }));
    fireEvent.click(screen.getAllByRole("button", { name: "Go with this" })[1]!);
    await waitFor(() => expect(api.foundryDo).toHaveBeenCalledWith("decide", { workflow: "w1", decision: "d1", choice: "B" }));
    fireEvent.change(screen.getByLabelText("Or say what you want"), { target: { value: "SQLite" } });
    fireEvent.click(screen.getByRole("button", { name: "Decide" }));
    await waitFor(() => expect(api.foundryDo).toHaveBeenCalledWith("decide", { workflow: "w1", decision: "d1", text: "SQLite" }));
  });

  it("while it is open, you can add your view", async () => {
    const api = await workflow(state({}, { decisions: [decision({ status: "debating", decision: undefined, selected: undefined, dissent: [] })] as never }));
    open();
    const view = screen.getByLabelText("Add your view");
    fireEvent.change(view, { target: { value: "Keep it simple" } });
    fireEvent.click(within(view.closest("form")!).getByRole("button", { name: "Add" }));
    await waitFor(() => expect(api.foundryDo).toHaveBeenCalledWith("discuss", { workflow: "w1", decision: "d1", text: "Keep it simple" }));
  });

  it("says so when nothing has needed a debate", async () => {
    await workflow(state());
    open();
    expect(screen.getByText("No decision has needed a debate yet.")).toBeInTheDocument();
  });
});

describe("the graph", () => {
  const nodes = [node({ id: "goal", type: "goal", title: "Goal", status: "done", agent: "" }), node({ id: "a", title: "API", deps: ["goal"], status: "running" }), node({ id: "b", title: "UI", deps: ["goal"] }), node({ id: "c", title: "Tests", deps: ["a", "b"], outputs: { summary: "wrote tests" }, error: "boom", status: "failed" })];
  const open = () => fireEvent.click(screen.getByRole("tab", { name: /Graph/ }));

  it("places each step after what it waits for, and a cycle cannot hang it", () => {
    const { placed } = placeNodes(nodes);
    const x = Object.fromEntries(placed.map((p) => [p.node.id, p.x]));
    expect(x.goal!).toBeLessThan(x.a!);
    expect(x.a).toBe(x.b);
    expect(x.c!).toBeGreaterThan(x.a!);
    expect(placeNodes([node({ id: "p", deps: ["q"] }), node({ id: "q", deps: ["p"] })]).placed).toHaveLength(2);
  });

  it("clicking a step shows its agent, what it produced and its error", async () => {
    await workflow(state({}, { graph: nodes }));
    open();
    fireEvent.click(screen.getByRole("button", { name: /Tests/ }));
    const detail = screen.getByLabelText("Details of Tests");
    expect(within(detail).getByText("boom")).toBeInTheDocument();
    expect(within(detail).getByText(/wrote tests/)).toBeInTheDocument();
    expect(within(detail).getByText("Backend engineer")).toBeInTheDocument();
  });

  it("a step not started can be given to someone able to do it, and only them", async () => {
    const api = await workflow(state({}, { graph: nodes }));
    open();
    fireEvent.click(screen.getByRole("button", { name: /UI/ }));
    const select = screen.getByLabelText("Give it to") as HTMLSelectElement;
    expect([...select.options].map((o) => o.value)).toEqual(["backend"]);
    fireEvent.click(screen.getByRole("button", { name: "Do this sooner" }));
    await waitFor(() => expect(api.foundryDo).toHaveBeenCalledWith("priority", { workflow: "w1", node: "b", priority: 1 }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel this step" }));
    await waitFor(() => expect(api.foundryDo).toHaveBeenCalledWith("cancel_task", { workflow: "w1", node: "b" }));
  });

  it("says so before there is anything to show", async () => {
    await workflow(state({}, { graph: [] }));
    open();
    expect(screen.getByText(/graph appears as soon/)).toBeInTheDocument();
  });
});

describe("staying where you are", () => {
  it("new messages move the feed, never the page: the goal and its controls stay on screen", async () => {
    const spy = vi.fn();
    const original = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = spy;
    try {
      await workflow(state({}, { messages: [message()] }));
      expect(spy).not.toHaveBeenCalled();
    } finally {
      Element.prototype.scrollIntoView = original;
    }
  });

  it("the window title reads like the menu", async () => {
    await renderApp(new FakeApi(), "warroom");
    await waitFor(() => expect(document.title).toBe("War Room · XENIO"));
  });

  it("a project XENIO made never offers to write into your files", async () => {
    await workflow(state({ status: "done", outcome: "Not accepted.", greenfield: true }));
    expect(screen.queryByRole("button", { name: "Apply to my files" })).toBeNull();
  });
});

describe("live updates", () => {
  it("an event re-reads the real state", async () => {
    const api = await workflow(state());
    const calls = api.foundryState.mock.calls.length;
    api.foundryState = vi.fn(async () => state({}, { messages: [message({ content: "fresh from the server" })] }));
    act(() => { api.emit("foundry.event", { seq: 2, type: "message", workflow: "w1" }); });
    expect(await screen.findByText("fresh from the server", {}, { timeout: 2500 })).toBeInTheDocument();
    expect(calls).toBeGreaterThan(0);
  });

  it("a burst of events does not mean a burst of requests", async () => {
    const api = await workflow(state());
    const spy = vi.fn(async () => state());
    api.foundryState = spy;
    act(() => { for (let i = 0; i < 40; i++) api.emit("foundry.event", { seq: i, type: "node", workflow: "w1" }); });
    await new Promise((r) => setTimeout(r, 900));
    expect(spy.mock.calls.length).toBeLessThanOrEqual(2);
  });
});

describe("the timeline", () => {
  it("reads as sentences, from real events", () => {
    const names = new Map([["backend", "Backend engineer"]]);
    const row = (type: string, data: Record<string, unknown>, agent = "") => ({ id: "e", seq: 1, type, workflow: "w", agent, task: "", at: AT, data });
    expect(describeEvent(row("agent.tool", { title: "$ pytest", status: "completed" }, "backend"), names)).toBe("Backend engineer: $ pytest");
    expect(describeEvent(row("agent.tool", { title: "write x", status: "failed" }, "backend"), names)).toContain("refused or failed");
    expect(describeEvent(row("loop.finished", { accepted: true, iterations: 2 }), names)).toBe("Accepted after 2 round(s)");
    expect(describeEvent(row("workflow", { status: "paused", reason: "you paused it" }), names)).toContain("you paused it");
    expect(describeEvent(row("something.new", {}), names)).toBe("something.new");
  });
});

const agent = (over = {}) => ({ id: "backend", name: "Backend engineer", role: "backend", description: "builds", instructions: "", expertise: [], responsibilities: [], provider: "", model: "", capabilities: [], skills: [], kind: "permanent", state: "enabled", source: "builtin", version: 1, permissions: { tools: [], terminal: "write", writes: true, writes_code: true }, performance: perf, ...over });

describe("agents", () => {
  async function agents(list: ReturnType<typeof agent>[]) {
    const api = new FakeApi();
    api.foundry = vi.fn(async () => overview({ agents: list as never }));
    await renderApp(api, "warroom");
    fireEvent.click(await screen.findByRole("tab", { name: /Agents/ }));
    return api;
  }

  it("shows what they may do and how they have done", async () => {
    await agents([agent({ performance: { ...perf, runs: 4, succeeded: 3, workflows: 2 } })]);
    expect(screen.getByText(/3 of 4 tasks succeeded, 2 project/)).toBeInTheDocument();
    expect(screen.getByText(/Can change files/)).toBeInTheDocument();
  });

  it("you can switch one off, and keep a temporary specialist", async () => {
    const api = await agents([agent({ id: "pay", name: "Payments", kind: "temporary", source: "factory" })]);
    fireEvent.click(screen.getByRole("button", { name: "Keep permanently" }));
    await waitFor(() => expect(api.foundryDo).toHaveBeenCalledWith("agent_promote", { agent: "pay" }));
    fireEvent.click(screen.getByRole("button", { name: "Switch off" }));
    await waitFor(() => expect(api.foundryDo).toHaveBeenCalledWith("agent_disable", { agent: "pay" }));
  });

  it("you can choose an agent's model", async () => {
    const api = await agents([agent()]);
    const input = screen.getByLabelText("Model for Backend engineer");
    fireEvent.change(input, { target: { value: "kimi-k2-7-code" } });
    fireEvent.blur(input);
    await waitFor(() => expect(api.foundryDo).toHaveBeenCalledWith("agent_update", expect.objectContaining({ agent: "backend", changes: { model: "kimi-k2-7-code" } })));
  });
});

const skill = (over = {}) => ({ id: "s1", name: "hello-tool", description: "says hello", kind: "tool", state: "awaiting_approval", risk: "medium", source_text: "npm hello-tool", permissions: ["talk to the internet (off, unless you allow it)"], agents: [], incoming: { version: "1.0.0", license: "MIT", homepage: "" }, assessment: { findings: [{ severity: "medium", kind: "uses the network", why: "it talks to the internet", file: "a.js", line: 3, excerpt: "fetch(x)" }], files: 2, dependencies: {}, bins: {}, install_scripts: {} }, ...over });

describe("skills", () => {
  async function skills(list: ReturnType<typeof skill>[], agents: ReturnType<typeof agent>[] = []) {
    const api = new FakeApi();
    api.foundry = vi.fn(async () => overview({ skills: list as never, agents: agents as never }));
    await renderApp(api, "warroom");
    fireEvent.click(await screen.findByRole("tab", { name: /Skills/ }));
    return api;
  }

  it("asking only inspects: nothing is installed yet", async () => {
    const api = await skills([]);
    fireEvent.change(screen.getByLabelText("A skill to install"), { target: { value: "npx cowsay" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() => expect(api.foundryDo).toHaveBeenCalledWith("skill_request", { request: "npx cowsay" }));
    expect(api.foundryDo).not.toHaveBeenCalledWith("skill_install", expect.anything());
  });

  it("before you say yes you see what it would do and what was found, and where", async () => {
    await skills([skill()]);
    expect(screen.getByText(/Medium risk/)).toBeInTheDocument();
    expect(screen.getByText("talk to the internet (off, unless you allow it)")).toBeInTheDocument();
    expect(screen.getByText("it talks to the internet")).toBeInTheDocument();
    expect(screen.getByText(/a.js:3/)).toBeInTheDocument();
    expect(screen.getByText(/Nothing from it has run/)).toBeInTheDocument();
  });

  it("the network is off unless you tick it", async () => {
    const api = await skills([skill()]);
    fireEvent.click(screen.getByRole("button", { name: /Add to the skills base/ }));
    await waitFor(() => expect(api.foundryDo).toHaveBeenCalledWith("skill_install", { skill: "s1", network: false }));
    fireEvent.click(screen.getByLabelText("Let it use the internet"));
    fireEvent.click(screen.getByRole("button", { name: /Add to the skills base/ }));
    await waitFor(() => expect(api.foundryDo).toHaveBeenCalledWith("skill_install", { skill: "s1", network: true }));
  });

  it("something that looks dangerous says why, and only installs if you say so, locked down", async () => {
    const api = await skills([skill({ state: "blocked", risk: "blocked", why_blocked: "This looks dangerous: it goes looking for your private keys (a.js:1)." })]);
    expect(screen.getByText(/goes looking for your private keys/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Add to the skills base/ })).toBeNull();
    expect(screen.queryByLabelText("Let it use the internet")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Install anyway, locked down" }));
    expect(api.foundryDo).not.toHaveBeenCalled();
    fireEvent.click(await screen.findByRole("button", { name: "Install it, locked down" }));
    await waitFor(() => expect(api.foundryDo).toHaveBeenCalledWith("skill_install", { skill: "s1", accept_risk: true }));
  });

  it("a repository of skills lets you choose, and leaves the dangerous one out by default", async () => {
    const contains = [{ folder: "skills/pdf", name: "pdf", description: "read PDFs", risk: "low", permissions: [], files: 2, findings: [], already: false }, { folder: "skills/design", name: "design", description: "good UI", risk: "low", permissions: [], files: 2, findings: [], already: false }, { folder: "skills/bad", name: "bad", description: "", risk: "blocked", permissions: [], files: 1, findings: [{ severity: "critical", kind: "x", why: "it steals keys", file: "x.js", line: 1, excerpt: "" }], already: false }, { folder: "skills/old", name: "old", description: "", risk: "low", permissions: [], files: 1, findings: [], already: true }];
    const api = await skills([skill({ kind: "collection", contains, name: "acme/skills" })]);
    expect(screen.getByText(/it steals keys/)).toBeInTheDocument();
    expect(screen.getByText("already here")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox", { name: /design/ }));
    fireEvent.click(screen.getByRole("button", { name: "Add 1 to the skills base" }));
    await waitFor(() => expect(api.foundryDo).toHaveBeenCalledWith("skill_install", { skill: "s1", picks: ["skills/pdf"], accept_risk: false }));
  });

  it("you can see and change which jobs a skill is for", async () => {
    const api = await skills([skill({ state: "enabled", incoming: undefined, version: "1", universal: true, for_work: ["ui"] })], [agent()]);
    expect(screen.getByRole("button", { name: "Frontend and design" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("button", { name: "Testing" }));
    await waitFor(() => expect(api.foundryDo).toHaveBeenCalledWith("skill_work", { skill: "s1", work: ["ui", "test"] }));
    fireEvent.click(screen.getByRole("button", { name: "Frontend and design" }));
    await waitFor(() => expect(api.foundryDo).toHaveBeenCalledWith("skill_work", { skill: "s1", work: [] }));
  });

  it("a skill says honestly whether agents have really used it", async () => {
    await skills([skill({ state: "enabled", incoming: undefined, version: "1", use: { used: 0, added: 0, by: [] } })], [agent()]);
    expect(screen.getByText("No agent has needed it yet.")).toBeInTheDocument();
  });

  it("and who has, when they have", async () => {
    await skills([skill({ state: "enabled", incoming: undefined, version: "1", use: { used: 3, added: 1, by: ["backend", "new-one"] } })], [agent({ id: "backend", name: "Backend engineer" })]);
    expect(screen.getByText("Agents have used it 3 times and added it to 1 project: Backend engineer, New one.")).toBeInTheDocument();
  });

  it("a skill in the base can be for every agent or only some", async () => {
    const api = await skills([skill({ state: "enabled", incoming: undefined, version: "1.0.0", universal: true })], [agent()]);
    const box = screen.getByLabelText(/Every agent whose job it suits/) as HTMLInputElement;
    expect(box.checked).toBe(true);
    expect(screen.queryByText(/Who has it/)).toBeNull();
    fireEvent.click(box);
    await waitFor(() => expect(api.foundryDo).toHaveBeenCalledWith("skill_everyone", { skill: "s1", everyone: false }));
  });

  it("one that failed its test does not pretend to be active", async () => {
    await skills([skill({ state: "failed", incoming: undefined, failure: "it would not run in the sandbox" })]);
    expect(screen.getByText(/It is not active: it would not run/)).toBeInTheDocument();
  });

  it("you choose who gets it, and can manage it after", async () => {
    const api = await skills([skill({ state: "enabled", incoming: undefined, version: "1.0.0", universal: false, versions: [{ version: "0.9", installed: AT }, { version: "1.0.0", installed: AT }] })], [agent()]);
    fireEvent.click(screen.getByLabelText("Backend engineer"));
    await waitFor(() => expect(api.foundryDo).toHaveBeenCalledWith("skill_assign", { skill: "s1", agents: ["backend"] }));
    for (const [name, command] of [["Switch off", "skill_disable"], ["Check for an update", "skill_update"], ["Roll back", "skill_rollback"], ["Uninstall", "skill_uninstall"]]) {
      fireEvent.click(screen.getByRole("button", { name }));
      await waitFor(() => expect(api.foundryDo).toHaveBeenCalledWith(command, { skill: "s1" }));
    }
  });
});
