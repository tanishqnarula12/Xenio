// The page's state: what XENIO last reported (snapshot + events), what the page observes itself (its microphone and
// speaker), and per-topic revision counters that tell pages when to re-read their data. One reducer per event type.

import { useSyncExternalStore } from "react";
import type { StreamStatus } from "../api/client";
import type { Approval, AssistantSnapshot, Message, Snapshot, ToolActivity, UiSettings, VoiceInfo, XenioEvent } from "../api/types";
import { IDLE_VOICE, type LocalVoice } from "../orb/machine";

export type Topic = "runs" | "tasks" | "approvals" | "activity" | "schedules" | "health" | "agents" | "memory" | "skills";

export interface Notice {
  id: string;
  tone: "info" | "attention" | "danger" | "ok";
  title: string;
  message: string;
}

export interface AppState {
  stream: StreamStatus;
  snapshot: Snapshot | null;
  assistant: AssistantSnapshot | null;
  conversation: Message[];
  streaming: { turn: string; text: string } | null;
  tools: ToolActivity[];
  approvals: Approval[];
  voice: LocalVoice;
  voiceError: string | null;
  notices: Notice[];
  settings: UiSettings | null;
  revision: Record<Topic, number>;
  loadError: string | null;
  lastEventAt: string | null;
}

export const initialState: AppState = {
  stream: "connecting", snapshot: null, assistant: null, conversation: [], streaming: null, tools: [], approvals: [],
  voice: IDLE_VOICE, voiceError: null, notices: [], settings: null,
  revision: { runs: 0, tasks: 0, approvals: 0, activity: 0, schedules: 0, health: 0, agents: 0, memory: 0, skills: 0 },
  loadError: null,
  lastEventAt: null,
};

type Listener = () => void;

export class Store {
  private state: AppState;
  private readonly listeners = new Set<Listener>();

  constructor(initial: AppState = initialState) {
    this.state = initial;
  }

  get = (): AppState => this.state;

  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  set(patch: Partial<AppState> | ((s: AppState) => Partial<AppState>)): void {
    const next = typeof patch === "function" ? patch(this.state) : patch;
    this.state = { ...this.state, ...next };
    this.listeners.forEach((l) => l());
  }

  bump(...topics: Topic[]): Partial<AppState> {
    const revision = { ...this.state.revision };
    topics.forEach((t) => { revision[t] += 1; });
    return { revision };
  }

  notify(notice: Omit<Notice, "id">): void {
    const id = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`;
    this.set((s) => ({ notices: [...s.notices.slice(-3), { ...notice, id }] }));
  }

  dismiss(id: string): void {
    this.set((s) => ({ notices: s.notices.filter((n) => n.id !== id) }));
  }

  /** Apply one event from XENIO. Unknown types are ignored (a newer XENIO may send more). */
  apply(event: XenioEvent): void {
    const d = event.data as Record<string, unknown>;
    const s = this.state;
    const at = { lastEventAt: event.at };
    switch (event.type) {
      case "assistant.state":
        this.set({ ...at, assistant: d as unknown as AssistantSnapshot, ...this.bump("agents") });
        return;
      case "chat.user": {
        const m = d as unknown as Message;
        if (s.conversation.some((x) => x.id === m.id)) return;
        this.set({ ...at, conversation: [...s.conversation, m], streaming: { turn: m.turn, text: "" }, tools: [] });
        return;
      }
      case "chat.chunk":
        if (s.streaming && s.streaming.turn === d.turn) {
          this.set({ ...at, streaming: { turn: s.streaming.turn, text: s.streaming.text + String(d.text ?? "") } });
        }
        return;
      case "chat.tool": {
        const t = d as unknown as ToolActivity;
        const others = s.tools.filter((x) => x.id !== t.id);
        this.set({ ...at, tools: [...others, t] });
        return;
      }
      case "chat.done": {
        const m = d as unknown as Message;
        const conversation = s.conversation.some((x) => x.id === m.id) ? s.conversation : [...s.conversation, m];
        // a turn may have changed tasks, reminders or memory through tools that run inside Hermes, where no event is
        // published: the views that show them re-read XENIO's records (only those on screen fetch)
        this.set({ ...at, conversation, streaming: null, ...this.bump("activity", "tasks", "schedules", "memory") });
        return;
      }
      case "chat.reset":
        this.set({ ...at, conversation: [], streaming: null, tools: [] });
        return;
      case "approval.requested": {
        const a = d as unknown as Approval;
        if (s.approvals.some((x) => x.id === a.id)) return;
        this.set({ ...at, approvals: [...s.approvals, a], ...this.bump("approvals", "activity") });
        return;
      }
      case "approval.settled":
        this.set({ ...at, approvals: s.approvals.filter((x) => x.id !== d.id), ...this.bump("approvals", "activity") });
        return;
      case "engineering.run":
        this.set({ ...at, ...this.bump("runs", "agents") });
        return;
      case "engineering.milestone":
        this.set({ ...at, ...this.bump("runs", "agents", "activity") });
        if (s.settings?.notifications === false && !d.action_required) return;
        this.notify({ tone: d.action_required ? "attention" : "info", title: String(d.title ?? "Engineering"),
          message: String(d.message ?? "") });
        return;
      case "notification":
        this.set({ ...at, ...this.bump("activity", "schedules") });
        if (s.settings?.notifications === false && d.priority !== "high") return;
        this.notify({ tone: d.priority === "high" ? "attention" : "info", title: String(d.title ?? "XENIO"),
          message: String(d.message ?? "") });
        return;
      case "task.changed":
        this.set({ ...at, ...this.bump("tasks", "activity") });
        return;
      case "memory.changed":
        this.set({ ...at, ...this.bump("memory", "activity", "health") });
        return;
      case "skill.changed":
        this.set({ ...at, ...this.bump("skills", "activity") });
        return;
      case "settings.changed": {
        const { changed: _changed, ...settings } = d;  // which keys changed: for Activity, not a setting
        this.set({ ...at, settings: settings as unknown as UiSettings, ...this.bump("activity") });
        return;
      }
      case "grant.changed":
        if (s.snapshot) this.set({ ...at, snapshot: { ...s.snapshot, grant: (d.grant ?? null) as Snapshot["grant"] } });
        return;
      case "overlay.changed":
        if (s.snapshot) this.set({ ...at, snapshot: { ...s.snapshot, overlay: d as unknown as Snapshot["overlay"] } });
        return;
      case "schedule.changed":
        this.set({ ...at, ...this.bump("schedules") });
        return;
      case "voice.changed":
        if (s.snapshot) this.set({ ...at, snapshot: { ...s.snapshot, voice: d as unknown as VoiceInfo }, ...this.bump("health") });
        return;
      case "health.changed":
        this.set({ ...at, ...this.bump("health", "activity") });
        return;
      case "request.received":
      case "assistant.error":
      case "system.event":
        this.set({ ...at, ...this.bump("activity") });
        return;
      default:
        return;
    }
  }

  loadSnapshot(snapshot: Snapshot): void {
    this.set({ snapshot, assistant: snapshot.assistant, conversation: snapshot.conversation, approvals: snapshot.approvals,
      streaming: snapshot.assistant.turn ? { turn: snapshot.assistant.turn, text: "" } : null, loadError: null,
      ...this.bump("runs", "tasks", "approvals", "activity", "schedules", "health", "agents", "memory", "skills") });
  }
}

export function useStore<T>(store: Store, select: (s: AppState) => T): T {
  return useSyncExternalStore(store.subscribe, () => select(store.get()), () => select(store.get()));
}
