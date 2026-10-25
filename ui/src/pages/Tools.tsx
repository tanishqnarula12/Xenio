import { useMemo, useState } from "react";
import type { HealthCheck, SkillInfo, ToolInfo } from "../api/types";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { Empty, ErrorNote, Loaded, PageHeader, Section, Status, Time } from "../components/ui";
import { useApp } from "../state/context";
import { href } from "../state/router";
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

function Recent({ tool }: { tool: ToolInfo }) {
  const r = tool.recent;
  if (!r || !r.calls) return <span className="meta">not used lately</span>;
  if (!r.failed && !r.blocked) return <span className="meta tabular">{r.calls} recent call{r.calls > 1 ? "s" : ""}, no problems</span>;
  return (
    <span>
      <Status tone={r.failed ? "danger" : "attention"}>{[r.failed && `${r.failed} failed`, r.blocked && `${r.blocked} blocked`].filter(Boolean).join(", ")}</Status>
      {r.last_problem && <span className="meta"> · last <Time iso={r.last_problem.at} />{r.last_problem.detail ? `: ${r.last_problem.detail}` : ""}</span>}
    </span>
  );
}

function SkillSwitch({ skill }: { skill: SkillInfo }) {
  const { app } = useApp();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState(false);
  const enabled = skill.enabled !== false;
  const toggle = async () => {
    setConfirm(false);
    setBusy(true);
    setError(null);
    try {
      await app.api.setSkill(skill.name, !enabled);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <span className="skill-switch">
      <label className="switch" title={skill.essential ? "Essential to Hermes: always on" : undefined}>
        <input type="checkbox" role="switch" checked={enabled} disabled={busy || skill.essential}
          aria-label={`${skill.name}: ${enabled ? "on" : "off"}`}
          onChange={() => (enabled ? setConfirm(true) : void toggle())} />
        <span className="switch__track" aria-hidden="true" />
        <span className="switch__label">{skill.essential ? "Always on" : enabled ? "On" : "Off"}</span>
      </label>
      {error && <ErrorNote message={error} />}
      {confirm && (
        <ConfirmDialog title={`Switch off ${skill.name}?`} confirmLabel="Switch off" onCancel={() => setConfirm(false)}
          onConfirm={() => void toggle()}
          body="Hermes stops offering this skill in every conversation (its files stay where they are, and you can switch it on again here). XENIO backs up Hermes' settings first." />
      )}
    </span>
  );
}

function Skills({ skills }: { skills: SkillInfo[] }) {
  const [query, setQuery] = useState("");
  const [only, setOnly] = useState<"all" | "setup" | "off" | "recent">("all");
  const recentCutoff = Date.now() - 7 * 86_400_000;
  const shown = skills.filter((s) => (!query || `${s.name} ${s.description} ${s.category}`.toLowerCase().includes(query.toLowerCase()))
    && (only === "all" || (only === "setup" && s.available === false) || (only === "off" && s.enabled === false)
      || (only === "recent" && Boolean(s.changed) && Date.parse(s.changed!) > recentCutoff)));
  const counts = { off: skills.filter((s) => s.enabled === false).length, setup: skills.filter((s) => s.available === false).length };
  return (
    <Section title="Hermes skills" id="skills" action={
      <input type="search" className="search" placeholder="Search skills" aria-label="Search skills" value={query} onChange={(e) => setQuery(e.target.value)} />
    }>
      <p className="meta">Skills are instructions Hermes can follow; a script inside one only runs through a tool call that XENIO checks.
        Adding a script to a skill, or deleting one, asks you first. Installing skills is done with <code>hermes skills</code> in a terminal,
        where you see what you install.</p>
      <div className="segmented" role="radiogroup" aria-label="Show skills">
        {([["all", `All (${skills.length})`], ["setup", `Need setup (${counts.setup})`], ["off", `Off (${counts.off})`],
          ["recent", "Changed this week"]] as const).map(([id, label]) => (
          <button key={id} type="button" role="radio" aria-checked={only === id} className={only === id ? "is-on" : ""} onClick={() => setOnly(id)}>{label}</button>
        ))}
      </div>
      {shown.length ? (
        <ul className="skills">
          {shown.slice(0, 200).map((s) => (
            <li key={`${s.category}/${s.name}`} className={`skill ${s.enabled === false ? "skill--off" : ""}`}>
              <div className="skill__main">
                <p className="skill__name"><strong>{s.name}</strong>{s.version && <span className="meta"> v{s.version}</span>}
                  <span className="meta"> · {s.category}</span></p>
                <p>{s.description}</p>
                <p className="meta">
                  {s.available === false ? <Status tone="attention">{s.availability ?? "needs setup"}</Status> : <Status tone="ok">Ready</Status>}
                  {!!s.scripts?.length && <> · includes {s.scripts.length} script{s.scripts.length > 1 ? "s" : ""}</>}
                  {s.changed && <> · changed <Time iso={s.changed} /></>}
                  {s.author && <> · by {s.author}</>}
                </p>
              </div>
              <SkillSwitch skill={s} />
            </li>
          ))}
        </ul>
      ) : <Empty>{skills.length ? "No skill matches." : "No Hermes skills are installed."}</Empty>}
    </Section>
  );
}

function ToolGroups({ tools, health, platform }: { tools: ToolInfo[]; health: Record<string, HealthCheck>; platform: string }) {
  const groups = useMemo(() => {
    const out = new Map<string, ToolInfo[]>();
    tools.forEach((t) => out.set(t.group, [...(out.get(t.group) ?? []), t]));
    return [...out.entries()];
  }, [tools]);
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
              <thead><tr><th scope="col">Tool</th><th scope="col">What it does</th><th scope="col">Care</th><th scope="col">Lately</th></tr></thead>
              <tbody>
                {items.map((t) => (
                  <tr key={t.name}><td><code>{t.name}</code></td><td>{t.description}</td><td>{LEVEL[t.level] ?? t.level}</td>
                    <td><Recent tool={t} /></td></tr>
                ))}
              </tbody>
            </table>
          </Section>
        );
      })}
    </>
  );
}

export function ToolsPage() {
  const { app, store } = useApp();
  const data = useResource((s) => app.api.tools(s), ["health", "skills"]);
  const platform = store.get().snapshot?.platform ?? "";
  return (
    <>
      <PageHeader title="Skills & tools" intro={<>What XENIO can really do on this Mac right now, how careful it is with each, and what went wrong lately
        (from the guard's record, also in <a className="link" href={href("activity")}>Activity</a>). Every call goes through XENIO's safety rules; this page cannot change them.</>} />
      <Loaded resource={data} what="tools">
        {(d) => (
          <>
            <Skills skills={d.skills} />
            <ToolGroups tools={d.tools} health={d.health} platform={platform} />
          </>
        )}
      </Loaded>
    </>
  );
}
