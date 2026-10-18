// The orb's state machine. The backend decides what XENIO is doing (app/ui/assistant.py); the page adds only what it
// can observe itself: its microphone is recording (LISTENING), the recording is being transcribed (PROCESSING), TTS
// audio is really playing (SPEAKING). Global assistant state and background agent work stay separate.

import type { StreamStatus } from "../api/client";
import type { AssistantSnapshot, OrbState } from "../api/types";

export interface LocalVoice {
  listening: boolean;
  transcribing: boolean;
  speaking: boolean;
}

export const IDLE_VOICE: LocalVoice = { listening: false, transcribing: false, speaking: false };

const ANY_TIME: OrbState[] = ["DISCONNECTED", "CONNECTING", "ERROR", "PAUSED", "WAITING_APPROVAL"];
const TURN: OrbState[] = ["PROCESSING", "THINKING", "SEARCHING", "EXECUTING", "PLANNING"];
const BACKGROUND: OrbState[] = ["PLANNING", "ORCHESTRATING", "EXECUTING"];

/** Which state may follow which. Anything else is either a resync (shown) or impossible (refused, see nextState). */
export const TRANSITIONS: Record<OrbState, readonly OrbState[]> = {
  DISCONNECTED: ["CONNECTING", "INITIALIZING", "IDLE"],
  CONNECTING: ["INITIALIZING", "IDLE", ...BACKGROUND],
  INITIALIZING: ["IDLE", ...BACKGROUND],
  IDLE: ["LISTENING", "PROCESSING", "SPEAKING", ...BACKGROUND],
  LISTENING: ["PROCESSING", "IDLE", ...BACKGROUND],
  PROCESSING: [...TURN, "COMPLETED", "IDLE", "SPEAKING", "ORCHESTRATING"],
  THINKING: [...TURN, "COMPLETED", "IDLE", "SPEAKING", "ORCHESTRATING"],
  SEARCHING: [...TURN, "COMPLETED", "IDLE", "SPEAKING", "ORCHESTRATING"],
  EXECUTING: [...TURN, "COMPLETED", "IDLE", "SPEAKING", "ORCHESTRATING", "LISTENING"],
  PLANNING: [...TURN, "COMPLETED", "IDLE", "SPEAKING", "ORCHESTRATING", "LISTENING"],
  ORCHESTRATING: ["IDLE", "LISTENING", "COMPLETED", "SPEAKING", ...TURN],
  SPEAKING: ["IDLE", "LISTENING", "COMPLETED", "PROCESSING", ...BACKGROUND],
  WAITING_APPROVAL: ["IDLE", "COMPLETED", "SPEAKING", "LISTENING", ...TURN, ...BACKGROUND],
  PAUSED: ["IDLE", ...BACKGROUND],
  COMPLETED: ["IDLE", "SPEAKING", "LISTENING", "PROCESSING", ...BACKGROUND],
  ERROR: ["IDLE", "LISTENING", "PROCESSING", "INITIALIZING", ...BACKGROUND],
};

export function isAllowed(from: OrbState, to: OrbState): boolean {
  return from === to || ANY_TIME.includes(to) || TRANSITIONS[from].includes(to);
}

/**
 * The state to show next. An allowed transition is taken. An unexpected one is a resync (the page reloaded or missed
 * events mid-turn) and shows the backend's truth - except the two states that must be earned: COMPLETED (only after
 * work) and SPEAKING (only while audio plays), which are refused and the current state is kept.
 */
export function nextState(from: OrbState, to: OrbState): OrbState {
  if (isAllowed(from, to)) return to;
  if (to === "COMPLETED" || to === "SPEAKING") return from;
  return to;
}

/** What the orb shows, from the live inputs. Priority: no backend > a human decision > what the page observes > XENIO. */
export function deriveOrbState(stream: StreamStatus, assistant: AssistantSnapshot | null, voice: LocalVoice): OrbState {
  if (stream === "unauthorized" || stream === "closed") return "DISCONNECTED";
  if (!assistant) return stream === "retrying" ? "DISCONNECTED" : "CONNECTING";
  if (stream === "retrying") return "DISCONNECTED";
  const backend = assistant.state;
  if (backend === "DISCONNECTED" || backend === "CONNECTING" || backend === "INITIALIZING" || backend === "PAUSED") {
    return backend;
  }
  if (backend === "WAITING_APPROVAL") return backend;
  if (voice.speaking) return "SPEAKING";
  if (voice.listening) return "LISTENING";
  if (voice.transcribing) return "PROCESSING";
  return backend;
}

export interface StateCopy {
  label: string;
  sentence: string;
}

/** Plain words for every state (the label beside the orb and what screen readers announce). */
export function describe(state: OrbState, assistant: AssistantSnapshot | null): StateCopy {
  const tool = assistant?.tool ? friendlyTool(assistant.tool) : "";
  const agents = assistant?.background.agents ?? 0;
  switch (state) {
    case "DISCONNECTED": return { label: "Not connected", sentence: "XENIO's backend is not reachable right now." };
    case "CONNECTING": return { label: "Connecting", sentence: "Connecting to XENIO." };
    case "INITIALIZING": return { label: "Starting", sentence: "Starting Hermes, XENIO's reasoning engine." };
    case "IDLE": return { label: "Ready", sentence: "Ready when you are." };
    case "LISTENING": return { label: "Listening", sentence: "Listening. Stop talking and XENIO takes it from there." };
    case "PROCESSING": return { label: "Working on it", sentence: "Working on your request." };
    case "THINKING": return { label: "Thinking", sentence: "Thinking it through." };
    case "SEARCHING": return { label: "Looking it up", sentence: tool ? `Looking it up: ${tool}.` : "Looking it up." };
    case "PLANNING": return { label: "Planning", sentence: "Planning the work with the engineering team." };
    case "ORCHESTRATING": return { label: "Coordinating", sentence: `Coordinating ${agents} agents working in parallel.` };
    case "EXECUTING": return { label: "Acting", sentence: tool ? `Acting: ${tool}.` : agents ? "An agent is working." : "Carrying it out." };
    case "SPEAKING": return { label: "Speaking", sentence: "Speaking the answer." };
    case "WAITING_APPROVAL": return { label: "Needs your decision", sentence: "An action is waiting for your yes or no." };
    case "PAUSED": return { label: "Paused", sentence: "Paused. Nothing new starts until you resume." };
    case "COMPLETED": return { label: "Done", sentence: "Done." };
    case "ERROR": return { label: "Something went wrong", sentence: assistant?.error ?? "The last request failed." };
  }
}

const TOOL_WORDS: Record<string, string> = {
  web_search: "searching the web", web_extract: "reading a web page", read_file: "reading a file",
  search_files: "searching files", xenio_list_directory: "listing a folder", xenio_email_search: "searching your mail",
  xenio_email_read: "reading an email", xenio_calendar_events: "checking your calendar", xenio_look_at_screen: "looking at your screen",
  xenio_find_ui: "reading the window", xenio_open_app: "opening an app", xenio_gui: "using the keyboard and mouse",
  xenio_media: "controlling music", terminal: "running a command", xenio_engineering: "the engineering team",
  xenio_open_url: "opening a page", xenio_download_file: "downloading a file", patch: "editing a file", write_file: "writing a file",
};

export function friendlyTool(name: string): string {
  return TOOL_WORDS[name] ?? name.replace(/^xenio_/, "").replace(/_/g, " ");
}
