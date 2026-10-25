// Domain models: exactly what XENIO's /api/v1 returns (app/ui/controller.py). Nothing here is invented by the page.

export const ORB_STATES = [
  "DISCONNECTED", "CONNECTING", "INITIALIZING", "IDLE", "LISTENING", "PROCESSING", "THINKING", "SEARCHING",
  "PLANNING", "ORCHESTRATING", "EXECUTING", "SPEAKING", "WAITING_APPROVAL", "PAUSED", "COMPLETED", "ERROR",
] as const;
export type OrbState = (typeof ORB_STATES)[number];

export interface BackgroundWork {
  state: OrbState | null;
  runs: number;
  agents: number;
}

export interface AssistantSnapshot {
  state: OrbState;
  connection: "connecting" | "initializing" | "connected" | "disconnected";
  paused: boolean;
  approvals: number;
  turn: string | null;
  tool: string | null;
  error: string | null;
  background: BackgroundWork;
  /** XENIO's talk engine (the Mac's microphone and speaker): what it is doing right now */
  voice?: "listening" | "transcribing" | "speaking" | null;
  /** when that began (epoch ms): the floating orb measures how fast it appeared */
  voice_since?: number | null;
}

export interface VoiceInfo {
  stt: boolean;
  tts: boolean;
  /** XENIO's own talk engine runs ("Hey XENIO", the Mac's microphone and speaker); otherwise the page records itself */
  engine?: boolean;
  wake?: boolean;
  wake_phrase?: string;
  problem?: string | null;
}

export interface Message {
  id: string;
  turn: string;
  role: "user" | "assistant";
  text: string;
  via?: "text" | "voice";
  /** asked to be answered on screen only (the API's ``quiet``): never spoken */
  quiet?: boolean;
  ok?: boolean;
  cancelled?: boolean;
  error?: string | null;
  actions?: string[];
  sources?: { title: string; url: string }[];
  at: string;
}

export interface ToolActivity {
  turn: string;
  id: string;
  title: string;
  status: "pending" | "in_progress" | "completed" | "failed" | string;
  detail: string;
}

export interface Approval {
  id: string;
  action: string;
  description: string;
  risk: "READ" | "SAFE_WRITE" | "CONFIRM_REQUIRED" | "HIGH_RISK" | string;
  strict: boolean;
  source: string;
  requested_at: string;
  expires_at: string;
  status: "pending" | "approved" | "approved for the task" | "denied" | "expired";
  decided_at?: string;
  /** everything it writes stays in XENIO's workspace: "allow for this task" is offered (checked again by XENIO) */
  confined?: boolean;
  /** V3: why XENIO asks, what it reaches, who asked, what a yes allows and for how long (written by XENIO) */
  why?: string;
  targets?: string[];
  requested_by?: string;
  scope?: string;
}

/** The user's "allow for this task": confirmable actions inside XENIO's workspace go ahead until it ends. */
/** XENIO Orb, the native floating orb and summon shortcut (macOS) */
export interface OverlayInfo {
  supported: boolean;
  running: boolean;
  pid?: number | null;
  problem?: string | null;
  status?: { shortcut?: string; shortcut_status?: string; accessibility?: boolean; screens?: number; location?: string };
  timings?: Record<string, { last: number; median: number; n: number }>;
}

export interface TaskGrant {
  active: boolean;
  until: string;
  seconds_left: number;
  covered: number;
  reason: string;
}

export interface Reminder {
  id: string;
  kind: "reminder";
  text: string;
  schedule: string | null;
  enabled: boolean;
  next_run: string | null;
  last_run: string | null;
  last_status: string | null;
  done: boolean;
}

export interface WorkspaceProject {
  name: string;
  path: string;
  kind: string;
  git: boolean;
  files: number;
  updated: string;
  runs: number;
  last_run: { id: string; status: string; request: string; verified?: boolean; next?: string; left_to_fix?: number } | null;
}

export interface UserLocation {
  label: string | null;
  lat: number | null;
  lon: number | null;
  source: "settings" | "device";
  accuracy_m?: number | null;
  updated: string;
}

