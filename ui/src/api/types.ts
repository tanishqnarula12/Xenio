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
  status: "pending" | "approved" | "denied" | "expired";
  decided_at?: string;
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
  current: { run: string; task: string; title: string; status: TaskStatus; failure: string | null }[];
  blocked: { run: string; task: string; title: string; status: TaskStatus; failure: string | null }[];
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
  store: "memory" | "user";
  text: string;
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
}

export interface SkillInfo {
  name: string;
  category: string;
  description: string;
}

export interface UiSettings {
  motion: "system" | "full" | "reduced";
  speak_replies: boolean;
  orb_detail: "auto" | "low" | "high";
  notifications: boolean;
  transcript_lines: number;
  wake_word?: boolean;
}
