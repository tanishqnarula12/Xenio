// DEMO DATA ONLY. Used when the interface is built or served with VITE_XENIO_DEMO=1, for working on the UI without a
// running XENIO. Every snapshot says demo: true and the interface shows a permanent "Demo data" banner; nothing here
// is ever mixed with a real XENIO's data (the real client is chosen at start-up, never both).

import type { StreamStatus, XenioApi } from "./client";
import type { Agent, Approval, AssistantSnapshot, Message, RunDetail, RunSummary, Snapshot, XenioEvent } from "./types";

const now = () => new Date().toISOString().slice(0, 19);
const ago = (minutes: number) => new Date(Date.now() - minutes * 60_000).toISOString().slice(0, 19);

const DEMO_RUN: RunDetail = {
  id: "demo-run-1", request: "Add a due date to tasks in the task tracker", mode: "change", status: "running",
  project: "task-tracker", created: ago(6), updated: ago(1), plan_summary: "Backend stores the date; the web page shows it; tests cover both.",
  risks: ["The API changes shape: the web page must be updated in the same run."],
  tasks: [
    { id: "backend", title: "Store and return a due date", role: "backend", description: "", depends_on: [], allowed_paths: ["src/store.py", "src/api.py"], status: "succeeded", attempts: 1, failure: "" },
    { id: "frontend", title: "Show the due date in the list", role: "frontend", description: "", depends_on: ["backend"], allowed_paths: ["web/app.js"], status: "running", attempts: 1, failure: "" },
    { id: "tests", title: "Tests for due dates", role: "tester", description: "", depends_on: ["backend"], allowed_paths: ["tests/test_due.py"], status: "running", attempts: 1, failure: "" },
    { id: "review", title: "Review the change", role: "reviewer", description: "", depends_on: ["frontend", "tests"], allowed_paths: [], status: "pending", attempts: 0, failure: "" },
  ],
  events: [
    { time: ago(6), kind: "planned", text: "Planned 4 tasks for 4 roles." },
    { time: ago(4), kind: "task_done", text: "backend: Store and return a due date - done, 2 files changed." },
    { time: ago(2), kind: "task_started", text: "frontend and tests started in parallel." },
  ],
  baseline_tests: [], final_tests: [], scope_violations: [], test_tampering: [], security_findings: [], reviews: {},
  turns: [{ role: "planner", model: "demo-model", seconds: 12 }, { role: "backend", model: "demo-model", seconds: 41 }],
  answer: "", outcome: "", verified: false, diff_stat: "", approvals_needed: [], report_path: "",
  running: [{ task: "frontend", role: "frontend", model: "demo-model" }, { task: "tests", role: "tester", model: "demo-model" }],
  live: true,
};

const summary = (run: RunDetail): RunSummary => ({
  id: run.id, request: run.request, mode: run.mode, status: run.status, project: run.project, created: run.created,
  updated: run.updated, verified: run.verified, outcome: run.outcome, live: run.live, running: run.running,
  tasks: run.tasks.reduce<Record<string, number>>((acc, t) => ({ ...acc, [t.status]: (acc[t.status] ?? 0) + 1 }), {}),
});

const ROLES: [string, string][] = [["planner", "Planner"], ["frontend", "Frontend engineer"], ["backend", "Backend engineer"],
  ["database", "Database engineer"], ["tester", "Tester"], ["security", "Security reviewer"], ["reviewer", "Code reviewer"],
  ["docs", "Documentation writer"]];

export class DemoApi implements XenioApi {
  readonly demo = true;
  private seq = 1;
  private listeners = new Set<(e: XenioEvent) => void>();
  private conversation: Message[] = [];
  private state: AssistantSnapshot = { state: "IDLE", connection: "connected", paused: false, approvals: 0, turn: null,
    tool: null, error: null, background: { state: "ORCHESTRATING", runs: 1, agents: 2 } };
  private pending: Approval[] = [];
  private history: Approval[] = [];

