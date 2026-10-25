import { Bell, Bot, HeartPulse, ListChecks, MessageSquare, Power, ShieldCheck, TriangleAlert, Wrench, type LucideIcon } from "lucide-react";
import { useEffect, useState } from "react";
import type { ActivityKind } from "../api/client";
import type { ToolCall, XenioEvent } from "../api/types";
import { Button, Empty, ErrorNote, Loaded, PageHeader, Status, Time, type Tone } from "../components/ui";
import { useApp } from "../state/context";
import { useResource } from "../state/useResource";
import { activityLine, activityTone } from "./activityText";

const PAGE = 100;

const ICON: Record<string, LucideIcon> = {
  "request.received": MessageSquare, "assistant.error": TriangleAlert, "engineering.milestone": Bot, "engineering.run": Bot,
  "approval.requested": ShieldCheck, "approval.settled": ShieldCheck, notification: Bell, "schedule.result": Bell,
  "health.changed": HeartPulse, "task.changed": ListChecks, "memory.changed": ListChecks, "skill.changed": Wrench,
  "settings.changed": Wrench, "system.event": Power,
};

const FILTERS: [ActivityKind, string][] = [["all", "All"], ["requests", "Requests"], ["engineering", "Engineering"],
  ["approvals", "Approvals"], ["scheduled", "Reminders & checks"], ["changes", "Changes"], ["system", "System"]];

function Segmented<T extends string>({ label, options, value, onChange }: {
  label: string; options: [T, string][]; value: T; onChange: (v: T) => void;
}) {
  return (
    <div className="segmented" role="radiogroup" aria-label={label}>
      {options.map(([id, text]) => (
        <button key={id} type="button" role="radio" aria-checked={value === id} className={value === id ? "is-on" : ""}
          onClick={() => onChange(id)}>{text}</button>
      ))}
    </div>
  );
}

function EventItem({ event }: { event: XenioEvent }) {
  const Icon = ICON[event.type] ?? Bell;
  const tone = activityTone(event);
  return (
    <li className="timeline__item">
      <Icon size={16} aria-hidden="true" className="timeline__icon" />
      <p className="timeline__text">
        {tone && <><Status tone={tone}>{tone === "danger" ? "Problem" : tone === "ok" ? "Recovered" : "Note"}</Status>{" "}</>}
        {activityLine(event)}
      </p>
      <Time iso={event.at} />
    </li>
  );
}

function Events() {
  const { app } = useApp();
  const [kind, setKind] = useState<ActivityKind>("all");
  const first = useResource((s) => app.api.activity(PAGE, s, 0, kind), ["activity"], [kind]);
  const [older, setOlder] = useState<XenioEvent[]>([]);
  const [next, setNext] = useState<{ offset: number; more: boolean } | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreError, setMoreError] = useState<string | null>(null);
  useEffect(() => { setOlder([]); setNext(null); setMoreError(null); }, [kind]);

  const more = next ?? (first.data ? { offset: first.data.next_offset ?? PAGE, more: Boolean(first.data.more) } : null);
  const loadOlder = async () => {
    if (!more) return;
    setLoadingMore(true);
    setMoreError(null);
    try {
      const page = await app.api.activity(PAGE, undefined, more.offset, kind);
      setOlder((o) => [...o, ...page.events]);
      setNext({ offset: page.next_offset ?? more.offset + PAGE, more: Boolean(page.more) });
    } catch (e) {
      setMoreError((e as Error).message);
    } finally {
      setLoadingMore(false);
    }
  };

  return (
    <>
      <Segmented label="Show" options={FILTERS} value={kind} onChange={setKind} />
      <Loaded resource={first} what="activity">
        {(data) => {
          const seen = new Set<string>();
          const shown = [...data.events, ...older].filter((e) => !seen.has(e.id) && seen.add(e.id));  // a page can shift as new events arrive
          if (!shown.length) return <Empty>{kind === "all" ? "Nothing has happened yet." : "Nothing of this kind yet."}</Empty>;
          return (
            <>
              <ol className="timeline">{shown.map((e) => <EventItem key={e.id} event={e} />)}</ol>
              {moreError && <ErrorNote message={`Older activity could not be loaded: ${moreError}`} retry={() => void loadOlder()} />}
              {more?.more && (
                <div className="list-more">
                  <Button variant="quiet" disabled={loadingMore} onClick={() => void loadOlder()}>
                    {loadingMore ? "Loading…" : "Show older activity"}
                  </Button>
                </div>
              )}
            </>
          );
        }}
      </Loaded>
    </>
  );
}

