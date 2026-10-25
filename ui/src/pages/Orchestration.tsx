import { AlertTriangle, Check, CircleDashed, CircleSlash, Loader2, Plus, X, type LucideIcon } from "lucide-react";
import { useRef, useState, type FormEvent } from "react";
import type { RunDetail, RunDiff, RunReview, RunSummary, RunTask } from "../api/types";
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

export type StageState = "done" | "current" | "failed" | "pending" | "warning" | "skipped";
export interface Stage { id: string; label: string; state: StageState; note?: string }

/** The lifecycle THIS run really went through (from its status, events, tests and reviews) - a run that skipped a
 *  stage (a small change needs no go-ahead; an explanation has no tests) does not show it. */
export function lifecycle(run: RunDetail): Stage[] {
  const happened = (kind: string) => run.events?.some((e) => e.kind === kind) ?? false;
  const failed = run.status === "failed" || run.status === "cancelled";
  const stages: Stage[] = [{ id: "request", label: "Request", state: "done" }];
  const planned = Boolean(run.tasks?.length) || happened("planned");
  stages.push({ id: "plan", label: "Plan", state: planned ? "done" : run.status === "planning" ? "current" : failed ? "failed" : "pending" });
  if (happened("planned") && (run.tasks?.length ?? 0) > 1) {
    stages.push({ id: "goahead", label: "Your go-ahead", state: run.status === "planned" ? "current"
      : run.status === "cancelled" && !run.tasks.some((t) => t.status !== "pending") ? "failed" : "done" });
  }
  if (run.mode !== "explain") {
    const anyDone = run.tasks?.some((t) => t.status === "succeeded");
    const execState: StageState = run.status === "running" ? "current"
      : ["verifying", "reviewing", "done"].includes(run.status) ? "done"
      : failed && anyDone ? "failed" : failed ? "failed" : "pending";
    stages.push({ id: "execute", label: "Work", state: execState, note: milestones(run.tasks) });
    const tests = run.final_tests?.[0];
    if (tests || happened("verifying") || run.status === "verifying") {
      const testState: StageState = run.status === "verifying" ? "current"
        : tests?.status === "not_run" ? "skipped"
        : tests && (tests.failed || tests.errors) ? "failed" : tests || happened("tested") ? "done" : "pending";
      stages.push({ id: "test", label: "Tests", state: testState,
        note: tests ? (tests.status === "not_run" ? "no tests ran" : `${tests.passed} passed, ${tests.failed} failed`) : undefined });
    }
    if (Object.keys(run.reviews ?? {}).length || run.status === "reviewing") {
      stages.push({ id: "review", label: "Review", state: run.status === "reviewing" ? "current" : "done",
        note: `${Object.keys(run.reviews ?? {}).length} reviewer(s)` });
    }
  }
  stages.push({ id: "result", label: run.status === "cancelled" ? "Cancelled" : run.status === "failed" ? "Failed" : "Result",
    state: run.status === "done" ? (run.verified ? "done" : "warning") : failed ? "failed" : "pending",
    note: run.status === "done" ? (run.verified ? "verified" : "not verified") : undefined });
  if (run.mode === "change" && run.status === "done") {
    stages.push({ id: "apply", label: "Applied to your files", state: happened("applied") ? "done" : "pending",
      note: happened("applied") ? undefined : "not yet: it asks you first" });
    if (happened("pushed") || happened("pull_request")) {
      stages.push({ id: "publish", label: happened("pull_request") ? "Pull request" : "Pushed", state: "done" });
    }
  }
  return stages;
}

const STAGE_ICON: Record<StageState, [LucideIcon, string]> = {
  done: [Check, "done"], current: [Loader2, "now"], failed: [X, "stopped here"], pending: [CircleDashed, "not yet"],
  warning: [AlertTriangle, "needs attention"], skipped: [CircleSlash, "did not happen"],
};

