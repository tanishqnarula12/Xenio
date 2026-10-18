import { Bell, Bot, HeartPulse, MessageSquare, ShieldCheck, TriangleAlert, type LucideIcon } from "lucide-react";
import { useState } from "react";
import { Empty, Loaded, PageHeader, Time } from "../components/ui";
import { useApp } from "../state/context";
import { useResource } from "../state/useResource";
import { activityLine } from "./activityText";

type Filter = "all" | "requests" | "engineering" | "approvals" | "scheduled" | "system";

const KIND: Record<string, [Filter, LucideIcon]> = {
  "request.received": ["requests", MessageSquare], "assistant.error": ["system", TriangleAlert],
  "engineering.milestone": ["engineering", Bot], "engineering.run": ["engineering", Bot],
  "approval.requested": ["approvals", ShieldCheck], "approval.settled": ["approvals", ShieldCheck],
  notification: ["scheduled", Bell], "schedule.result": ["scheduled", Bell], "health.changed": ["system", HeartPulse],
};

export function ActivityPage() {
  const { app } = useApp();
  const [filter, setFilter] = useState<Filter>("all");
  const activity = useResource((s) => app.api.activity(300, s), ["activity"]);
  const filters: [Filter, string][] = [["all", "All"], ["requests", "Requests"], ["engineering", "Engineering"],
    ["approvals", "Approvals"], ["scheduled", "Scheduled checks"], ["system", "System"]];
  return (
    <>
      <PageHeader title="Activity" intro="What XENIO did, as it happened, kept across restarts. What you typed or said is never stored here." />
      <div className="segmented" role="radiogroup" aria-label="Show">
        {filters.map(([id, label]) => (
          <button key={id} type="button" role="radio" aria-checked={filter === id} className={filter === id ? "is-on" : ""}
            onClick={() => setFilter(id)}>{label}</button>
        ))}
      </div>
      <Loaded resource={activity} what="activity">
        {(data) => {
          const shown = data.events.filter((e) => filter === "all" || KIND[e.type]?.[0] === filter);
          if (!shown.length) return <Empty>{filter === "all" ? "Nothing has happened yet." : "Nothing of this kind yet."}</Empty>;
          return (
            <ol className="timeline">
              {shown.map((e) => {
                const Icon = KIND[e.type]?.[1] ?? Bell;
                return (
                  <li key={e.id} className="timeline__item">
                    <Icon size={16} aria-hidden="true" className="timeline__icon" />
                    <p className="timeline__text">{activityLine(e)}</p>
                    <Time iso={e.at} />
                  </li>
                );
              })}
            </ol>
          );
        }}
      </Loaded>
    </>
  );
}