const CALL: Record<ToolCall["status"], [Tone, string]> = {
  requested: ["neutral", "Requested"], allowed: ["neutral", "Allowed to run"], blocked: ["danger", "Blocked by XENIO"],
  asked: ["attention", "Waiting for you"], approved: ["ok", "You allowed it"], denied: ["neutral", "You declined"],
  expired: ["neutral", "Not answered (not run)"], running: ["running", "Running"], completed: ["ok", "Completed"],
  failed: ["danger", "Failed"],
};
const PROBLEMS = new Set<ToolCall["status"]>(["blocked", "failed", "denied", "expired"]);
const LEVEL: Record<string, string> = { READ: "Reads", SAFE_WRITE: "Changes", CONFIRM_REQUIRED: "Asks first", HIGH_RISK: "High risk" };

function ToolCalls() {
  const { app } = useApp();
  const [only, setOnly] = useState<"all" | "problems">("all");
  const calls = useResource((s) => app.api.audit(300, s), ["activity"]);
  return (
    <>
      <Segmented label="Show calls" options={[["all", "All calls"], ["problems", "Blocked, failed or declined"]]} value={only} onChange={setOnly} />
      <p className="meta">Every tool call XENIO's guard checked, newest first. Hermes' own tools (terminal, files, browser) do not
        report their result to the guard: "Allowed to run" is not proof that they worked.</p>
      <Loaded resource={calls} what="tool calls">
        {(data) => {
          const shown = data.calls.filter((c) => only === "all" || PROBLEMS.has(c.status));
          if (!shown.length) return <Empty>{only === "all" ? "No tool calls recorded yet." : "Nothing was blocked, failed or declined."}</Empty>;
          return (
            <table className="table table--calls">
              <caption className="sr-only">Tool calls</caption>
              <thead><tr><th scope="col">When</th><th scope="col">Tool</th><th scope="col">What</th><th scope="col">Care</th>
                <th scope="col">Outcome</th></tr></thead>
              <tbody>
                {shown.map((c, i) => {
                  const [tone, label] = CALL[c.status] ?? (["neutral", c.status] as [Tone, string]);
                  return (
                    <tr key={`${c.at}-${c.tool}-${i}`}>
                      <td><Time iso={c.at} /></td>
                      <td><code>{c.tool}</code></td>
                      <td>{c.summary || "—"}{c.detail && <p className="meta">{c.detail}</p>}</td>
                      <td>{c.level ? LEVEL[c.level] ?? c.level : "—"}</td>
                      <td><Status tone={tone}>{label}</Status>{c.ms != null && <span className="meta tabular"> {(c.ms / 1000).toFixed(1)} s</span>}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          );
        }}
      </Loaded>
    </>
  );
}

export function ActivityPage() {
  const [view, setView] = useState<"events" | "calls">("events");
  return (
    <>
      <PageHeader title="Activity" intro="What XENIO did, as it happened, kept across restarts (Settings decides for how long). What you typed or said is never stored here."
        actions={<Segmented label="View" options={[["events", "Events"], ["calls", "Tool calls"]]} value={view} onChange={setView} />} />
      {view === "events" ? <Events /> : <ToolCalls />}
    </>
  );
}