  private emit(type: string, data: Record<string, unknown>) {
    const event: XenioEvent = { v: 1, seq: ++this.seq, id: `demo-${this.seq}`, type, at: now(), data };
    this.listeners.forEach((l) => l(event));
  }

  private setState(patch: Partial<AssistantSnapshot>) {
    this.state = { ...this.state, ...patch };
    this.emit("assistant.state", { ...this.state });
  }

  async snapshot(): Promise<Snapshot> {
    return { api: 1, name: "Xenio", assistant: { ...this.state }, conversation: [...this.conversation], approvals: [...this.pending],
      seq: this.seq, hermes: { name: "hermes-agent (demo)", version: "demo" }, voice: { stt: false, tts: false },
      platform: "demo", demo: true };
  }

  async chat(text: string, via: "text" | "voice") {
    const turn = `t${this.seq}`;
    const user: Message = { id: `${turn}-u`, turn, role: "user", text, via, at: now() };
    this.conversation.push(user);
    this.emit("chat.user", { ...user });
    this.setState({ state: "PROCESSING", turn });
    setTimeout(() => this.setState({ state: "SEARCHING", tool: "web_search" }), 500);
    setTimeout(() => this.setState({ state: "THINKING", tool: null }), 1400);
    const reply = "This is demo data. Start XENIO with python -m app.main --ui to talk to the real assistant.";
    setTimeout(() => this.emit("chat.chunk", { turn, text: reply }), 1800);
    setTimeout(() => {
      const message: Message = { id: `${turn}-a`, turn, role: "assistant", text: reply, ok: true, actions: [], sources: [], at: now() };
      this.conversation.push(message);
      this.emit("chat.done", { ...message });
      this.setState({ state: "COMPLETED", turn: null });
      setTimeout(() => this.setState({ state: "IDLE" }), 2500);
    }, 2200);
    return { turn };
  }

  async cancel() { return { cancelled: false }; }
  async newConversation() { this.conversation = []; this.emit("chat.reset", {}); return { ok: true }; }
  async setPaused(paused: boolean) { this.setState({ paused, state: paused ? "PAUSED" : "IDLE" }); return this.state; }
  async dismissError() { this.setState({ error: null, state: "IDLE" }); return this.state; }
  async approvals() { return { pending: [...this.pending], history: [...this.history] }; }

  async decide(id: string, decision: "approve" | "deny") {
    const found = this.pending.find((a) => a.id === id);
    if (!found) throw new Error("this approval is no longer open");
    this.pending = this.pending.filter((a) => a.id !== id);
    const settled: Approval = { ...found, status: decision === "approve" ? "approved" : "denied", decided_at: now() };
    this.history.unshift(settled);
    this.emit("approval.settled", { ...settled });
    this.setState({ approvals: this.pending.length, state: "IDLE" });
    return { id, decision };
  }

  /** Demo only: put a sample question in Approvals so the flow can be tried without a real action. */
  requestSampleApproval() {
    const approval: Approval = { id: `demo${Date.now().toString(16)}`, action: "terminal.install",
      description: "run: pip install requests (demo)", risk: "CONFIRM_REQUIRED", strict: false, source: "demo",
      requested_at: now(), expires_at: new Date(Date.now() + 120_000).toISOString().slice(0, 19), status: "pending" };
    this.pending.push(approval);
    this.emit("approval.requested", { ...approval });
    this.setState({ approvals: this.pending.length, state: "WAITING_APPROVAL" });
  }

  async runs() { return { available: true, runs: [summary(DEMO_RUN)] }; }
  async run(id: string) {
    if (id !== DEMO_RUN.id) throw new Error("no such run");
    return DEMO_RUN;
  }
  async runReport() { return { run: DEMO_RUN.id, status: DEMO_RUN.status, report: "# Demo report\n\nNo real run took place." }; }
  async runAction() { return { note: "demo: nothing was done" }; }

