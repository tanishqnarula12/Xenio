import { useRef, useState } from "react";
import type { RunDetail, RunSummary } from "../api/types";
import { DependencyGraph } from "../components/DependencyGraph";
import { RichText } from "../components/RichText";
import { Button, Empty, ErrorNote, Loaded, PageHeader, Section, Status, StatusOf, Time } from "../components/ui";
import { arrive, useGsap } from "../motion/motion";
import { useApp } from "../state/context";
import { href } from "../state/router";
import { useResource } from "../state/useResource";

const ACTIVE = ["planning", "planned", "running", "verifying", "reviewing"];

/** Progress from real task states only: "2 of 4 tasks done", never an invented percentage. */
export function milestones(tasks: RunDetail["tasks"] | undefined): string {
  if (!tasks?.length) return "No plan yet";
  const n = (s: string) => tasks.filter((t) => t.status === s).length;
  const parts = [`${n("succeeded")} of ${tasks.length} tasks done`];
  if (n("running")) parts.push(`${n("running")} running`);
  if (n("blocked")) parts.push(`${n("blocked")} blocked`);
  if (n("failed")) parts.push(`${n("failed")} failed`);
  return parts.join(" · ");
}

function RunList({ runs, current }: { runs: RunSummary[]; current: string | null }) {
  return (
    <ul className="run-list" aria-label="Engineering runs">
      {runs.map((r) => (
        <li key={r.id}>
          <a href={href("orchestration", r.id)} className={`run-item ${r.id === current ? "run-item--current" : ""}`}
            aria-current={r.id === current ? "true" : undefined}>
            <span className="run-item__request">{r.request}</span>
            <span className="run-item__meta"><StatusOf value={r.status} /><Time iso={r.created} /></span>
            {!r.live && <span className="meta">From before XENIO restarted</span>}
          </a>
        </li>
      ))}
    </ul>
  );
}

function Verification({ run }: { run: RunDetail }) {
  const tests = run.final_tests[0];
  const base = run.baseline_tests[0];
  const verdict = run.status !== "done" ? null : run.verified ? <Status tone="ok">Verified by XENIO's own checks</Status>
    : <Status tone="attention">Finished, but not verified</Status>;
  return (
    <Section title="Verification" id="run-verify">
      {verdict && <p>{verdict}</p>}
      {tests ? (
        <p className="tabular">Tests run by XENIO{tests.sandboxed ? " in a sandbox" : ""}: {tests.passed} passed, {tests.failed} failed
          {tests.errors ? `, ${tests.errors} errors` : ""}{base ? ` (before: ${base.passed} passed, ${base.failed} failed)` : ""}.
          {tests.detail ? ` ${tests.detail}` : ""}</p>
      ) : <p className="meta">No tests have been run for this work yet.</p>}
      {!!run.scope_violations?.length && <ErrorNote message={`Changed files outside its scope: ${run.scope_violations.join(", ")}`} />}
      {!!run.test_tampering?.length && <ErrorNote message={`Tests were weakened or removed: ${run.test_tampering.join(", ")}`} />}
      {!!run.security_findings?.length && (
        <ul className="rows">
          {run.security_findings.map((f, i) => (
            <li key={i} className="row"><Status tone="danger">Security</Status>
              <span className="row-main">{f.message ?? f.rule} {f.file ? `(${f.file}${f.line ? `:${f.line}` : ""})` : ""}</span></li>
          ))}
        </ul>
      )}
    </Section>
  );
}

