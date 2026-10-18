// The typed client for XENIO's local API (/api/v1). The page holds no secrets: only the per-start session token XENIO
// printed (it arrives in the URL fragment, which browsers never send to a server, and lives in this tab only).

import type {
  Agent, Approval, HealthCheck, MemoryEntry, PersonalTask, Project, RunDetail, RunSummary, ScheduledJob, SkillInfo,
  Snapshot, ToolInfo, UiSettings, XenioEvent,
} from "./types";

export class ApiError extends Error {
  readonly status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export type StreamStatus = "connecting" | "open" | "retrying" | "unauthorized" | "closed";

export interface Unavailable {
  available: false;
  reason?: string;
}

export interface XenioApi {
  readonly demo: boolean;
  snapshot(signal?: AbortSignal): Promise<Snapshot>;
  chat(text: string, via: "text" | "voice"): Promise<{ turn: string }>;
  cancel(): Promise<{ cancelled: boolean }>;
  newConversation(): Promise<{ ok: boolean }>;
  setPaused(paused: boolean): Promise<unknown>;
  dismissError(): Promise<unknown>;
  approvals(signal?: AbortSignal): Promise<{ pending: Approval[]; history: Approval[] }>;
  decide(id: string, decision: "approve" | "deny", confirmText?: string): Promise<{ id: string; decision: string }>;
  runs(signal?: AbortSignal): Promise<{ available: boolean; reason?: string; runs: RunSummary[] }>;
  run(id: string, signal?: AbortSignal): Promise<RunDetail>;
  runReport(id: string, signal?: AbortSignal): Promise<{ run: string; status: string; report: string }>;
  runAction(id: string, action: "approve" | "cancel" | "apply" | "push" | "pull_request"): Promise<Record<string, unknown>>;
  agents(signal?: AbortSignal): Promise<{ available: boolean; reason?: string; agents: Agent[] }>;
  tasks(status?: string, signal?: AbortSignal): Promise<{ tasks: PersonalTask[]; counts: Record<string, number> }>;
  createTask(task: { title: string; priority?: string; due?: string; project?: string; description?: string }): Promise<PersonalTask>;
  updateTask(id: number, action: "complete" | "reopen"): Promise<PersonalTask>;
  deleteTask(id: number): Promise<PersonalTask>;
  projects(signal?: AbortSignal): Promise<{ projects: Project[]; current: string | null }>;
  schedules(signal?: AbortSignal): Promise<{ available: boolean; jobs: ScheduledJob[]; ticking?: boolean; timezone?: string; note?: string }>;
  memory(query: string, signal?: AbortSignal): Promise<{ available: boolean; editable?: boolean; entries: MemoryEntry[] }>;
  activity(limit?: number, signal?: AbortSignal): Promise<{ events: XenioEvent[] }>;
  health(force?: boolean, signal?: AbortSignal): Promise<{ checks: HealthCheck[]; checked_at: string; platform: string }>;
  tools(signal?: AbortSignal): Promise<{ tools: ToolInfo[]; skills: SkillInfo[]; health: Record<string, HealthCheck> }>;
  settings(signal?: AbortSignal): Promise<UiSettings>;
  saveSettings(changes: Partial<UiSettings>): Promise<UiSettings>;
  transcribe(wav: Blob): Promise<{ text: string }>;
  speak(text: string, signal?: AbortSignal): Promise<ArrayBuffer>;
  voiceListen(): Promise<{ listening: boolean }>;
  voiceFinish(): Promise<{ finished: boolean }>;
  voiceLevel(signal?: AbortSignal): Promise<{ level: number; phase: string | null }>;
  subscribe(since: number, onEvent: (event: XenioEvent) => void, onStatus: (status: StreamStatus) => void): () => void;
}

const TOKEN_KEY = "xenio.session-token";

/** The session token: from the URL fragment XENIO opened (then removed from the address bar), else this tab's copy. */
export function readToken(win: Window = window): string | null {
  const match = /(?:^#|&)token=([A-Za-z0-9_-]{20,})/.exec(win.location.hash);
  if (match?.[1]) {
    try {
      win.sessionStorage.setItem(TOKEN_KEY, match[1]);
    } catch {
      /* storage blocked: keep it in memory for this page only */
    }
    const route = win.location.hash.replace(/(?:^#|&)token=[A-Za-z0-9_-]+/, "").replace(/^#?&?/, "");
    win.history.replaceState(null, "", win.location.pathname + win.location.search + (route ? `#${route}` : ""));
    return match[1];
  }
  try {
    return win.sessionStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

function withTimeout(ms: number, signal?: AbortSignal): AbortSignal {
  const timeout = AbortSignal.timeout(ms);
  return signal && "any" in AbortSignal ? AbortSignal.any([signal, timeout]) : (signal ?? timeout);
}

export class HttpApi implements XenioApi {
  readonly demo = false;
  private readonly base: string;
  private readonly token: string;

  constructor(token: string, base = "/api/v1") {
    this.token = token;
    this.base = base;
  }

  private async request<T>(method: string, path: string, options: { body?: unknown; raw?: BodyInit; type?: string;
    signal?: AbortSignal; timeout?: number; binary?: boolean } = {}): Promise<T> {
    const headers: Record<string, string> = { Authorization: `Bearer ${this.token}` };
    let body: BodyInit | undefined;
    if (options.raw !== undefined) {
      body = options.raw;
      headers["Content-Type"] = options.type ?? "application/octet-stream";
    } else if (options.body !== undefined) {
      body = JSON.stringify(options.body);
      headers["Content-Type"] = "application/json";
    }
    let response: Response;
    try {
      response = await fetch(this.base + path, { method, headers, body, credentials: "omit", cache: "no-store",
        signal: withTimeout(options.timeout ?? 20_000, options.signal) });
    } catch (error) {
      if (error instanceof DOMException && error.name === "TimeoutError") {
        throw new ApiError("XENIO did not answer in time", 504);
      }
      if (error instanceof DOMException && error.name === "AbortError") throw error;
      throw new ApiError("XENIO is not reachable. Is `python -m app.main --ui` still running?", 0);
    }
    if (!response.ok) {
      let message = `XENIO answered ${response.status}`;
      try {
        const data = (await response.json()) as { error?: string };
        if (data.error) message = data.error;
      } catch {
        /* not JSON */
      }
      throw new ApiError(message, response.status);
    }
    return (options.binary ? await response.arrayBuffer() : await response.json()) as T;
  }

  snapshot(signal?: AbortSignal) { return this.request<Snapshot>("GET", "/snapshot", { signal }); }
  chat(text: string, via: "text" | "voice") { return this.request<{ turn: string }>("POST", "/chat", { body: { text, via } }); }
  cancel() { return this.request<{ cancelled: boolean }>("POST", "/chat/cancel", { body: {} }); }
  newConversation() { return this.request<{ ok: boolean }>("POST", "/chat/new", { body: {}, timeout: 70_000 }); }
  setPaused(paused: boolean) { return this.request("POST", "/assistant/pause", { body: { paused } }); }
  dismissError() { return this.request("POST", "/assistant/dismiss", { body: {} }); }
  approvals(signal?: AbortSignal) { return this.request<{ pending: Approval[]; history: Approval[] }>("GET", "/approvals", { signal }); }
  decide(id: string, decision: "approve" | "deny", confirmText = "") {
    return this.request<{ id: string; decision: string }>("POST", `/approvals/${encodeURIComponent(id)}`,
      { body: { decision, confirm_text: confirmText } });
  }
  runs(signal?: AbortSignal) { return this.request<{ available: boolean; reason?: string; runs: RunSummary[] }>("GET", "/engineering/runs", { signal }); }
  run(id: string, signal?: AbortSignal) { return this.request<RunDetail>("GET", `/engineering/runs/${encodeURIComponent(id)}`, { signal }); }
  runReport(id: string, signal?: AbortSignal) {
    return this.request<{ run: string; status: string; report: string }>("GET", `/engineering/runs/${encodeURIComponent(id)}/report`, { signal });
  }
  runAction(id: string, action: "approve" | "cancel" | "apply" | "push" | "pull_request") {
    // apply/push/pull_request wait for the user's answer in Approvals: allow the gate's full time
    return this.request<Record<string, unknown>>("POST", `/engineering/runs/${encodeURIComponent(id)}/${action}`,
      { body: {}, timeout: 300_000 });
  }
  agents(signal?: AbortSignal) { return this.request<{ available: boolean; reason?: string; agents: Agent[] }>("GET", "/agents", { signal }); }
  tasks(status = "all", signal?: AbortSignal) {
    return this.request<{ tasks: PersonalTask[]; counts: Record<string, number> }>("GET", `/tasks?status=${encodeURIComponent(status)}`, { signal });
  }
  createTask(task: { title: string; priority?: string; due?: string; project?: string; description?: string }) {
    return this.request<PersonalTask>("POST", "/tasks", { body: task });
  }
  updateTask(id: number, action: "complete" | "reopen") { return this.request<PersonalTask>("POST", `/tasks/${id}`, { body: { action } }); }
  deleteTask(id: number) { return this.request<PersonalTask>("DELETE", `/tasks/${id}`); }
  projects(signal?: AbortSignal) { return this.request<{ projects: Project[]; current: string | null }>("GET", "/projects", { signal }); }
  schedules(signal?: AbortSignal) {
    return this.request<{ available: boolean; jobs: ScheduledJob[]; ticking?: boolean; timezone?: string; note?: string }>("GET", "/schedules", { signal });
  }
  memory(query: string, signal?: AbortSignal) {
    return this.request<{ available: boolean; editable?: boolean; entries: MemoryEntry[] }>("GET", `/memory?q=${encodeURIComponent(query)}`, { signal });
  }
  activity(limit = 200, signal?: AbortSignal) { return this.request<{ events: XenioEvent[] }>("GET", `/activity?limit=${limit}`, { signal }); }
  health(force = false, signal?: AbortSignal) {
    return this.request<{ checks: HealthCheck[]; checked_at: string; platform: string }>("GET", `/health${force ? "?force=1" : ""}`,
      { signal, timeout: 60_000 });
  }
  tools(signal?: AbortSignal) {
    return this.request<{ tools: ToolInfo[]; skills: SkillInfo[]; health: Record<string, HealthCheck> }>("GET", "/tools", { signal, timeout: 60_000 });
  }
  settings(signal?: AbortSignal) { return this.request<UiSettings>("GET", "/settings", { signal }); }
  saveSettings(changes: Partial<UiSettings>) { return this.request<UiSettings>("PUT", "/settings", { body: changes }); }
  transcribe(wav: Blob) {
    return this.request<{ text: string }>("POST", "/voice/transcribe", { raw: wav, type: "audio/wav", timeout: 70_000 });
  }
  speak(text: string, signal?: AbortSignal) {
    return this.request<ArrayBuffer>("POST", "/voice/speak", { body: { text }, binary: true, signal, timeout: 70_000 });
  }
  voiceListen() { return this.request<{ listening: boolean }>("POST", "/voice/listen", { body: {} }); }
  voiceFinish() { return this.request<{ finished: boolean }>("POST", "/voice/finish", { body: {} }); }
  voiceLevel(signal?: AbortSignal) { return this.request<{ level: number; phase: string | null }>("GET", "/voice/level", { signal }); }

  subscribe(since: number, onEvent: (event: XenioEvent) => void, onStatus: (status: StreamStatus) => void): () => void {
    const stream = new EventStream(this.base + "/events", this.token, onEvent, onStatus);
    stream.start(since);
    return () => stream.stop();
  }
}

/** Server-Sent Events over fetch (so the token stays in a header, never in a URL), with replay and backoff. */
export class EventStream {
  private controller: AbortController | null = null;
  private stopped = false;
  private lastSeq = 0;
  private readonly seen = new Set<string>();
  private attempt = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;

  constructor(private readonly url: string, private readonly token: string,
    private readonly onEvent: (event: XenioEvent) => void, private readonly onStatus: (status: StreamStatus) => void) {}

  start(since: number): void {
    this.lastSeq = since;
    void this.connect();
  }

  stop(): void {
    this.stopped = true;
    this.controller?.abort();
    if (this.timer) clearTimeout(this.timer);
    this.onStatus("closed");
  }

  /** Accept each event once and in order; a reset (events lost) is always passed on. */
  accept(event: XenioEvent): boolean {
    if (event.type === "stream.reset") {
      this.lastSeq = event.seq;
      return true;
    }
    if (this.seen.has(event.id) || event.seq <= this.lastSeq) return false;
    this.seen.add(event.id);
    if (this.seen.size > 1000) this.seen.delete(this.seen.values().next().value as string);
    this.lastSeq = event.seq;
    return true;
  }

  private async connect(): Promise<void> {
    if (this.stopped) return;
    this.onStatus(this.attempt === 0 ? "connecting" : "retrying");
    this.controller = new AbortController();
    try {
      const response = await fetch(`${this.url}?since=${this.lastSeq}`, {
        headers: { Authorization: `Bearer ${this.token}`, Accept: "text/event-stream" },
        credentials: "omit", cache: "no-store", signal: this.controller.signal,
      });
      if (response.status === 401) {
        this.onStatus("unauthorized");
        return;
      }
      if (!response.ok || !response.body) throw new Error(`stream answered ${response.status}`);
      this.attempt = 0;
      this.onStatus("open");
      const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
      let buffer = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += value;
        let cut: number;
        while ((cut = buffer.indexOf("\n\n")) >= 0) {
          const block = buffer.slice(0, cut);
          buffer = buffer.slice(cut + 2);
          const data = block.split("\n").filter((l) => l.startsWith("data: ")).map((l) => l.slice(6)).join("\n");
          if (!data) continue;
          try {
            const event = JSON.parse(data) as XenioEvent;
            if (this.accept(event)) this.onEvent(event);
          } catch {
            /* a malformed block is skipped, never rendered */
          }
        }
      }
      throw new Error("stream ended");
    } catch {
      if (this.stopped) return;
      this.attempt += 1;
      this.onStatus("retrying");
      const wait = Math.min(10_000, 500 * 2 ** Math.min(this.attempt, 5));
      this.timer = setTimeout(() => void this.connect(), wait);
    }
  }
}