export interface Snapshot {
  api: number;
  name: string;
  assistant: AssistantSnapshot;
  conversation: Message[];
  approvals: Approval[];
  seq: number;
  hermes: { name: string | null; version: string | null };
  voice: VoiceInfo;
  grant?: TaskGrant | null;
  overlay?: OverlayInfo;
  platform: string;
  demo: boolean;
}

export interface XenioEvent<T = Record<string, unknown>> {
  v: number;
  seq: number;
  id: string;
  type: string;
  at: string;
  data: T;
}

// ---- engineering (V6) ---------------------------------------------------------------------------------------------

export type TaskStatus = "pending" | "ready" | "running" | "blocked" | "succeeded" | "failed" | "cancelled";
export type RunStatus = "planning" | "planned" | "running" | "verifying" | "reviewing" | "done" | "failed" | "cancelled" | string;

export interface RunSummary {
  id: string;
  request: string;
  mode: string;
  status: RunStatus;
  project: string | null;
  created: string;
  updated: string;
  verified: boolean;
  outcome: string;
  tasks: Partial<Record<TaskStatus, number>>;
  running: RunningAgent[];
  live: boolean;
}

export interface RunningAgent {
  task: string;
  role: string;
  model: string;
  title?: string;
  /** when this agent's session started on the task (XENIO's clock) */
  since?: string | null;
}

export interface RunTask {
  id: string;
  title: string;
  role: string;
  description: string;
  depends_on: string[];
  allowed_paths: string[];
  status: TaskStatus;
  attempts: number;
  failure: string;
  result?: { summary?: string; changed_files?: string[]; model?: string; timings?: Record<string, number> };
}

export interface RunEvent {
  time: string;
  kind: string;
  text: string;
}

export interface AgentTurn {
  role?: string;
  task?: string;
  model?: string;
  seconds?: number;
  stop?: string;
  usage?: { totalTokens?: number; inputTokens?: number; outputTokens?: number; cachedReadTokens?: number };
  cost?: string; // "unknown" unless a provider reported one: XENIO never estimates money
  tools?: number;
  failed_tools?: string[];
}

export interface RunDetail {
  id: string;
  request: string;
  mode: string;
  status: RunStatus;
  project: string;
  created: string;
  updated: string;
  plan_summary: string;
  risks: string[];
  tasks: RunTask[];
  events: RunEvent[];
  baseline_tests: TestRun[];
  final_tests: TestRun[];
  scope_violations: string[];
  test_tampering: string[];
  security_findings: { file?: string; line?: number; rule?: string; message?: string; severity?: string }[];
  reviews: Record<string, unknown>;
  turns: AgentTurn[];
  answer: string;
  outcome: string;
  verified: boolean;
  diff_stat: string;
  approvals_needed: string[];
  report_path: string;
  running: RunningAgent[];
  live: boolean;
}

export interface TestRun {
  command: string;
  status: "passed" | "failed" | "no_tests" | "blocked" | "timeout" | "error" | "not_run" | string;
  passed: number;
  failed: number;
  errors: number;
  skipped: number;
  seconds: number;
  sandboxed: boolean;
  detail: string;
  failures?: string[];
}

export interface Agent {
  id: string;
  name: string;
  description: string;
  capabilities: string[];
  tools: string[];
  model: string;
  state: "idle" | "working" | "blocked" | "disabled";
  current: AgentWork[];
  blocked: AgentWork[];
}

export interface AgentWork {
  run: string;
  task: string;
  title: string;
  status: TaskStatus;
  failure: string | null;
  since?: string | null;
  model?: string;
  run_request?: string;
  /** the run's most recent milestone (real event text) */
  latest?: { text: string; at: string } | null;
}

export interface RunReview {
  summary?: string;
  findings?: { severity?: string; file?: string; line?: number; issue?: string; evidence?: string; fix?: string }[];
  acceptance?: { criterion?: string; met?: boolean; why?: string }[];
}

export interface RunDiff {
  run: string;
  files: { status: string; path: string }[];
  diff: string;
}