function RunActions({ run }: { run: RunDetail }) {
  const { app } = useApp();
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<{ tone: "ok" | "danger"; text: string } | null>(null);
  const act = async (action: "approve" | "cancel" | "apply" | "push" | "pull_request") => {
    setBusy(action);
    setMessage(null);
    try {
      const out = await app.api.runAction(run.id, action);
      setMessage({ tone: "ok", text: String((out as { note?: string }).note ?? `${action.replace("_", " ")}: done`) });
    } catch (e) {
      setMessage({ tone: "danger", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(null);
    }
  };
  const live = run.live;
  return (
    <div className="run-actions">
      {live && run.status === "planned" && <Button variant="primary" disabled={!!busy} onClick={() => void act("approve")}>Start the planned work</Button>}
      {live && ACTIVE.includes(run.status) && <Button variant="danger" disabled={!!busy} onClick={() => void act("cancel")}>Cancel the run</Button>}
      {live && run.status === "done" && run.mode === "change" && (
        <>
          <Button disabled={!!busy} onClick={() => void act("apply")}>Write the changes into my files…</Button>
          <Button disabled={!!busy} onClick={() => void act("push")}>Push the branch…</Button>
          <Button disabled={!!busy} onClick={() => void act("pull_request")}>Open a pull request…</Button>
          <p className="meta">Each of these asks you first, in Approvals.</p>
        </>
      )}
      {busy && <p className="meta" role="status">Working: {busy.replace("_", " ")}… {["apply", "push", "pull_request"].includes(busy) ? "Answer the question in Approvals." : ""}</p>}
      {message && <p className={message.tone === "ok" ? "ok-text" : "error-text"} role="status">{message.text}</p>}
    </div>
  );
}

function RunView({ id }: { id: string }) {
  const { app } = useApp();
  const run = useResource((s) => app.api.run(id, s), ["runs"], [id]);
  const [report, setReport] = useState<string | null>(null);
  const [reportError, setReportError] = useState<string | null>(null);
  const root = useRef<HTMLDivElement>(null);
  useGsap(root, (r) => { arrive(".section", r, 0.04); }, [id]);
  return (
    <div ref={root} className="run-view">
      <Loaded resource={run} what="run">
        {(r) => (
          <>
            <header className="run-head">
              <h2 className="run-title">{r.request}</h2>
              <p className="run-facts"><StatusOf value={r.status} /> <span className="meta">{r.mode}{r.project ? ` · ${r.project}` : ""} · started <Time iso={r.created} /> · updated <Time iso={r.updated} /></span></p>
              <p className="tabular">{milestones(r.tasks)}{r.running?.length ? ` · ${r.running.length} agent${r.running.length > 1 ? "s" : ""} working now` : ""}</p>
              {r.outcome && <p>{r.outcome}</p>}
              <RunActions run={r} />
            </header>
            {(r.plan_summary || r.risks?.length) && (
              <Section title="Plan" id="run-plan">
                {r.plan_summary && <p>{r.plan_summary}</p>}
                {!!r.risks?.length && <ul className="plain-list">{r.risks.map((x, i) => <li key={i}>{x}</li>)}</ul>}
              </Section>
            )}
            {!!r.tasks?.length && (
              <Section title="Tasks and dependencies" id="run-tasks">
                <DependencyGraph tasks={r.tasks} />
                <table className="table">
                  <caption className="sr-only">Tasks of this run</caption>
                  <thead><tr><th scope="col">Task</th><th scope="col">Role</th><th scope="col">Waits for</th><th scope="col">Status</th></tr></thead>
                  <tbody>
                    {r.tasks.map((t) => (
                      <tr key={t.id}>
                        <td>{t.title}{t.failure && <p className="error-text">{t.failure}</p>}{t.result?.summary && <p className="meta">{t.result.summary}</p>}</td>
                        <td>{t.role}</td>
                        <td>{t.depends_on.length ? t.depends_on.join(", ") : "nothing"}</td>
                        <td><StatusOf value={t.status} /></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </Section>
            )}
            {r.answer && <Section title="Answer" id="run-answer"><RichText text={r.answer} /></Section>}
            <Verification run={r} />
            <Section title="Timeline" id="run-timeline">
              {r.events?.length ? (
                <ol className="timeline">
                  {r.events.map((e, i) => <li key={i} className="timeline__item"><p className="timeline__text">{e.text}</p><Time iso={e.time} /></li>)}
                </ol>
              ) : <Empty>No events yet.</Empty>}
            </Section>
            {!!r.turns?.length && (
              <Section title="Agent turns" id="run-turns">
                <table className="table">
                  <caption className="sr-only">Every agent turn of this run</caption>
                  <thead><tr><th scope="col">Role</th><th scope="col">Task</th><th scope="col">Model</th><th scope="col">Time</th>
                    <th scope="col">Tokens</th><th scope="col">Cost</th></tr></thead>
                  <tbody>
                    {r.turns.map((t, i) => (
                      <tr key={i}><td>{t.role ?? ""}</td><td>{t.task ?? ""}</td><td>{t.model ?? "Hermes main model"}</td>
                        <td className="tabular">{t.seconds != null ? `${Math.round(t.seconds)} s` : ""}</td>
                        <td className="tabular">{t.usage?.totalTokens != null ? t.usage.totalTokens.toLocaleString() : "not reported"}</td>
                        <td>{t.cost && t.cost !== "unknown" ? t.cost : "unknown"}</td></tr>
                    ))}
                  </tbody>
                </table>
              </Section>
            )}
            <Section title="Report" id="run-report" action={report === null ? (
              <Button variant="quiet" onClick={() => void app.api.runReport(id).then((x) => setReport(x.report), (e: Error) => setReportError(e.message))}>Show the report</Button>
            ) : undefined}>
              {reportError && <ErrorNote message={reportError} />}
              {report !== null ? <RichText text={report} className="report" /> : <p className="meta">XENIO writes a report when the run ends.</p>}
            </Section>
          </>
        )}
      </Loaded>
    </div>
  );
}

export function OrchestrationPage({ runId }: { runId: string | null }) {
  const { app } = useApp();
  const runs = useResource((s) => app.api.runs(s), ["runs"]);
  return (
    <>
      <PageHeader title="Orchestration" intro="How XENIO's engineering team splits, runs and verifies work: every task, who does it, what it waits for." />
      <Loaded resource={runs} what="engineering runs">
        {(data) => {
          if (!data.available) return <Empty>{data.reason ?? "The engineering team is switched off."}</Empty>;
          if (!data.runs.length) return <Empty>No engineering runs yet. Ask XENIO to build, fix, test, review or explain a repository.</Empty>;
          const current = runId ?? data.runs[0]!.id;
          return (
            <div className="split">
              <RunList runs={data.runs} current={current} />
              <RunView id={current} />
            </div>
          );
        }}
      </Loaded>
    </>
  );
}
