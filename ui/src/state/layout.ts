// How this viewer arranged the screen: the sidebar (full or an icon rail), Home's side panel (shown, hidden, its
// width), the orb's size and which Home sections are minimised. Kept in this browser's localStorage when it can be;
// the page works the same without it (private windows, blocked storage). Nothing here is sent to XENIO.

import { useSyncExternalStore } from "react";

export interface Layout {
  sidebar: "open" | "rail";
  side: boolean;
  sideWidth: number;
  orbStep: number;
  folded: string[];
}

/** the orb's size as a share of the room it has; ORB_STEPS[DEFAULT_ORB_STEP] is the default */
export const ORB_STEPS = [0.5, 0.65, 0.8, 0.92, 1] as const;
export const DEFAULT_ORB_STEP = 3;
export const SIDE_MIN = 280;
export const SIDE_MAX = 560;
const KEY = "xenio.layout";
const DEFAULTS: Layout = {
  sidebar: "open", side: true, sideWidth: 340, orbStep: DEFAULT_ORB_STEP,
  folded: ["home-schedules", "home-activity", "home-system"],
};

function clean(raw: Partial<Layout>): Layout {
  const l = { ...DEFAULTS, ...raw };
  return {
    sidebar: l.sidebar === "rail" ? "rail" : "open",
    side: l.side !== false,
    sideWidth: Math.round(Math.min(SIDE_MAX, Math.max(SIDE_MIN, Number(l.sideWidth) || DEFAULTS.sideWidth))),
    orbStep: Math.min(ORB_STEPS.length - 1, Math.max(0, Math.round(Number(l.orbStep) || 0))),
    folded: Array.isArray(l.folded) ? l.folded.filter((f): f is string => typeof f === "string").slice(0, 20) : DEFAULTS.folded,
  };
}

function read(): Layout {
  try {
    const raw: unknown = JSON.parse(localStorage.getItem(KEY) ?? "null");
    if (raw && typeof raw === "object") return clean(raw as Partial<Layout>);
  } catch {
    /* storage unavailable or damaged: defaults */
  }
  return clean({});
}

let current = read();
const listeners = new Set<() => void>();

export function getLayout(): Layout {
  return current;
}

export function setLayout(patch: Partial<Layout> | ((l: Layout) => Partial<Layout>)): void {
  current = clean({ ...current, ...(typeof patch === "function" ? patch(current) : patch) });
  try {
    localStorage.setItem(KEY, JSON.stringify(current));
  } catch {
    /* kept for this page only */
  }
  listeners.forEach((l) => l());
}

export function toggleFolded(id: string): void {
  setLayout((l) => ({ folded: l.folded.includes(id) ? l.folded.filter((f) => f !== id) : [...l.folded, id] }));
}

/** re-read storage (tests, or another tab changed it) */
export function reloadLayout(): void {
  current = read();
  listeners.forEach((l) => l());
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useLayout(): Layout {
  return useSyncExternalStore(subscribe, getLayout, getLayout);
}