  async agents() {
    const agents: Agent[] = ROLES.map(([id, name]) => {
      const running = DEMO_RUN.running.find((r) => r.role === id);
      const task = DEMO_RUN.tasks.find((t) => t.role === id && running);
      return { id, name, description: `${name} (demo)`, capabilities: [], tools: [], model: "demo-model",
        state: running ? "working" : "idle", blocked: [],
        current: task ? [{ run: DEMO_RUN.id, task: task.id, title: task.title, status: task.status, failure: null }] : [] };
    });
    return { available: true, agents };
  }

  async tasks() {
    return { tasks: [
      { id: 1, title: "Renew passport", status: "pending" as const, priority: "high" as const, due: "2026-11-02", source: "user", created: ago(900), updated: ago(900) },
      { id: 2, title: "Send the deck to Riya", status: "done" as const, priority: "normal" as const, source: "user", created: ago(2000), updated: ago(60) },
    ], counts: { open: 1, done: 1 } };
  }
  async createTask(task: { title: string }) {
    return { id: Date.now(), title: task.title, status: "pending" as const, priority: "normal" as const, source: "user", created: now(), updated: now() };
  }
  async updateTask(id: number, action: "complete" | "reopen") {
    return { id, title: "Demo task", status: action === "complete" ? "done" as const : "pending" as const, priority: "normal" as const, source: "user", created: now(), updated: now() };
  }
  async deleteTask(id: number) { return { id, title: "Demo task", status: "pending" as const, priority: "normal" as const, source: "user", created: now(), updated: now() }; }
  async projects() { return { projects: [{ name: "task-tracker", description: "Demo project", status: "active" }], current: "task-tracker" }; }
  async schedules() {
    return { available: true, ticking: true, timezone: "IST", jobs: [{ id: "demo-job", name: "Morning inbox check", schedule: "every day at 08:00",
      enabled: true, next_run: ago(-600), last_run: ago(840), last_status: "ok", last_error: null, task: "Tell me if anything urgent arrived overnight",
      runs: [{ at: ago(840), result: "silent", message: "" }] }] };
  }
  async memory() { return { available: true, editable: false, entries: [{ store: "user" as const, text: "Prefers short answers. (demo)" }] }; }
  async activity() {
    return { events: [{ v: 1, seq: 1, id: "demo-a1", type: "engineering.milestone", at: ago(4), data: { title: "Engineering", message: "backend finished (demo)" } }] };
  }
  async health() {
    return { checked_at: now(), platform: "demo", checks: [{ name: "Hermes", status: "OK" as const, detail: "demo" }, { name: "TTS", status: "WARN" as const, detail: "not configured (demo)" }] };
  }
  async tools() { return { tools: [{ group: "Files", name: "xenio_list_directory", description: "List a folder (demo)", level: "READ", provider: "XENIO" as const }], skills: [], health: {} }; }
  async settings() { return { motion: "system" as const, speak_replies: false, orb_detail: "auto" as const, notifications: true, transcript_lines: 50 }; }
  async saveSettings(changes: Record<string, unknown>) { return { ...(await this.settings()), ...changes }; }
  async transcribe(): Promise<{ text: string }> { throw new Error("speech recognition is not available in demo mode"); }
  async speak(): Promise<ArrayBuffer> { throw new Error("speech output is not available in demo mode"); }
  async voiceListen(): Promise<{ listening: boolean }> { throw new Error("the microphone is not available in demo mode"); }
  async voiceFinish() { return { finished: false }; }
  async voiceLevel() { return { level: 0, phase: null }; }

  subscribe(_since: number, onEvent: (event: XenioEvent) => void, onStatus: (status: StreamStatus) => void) {
    this.listeners.add(onEvent);
    onStatus("open");
    return () => { this.listeners.delete(onEvent); onStatus("closed"); };
  }
}