function Lifecycle({ run }: { run: RunDetail }) {
  return (
    <ol className="lifecycle" aria-label="How far this run got">
      {lifecycle(run).map((st) => {
        const [Icon, word] = STAGE_ICON[st.state];
        return (
          <li key={st.id} className={`lifecycle__stage lifecycle__stage--${st.state}`} aria-current={st.state === "current" ? "step" : undefined}>
            <Icon size={14} aria-hidden="true" className={st.state === "current" ? "spin-when-motion" : undefined} />
            <span className="lifecycle__label">{st.label}</span>
            <span className="sr-only">: {word}</span>
            {st.note && <span className="lifecycle__note">{st.note}</span>}
          </li>
        );
      })}
    </ol>
  );
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
      {tests && tests.status !== "not_run" ? (
        <p className="tabular">Tests run by XENIO{tests.sandboxed ? " in a sandbox" : ""}: {tests.passed} passed, {tests.failed} failed
          {tests.errors ? `, ${tests.errors} errors` : ""}{base && base.status !== "not_run" ? ` (before: ${base.passed} passed, ${base.failed} failed)` : ""}.
          {tests.detail ? ` ${tests.detail}` : ""}</p>
      ) : <p className="meta">{tests?.detail ? `No tests ran: ${tests.detail}.` : "No tests have been run for this work yet."}</p>}
      {!!tests?.failures?.length && (
        <ul className="plain-list">{tests.failures.slice(0, 8).map((f, i) => <li key={i}><code>{f}</code></li>)}</ul>
      )}
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

const SEVERITY: Record<string, "danger" | "attention" | "neutral"> = { critical: "danger", high: "danger", medium: "attention", low: "neutral" };

function Reviews({ reviews }: { reviews: Record<string, unknown> }) {
  const entries = Object.entries(reviews ?? {}).filter(([, r]) => r && typeof r === "object") as [string, RunReview][];
  if (!entries.length) return null;
  return (
    <Section title="Review" id="run-reviews">
      {entries.map(([who, review]) => (
        <div key={who} className="review">
          <h3 className="review__who">{who.replace(/_/g, " ")} review</h3>
          {review.summary && <p>{review.summary}</p>}
          {!!review.findings?.length && (
            <ul className="rows">
              {review.findings.map((f, i) => (
                <li key={i} className="row review__finding">
                  <Status tone={SEVERITY[String(f.severity).toLowerCase()] ?? "neutral"}>{f.severity ?? "finding"}</Status>
                  <span className="row-main">{f.issue}{f.file && <span className="meta"> ({f.file}{f.line ? `:${f.line}` : ""})</span>}
                    {f.fix && <span className="meta review__fix">Fix: {f.fix}</span>}</span>
                </li>
              ))}
            </ul>
          )}
          {!!review.acceptance?.length && (
            <ul className="plain-list acceptance">
              {review.acceptance.map((a, i) => (
                <li key={i}><Status tone={a.met ? "ok" : "danger"}>{a.met ? "Met" : "Not met"}</Status> {a.criterion}
                  {a.why && <span className="meta"> — {a.why}</span>}</li>
              ))}
            </ul>
          )}
        </div>
      ))}
    </Section>
  );
}

function DiffView({ diff }: { diff: string }) {
  return (
    <pre className="diff" tabIndex={0} aria-label="The changes, as a diff">
      {diff.split("\n").map((line, i) => {
        const kind = line.startsWith("+++") || line.startsWith("---") ? "file" : line.startsWith("@@") ? "hunk"
          : line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : line.startsWith("diff ") ? "file" : "";
        return <span key={i} className={kind ? `diff__${kind}` : undefined}>{line}{"\n"}</span>;
      })}
    </pre>
  );
}

function Changes({ run }: { run: RunDetail }) {
  const { app } = useApp();
  const [diff, setDiff] = useState<RunDiff | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  if (!run.diff_stat && run.mode !== "change") return null;
  const load = async () => {
    setBusy(true);
    setError(null);
    try {
      setDiff(await app.api.runDiff(run.id));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Section title="Changes" id="run-changes" action={!diff ? <Button variant="quiet" disabled={busy} onClick={() => void load()}>
      {busy ? "Reading…" : "Show the changes"}</Button> : undefined}>
      {run.diff_stat ? <pre className="diff-stat">{run.diff_stat}</pre> : <p className="meta">No changes recorded yet.</p>}
      <p className="meta">These changes live in the run's own workspace. {run.events?.some((e) => e.kind === "applied")
        ? "They were applied to your files." : "They are not in your files unless you apply them (XENIO asks first)."}
        {" "}Nothing is committed to your branch, pushed or deployed unless the timeline says so.</p>
      {error && <ErrorNote message={error} />}
      {diff && (diff.diff ? <DiffView diff={diff.diff} /> : <p className="meta">The run changed no files.</p>)}
    </Section>
  );
}

function TaskDetail({ task, onClose }: { task: RunTask; onClose: () => void }) {
  const result = task.result ?? {};
  return (
    <div className="task-detail" role="region" aria-label={`Task: ${task.title}`}>
      <div className="task-detail__head">
        <h3>{task.title}</h3>
        <StatusOf value={task.status} />
        <Button variant="quiet" onClick={onClose}>Close</Button>
      </div>
      {task.description && <p>{task.description}</p>}
      <dl className="facts">
        <dt>Role</dt><dd>{task.role}</dd>
        <dt>Waits for</dt><dd>{task.depends_on.length ? task.depends_on.join(", ") : "nothing"}</dd>
        <dt>May change</dt><dd>{task.allowed_paths.length ? task.allowed_paths.map((p) => <code key={p}>{p} </code>) : "no files (read-only)"}</dd>
        <dt>Attempts</dt><dd className="tabular">{task.attempts}</dd>
        {result.model && (<><dt>Model</dt><dd>{result.model}</dd></>)}
        {!!result.changed_files?.length && (<><dt>Changed</dt><dd>{result.changed_files.map((f) => <code key={f}>{f} </code>)}</dd></>)}
        {result.timings && (<><dt>Time</dt><dd className="tabular">{Object.entries(result.timings)
          .map(([k, v]) => `${k.replace(/_/g, " ")} ${Math.round(Number(v))} s`).join(" · ")}</dd></>)}
      </dl>
      {result.summary && <p className="meta">Agent's summary: {result.summary}</p>}
      {task.failure && <ErrorNote message={task.failure} />}
    </div>
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
      {!live && ACTIVE.includes(run.status) && (
        <p className="meta">This run was interrupted when XENIO stopped; it cannot be resumed. Ask the engineering team again to redo it.</p>
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
  const [selected, setSelected] = useState<string | null>(null);
  const root = useRef<HTMLDivElement>(null);
  useGsap(root, (r) => { arrive(".section", r, 0.04); }, [id]);
  return (
    <div ref={root} className="run-view">
      <Loaded resource={run} what="run">
        {(r) => {
          const task = r.tasks?.find((t) => t.id === selected) ?? null;
          return (
            <>
              <header className="run-head">
                <h2 className="run-title">{r.request}</h2>
                <p className="run-facts"><StatusOf value={r.status} /> <span className="meta">{r.mode}{r.project ? ` · ${r.project}` : ""} · started <Time iso={r.created} /> · updated <Time iso={r.updated} /></span></p>
                <Lifecycle run={r} />
                <p className="tabular">{milestones(r.tasks)}{r.running?.length ? ` · ${r.running.length} agent${r.running.length > 1 ? "s" : ""} working now` : ""}</p>
                {r.outcome && <p>{r.outcome}</p>}
                <RunActions run={r} />
              </header>
              {Boolean(r.plan_summary || r.risks?.length) && (
                <Section title="Plan" id="run-plan">
                  {r.plan_summary && <p>{r.plan_summary}</p>}
                  {!!r.risks?.length && <ul className="plain-list">{r.risks.map((x, i) => <li key={i}>{x}</li>)}</ul>}
                </Section>
              )}
              {!!r.tasks?.length && (
                <Section title="Tasks and dependencies" id="run-tasks">
                  <DependencyGraph tasks={r.tasks} selected={selected} onSelect={setSelected} />
                  <table className="table">
                    <caption className="sr-only">Tasks of this run</caption>
                    <thead><tr><th scope="col">Task</th><th scope="col">Role</th><th scope="col">Waits for</th><th scope="col">Status</th>
                      <th scope="col"><span className="sr-only">Details</span></th></tr></thead>
                    <tbody>
                      {r.tasks.map((t) => (
                        <tr key={t.id} className={t.id === selected ? "is-selected" : undefined}>
                          <td>{t.title}{t.failure && <p className="error-text">{t.failure}</p>}{t.result?.summary && <p className="meta">{t.result.summary}</p>}</td>
                          <td>{t.role}</td>
                          <td>{t.depends_on.length ? t.depends_on.join(", ") : "nothing"}</td>
                          <td><StatusOf value={t.status} />{t.attempts > 1 && <span className="meta tabular"> · try {t.attempts}</span>}</td>
                          <td><Button variant="quiet" aria-pressed={t.id === selected} aria-label={`Details of ${t.title}`}
                            onClick={() => setSelected(t.id === selected ? null : t.id)}>Details</Button></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {task && <TaskDetail task={task} onClose={() => setSelected(null)} />}
                </Section>
              )}
              {r.answer && <Section title="Answer" id="run-answer"><RichText text={r.answer} /></Section>}
              <Verification run={r} />
              <Reviews reviews={r.reviews} />
              <Changes run={r} />
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
          );
        }}
      </Loaded>
    </div>
  );
}

/** Ask the engineering team for work. XENIO inspects and plans in the run's own workspace, then waits for the go-ahead. */
function NewRun() {
  const { app } = useApp();
  const [open, setOpen] = useState(false);
  const [request, setRequest] = useState("");
  const [project, setProject] = useState("");
  const [path, setPath] = useState("");
  const [mode, setMode] = useState<"" | "change" | "plan" | "explain" | "review" | "test">("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ tone: "ok" | "danger"; text: string } | null>(null);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setResult(null);
    try {
      const out = await app.api.startRun({ request: request.trim(), project: project.trim(), path: path.trim(), mode });
      setResult({ tone: "ok", text: out.note ?? "The team is planning it." });
      setRequest("");
    } catch (err) {
      setResult({ tone: "danger", text: (err as Error).message });
    } finally {
      setBusy(false);
    }
  };
  if (!open) return <Button variant="primary" onClick={() => setOpen(true)}><Plus size={14} aria-hidden="true" /> Ask the team</Button>;
  return (
    <form className="new-run" onSubmit={submit} aria-label="Ask the engineering team">
      <div className="field">
        <label htmlFor="run-request">What should the team do?</label>
        <textarea id="run-request" rows={3} value={request} maxLength={2000} required autoFocus
          placeholder="For example: add a high-score table to the snake game, with tests" onChange={(e) => setRequest(e.target.value)} />
      </div>
      <div className="new-run__row">
        <div className="field">
          <label htmlFor="run-project">Project (optional)</label>
          <input id="run-project" value={project} maxLength={120} placeholder="the current project" onChange={(e) => setProject(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="run-path">Folder (optional)</label>
          <input id="run-path" value={path} maxLength={500} placeholder="~/Xenio/snake" onChange={(e) => setPath(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="run-mode">Kind of work</label>
          <select id="run-mode" value={mode} onChange={(e) => setMode(e.target.value as typeof mode)}>
            <option value="">Let XENIO decide</option>
            <option value="change">Change the code</option>
            <option value="plan">Plan only</option>
            <option value="explain">Explain</option>
            <option value="review">Review</option>
            <option value="test">Test</option>
          </select>
        </div>
      </div>
      <p className="meta">The team works in its own copy: your files are not touched. A plan with several tasks waits for your go-ahead;
        writing the result into your files, pushing or opening a pull request each ask you first.</p>
      <div className="new-run__actions">
        <Button variant="primary" type="submit" disabled={busy || request.trim().length < 4}>{busy ? "Sending…" : "Plan it"}</Button>
        <Button variant="quiet" onClick={() => setOpen(false)} disabled={busy}>Close</Button>
      </div>
      {result && <p className={result.tone === "ok" ? "ok-text" : "error-text"} role="status">{result.text}</p>}
    </form>
  );
}

export function OrchestrationPage({ runId }: { runId: string | null }) {
  const { app } = useApp();
  const runs = useResource((s) => app.api.runs(s), ["runs"]);
  return (
    <>
      <PageHeader title="Orchestration" intro="How XENIO's engineering team splits, runs and verifies work: every task, who does it, what it waits for, what was tested and reviewed." actions={runs.data?.available ? <NewRun /> : undefined} />
      <Loaded resource={runs} what="engineering runs">
        {(data) => {
          if (!data.available) return <Empty>{data.reason ?? "The engineering team is switched off."}</Empty>;
          if (!data.runs.length) return <Empty>No engineering runs yet. Ask the team above, or ask XENIO to build, fix, test, review or explain a repository.</Empty>;
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
