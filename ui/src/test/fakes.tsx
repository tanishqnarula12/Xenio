import { render } from "@testing-library/react";
import { vi } from "vitest";
import { App } from "../App";
import type { StreamStatus, XenioApi } from "../api/client";
import type { Agent, Approval, AssistantSnapshot, RunDetail, Snapshot, XenioEvent } from "../api/types";
import { AppController } from "../state/controller";
import { AppProvider } from "../state/context";
import { Store } from "../state/store";

export const assistant = (patch: Partial<AssistantSnapshot> = {}): AssistantSnapshot => ({
  state: "IDLE", connection: "connected", paused: false, approvals: 0, turn: null, tool: null, error: null,
  background: { state: null, runs: 0, agents: 0 }, ...patch,
});

export const snapshot = (patch: Partial<Snapshot> = {}): Snapshot => ({
  api: 1, name: "Xenio", assistant: assistant(), conversation: [], approvals: [], seq: 1,
  hermes: { name: "hermes-agent", version: "0.22.2" }, voice: { stt: true, tts: true }, platform: "darwin", demo: false, ...patch,
});

export const approval = (patch: Partial<Approval> = {}): Approval => ({
  id: "abc123", action: "terminal.install", description: "run: pip install requests", risk: "CONFIRM_REQUIRED", strict: false,
  source: "hermes", requested_at: new Date().toISOString().slice(0, 19),
  expires_at: new Date(Date.now() + 90_000).toISOString().slice(0, 19), status: "pending", ...patch,
});

export const run = (patch: Partial<RunDetail> = {}): RunDetail => ({
  id: "run-1", request: "Add due dates", mode: "change", status: "running", project: "tracker", created: "2026-10-18T10:00:00",
  updated: "2026-10-18T10:05:00", plan_summary: "", risks: [],
  tasks: [
    { id: "backend", title: "Store the date", role: "backend", description: "", depends_on: [], allowed_paths: [], status: "succeeded", attempts: 1, failure: "" },
    { id: "frontend", title: "Show the date", role: "frontend", description: "", depends_on: ["backend"], allowed_paths: [], status: "running", attempts: 1, failure: "" },
    { id: "tests", title: "Test it", role: "tester", description: "", depends_on: ["backend"], allowed_paths: [], status: "running", attempts: 1, failure: "" },
    { id: "review", title: "Review", role: "reviewer", description: "", depends_on: ["frontend", "tests"], allowed_paths: [], status: "pending", attempts: 0, failure: "" },
  ],
  events: [], baseline_tests: [], final_tests: [], scope_violations: [], test_tampering: [], security_findings: [], reviews: {},
  turns: [], answer: "", outcome: "", verified: false, diff_stat: "", approvals_needed: [], report_path: "",
  running: [{ task: "frontend", role: "frontend", model: "m" }, { task: "tests", role: "tester", model: "m" }], live: true, ...patch,
});

export const agent = (patch: Partial<Agent> = {}): Agent => ({
  id: "backend", name: "Backend engineer", description: "Server code", capabilities: ["read", "edit"], tools: ["read_file"],
  model: "Hermes main model", state: "idle", current: [], blocked: [], ...patch,
});

