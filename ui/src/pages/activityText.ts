import type { XenioEvent } from "../api/types";

/** One plain sentence per event. Requests are shown by size only: what was typed or said is never stored. */
export function activityLine(event: XenioEvent): string {
  const d = event.data as Record<string, unknown>;
  switch (event.type) {
    case "request.received": return `A ${d.via === "voice" ? "spoken" : "typed"} request (${d.chars ?? "?"} characters)`;
    case "assistant.error": return `A request failed: ${String(d.error ?? "unknown error")}`;
    case "engineering.milestone": return `${String(d.title ?? "Engineering")}: ${String(d.message ?? "")}`;
    case "engineering.run": return `Engineering run ${String(d.id ?? "")} is ${String(d.status ?? "")}: ${String(d.request ?? "")}`;
    case "approval.requested": return `XENIO asked to ${String(d.description ?? "do something")}`;
    case "approval.settled": return `${d.status === "approved" ? "Allowed" : d.status === "denied" ? "Declined" : "Not answered in time"}: ${String(d.description ?? "")}`;
    case "notification": return `${String(d.title ?? "Notification")}: ${String(d.message ?? "")}`;
    case "health.changed": return `${String(d.name ?? "A part")} changed from ${String(d.from ?? "?")} to ${String(d.to ?? "?")}`;
    default: return event.type;
  }
}
