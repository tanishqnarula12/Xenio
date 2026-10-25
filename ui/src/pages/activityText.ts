import type { XenioEvent } from "../api/types";

const MEMORY_STORE: Record<string, string> = { user: "about you", memory: "XENIO's notes" };

/** One plain sentence per event. Requests are shown by size only: what was typed or said is never stored. */
export function activityLine(event: XenioEvent): string {
  const d = event.data as Record<string, unknown>;
  switch (event.type) {
    case "request.received":
      if (d.via === "mission-control") return `An engineering request from Mission Control (${d.chars ?? "?"} characters)`;
      return `A ${d.via === "voice" ? "spoken" : "typed"} request (${d.chars ?? "?"} characters)`;
    case "assistant.error": return `A request failed: ${String(d.error ?? "unknown error")}`;
    case "engineering.milestone": return `${String(d.title ?? "Engineering")}: ${String(d.message ?? "")}`;
    case "engineering.run": return `Engineering run ${String(d.id ?? "")} is ${String(d.status ?? "")}: ${String(d.request ?? "")}`;
    case "approval.requested": return `XENIO asked to ${String(d.description ?? "do something")}`;
    case "approval.settled": {
      const outcome = d.status === "approved" ? "Allowed once" : d.status === "approved for the task" ? "Allowed for the task"
        : d.status === "denied" ? "Declined" : "Not answered in time (not allowed)";
      return `${outcome}: ${String(d.description ?? "")}`;
    }
    case "notification": return `${String(d.title ?? "Notification")}: ${String(d.message ?? "")}`;
    case "health.changed": return `${String(d.name ?? "A part")} changed from ${String(d.from ?? "?")} to ${String(d.to ?? "?")}`;
    case "task.changed": {
      const verb = { created: "added", complete: "marked done", reopen: "reopened", deleted: "deleted" }[String(d.change)] ?? String(d.change);
      return `Task #${String(d.id ?? "?")} ${verb}`;
    }
    case "memory.changed":
      return `Memory (${MEMORY_STORE[String(d.store)] ?? String(d.store)}): an entry was ${String(d.action ?? "changed")} by you`;
    case "skill.changed": return `The skill ${String(d.name ?? "?")} was switched ${d.enabled ? "on" : "off"}`;
    case "settings.changed": {
      const keys = Array.isArray(d.changed) ? (d.changed as string[]).map((k) => k.replace(/_/g, " ")) : [];
      return keys.length ? `Settings changed: ${keys.join(", ")}` : "Settings changed";
    }
    case "system.event": return String(d.what ?? "XENIO");
    default: return event.type;
  }
}

/** How the Activity page marks an event that went wrong or was recovered from (never colour alone). */
export function activityTone(event: XenioEvent): "danger" | "attention" | "ok" | null {
  const d = event.data as Record<string, unknown>;
  if (event.type === "assistant.error" || (event.type === "health.changed" && d.to === "FAIL")) return "danger";
  if (event.type === "system.event") return d.tone === "problem" ? "danger" : d.tone === "recovery" ? "ok" : null;
  if (event.type === "approval.settled" && d.status === "expired") return "attention";
  if (event.type === "engineering.run" && d.status === "failed") return "danger";
  return null;
}