// ---- personal ---------------------------------------------------------------------------------------------------------

// empty fields are left out by XENIO (Task.to_dict)
export interface PersonalTask {
  id: number;
  title: string;
  description?: string;
  status: "pending" | "in_progress" | "done" | "cancelled";
  priority: "low" | "normal" | "high";
  due?: string;
  project?: string;
  source: string;
  created: string;
  updated: string;
  overdue?: boolean;
}

export interface Project {
  name: string;
  description?: string | null;
  repository?: string | null;
  path?: string | null;
  status?: string | null;
  focus?: string | null;
  updated?: string | null;
}

export interface ScheduledJob {
  id: string;
  name: string;
  schedule: string | null;
  enabled: boolean;
  next_run: string | null;
  last_run: string | null;
  last_status: string | null;
  last_error: string | null;
  task: string;
  runs: { at: string; result: "message" | "silent" | "failed" | string; message: string }[];
}

export interface MemoryEntry {
  /** stable while the entry is unchanged: what a correction or deletion names */
  id: string;
  store: "memory" | "user";
  text: string;
}

export interface MemoryList {
  available: boolean;
  reason?: string;
  editable?: boolean;
  entries: MemoryEntry[];
  usage?: Record<"memory" | "user", { used: number | null; limit: number }>;
  folder?: string;
}

export type MemoryChange =
  | { action: "add"; store: "memory" | "user"; text: string }
  | { action: "correct"; id: string; text: string }
  | { action: "delete"; id: string };

/** V3 context engine: XENIO's own records of where the work stands (app/context.py) */
export interface ContextBrief {
  focus: { kind: "run" | "project"; name: string; why: string } | null;
  ambiguous: boolean;
  candidates: string[];
  project: { name: string; focus: string | null; path: string | null; status: string; notes: string[] } | null;
  work: { run: string; request: string; project: string | null; folder: string; status: RunStatus; updated: string;
    verified: boolean; progress: string; tests: string | null; outcome: string | null; applied: boolean; pushed: boolean;
    next: string }[];
  tasks: { id: number; title: string; priority: string; due: string | null; overdue: boolean; project: string | null }[];
  fix_next: { what: string; source: string; run: string }[];
  problems: { what: string; at: string }[];
  not_included: string[];
  ms: number;
}

/** One tool call as XENIO's guard saw it (app/ui/audit.py) */
export interface ToolCall {
  tool: string;
  at: string;
  status: "requested" | "allowed" | "blocked" | "asked" | "approved" | "denied" | "expired" | "running" | "completed" | "failed";
  level: string | null;
  summary: string;
  detail: string;
  ms?: number;
}

export interface HealthCheck {
  name: string;
  status: "OK" | "WARN" | "FAIL";
  detail: string;
}

export interface ToolInfo {
  group: string;
  name: string;
  description: string;
  level: string;
  provider: "XENIO" | "Hermes";
  /** from the guard's audit log: what happened to this tool's recent calls */
  recent?: { calls: number; failed: number; blocked: number; last_problem: { at: string; status: string; detail: string } | null };
}

export interface SkillInfo {
  name: string;
  category: string;
  description: string;
  version?: string | null;
  author?: string | null;
  enabled?: boolean;
  essential?: boolean;
  available?: boolean;
  availability?: string;
  requires?: string[];
  scripts?: string[];
  changed?: string;
}

export interface UiSettings {
  motion: "system" | "full" | "reduced";
  speak_replies: boolean;
  orb_detail: "auto" | "low" | "high";
  notifications: boolean;
  transcript_lines: number;
  wake_word?: boolean;
  desktop_notifications?: boolean;
  voice_approvals?: boolean;
  orb_overlay?: boolean;
  orb_overlay_mode?: "active" | "always";
  summon_shortcut?: "triple_space" | "ctrl_option_space" | "off";
  summon_interval_ms?: number;
  device_location?: boolean;
  orb_summon_mode?: "choose" | "chat" | "talk";
  activity_retention_days?: 7 | 30 | 90 | 365;
  text_size?: "default" | "large";
}