/** A XENIO that answers from fixtures; every method is a spy. emit() pushes an event down the live stream. */
export class FakeApi implements XenioApi {
  readonly demo = false;
  listeners: ((e: XenioEvent) => void)[] = [];
  status: ((s: StreamStatus) => void)[] = [];
  seq = 1;
  snap = snapshot();
  snapshot = vi.fn(async () => this.snap);
  chat = vi.fn(async (_text: string, _via: "text" | "voice", _quiet?: boolean) => ({ turn: "t1" }));
  cancel = vi.fn(async () => ({ cancelled: true }));
  newConversation = vi.fn(async () => ({ ok: true }));
  setPaused = vi.fn(async () => ({}));
  dismissError = vi.fn(async () => ({}));
  approvals = vi.fn(async () => ({ pending: [] as Approval[], history: [] as Approval[] }));
  decide = vi.fn(async (id: string, decision: "approve" | "approve_task" | "deny") => ({ id, decision }));
  runs = vi.fn(async () => ({ available: true, runs: [] as ReturnType<typeof toSummary>[] }));
  run = vi.fn(async (_id: string) => run());
  runReport = vi.fn(async () => ({ run: "run-1", status: "done", report: "# Report" }));
  runAction = vi.fn(async () => ({}));
  agents = vi.fn(async (): Promise<{ available: boolean; reason?: string; agents: Agent[] }> => ({ available: true, agents: [] }));
  tasks = vi.fn(async () => ({ tasks: [] as import("../api/types").PersonalTask[], counts: {} as Record<string, number> }));
  createTask = vi.fn(async (t: { title: string }) => ({ id: 9, title: t.title, status: "pending" as const, priority: "normal" as const, source: "user", created: "", updated: "" }));
  updateTask = vi.fn(async (id: number) => ({ id, title: "x", status: "done" as const, priority: "normal" as const, source: "user", created: "", updated: "" }));
  deleteTask = vi.fn(async (id: number) => ({ id, title: "x", status: "pending" as const, priority: "normal" as const, source: "user", created: "", updated: "" }));
  projects = vi.fn(async () => ({ projects: [] as import("../api/types").Project[], current: null as string | null,
    workspace: [] as import("../api/types").WorkspaceProject[], workspace_path: null as string | null }));
  schedules = vi.fn(async (): Promise<{ available: boolean; jobs: import("../api/types").ScheduledJob[]; ticking?: boolean;
    reminders?: import("../api/types").Reminder[]; note?: string }> => ({ available: true, jobs: [], ticking: true, reminders: [] }));
  memory = vi.fn(async (_q: string): Promise<import("../api/types").MemoryList> => ({ available: true, editable: false, entries: [] }));
  changeMemory = vi.fn(async (c: import("../api/types").MemoryChange) => ({ ok: true, store: "user", action: c.action }));
  context = vi.fn(async (): Promise<import("../api/types").ContextBrief> => ({ focus: null, ambiguous: false, candidates: [],
    project: null, work: [], tasks: [], fix_next: [], problems: [], not_included: [], ms: 1 }));
  activity = vi.fn(async (_limit?: number, _signal?: AbortSignal, _offset?: number, _kind?: string):
    Promise<{ events: XenioEvent[]; more?: boolean; next_offset?: number }> => ({ events: [] as XenioEvent[] }));
  audit = vi.fn(async (): Promise<{ calls: import("../api/types").ToolCall[] }> => ({ calls: [] }));
  setSkill = vi.fn(async (name: string, enabled: boolean) => ({ name, enabled }));
  runDiff = vi.fn(async (id: string): Promise<import("../api/types").RunDiff> => ({ run: id, files: [], diff: "" }));
  startRun = vi.fn(async () => ({ status: "started", note: "Planning in the background" }));
  summonOrb = vi.fn(async () => ({ ok: true }));
  health = vi.fn(async () => ({ checks: [] as import("../api/types").HealthCheck[], checked_at: "2026-10-18T10:00:00", platform: "darwin" }));
  tools = vi.fn(async (): Promise<{ tools: import("../api/types").ToolInfo[]; skills: import("../api/types").SkillInfo[];
    health: Record<string, import("../api/types").HealthCheck> }> => ({ tools: [], skills: [], health: {} }));
  settings = vi.fn(async () => ({ motion: "system" as const, speak_replies: false, orb_detail: "auto" as const, notifications: true, transcript_lines: 50 }));
  saveSettings = vi.fn(async (c: object) => ({ ...(await this.settings()), ...c }));
  transcribe = vi.fn(async () => ({ text: "what time is it" }));
  speak = vi.fn(async () => new ArrayBuffer(8));
  endGrant = vi.fn(async () => ({ grant: null }));
  createReminder = vi.fn(async (text: string, when: string) => ({ id: "abc123", text, when }));
  deleteReminder = vi.fn(async (id: string) => ({ id, text: "x" }));
  location = vi.fn(async (): Promise<{ location: import("../api/types").UserLocation | null }> => ({ location: null }));
  saveLocation = vi.fn(async (c: { label: string }) => ({ location: { label: c.label, lat: null, lon: null, source: "settings" as const, updated: "" } }));
  overlayStatus = vi.fn(async (_status: Record<string, string | boolean | number>) => ({ ok: true }));
  overlayMetric = vi.fn(async (_name: string, _ms: number) => ({ ok: true }));
  voiceListen = vi.fn(async () => ({ listening: true }));
  voiceFinish = vi.fn(async () => ({ finished: true }));
  voiceLevel = vi.fn(async () => ({ level: 0.4, phase: "listening" as string | null }));
  subscribe = vi.fn((_since: number, onEvent: (e: XenioEvent) => void, onStatus: (s: StreamStatus) => void) => {
    this.listeners.push(onEvent);
    this.status.push(onStatus);
    onStatus("open");
    return () => {};
  });

  emit(type: string, data: Record<string, unknown>): void {
    const event: XenioEvent = { v: 1, seq: ++this.seq, id: `e${this.seq}`, type, at: "2026-10-18T10:00:00", data };
    this.listeners.forEach((l) => l(event));
  }
}

export const toSummary = (r: RunDetail) => ({
  id: r.id, request: r.request, mode: r.mode, status: r.status, project: r.project, created: r.created, updated: r.updated,
  verified: r.verified, outcome: r.outcome, live: r.live, running: r.running,
  tasks: r.tasks.reduce<Record<string, number>>((a, t) => ({ ...a, [t.status]: (a[t.status] ?? 0) + 1 }), {}),
});

export async function renderApp(api: FakeApi, page = "home") {
  window.location.hash = `#/${page}`;
  const store = new Store();
  const app = new AppController(api, store);
  await app.init();
  const utils = render(<AppProvider store={store} app={app}><App /></AppProvider>);
  return { ...utils, store, app };
}
