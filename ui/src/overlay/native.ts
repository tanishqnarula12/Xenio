// The bridge to XENIO Orb (native/orb/main.swift): the page tells the floating panel what to do (show, size, hide,
// drag, take the keyboard for the quick chat, open Mission Control, the shortcut's settings); the helper tells the
// page what happened (a summon, Command-W in the chat, its status, the Mac's location). Outside the helper (a normal
// browser tab, tests) every call is a no-op.

export type NativeMessage =
  | { type: "summon"; via: string; at: number; mode?: "chat" | "voice" }  // mode: XENIO asked for one way to talk
  | { type: "closeChat" }
  | { type: "status"; shortcut: string; shortcut_status: string; accessibility: boolean; screens: number; location?: string }
  | { type: "location"; lat: number; lon: number; accuracy_m: number; label: string }
  | { type: "moved"; dx: number; dy: number }  // the panel is being dragged: how far since the last move (page pixels, y down)
  | { type: "hover"; on: boolean }
  | { type: "app"; away: boolean }
  | { type: "music"; on: boolean; error?: string }  // music mode started or stopped (and why it could not)
  | { type: "musicLevel"; level: number; bass: number; beat: boolean };  // what the Mac is playing: loudness, bass, a beat  // XENIO's window is minimized, closed or hidden (away), or back  // the pointer is over the panel (the page cannot always tell: the helper is never active)

type Outgoing =
  | { type: "ready" }
  | { type: "show" | "size"; width: number; height: number }
  | { type: "hide" }
  | { type: "chat"; on: boolean }  // the quick chat is open: the panel takes the keyboard (without activating the helper)
  | { type: "openApp"; route?: string }  // Mission Control: XENIO's full page in a real window (at a page: "/orchestration/ID")
  | { type: "dragStart" }
  | { type: "config"; shortcut: string; interval_ms: number; location: boolean }
  | { type: "open"; url: string }
  | { type: "surface"; glass: boolean }
  | { type: "music"; on: boolean }  // music mode: the orb moves with what the Mac is playing  // glass: no card, the orb floats; otherwise the dark card the chat sits on
  | { type: "glass"; shapes: { x: number; y: number; w: number; h: number; r: number; style?: "regular" | "clear" | "tinted" }[] };  // real Liquid Glass behind these

interface WebkitBridge {
  messageHandlers?: { xenio?: { postMessage: (message: Outgoing) => void } };
}

declare global {
  interface Window {
    webkit?: WebkitBridge;
    xenioNative?: { receive: (message: NativeMessage) => void };
  }
}

export function inHelper(): boolean {
  return Boolean(window.webkit?.messageHandlers?.xenio);
}

export function post(message: Outgoing): void {
  window.webkit?.messageHandlers?.xenio?.postMessage(message);
}

export function listen(handler: (message: NativeMessage) => void): () => void {
  window.xenioNative = { receive: handler };
  return () => {
    if (window.xenioNative?.receive === handler) delete window.xenioNative;
  };
}
