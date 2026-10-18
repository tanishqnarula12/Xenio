import { useMemo, useState } from "react";
import type { HealthCheck, ToolInfo } from "../api/types";
import { Empty, Loaded, PageHeader, Section, Status } from "../components/ui";
import { useApp } from "../state/context";
import { useResource } from "../state/useResource";

// Which --health check decides whether a group of tools can work right now.
const GROUP_CHECK: Record<string, string[]> = {
  Files: ["Safety"], Web: ["Web"], Personal: ["Google", "Email", "Tasks"], Vision: ["Vision"], Desktop: ["Desktop"],
  Media: ["Fast path"], Engineering: ["Engineering", "Engineer"], Hermes: ["Hermes"],
};

const LEVEL: Record<string, string> = {
  READ: "Reads only", SAFE_WRITE: "Changes things, no question", CONFIRM_REQUIRED: "Asks you first", HIGH_RISK: "Asks you to type confirm",
};

export function availability(group: string, health: Record<string, HealthCheck>, platform: string): { tone: "ok" | "attention" | "danger" | "neutral"; label: string; detail?: string } {
  if (group === "Desktop" && !["darwin", "win32"].includes(platform)) return { tone: "neutral", label: "Not supported on this platform" };
  const checks = (GROUP_CHECK[group] ?? []).map((n) => health[n]).filter((c): c is HealthCheck => Boolean(c));
  if (!checks.length) return { tone: "neutral", label: "Not checked" };
  const worst = checks.find((c) => c.status === "FAIL") ?? checks.find((c) => c.status === "WARN") ?? checks[0]!;
  if (worst.status === "OK") return { tone: "ok", label: "Available", detail: worst.detail };
  const permission = /permission|accessibility|screen access|automation|not trusted/i.test(worst.detail);
  return worst.status === "WARN"
    ? { tone: "attention", label: permission ? "Needs a macOS permission" : "Needs setup", detail: worst.detail }
    : { tone: "danger", label: permission ? "Permission denied" : "Not available", detail: worst.detail };
}

export function ToolsPage() {
  const { app } = useApp();
  const data = useResource((s) => app.api.tools(s), ["health"]);
  const platform = useResourcePlatform();
  const [query, setQuery] = useState("");
  return (
    <>
      <PageHeader title="Skills & tools" intro="What XENIO can really do on this Mac right now, and how careful it is with each. Every call goes through XENIO's safety rules; this page cannot change them." />
      <Loaded resource={data} what="tools">
        {(d) => <ToolGroups tools={d.tools} health={d.health} platform={platform} skills={d.skills} query={query} setQuery={setQuery} />}
      </Loaded>
    </>
  );
}

function useResourcePlatform(): string {
  const { store } = useApp();
  return store.get().snapshot?.platform ?? "";
}

function ToolGroups({ tools, health, platform, skills, query, setQuery }: {
  tools: ToolInfo[]; health: Record<string, HealthCheck>; platform: string; skills: { name: string; category: string; description: string }[];
  query: string; setQuery: (q: string) => void;
}) {
  const groups = useMemo(() => {
    const out = new Map<string, ToolInfo[]>();
    tools.forEach((t) => out.set(t.group, [...(out.get(t.group) ?? []), t]));
    return [...out.entries()];
  }, [tools]);
  const shownSkills = skills.filter((s) => !query || `${s.name} ${s.description} ${s.category}`.toLowerCase().includes(query.toLowerCase()));
  return (
    <>
      {groups.map(([group, items]) => {
        const a = availability(group, health, platform);
        return (
          <Section key={group} title={group === "Hermes" ? "Hermes' own tools" : group} id={`tools-${group}`}
            action={<Status tone={a.tone}>{a.label}</Status>}>
            {a.detail && a.tone !== "ok" && <p className="meta">{a.detail}</p>}
            <table className="table">
              <caption className="sr-only">{group} tools</caption>
              <thead><tr><th scope="col">Tool</th><th scope="col">What it does</th><th scope="col">Care</th></tr></thead>
              <tbody>
                {items.map((t) => (
                  <tr key={t.name}><td><code>{t.name}</code></td><td>{t.description}</td><td>{LEVEL[t.level] ?? t.level}</td></tr>
                ))}
              </tbody>
            </table>
          </Section>
        );
      })}
      <Section title="Hermes skills" id="skills" action={
        <input type="search" className="search" placeholder="Search skills" aria-label="Search skills" value={query} onChange={(e) => setQuery(e.target.value)} />
      }>
        {shownSkills.length ? (
          <ul className="rows">
            {shownSkills.slice(0, 200).map((s) => (
              <li key={`${s.category}/${s.name}`} className="row"><span className="row-main"><strong>{s.name}</strong> {s.description}</span><span className="meta">{s.category}</span></li>
            ))}
          </ul>
        ) : <Empty>{skills.length ? "No skill matches that search." : "No Hermes skills are installed."}</Empty>}
      </Section>
    </>
  );
}
