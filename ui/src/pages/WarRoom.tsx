import { AlertTriangle, Check, ChevronLeft, CircleDashed, Copy, Download, ExternalLink, Fingerprint, FolderOpen, Hand, ImagePlus, KeyRound, Loader2, MessageSquare, MousePointer2, Pause, Play, RotateCw, Send, ShieldAlert, Square, Trash2, type LucideIcon } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import type { FoundryAgent, FoundryArtifact, FoundryCapability, FoundryMcpServer, FoundryDecision, FoundryEventRow, FoundryLive, FoundryMember, FoundryMessage, FoundryNode, FoundryOrder, FoundryOverview, FoundrySkill, FoundryState, VaultState } from "../api/types";
import { ApprovalCard } from "../components/ApprovalCard";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { XenioMark } from "../components/XenioMark";
import { RichText } from "../components/RichText";
import { Button, Empty, ErrorNote, PageHeader, Section, Status, StatusOf, Time } from "../components/ui";
import { useApp, useAppState } from "../state/context";
import { useReducedMotion } from "../motion/motion";
import { href } from "../state/router";

const OPEN = ["starting", "running", "paused", "waiting"];
const TASK_NODES = ["task", "research", "browser", "computer", "file"];
const NOTHING_TO_RUN = ["research", "document", "spreadsheet", "presentation", "browser", "desktop", "files", "answer"];

/** Re-reads on every event, at most once per interval, and never throws away what is on screen while it does. */
function useLive<T>(load: () => Promise<T>, deps: unknown[], every = 700) {
  const revision = useAppState((s) => s.revision.foundry);
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const loader = useRef(load);
  loader.current = load;
  const read = useCallback(() => {
    loader.current().then((value) => { setData(value); setError(null); }, (e: Error) => setError(e.message));
  }, []);
  useEffect(() => { setData(null); read(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, deps);
  useEffect(() => {
    if (revision === 0 || timer.current) return;
    timer.current = setTimeout(() => { timer.current = null; read(); }, every);
  }, [revision, read, every]);
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);
  return { data, error, reload: read };
}

function useAct() {
  const { app } = useApp();
  const [busy, setBusy] = useState(false);
  const act = useCallback(async (command: string, body: Record<string, unknown> = {}) => {
    setBusy(true);
    try {
      return await app.api.foundryDo(command, body);
    } catch (e) {
      app.store.notify({ tone: "danger", title: "That did not work", message: (e as Error).message });
      return null;
    } finally {
      setBusy(false);
    }
  }, [app]);
  return { act, busy };
}

// ---- the people ------------------------------------------------------------------------------------------------------

const SENDER_NAMES: Record<string, string> = { you: "You (CEO)", xenio: "XENIO (manager)" };
const readable = (id: string) => id.replace(/[-_]+/g, " ").replace(/^./, (c) => c.toUpperCase());

function hue(name: string): number {
  let h = 0;
  for (const c of name) h = (h * 31 + c.charCodeAt(0)) % 360;
  return h;
}

const LOGOS: [RegExp, string][] = [
  [/secur|pentest|\bauth/, "🛡️"], [/test|\bqa\b|quality/, "🧪"], [/review|audit/, "🔍"], [/data ?base|\bdb\b|\bsql\b|schema|storage/, "🗄️"], [/pay|billing|stripe|finance/, "💳"],
  [/front|\bui\b|\bux\b|design|css|\bweb/, "🎨"], [/devops|deploy|infra|cloud|\bsre\b|platform/, "🚀"], [/research|scout/, "🔭"], [/recommend|\bml\b|machine|\bai\b|\bmodel|data sci|analy/, "🧠"],
  [/mobile|\bios\b|android/, "📱"], [/doc|writer|editor|content/, "📚"], [/back|\bapi\b|server/, "⚙️"], [/plan|architect|lead|manager|product/, "🧭"],
];

/** A little character for each role, from what it does. */
export function logoFor(id: string, name: string): string {
  const words = `${name} ${id}`.toLowerCase();
  return LOGOS.find(([pattern]) => pattern.test(words))?.[1] ?? "🤖";
}

function Avatar({ id, name }: { id: string; name: string }) {
  if (id === "you") return <span className="wr-avatar wr-avatar--you" aria-hidden="true">You</span>;
  if (id === "xenio") return <span className="wr-avatar wr-avatar--xenio" aria-hidden="true"><XenioMark size={18} /></span>;
  return <span className="wr-avatar" style={{ background: `hsl(${hue(id)} 40% 20%)`, boxShadow: `inset 0 0 0 1px hsl(${hue(id)} 45% 34%)` }} aria-hidden="true">{logoFor(id, name)}</span>;
}

const tokens = (n: number) => (n >= 1_000_000 ? `${(n / 1_000_000).toFixed(1)}M` : n >= 1000 ? `${Math.round(n / 1000)}k` : String(n));

/** What this work has really cost, and which models carried it. */
function Spend({ state }: { state: FoundryState }) {
  const budget = state.workflow.budget;
  if (!budget) return null;
  const share = Math.min(1, budget.tokens_estimated / Math.max(1, budget.max_tokens));
  return (
    <div className="wr-spend">
      <div className="wr-spend__line"><span>{tokens(budget.tokens_estimated)} of {tokens(budget.max_tokens)} tokens</span><span className="wr-muted">{budget.calls} model calls · {Math.round(budget.seconds / 60)} min</span></div>
      <div className={`wr-spend__bar ${share > 0.85 ? "is-high" : ""}`} role="img" aria-label={`${Math.round(share * 100)}% of the token budget used`}><span style={{ width: `${Math.max(share * 100, 1)}%` }} /></div>
    </div>
  );
}

/** A number of tokens as you would type it: 150000, 150,000, 150k or 1.5m. Empty is 0: XENIO decides. */
export const asTokens = (value: string): number => {
  const t = value.trim().toLowerCase().replace(/[,_\s]/g, "");
  const m = /^(\d+(?:\.\d+)?)([km]?)$/.exec(t);
  if (!m) return 0;
  return Math.round(Number(m[1]) * (m[2] === "k" ? 1_000 : m[2] === "m" ? 1_000_000 : 1));
};
const shown = (n?: number) => (n ? tokens(n) : "");

/** Your own token limits: for a whole job, and for one agent's task. Empty means XENIO decides. */
function LimitFields({ id, job, task, setJob, setTask }: { id: string; job: string; task: string; setJob: (v: string) => void; setTask: (v: string) => void }) {
  return (
    <div className="wr-limits">
      <div className="field"><label htmlFor={`${id}-job`}>Most tokens for the whole job</label><input id={`${id}-job`} inputMode="numeric" value={job} onChange={(e) => setJob(e.target.value)} placeholder="XENIO decides, by the size of the job" /></div>
      <div className="field"><label htmlFor={`${id}-task`}>Most tokens for one agent's task</label><input id={`${id}-task`} inputMode="numeric" value={task} onChange={(e) => setTask(e.target.value)} placeholder="XENIO decides, by the kind of work" /></div>
      <p className="wr-muted wr-limits__hint">Type 150k or 1.5m. When a job reaches its limit it stops and asks you. When a task reaches its limit, that agent stops and says where it got to.</p>
    </div>
  );
}

/** The limits of the work you are looking at, changed while it runs. */
function LimitsControl({ state, act }: { state: FoundryState; act: (command: string, body: Record<string, unknown>) => Promise<unknown> }) {
  const wf = state.workflow;
  const [open, setOpen] = useState(false);
  const [job, setJob] = useState(shown(wf.budget_tokens));
  const [task, setTask] = useState(shown(wf.task_tokens));
  const [error, setError] = useState("");
  const apply = async (e: FormEvent) => {
    e.preventDefault();
    const jobTokens = asTokens(job), taskTokens = asTokens(task);
    if ((job.trim() && !jobTokens) || (task.trim() && !taskTokens)) { setError("Type a number, like 150k."); return; }
    setError("");
    await act("set_budget", { workflow: wf.id, job_tokens: jobTokens, task_tokens: taskTokens });
    setOpen(false);
  };
  return (
    <span className="wr-limits-control">
      <button type="button" className="wr-link" aria-expanded={open} onClick={() => setOpen(!open)}>Limits{wf.budget_tokens || wf.task_tokens ? " (yours)" : ""}</button>
      {open && (
        <form className="wr-limits-pop" onSubmit={(e) => void apply(e)} aria-label="Token limits for this work">
          <LimitFields id={`wr-lim-${wf.id}`} job={job} task={task} setJob={setJob} setTask={setTask} />
          {error && <p className="wr-error">{error}</p>}
          <p className="wr-command__row"><Button variant="primary" type="submit">Apply</Button><Button onClick={() => setOpen(false)}>Cancel</Button></p>
        </form>
      )}
    </span>
  );
}

function ModelsUsed({ state }: { state: FoundryState }) {
  const models = Object.entries(state.workflow.model_use ?? {}).sort((a, b) => b[1] - a[1]);
  if (!models.length) return null;
  return <Section title="Models doing the work"><p className="wr-spend__models">{models.map(([name, used]) => <span key={name} className="wr-chip">{name} {tokens(used)}</span>)}</p></Section>;
}

/** Whatever is waiting for your yes, right here, so you never have to go looking. */
function ApprovalsHere() {
  const pending = useAppState((s) => s.approvals);
  if (!pending.length) return null;
  return (
    <section className="wr-approvals" aria-label="Waiting for your yes">
      <h3 className="wr-approvals__title"><ShieldAlert size={15} aria-hidden="true" /> Waiting for your yes</h3>
      <div className="approval-list">{pending.map((a) => <ApprovalCard key={a.id} approval={a} />)}</div>
    </section>
  );
}

// ---- the conversation ------------------------------------------------------------------------------------------------

const SAYS = { verb: "says", tone: "neutral" };
const KINDS: Record<string, { verb: string; tone: string }> = {
  PROPOSAL: { verb: "proposes", tone: "propose" }, CHALLENGE: { verb: "challenges", tone: "challenge" }, COUNTERARGUMENT: { verb: "changes position", tone: "challenge" },
  REVIEW: { verb: "reviews", tone: "review" }, QUESTION: { verb: "asks", tone: "neutral" }, RESULT: { verb: "reports", tone: "result" }, BLOCKER: { verb: "is blocked", tone: "blocker" },
  ACK: { verb: "acknowledges", tone: "ack" }, DECISION: { verb: "decision", tone: "decision" }, ESCALATION: { verb: "needs you", tone: "blocker" }, DELEGATION: { verb: "hands over", tone: "neutral" }, INFORMATION: { verb: "says", tone: "neutral" },
};

function MessageCard({ m, names }: { m: FoundryMessage; names: Map<string, string> }) {
  const [open, setOpen] = useState(false);
  const kind = KINDS[m.type] ?? SAYS;
  const sender = SENDER_NAMES[m.sender] ?? names.get(m.sender) ?? readable(m.sender);
  const to = m.recipient === "team" || (m.recipient === "you" && ["ESCALATION", "BLOCKER"].includes(m.type)) ? "" : SENDER_NAMES[m.recipient] ?? names.get(m.recipient) ?? readable(m.recipient);
  const long = m.content.length > 420;
  return (
    <li className={`wr-msg wr-msg--${kind.tone} ${m.sender === "you" ? "wr-msg--mine" : ""}`}>
      <Avatar id={m.sender} name={sender} />
      <div className="wr-msg__body">
        <p className="wr-msg__head">
          <strong>{sender}</strong> <span className="wr-msg__verb">{kind.verb}</span>
          {to && <span className="wr-msg__to"> to {to}</span>}
          <span className="wr-msg__meta">{m.model && <span>{m.model}</span>}<Time iso={m.at} /></span>
        </p>
        <div className={long && !open ? "wr-msg__text wr-msg__text--cut" : "wr-msg__text"}><RichText text={m.content} /></div>
        {long && <button type="button" className="wr-link" onClick={() => setOpen(!open)}>{open ? "Show less" : "Show all"}</button>}
        {m.evidence.length > 0 && <p className="wr-chips" aria-label="Evidence">{m.evidence.map((e) => <span key={e} className="wr-chip">{e}</span>)}</p>}
      </div>
    </li>
  );
}

function Conversation({ state, names }: { state: FoundryState; names: Map<string, string> }) {
  const [agent, setAgent] = useState("");
  const [kind, setKind] = useState("");
  const [words, setWords] = useState("");
  const shown = state.messages.filter((m) => (!agent || m.sender === agent || m.recipient === agent) && (!kind || m.type === kind) && (!words.trim() || m.content.toLowerCase().includes(words.trim().toLowerCase())));
  const list = useRef<HTMLOListElement | null>(null);
  const near = useRef(true);
  useEffect(() => { // only the feed moves: scrollIntoView would drag the whole page, and hide the goal and its controls
    const el = list.current;
    if (el && near.current) el.scrollTop = el.scrollHeight;
  }, [shown.length]);
  const types = [...new Set(state.messages.map((m) => m.type))];
  const people = [...new Set(state.messages.map((m) => m.sender))];
  return (
    <div className="wr-convo">
      {state.messages.length > 6 && (
        <div className="wr-filters">
          <label>Find <input type="search" value={words} onChange={(e) => setWords(e.target.value)} placeholder="Any word" /></label>
          <label>Who <select value={agent} onChange={(e) => setAgent(e.target.value)}><option value="">Everyone</option>{people.map((p) => <option key={p} value={p}>{SENDER_NAMES[p] ?? names.get(p) ?? p}</option>)}</select></label>
          <label>Kind <select value={kind} onChange={(e) => setKind(e.target.value)}><option value="">Everything</option>{types.map((t) => <option key={t} value={t}>{KINDS[t]?.verb ?? t}</option>)}</select></label>
        </div>
      )}
      <ol className="wr-feed" ref={list} aria-live="polite" onScroll={(e) => { const el = e.currentTarget; near.current = el.scrollHeight - el.scrollTop - el.clientHeight < 120; }}>
        {shown.map((m) => <MessageCard key={m.id} m={m} names={names} />)}
        {!shown.length && <li className="wr-feed__empty">{state.messages.length ? "Nothing matches those filters." : "The team is getting started."}</li>}
      </ol>
    </div>
  );
}

/** Pictures to go with what you say. Dropped, pasted or chosen. They are looked at once, and the team reads what was seen. */
function usePictures() {
  const { app } = useApp();
  const [files, setFiles] = useState<{ id: string; name: string }[]>([]);
  const [adding, setAdding] = useState(false);
  const add = async (list: FileList | File[] | null) => {
    const pictures = Array.from(list ?? []).filter((f) => f.type.startsWith("image/")).slice(0, 4);
    if (!pictures.length) return;
    setAdding(true);
    try {
      for (const file of pictures) {
        const up = await app.api.upload(file);
        setFiles((was) => [...was, { id: up.id, name: up.name }]);
      }
    } catch (e) {
      app.store.notify({ tone: "danger", title: "That picture could not be added", message: (e as Error).message });
    } finally {
      setAdding(false);
    }
  };
  return { files, adding, add, clear: () => setFiles([]), drop: (id: string) => setFiles((was) => was.filter((f) => f.id !== id)) };
}

function Pictures({ pictures }: { pictures: ReturnType<typeof usePictures> }) {
  if (!pictures.files.length && !pictures.adding) return null;
  return (
    <p className="wr-chips wr-pictures">
      {pictures.files.map((f) => <span key={f.id} className="wr-chip">{f.name} <button type="button" className="wr-link" aria-label={`Remove ${f.name}`} onClick={() => pictures.drop(f.id)}>×</button></span>)}
      {pictures.adding && <span className="wr-muted">Adding…</span>}
    </p>
  );
}

function AttachButton({ pictures }: { pictures: ReturnType<typeof usePictures> }) {
  const input = useRef<HTMLInputElement | null>(null);
  return (
    <>
      <input ref={input} type="file" accept="image/*" multiple hidden aria-label="Add a picture" onChange={(e) => { void pictures.add(e.target.files); e.target.value = ""; }} />
      <Button variant="quiet" type="button" aria-label="Add a picture" onClick={() => input.current?.click()}><ImagePlus size={15} aria-hidden="true" /></Button>
    </>
  );
}

function Composer({ team, send, finished }: { team: FoundryMember[]; send: (text: string, to: string, uploads: string[]) => Promise<void>; finished: boolean }) {
  const [text, setText] = useState("");
  const [to, setTo] = useState("team");
  const pictures = usePictures();
  const go = async (e?: FormEvent) => {
    e?.preventDefault();
    const words = text.trim();
    if (!words && !pictures.files.length) return;
    setText("");
    const uploads = pictures.files.map((f) => f.id);
    pictures.clear();
    await send(words, to, uploads);
  };
  return (
    <form className={`wr-composer ${finished ? "wr-composer--change" : ""}`} onSubmit={(e) => void go(e)}>
      {!finished && <select aria-label="Say it to" value={to} onChange={(e) => setTo(e.target.value)}>
        <option value="team">The whole team</option>
        {team.map((m) => <option key={m.agent} value={m.agent}>{m.name}</option>)}
      </select>}
      <Pictures pictures={pictures} />
      <AttachButton pictures={pictures} />
      <textarea aria-label="Your message" rows={1} value={text} onPaste={(e) => { if (e.clipboardData.files.length) void pictures.add(e.clipboardData.files); }} placeholder={finished ? "Tried it? Tell the team what to change." : "Say anything, to anyone, any time: “Stop, use PostgreSQL instead.”"} onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void go(); } }} />
      <Button variant="primary" type="submit" disabled={!text.trim() && !pictures.files.length} aria-label="Send"><Send size={15} aria-hidden="true" /></Button>
    </form>
  );
}

// ---- the decision room -----------------------------------------------------------------------------------------------

function DecisionCard({ d, workflowOpen, act }: { d: FoundryDecision; workflowOpen: boolean; act: (c: string, b: Record<string, unknown>) => Promise<unknown> }) {
  const [words, setWords] = useState("");
  const waiting = d.status === "needs_human" && workflowOpen;
  const debating = d.status === "debating" && workflowOpen;
  const challenges = d.rounds.flatMap((r) => r.responses).filter((r) => r.stance === "challenge");
  const nameOf = (id: string) => d.options.find((o) => o.agent === id)?.name ?? id;
  return (
    <article className={`wr-decision wr-decision--${d.status}`}>
      <header className="wr-decision__head">
        <h3>{d.question}</h3>
        <StatusOf value={d.status === "needs_human" ? "waiting" : d.status === "decided" || d.status === "resolved" ? "done" : d.status === "stopped" ? "cancelled" : "running"} />
      </header>
      {d.decision && d.status !== "needs_human" && (
        <div className="wr-verdict">
          <p className="wr-verdict__label">{d.decided_by === "you" ? "You decided" : "The team decided"}{typeof d.confidence === "number" && d.decided_by !== "you" ? `, ${Math.round(d.confidence * 100)}% confident` : ""}</p>
          <p className="wr-verdict__text">{d.decision}</p>
          {d.provisional && <p className="wr-verdict__flag"><AlertTriangle size={14} aria-hidden="true" /> A judgment call ({(d.why_provisional ?? []).join("; ")}). Tell the team if you want it otherwise.</p>}
          {d.reasons?.length ? <ul>{d.reasons.map((r) => <li key={r}>{r}</li>)}</ul> : null}
        </div>
      )}
      {waiting && (
        <div className="wr-ask">
          <p className="wr-ask__q"><Hand size={15} aria-hidden="true" /> {d.question_for_human || d.question}</p>
          {d.why_human?.length ? <p className="wr-ask__why">Why it came to you: {d.why_human.join("; ")}.</p> : null}
          {d.open_challenges?.length ? <ul className="wr-ask__open">{d.open_challenges.map((c) => <li key={c}>{c}</li>)}</ul> : null}
          <form className="wr-inline" onSubmit={(e) => { e.preventDefault(); if (words.trim()) void act("decide", { workflow: d.workflow, decision: d.id, text: words.trim() }); }}>
            <input aria-label="Or say what you want" placeholder="Or say what you want" value={words} onChange={(e) => setWords(e.target.value)} />
            <Button type="submit" disabled={!words.trim()}>Decide</Button>
          </form>
        </div>
      )}
      <div className="wr-options">
        {d.options.map((o) => (
          <section key={o.agent} className={`wr-option ${d.selected === o.label && d.status !== "needs_human" ? "wr-option--chosen" : ""}`}>
            <p className="wr-option__who"><Avatar id={o.agent} name={o.name} /> <strong>{o.name}</strong> <span className="wr-msg__meta">{o.model}{o.confidence ? ` · ${Math.round(o.confidence * 100)}%` : ""}</span></p>
            <p className="wr-option__position">{o.position}</p>
            {o.history.length > 1 && <p className="wr-option__changed">Changed position{o.why_changed ? `: ${o.why_changed}` : ""}</p>}
            {o.evidence.length > 0 && <ul className="wr-evidence">{o.evidence.map((e) => <li key={e.text}><span className={e.checked ? "wr-mark wr-mark--ok" : "wr-mark"}>{e.checked ? "checked" : "claimed"}</span> {e.text}</li>)}</ul>}
            {o.risks.length > 0 && <details><summary>Risks and costs</summary><ul>{[...o.risks, ...o.tradeoffs].map((r) => <li key={r}>{r}</li>)}</ul></details>}
            {waiting && <Button onClick={() => void act("decide", { workflow: d.workflow, decision: d.id, choice: o.label })}>Go with this</Button>}
          </section>
        ))}
      </div>
      {challenges.length > 0 && (
        <details className="wr-challenges"><summary>{challenges.length} challenge{challenges.length === 1 ? "" : "s"} raised</summary>
          <ul>{challenges.map((c, i) => <li key={`${c.from}${i}`}><strong>{c.name}</strong> to {nameOf(c.to)}{c.severity === "high" ? " (serious)" : ""}: {c.point}</li>)}</ul>
        </details>
      )}
      {d.dissent?.length ? <div className="wr-dissent"><h4>Dissent, kept on record</h4><ul>{d.dissent.map((x) => <li key={x.agent}><strong>{x.name}:</strong> {x.position}{x.why_not_chosen ? <span className="wr-muted"> Not chosen: {x.why_not_chosen}</span> : null}</li>)}</ul></div> : null}
      {d.required_validation?.length ? <details><summary>To prove before trusting this</summary><ul>{d.required_validation.map((v) => <li key={v}>{v}</li>)}</ul></details> : null}
      {d.uncertainties?.length ? <details><summary>Still unknown</summary><ul>{d.uncertainties.map((v) => <li key={v}>{v}</li>)}</ul></details> : null}
      {debating && (
        <form className="wr-inline" onSubmit={(e) => { e.preventDefault(); if (words.trim()) { void act("discuss", { workflow: d.workflow, decision: d.id, text: words.trim() }); setWords(""); } }}>
          <input aria-label="Add your view" placeholder="Add your view: everyone hears it, and it binds the decision" value={words} onChange={(e) => setWords(e.target.value)} />
          <Button type="submit" disabled={!words.trim()}>Add</Button>
        </form>
      )}
    </article>
  );
}

// ---- the graph -------------------------------------------------------------------------------------------------------

const NODE_W = 176, NODE_H = 56, GAP_X = 64, GAP_Y = 18; // room for the lines between steps to curve instead of crossing

export function placeNodes(nodes: FoundryNode[]) {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const depth = new Map<string, number>();
  const visit = (id: string, seen: Set<string>): number => {
    if (depth.has(id)) return depth.get(id)!;
    if (seen.has(id)) return 0;
    seen.add(id);
    const deps = (byId.get(id)?.deps ?? []).filter((d) => byId.has(d));
    const d = deps.length ? Math.max(...deps.map((x) => visit(x, seen))) + 1 : 0;
    depth.set(id, d);
    return d;
  };
  nodes.forEach((n) => visit(n.id, new Set()));
  const rows = new Map<number, number>();
  const placed = nodes.map((node) => {
    const col = depth.get(node.id) ?? 0;
    const row = rows.get(col) ?? 0;
    rows.set(col, row + 1);
    return { node, x: col * (NODE_W + GAP_X), y: row * (NODE_H + GAP_Y) };
  });
  return { placed, width: Math.max(...placed.map((p) => p.x)) + NODE_W, height: Math.max(...placed.map((p) => p.y)) + NODE_H };
}

const NODE_ICON: Record<string, LucideIcon> = { done: Check, failed: AlertTriangle, blocked: AlertTriangle, running: Loader2, waiting: Hand, cancelled: Square, skipped: CircleDashed, pending: CircleDashed };

const LIVE = new Set(["running", "waiting"]);

function Graph({ state, names, act }: { state: FoundryState; names: Map<string, string>; act: (c: string, b: Record<string, unknown>) => Promise<unknown> }) {
  const [selected, setSelected] = useState<string | null>(null);
  const [hover, setHover] = useState<string | null>(null);
  const reduced = useReducedMotion();
  const scroller = useRef<HTMLDivElement | null>(null);
  const shownOnce = useRef(false);
  useEffect(() => { // a long graph opens where the work is, not at its beginning
    if (shownOnce.current || !scroller.current || !state.graph.length) return;
    shownOnce.current = true;
    const here = placeNodes(state.graph).placed.find((p) => p.node.status === "running" || p.node.status === "waiting") ?? placeNodes(state.graph).placed.at(-1);
    if (here) scroller.current.scrollLeft = Math.max(0, here.x - 160);
  }, [state.graph]);
  if (!state.graph.length) return <Empty>The graph appears as soon as the team starts.</Empty>;
  const steps = state.graph.filter((n) => !n.payload?.managed); // the test and fix rounds have their own place below
  const { placed, width, height } = placeNodes(steps);
  const at = new Map(placed.map((p) => [p.node.id, p]));
  const node = selected ? state.graph.find((n) => n.id === selected) : null;
  const focus = hover ?? selected;
  const edges = placed.flatMap((p) => p.node.deps.filter((d) => at.has(d)).map((d) => {
    const a = at.get(d)!;
    const x1 = a.x + NODE_W, y1 = a.y + NODE_H / 2, x2 = p.x, y2 = p.y + NODE_H / 2, bend = Math.max(22, (x2 - x1) / 2);
    const live = a.node.status === "done" && LIVE.has(p.node.status); // the work is flowing into this step right now
    const lit = focus !== null && (d === focus || p.node.id === focus);
    return { id: `${d}-${p.node.id}`, from: a.node, to: p.node, live, lit, d: `M${x1} ${y1} C${x1 + bend} ${y1} ${x2 - bend} ${y2} ${x2} ${y2}` };
  }));
  const count = (...s: string[]) => steps.filter((n) => s.includes(n.status)).length;
  const tally = [["done", count("done"), "done"], ["running", count("running"), "running"], ["waiting", count("waiting"), "waiting for you"], ["failed", count("failed", "blocked"), "stuck"]] as const;
  return (
    <div className="wr-graph">
      <div className="wr-graph__strip" aria-label="Where the work is">
        {tally.filter(([, n], i) => n > 0 || i < 2).map(([key, n, label]) => (
          <span key={key} className={`wr-graph__count wr-graph__count--${key}`}><span className="wr-graph__dot" aria-hidden="true" />{n} {label}</span>
        ))}
        <span className="wr-graph__total">{steps.length} steps</span>
      </div>
      <div className="wr-graph__scroll" ref={scroller} tabIndex={0} role="group" aria-label="The workflow as a graph">
        <div className={`wr-graph__canvas ${focus ? "has-focus" : ""}`} style={{ width, height }}>
          <svg width={width} height={height} aria-hidden="true">
            {edges.map((e) => (
              <path key={e.id} className={`wr-edge wr-edge--${e.from.status} ${e.live ? "wr-edge--live" : ""} ${e.to.status === "pending" ? "wr-edge--ahead" : ""} ${e.lit ? "is-lit" : ""}`} d={e.d} />
            ))}
          </svg>
          {!reduced && edges.filter((e) => e.live).map((e) => [0, 1].map((k) => ( // work travelling into the step that is running, along the same curve
            <span key={`${e.id}-p${k}`} aria-hidden="true" className={`wr-packet wr-packet--${e.to.status}`} style={{ offsetPath: `path("${e.d}")`, animationDelay: `-${k * 0.9}s` }} />
          )))}
          {placed.map(({ node: n, x, y }) => {
            const Icon = NODE_ICON[n.status] ?? CircleDashed;
            const near = focus !== null && (n.id === focus || edges.some((e) => e.lit && (e.from.id === n.id || e.to.id === n.id)));
            return (
              <button key={n.id} type="button" className={`wr-node wr-node--${n.status} ${selected === n.id ? "is-selected" : ""} ${near ? "is-near" : ""}`} style={{ left: x, top: y, width: NODE_W, height: NODE_H }} aria-pressed={selected === n.id}
                      onClick={() => setSelected(selected === n.id ? null : n.id)} onMouseEnter={() => setHover(n.id)} onMouseLeave={() => setHover(null)} onFocus={() => setHover(n.id)} onBlur={() => setHover(null)}>
                <span className="wr-node__c wr-node__c--tl" aria-hidden="true" /><span className="wr-node__c wr-node__c--tr" aria-hidden="true" />
                <span className="wr-node__c wr-node__c--bl" aria-hidden="true" /><span className="wr-node__c wr-node__c--br" aria-hidden="true" />
                <span className="wr-node__title"><Icon size={13} aria-hidden="true" className={n.status === "running" ? "wr-spin" : ""} /> <span className="wr-node__name">{n.title}</span></span>
                <span className="wr-node__sub">{n.agent ? names.get(n.agent) ?? n.agent : n.type} · {n.status}</span>
              </button>
            );
          })}
        </div>
      </div>
      {node && <NodeDetail node={node} state={state} names={names} act={act} />}
      <Rounds state={state} names={names} />
    </div>
  );
}

/** The test, review and fix rounds, as rounds. */
export function roundsOf(nodes: FoundryNode[]) {
  const rounds = new Map<string, { test?: FoundryNode; review?: FoundryNode; fixes: FoundryNode[] }>();
  const at = (n: string) => { if (!rounds.has(n)) rounds.set(n, { fixes: [] }); return rounds.get(n)!; };
  for (const node of nodes) {
    const key = /^(?:test|review)-(\d+)$/.exec(node.id)?.[1] ?? /^task-fix-(\d+)-/.exec(node.id)?.[1];
    if (!key) continue;
    if (node.id.startsWith("test-")) at(key).test = node;
    else if (node.id.startsWith("review-")) at(key).review = node;
    else at(key).fixes.push(node);
  }
  return [...rounds.entries()].map(([number, round]) => ({ number, ...round }));
}

function Rounds({ state, names }: { state: FoundryState; names: Map<string, string> }) {
  const rounds = roundsOf(state.graph);
  if (!rounds.length) return null;
  return (
    <section className="wr-rounds" aria-label="Rounds of testing and fixing">
      <h3 className="wr-rounds__title">Test, review, fix</h3>
      {rounds.map((r) => {
        const tests = (r.test?.outputs?.tests as { status: string; passed: number; failed: number }[] | undefined) ?? [];
        const passed = tests.reduce((n, t) => n + (t.passed ?? 0), 0), failed = tests.reduce((n, t) => n + (t.failed ?? 0), 0);
        const issues = (r.review?.outputs?.issues as string[] | undefined) ?? [];
        return (
          <div key={r.number} className="wr-round">
            <span className="wr-round__n">Round {r.number}</span>
            <span className={`wr-round__part ${failed ? "is-bad" : ""}`}>🧪 {tests.length ? `${passed} passed${failed ? `, ${failed} failed` : ""}` : r.test?.status ?? "waiting"}</span>
            <span className={`wr-round__part ${issues.length ? "is-warn" : ""}`}>🔍 {r.review ? (r.review.status === "done" ? (issues.length ? `${issues.length} to fix` : "clean") : r.review.status) : "waiting"}</span>
            {r.fixes.map((f) => <span key={f.id} className={`wr-round__part ${f.status === "failed" ? "is-bad" : ""}`}>{logoFor(f.agent, names.get(f.agent) ?? f.agent)} {names.get(f.agent) ?? f.agent}: {f.status}</span>)}
          </div>
        );
      })}
    </section>
  );
}

function NodeDetail({ node, state, names, act }: { node: FoundryNode; state: FoundryState; names: Map<string, string>; act: (c: string, b: Record<string, unknown>) => Promise<unknown> }) {
  const wid = state.workflow.id;
  const open = OPEN.includes(state.workflow.status);
  const events = state.events.filter((e) => e.task === node.id || e.task === String(node.payload?.task ?? "\0")).slice(-30).reverse();
  const outputs = Object.entries(node.outputs ?? {});
  return (
    <aside className="wr-detail" aria-label={`Details of ${node.title}`}>
      <h3>{node.title}</h3>
      <dl className="wr-facts">
        <div><dt>Kind</dt><dd>{node.type}</dd></div>
        <div><dt>Status</dt><dd><StatusOf value={node.status} /></dd></div>
        {node.agent && <div><dt>Agent</dt><dd>{names.get(node.agent) ?? node.agent}</dd></div>}
        {node.deps.length > 0 && <div><dt>Waits for</dt><dd>{node.deps.join(", ")}</dd></div>}
        {node.started && <div><dt>Started</dt><dd><Time iso={node.started} /></dd></div>}
        {node.finished && <div><dt>Finished</dt><dd><Time iso={node.finished} /></dd></div>}
      </dl>
      {node.error && <p className="wr-error"><AlertTriangle size={14} aria-hidden="true" /> {node.error}</p>}
      {typeof node.payload?.description === "string" && node.payload.description && <p className="wr-muted">{node.payload.description}</p>}
      {outputs.length > 0 && <details open><summary>What it produced</summary><pre className="wr-pre">{JSON.stringify(node.outputs, null, 1)}</pre></details>}
      {events.length > 0 && <details><summary>What happened ({events.length})</summary><ul className="wr-log">{events.map((e) => <li key={e.id}><Time iso={e.at} /> {describe(e, names)}</li>)}</ul></details>}
      {open && (
        <div className="wr-detail__actions">
          {node.status === "waiting" && node.type === "approval" && <>
            <Button variant="primary" onClick={() => void act("approve", { workflow: wid, node: node.id })}>Approve</Button>
            <Button variant="danger" onClick={() => void act("reject", { workflow: wid, node: node.id })}>Reject</Button>
          </>}
          {node.status === "pending" && <Button variant="quiet" onClick={() => void act("priority", { workflow: wid, node: node.id, priority: node.priority + 1 })}>Do this sooner</Button>}
          {node.status === "pending" && TASK_NODES.includes(node.type) && (
            <label className="wr-reassign">Give it to <select value={node.agent} onChange={(e) => void act("reassign", { workflow: wid, node: node.id, agent: e.target.value })}>
              {state.team.filter((m) => !node.writes || m.permissions.writes).map((m) => <option key={m.agent} value={m.agent}>{m.name}</option>)}
            </select></label>
          )}
          {["pending", "running", "waiting"].includes(node.status) && node.type !== "goal" && <Button variant="quiet" onClick={() => void act("cancel_task", { workflow: wid, node: node.id })}>Cancel this step</Button>}
        </div>
      )}
    </aside>
  );
}

// ---- the timeline ----------------------------------------------------------------------------------------------------

export function describe(e: FoundryEventRow, names: Map<string, string>): string {
  const d = e.data as Record<string, string & number & Record<string, unknown>>;
  const who = names.get(e.agent) ?? e.agent;
  switch (e.type) {
    case "workflow": return `The workflow is ${d.status}${d.reason ? `: ${d.reason}` : ""}`;
    case "workflow.started": return "Work began";
    case "team.assembled": return `The team was assembled (${(d.members as unknown as unknown[]).length} people)`;
    case "agent.created": return `${d.name} joined as a new ${d.kind} specialist`;
    case "agent.reused": return `${d.name} was brought in again`;
    case "agent.turn": return d.state === "started" ? `${who} started: ${d.title}` : `${who} finished a step in ${d.seconds ?? "?"} s, ${d.tools ?? 0} tool calls`;
    case "agent.tool": return `${who}: ${d.title}${d.status === "failed" ? " (refused or failed)" : ""}`;
    case "node": return `${d.title}: ${d.note || d.status}${d.error ? ` (${d.error})` : ""}`;
    case "decision.opened": return `The team began to debate: ${d.question}`;
    case "decision": return `A decision is ${d.status}`;
    case "decision.absent": return `${who} could not take part in a decision`;
    case "loop.iteration": return `Round ${d.iteration} of test and review: ${d.issues} problem(s), ${d.outcome}`;
    case "loop.finished": return d.accepted ? `Accepted after ${d.iterations} round(s)` : `Not accepted after ${d.iterations} round(s): ${d.why}`;
    case "dependencies": return `Packages installed for the team: ${((d.installed as unknown as string[]) ?? []).join(", ") || "none"}`;
    case "intervention": return `You: ${(d.intervention as unknown as { kind: string; text: string }).kind} ${(d.intervention as unknown as { text: string }).text ?? ""}`;
    case "message": return `${(d.message as unknown as FoundryMessage).sender}: ${(d.message as unknown as FoundryMessage).content.slice(0, 120)}`;
    case "engineering": return String(d.text ?? d.kind);
    case "agent.hired": return `Brought in ${d.name} for: ${d.job}`;
    case "skill.used": return `Used the skill ${d.skill}${d.read && d.read !== "instructions" ? ` (${d.read})` : ""}`;
    case "skill.added": return `Added the skill ${d.name} to the project, in ${d.folder}`;
    case "skill.refused": return `Was refused the skill ${d.skill}: it is not theirs`;
    case "preview": return d.state === "running" ? `It started, at ${d.url}` : d.state === "failed" ? `It would not start: ${d.detail}` : "It was stopped";
    default: return e.type;
  }
}

function Timeline({ state, names }: { state: FoundryState; names: Map<string, string> }) {
  const rows = [...state.events].filter((e) => e.type !== "message" && e.type !== "decision").reverse();
  return <ol className="wr-timeline">{rows.map((e) => <li key={e.id} className={`wr-timeline__row wr-timeline__row--${e.type.split(".")[0]}`}><Time iso={e.at} /> <span>{describe(e, names)}</span></li>)}{!rows.length && <li>Nothing yet.</li>}</ol>;
}

// ---- the people on it ------------------------------------------------------------------------------------------------

function TeamRail({ state, onAsk }: { state: FoundryState; onAsk: (agent: string) => void }) {
  return (
    <ul className="wr-team">
      {state.team.map((m) => (
        <li key={m.agent} className={`wr-member wr-member--${m.state}`}>
          <Avatar id={m.agent} name={m.name} />
          <div>
            <p className="wr-member__name">{m.name}{!m.reused && <span className="wr-tag">new</span>}</p>
            <p className="wr-member__what">{m.state === "idle" ? m.why || "ready" : `${m.state === "thinking" ? "Thinking about" : "Working on"} ${m.task}`}</p>
            <p className="wr-msg__meta">{m.model || "balanced"}{m.tools_used ? ` · ${m.tools_used} tool calls` : ""}{m.permissions.writes ? "" : " · read-only"}{m.skills.length ? ` · ${m.skills.length} skill(s)` : ""}</p>
          </div>
          {OPEN.includes(state.workflow.status) && <button type="button" className="wr-link" aria-label={`Ask ${m.name} for a review`} onClick={() => onAsk(m.agent)}><MessageSquare size={14} aria-hidden="true" /></button>}
        </li>
      ))}
      {!state.team.length && <li className="wr-muted">The team is being assembled.</li>}
    </ul>
  );
}

const ORDER_TONES: Record<string, string> = { done: "ok", "not done": "danger", heard: "neutral", "with the team": "running", noted: "neutral" };

/** What you have told them to do, and how each one really stands. Only an independent review can mark one done. */
function Orders({ orders }: { orders: FoundryOrder[] }) {
  return (
    <ul className="wr-orders">
      {[...orders].reverse().map((o) => (
        <li key={o.id}>
          <p>{o.text}</p>
          <p className="wr-orders__state"><Status tone={(ORDER_TONES[o.state] ?? "neutral") as "ok"}>{o.state}</Status>{o.evidence && <span className="wr-muted"> {o.evidence}</span>}</p>
        </li>
      ))}
    </ul>
  );
}

// ---- the agent floor -------------------------------------------------------------------------------------------------

const arrowColour = (name: string) => { const all = ["#2e7dff", "#12a150", "#b154f0", "#e0841b", "#0fa3b1", "#d6336c", "#5c7cfa", "#7a9e00"]; let n = 0; for (const c of name) n += c.charCodeAt(0); return all[n % all.length] ?? "#2e7dff"; };

function Arrow({ colour, label }: { colour: string; label: string }) {
  return <span className="wr-arrow" style={{ color: colour }}><MousePointer2 size={13} fill="currentColor" aria-hidden="true" /><span style={{ background: colour }}>{label}</span></span>;
}

/** Everyone on the team, as they really are right now. */
function Floor({ state }: { state: FoundryState }) {
  const last = useMemo(() => {
    const out = new Map<string, FoundryEventRow>();
    for (const e of state.events) if (e.agent && ["agent.tool", "web", "computer.action", "artifact", "vision"].includes(e.type)) out.set(e.agent, e);
    return out;
  }, [state.events]);
  const lists = useMemo(() => { // each agent's own task list, as it last wrote it
    const out = new Map<string, { text: string; status: string }[]>();
    for (const e of state.events) if (e.type === "agent.todo" && e.agent && Array.isArray(e.data.items)) out.set(e.agent, e.data.items as { text: string; status: string }[]);
    return out;
  }, [state.events]);
  return (
    <div className="wr-floor">
      {state.team.map((m) => {
        const e = last.get(m.agent);
        const what = e ? String(e.data.title ?? e.data.label ?? e.data.query ?? e.data.url ?? e.data.path ?? e.data.what ?? "") : "";
        const busy = m.state !== "idle";
        return (
          <article key={m.agent} className={`wr-seat ${busy ? "is-busy" : ""}`}>
            <header><Avatar id={m.agent} name={m.name} /><div><h3>{m.name}</h3><p className="wr-muted">{m.work || m.role}</p></div><Status tone={busy ? "running" : "neutral"}>{busy ? "working" : "idle"}</Status></header>
            <p className="wr-seat__task">{busy ? m.task : m.why || "Ready when needed."}</p>
            {what && <p className="wr-seat__tool">{busy ? "Now" : "Last"}: {what}</p>}
            {busy && lists.get(m.agent)?.length ? (
              <ul className="wr-todo" aria-label={`${m.name}'s task list`}>
                {lists.get(m.agent)!.map((t, i) => (
                  <li key={`${i}-${t.text}`} className={`wr-todo__item wr-todo__item--${t.status}`}>
                    <span className="wr-todo__mark" aria-hidden="true">{t.status === "done" ? "✓" : t.status === "doing" ? "●" : "○"}</span>
                    <span>{t.text}</span><span className="sr-only"> ({t.status === "pending" ? "to do" : t.status})</span>
                  </li>
                ))}
              </ul>
            ) : null}
            <p className="wr-msg__meta"><Arrow colour={arrowColour(m.name)} label={m.name} /> <span>{m.model || "balanced"}</span>{m.tools_used ? <span>{m.tools_used} tool calls</span> : null}{m.writing && busy ? <span>{m.writing} pieces of reply so far</span> : null}</p>
          </article>
        );
      })}
      {!state.team.length && <Empty>The team is being assembled.</Empty>}
    </div>
  );
}

// ---- live view -------------------------------------------------------------------------------------------------------

/** A picture of an agent's private browser, drawn from what it really has on screen. No model sees this unless an agent asks to look. */
function BrowserCard({ workflow, b, tick }: { workflow: string; b: FoundryLive["browsers"][number]; tick: number }) {
  const { app } = useApp();
  const [src, setSrc] = useState("");
  useEffect(() => {
    if (!b.picture) return;
    let gone = false;
    let made = "";
    app.api.foundryPicture(workflow, b.agent).then((raw) => { if (gone) return; made = URL.createObjectURL(new Blob([raw], { type: "image/jpeg" })); setSrc((was) => { if (was) URL.revokeObjectURL(was); return made; }); }, () => undefined);
    return () => { gone = true; };
  }, [app, workflow, b.agent, b.picture, tick]);
  return (
    <figure className="wr-browser">
      <figcaption><Arrow colour={b.colour} label={b.name} /><span className="wr-muted" title={b.url}>{b.title || b.url || "No page yet"}</span>{b.signed_in && <Status tone="running">signed in</Status>}</figcaption>
      {src ? <img src={src} alt={`What ${b.name} has on its browser now`} /> : <div className="wr-browser__blank">No picture yet</div>}
      <p className="wr-muted">{b.last.action ? `${b.last.action}${b.last.label ? `: ${b.last.label}` : ""}` : "Nothing yet"} · its own private browser, not your screen</p>
    </figure>
  );
}

function LiveView({ workflow, open }: { workflow: string; open: boolean }) {
  const { app } = useApp();
  const [live, setLive] = useState<FoundryLive | null>(null);
  const [tick, setTick] = useState(0);
  const { act } = useAct();
  useEffect(() => {
    let stopped = false;
    const read = () => app.api.foundryDo("live", { workflow }).then((value) => { if (!stopped) { setLive(value as unknown as FoundryLive); setTick((t) => t + 1); } }, () => undefined);
    void read();
    const timer = open ? setInterval(read, 1500) : null;
    return () => { stopped = true; if (timer) clearInterval(timer); };
  }, [app, workflow, open]);
  if (!live) return <p className="wr-muted" role="status">Loading…</p>;
  const c = live.computer;
  return (
    <div className="wr-live">
      <section className={`wr-takeover ${c.taken_over ? "is-yours" : ""}`}>
        <div>
          <h3>{c.taken_over ? "The computer is yours" : "Your Mac"}</h3>
          <p className="wr-muted">{c.taken_over ? "No agent will touch your screen until you hand it back. When you do, they read the screen again first." : live.allowed ? "This job may use your screen. Agents work in the background and your mouse stays yours." : "This job has not asked to use your screen."}</p>
          {c.holder && <p>Borrowing the real pointer for one action: <Arrow colour={c.holder.colour} label={c.holder.name} /></p>}
          {c.waiting.length > 0 && <p className="wr-muted">Waiting for the pointer: {c.waiting.map((w) => w.name).join(", ")}</p>}
        </div>
        {c.taken_over ? <Button variant="primary" onClick={() => void act("hand_back", {})}><Play size={14} aria-hidden="true" /> Hand it back</Button> : <Button variant="danger" onClick={() => void act("take_over", { why: "you took over" })}><Hand size={14} aria-hidden="true" /> Take over</Button>}
      </section>
      {c.arrows.length > 0 && (
        <section>
          <h3>On your real screen</h3>
          <ul className="wr-arrows">{c.arrows.map((a) => <li key={a.name}><Arrow colour={a.colour} label={a.name} /><span>{a.action}{a.label ? ` ${a.label}` : ""}</span><span className="wr-muted">{a.app}{a.window ? `: ${a.window}` : ""}</span><span className="wr-muted">{a.how}</span></li>)}</ul>
        </section>
      )}
      {live.browsers.length > 0 ? <section className="wr-browsers">{live.browsers.map((b) => <BrowserCard key={b.agent} workflow={workflow} b={b} tick={tick} />)}</section> : !c.arrows.length && <Empty>No agent is using a browser or your screen right now. When one does, you will see it here.</Empty>}
      {live.actions.length > 0 && (
        <section>
          <h3>What they really did</h3>
          <ol className="wr-actions">{[...live.actions].reverse().slice(0, 20).map((a, i) => <li key={`${a.at}-${i}`} className={a.ok ? "" : "is-bad"}><Time iso={a.at} /> <strong>{readable(a.agent)}</strong> {a.action}{a.label ? `: ${a.label}` : ""} <span className="wr-muted">{a.where}{a.x != null ? ` at ${a.x}, ${a.y}` : ""}</span></li>)}</ol>
        </section>
      )}
    </div>
  );
}

// ---- the shared workspace --------------------------------------------------------------------------------------------

const size = (n: number) => (n > 1_000_000 ? `${(n / 1_000_000).toFixed(1)} MB` : n > 1024 ? `${Math.round(n / 1024)} KB` : `${n} bytes`);

function FileRow({ workflow, file }: { workflow: string; file: FoundryArtifact }) {
  const { app } = useApp();
  const [preview, setPreview] = useState("");
  const picture = ["png", "jpg", "jpeg", "gif", "webp"].includes(file.kind);
  const text = ["md", "txt", "csv", "json"].includes(file.kind);
  const [words, setWords] = useState("");
  const save = async () => {
    const raw = await app.api.foundryFile(workflow, file.path);
    const url = URL.createObjectURL(new Blob([raw]));
    const a = document.createElement("a");
    a.href = url; a.download = file.path.split("/").pop() ?? "file"; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  };
  const look = async () => {
    if (preview || words) { setPreview(""); setWords(""); return; }
    const raw = await app.api.foundryFile(workflow, file.path);
    if (picture) setPreview(URL.createObjectURL(new Blob([raw])));
    else setWords(new TextDecoder().decode(raw).slice(0, 6000));
  };
  return (
    <li className="wr-file">
      <div className="wr-file__line">
        <strong title={file.path}>{file.path}</strong>
        <span className="wr-muted">{file.kind} · {size(file.bytes)} · <Time iso={file.modified} />{file.versions > 1 ? ` · ${file.versions} versions` : ""}</span>
        <span className="wr-file__do">{(picture || text) && <button type="button" className="wr-link" onClick={() => void look()}>{preview || words ? "Hide" : "Look"}</button>}<button type="button" className="wr-link" aria-label={`Save ${file.path}`} onClick={() => void save()}><Download size={14} aria-hidden="true" /></button></span>
      </div>
      {preview && <img className="wr-file__picture" src={preview} alt={file.path} />}
      {words && <pre className="wr-file__words">{words}</pre>}
      {file.history[0] && <p className="wr-muted wr-file__last">{file.history[0].what}</p>}
    </li>
  );
}

function Files({ workflow }: { workflow: string }) {
  const { app } = useApp();
  const { data, error, reload } = useLive(() => app.api.foundryDo("artifacts", { workflow }) as unknown as Promise<{ files: FoundryArtifact[]; root: string }>, [workflow], 3000);
  if (error && !data) return <ErrorNote message={error} retry={reload} />;
  if (!data) return <p className="wr-muted" role="status">Loading…</p>;
  if (!data.files.length) return <Empty>Nothing has been made yet.</Empty>;
  return <ul className="wr-files">{data.files.map((f) => <FileRow key={f.path} workflow={workflow} file={f} />)}</ul>;
}

// ---- what XENIO can really do, and your vault ------------------------------------------------------------------------

function CanDo() {
  const { app } = useApp();
  const { data, error, reload } = useLive(() => app.api.foundryDo("capabilities", {}) as unknown as Promise<{ capabilities: FoundryCapability[] }>, [], 5000);
  if (error && !data) return <ErrorNote message={error} retry={reload} />;
  if (!data) return <p className="wr-muted" role="status">Loading…</p>;
  return (
    <>
      <p className="wr-muted wr-cando__intro">Found by asking this Mac, just now. If something is missing it says what would fix it, and no agent will pretend otherwise.</p>
      <ul className="wr-cando">{data.capabilities.map((c) => <li key={c.id} className={c.available ? "" : "is-missing"}>{c.available ? <Check size={15} aria-hidden="true" /> : <AlertTriangle size={15} aria-hidden="true" />}<div><p><strong>{c.name}</strong> <span className="wr-muted">{c.kind}</span></p><p className="wr-muted">{c.available ? c.how : `Needs ${c.missing}`}</p></div></li>)}</ul>
    </>
  );
}

/** A project XENIO built, on your GitHub: only when you click, never forced. */
function GitHubPanel({ state }: { state: FoundryState }) {
  const { app } = useApp();
  const wf = state.workflow;
  const folder = (wf.project || wf.delivered || "").split("/").filter(Boolean).pop() ?? "project";
  const [name, setName] = useState(wf.name || folder); // the name the plan chose, like arts-college-landing-page
  const [renaming, setRenaming] = useState(false);
  const [isPrivate, setPrivate] = useState(true);
  const [github, setGithub] = useState<{ remote: string; branch: string; ahead: number; behind: number } | null>((wf as unknown as { github?: { remote: string; branch: string; ahead: number; behind: number } }).github ?? null);
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState("");
  useEffect(() => { void app.api.foundryDo("github_status", { workflow: wf.id }).then((out) => setGithub((out as { github?: typeof github }).github ?? null), () => undefined); }, [app, wf.id]);
  const run = async (command: string, body: Record<string, unknown> = {}) => {
    setBusy(true); setSaid("");
    try {
      const out = (await app.api.foundryDo(command, { workflow: wf.id, ...body })) as { github?: typeof github };
      setGithub(out.github ?? null);
      setSaid(command === "github_publish" ? "It is on GitHub." : command === "github_push" ? "Pushed." : command === "github_rename" ? "Renamed. The old address still leads here." : "Pulled.");
      if (command === "github_rename") setRenaming(false);
    } catch (e) {
      setSaid((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="wr-result__actions wr-github">
      {github?.remote ? (
        <>
          <p><a href={github.remote} target="_blank" rel="noreferrer">{github.remote.replace("https://github.com/", "github.com/")}</a> <span className="wr-muted">{github.branch}{github.ahead > 0 ? `, ${github.ahead} commit${github.ahead === 1 ? "" : "s"} not pushed yet` : github.ahead < 0 ? ", not pushed yet" : ", up to date"}</span></p>
          <Button onClick={() => void run("github_push")} disabled={busy}>Push latest</Button>
          <Button variant="quiet" onClick={() => void run("github_pull")} disabled={busy}>Pull</Button>
          <Button variant="quiet" onClick={() => { setName(github.remote.split("/").filter(Boolean).pop() ?? name); setRenaming(!renaming); }} disabled={busy} aria-expanded={renaming}>Rename</Button>
          {renaming && (
            <form onSubmit={(e) => { e.preventDefault(); void run("github_rename", { name: name.trim() }); }} className="wr-github__new">
              <div className="field"><label htmlFor={`gh-rename-${wf.id}`}>New name on GitHub</label><input id={`gh-rename-${wf.id}`} value={name} onChange={(e) => setName(e.target.value)} /></div>
              <Button type="submit" disabled={busy || !/^[A-Za-z0-9._-]{1,100}$/.test(name.trim())}>Rename the repository</Button>
            </form>
          )}
        </>
      ) : (
        <form onSubmit={(e) => { e.preventDefault(); void run("github_publish", { name, private: isPrivate }); }} className="wr-github__new">
          <div className="field"><label htmlFor={`gh-${wf.id}`}>Put it on your GitHub as</label><input id={`gh-${wf.id}`} value={name} onChange={(e) => setName(e.target.value)} /></div>
          <label className="wr-check"><input type="checkbox" checked={isPrivate} onChange={(e) => setPrivate(e.target.checked)} /> Private</label>
          <Button type="submit" disabled={busy || !/^[A-Za-z0-9._-]{1,100}$/.test(name.trim())}>Create the repository and push</Button>
        </form>
      )}
      {said && <p className="wr-muted" role="status">{said}</p>}
    </div>
  );
}

/** Figma, through XENIO's plugin in the Figma app: whether it is open, the pairing code, and how to set it up once. */
function FigmaBox() {
  const { app } = useApp();
  const { data } = useLive(() => app.api.foundryDo("figma_status", {}) as unknown as Promise<{ listening: boolean; error: string; connected: boolean; file: string; page: string; token: string; plugin: string }>, [], 4000);
  const [shown, setShown] = useState(false);
  const [copied, setCopied] = useState("");
  if (!data) return null;
  const copy = (text: string, what: string) => { void navigator.clipboard?.writeText(text).then(() => setCopied(`${what} copied`), () => setCopied("Could not copy")); };
  return (
    <section className="wr-vault__how wr-figma">
      <h3>Figma {data.connected ? <Status tone="ok">Connected{data.file ? `: ${data.file}` : ""}</Status> : <Status tone="neutral">Not connected</Status>}</h3>
      {data.connected ? <p>Agents can draw in this file now. Ask the team for a design "in Figma". The first drawing in each job asks you, and everything can be undone in Figma.</p> : (
        <ol className="wr-figma__steps">
          <li>In the Figma app, open the file to design in.</li>
          <li>The first time only: Plugins, Development, Import plugin from manifest, and pick <code>{data.plugin}</code> <button type="button" className="wr-link" onClick={() => copy(data.plugin, "Path")}>Copy path</button></li>
          <li>Plugins, Development, XENIO. Paste the pairing code below, and Connect.</li>
        </ol>
      )}
      <p className="wr-figma__code"><span className="wr-muted">Pairing code:</span> <code>{shown ? data.token : "••••••••••••"}</code> <button type="button" className="wr-link" onClick={() => setShown(!shown)}>{shown ? "Hide" : "Show"}</button> <button type="button" className="wr-link" onClick={() => copy(data.token, "Pairing code")}>Copy</button></p>
      {copied && <p className="wr-muted" role="status">{copied}</p>}
      {!data.listening && <p className="wr-error">XENIO cannot listen for Figma: {data.error || "restart XENIO"}.</p>}
    </section>
  );
}

/** MCP servers you connect: a program on this Mac or an address. Agents use one only if you switch it on, and ask you the first time in each job. */
function Connectors() {
  const { app } = useApp();
  const { data, error, reload } = useLive(() => app.api.foundryDo("mcp_list", {}) as unknown as Promise<{ servers: FoundryMcpServer[] }>, [], 10000);
  const { act, busy } = useAct();
  const [paste, setPaste] = useState(false);
  const [config, setConfig] = useState("");
  const [name, setName] = useState("");
  const [where, setWhere] = useState("");
  const [token, setToken] = useState("");
  const [agents, setAgents] = useState(true);
  const [tested, setTested] = useState<Record<string, string>>({});
  const [removing, setRemoving] = useState<string | null>(null);
  const run = async (command: string, body: Record<string, unknown>) => { const out = await act(command, body); reload(); return out; };
  const add = async (e: FormEvent) => {
    e.preventDefault();
    const address = /^https?:\/\//.test(where.trim());
    const body = paste ? { config, token } : { name, ...(address ? { url: where.trim() } : { command: where.trim() }), token, for_agents: agents };
    const out = await run("mcp_add", body);
    if (out) { setConfig(""); setName(""); setWhere(""); setToken(""); }
  };
  const test = async (server: string) => {
    setTested((t) => ({ ...t, [server]: "Connecting…" }));
    const out = (await act("mcp_test", { name: server })) as { tools?: { name: string }[] } | undefined;
    setTested((t) => ({ ...t, [server]: out?.tools ? `Connected: ${out.tools.length} tool${out.tools.length === 1 ? "" : "s"}.` : "It did not connect: see the message above." }));
    reload();
  };
  if (error && !data) return <ErrorNote message={error} retry={reload} />;
  if (!data) return <p className="wr-muted" role="status">Loading…</p>;
  return (
    <div className="wr-connectors">
      <FigmaBox />
      <section className="wr-vault__how">
        <h3>Connect a service (MCP)</h3>
        <p>Add an MCP server: a program on this Mac, or an address. Agents can use it only if you switch it on, and the first use in each job asks you. A token goes into your Mac's Keychain, never into a file, a log or a model.</p>
        <p className="wr-muted">Some services only let apps they have approved sign in. Figma's MCP address is one of them: use the Figma box above instead.</p>
      </section>
      <form className="wr-connectors__add" onSubmit={(e) => void add(e)}>
        <p className="segmented" role="tablist" aria-label="How to add it">
          <button type="button" role="tab" aria-selected={!paste} className={!paste ? "is-on" : ""} onClick={() => setPaste(false)}>Fill in</button>
          <button type="button" role="tab" aria-selected={paste} className={paste ? "is-on" : ""} onClick={() => setPaste(true)}>Paste a config</button>
        </p>
        {paste ? (
          <div className="field"><label htmlFor="mcp-config">The mcpServers block, as you would give it to any MCP app</label><textarea id="mcp-config" rows={6} value={config} onChange={(e) => setConfig(e.target.value)} placeholder={'{ "mcpServers": { "github": { "command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"], "env": { "GITHUB_TOKEN": "…" } } } }'} /></div>
        ) : (
          <div className="wr-limits">
            <div className="field"><label htmlFor="mcp-name">Name</label><input id="mcp-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="github" /></div>
            <div className="field"><label htmlFor="mcp-where">Command or address</label><input id="mcp-where" value={where} onChange={(e) => setWhere(e.target.value)} placeholder="npx -y @some/mcp-server, or https://…/mcp" /></div>
          </div>
        )}
        <div className="field"><label htmlFor="mcp-token">Access token, if it needs one</label><input id="mcp-token" type="password" autoComplete="off" value={token} onChange={(e) => setToken(e.target.value)} placeholder="Goes to your Keychain" /></div>
        {!paste && <label className="wr-check"><input type="checkbox" checked={agents} onChange={(e) => setAgents(e.target.checked)} /> Let agents use it (they ask you the first time in each job)</label>}
        <p><Button variant="primary" type="submit" disabled={busy || (paste ? !config.trim() : !name.trim() || !where.trim())}>Add</Button></p>
      </form>
      {removing && <ConfirmDialog title={`Remove ${removing}?`} body="Its token is taken out of your Keychain too. Agents can no longer use it." confirmLabel="Remove it" onCancel={() => setRemoving(null)} onConfirm={() => { const n = removing; setRemoving(null); void run("mcp_remove", { name: n }); }} />}
      {data.servers.length === 0 ? <Empty>No services connected yet.</Empty> : (
        <ul className="wr-cando">{data.servers.map((s) => (
          <li key={s.name}>
            <div>
              <p><strong>{s.name}</strong> <span className="wr-muted">{s.transport === "http" ? s.url : s.command.join(" ")}</span></p>
              <p className="wr-muted">{s.has_token ? "Token in your Keychain. " : ""}{s.tools.length ? `${s.tools.length} tools: ${s.tools.slice(0, 8).map((t) => t.name).join(", ")}${s.tools.length > 8 ? "…" : ""}` : "Not tested yet."}</p>
              {tested[s.name] && <p className="wr-muted" role="status">{tested[s.name]}</p>}
              <p className="wr-connectors__row">
                <Button onClick={() => void test(s.name)} disabled={busy}>Test</Button>
                <label className="wr-check"><input type="checkbox" checked={s.for_agents} onChange={(e) => void run("mcp_update", { name: s.name, for_agents: e.target.checked })} /> Agents may use it</label>
                <Button variant="danger" onClick={() => setRemoving(s.name)}>Remove</Button>
              </p>
            </div>
          </li>
        ))}</ul>
      )}
    </div>
  );
}

function Vault() {
  const { app } = useApp();
  const { data, error, reload } = useLive(() => app.api.foundryDo("vault_status", {}) as unknown as Promise<VaultState>, [], 5000);
  const { act, busy } = useAct();
  const [removing, setRemoving] = useState<string | null>(null);
  const [site, setSite] = useState("");
  const run = async (command: string, body: Record<string, unknown> = {}) => { await act(command, body); reload(); };
  if (error && !data) return <ErrorNote message={error} retry={reload} />;
  if (!data) return <p className="wr-muted" role="status">Loading…</p>;
  return (
    <div className="wr-vault">
      <section className="wr-vault__how">
        <h3><KeyRound size={15} aria-hidden="true" /> Your logins, placed and never seen</h3>
        <p>When an agent reaches a sign-in page you get a fingerprint request naming the website. Once you touch the sensor, XENIO places your login on that page and the agent carries on. No agent, model or part of XENIO ever sees it, and there is no way to read a password back out of here.</p>
        <p className="wr-muted">Logins are encrypted with AES-256. The key is locked by your fingerprint inside the Secure Enclave chip and by your secret password, which is never stored. Each login only ever goes to its own website. You type into a window of the Mac's own, never into this page.</p>
      </section>
      {!data.exists ? <Button variant="primary" disabled={busy} onClick={() => void run("vault_setup")}><KeyRound size={14} aria-hidden="true" /> Create your vault</Button> : (
        <>
          <p className="wr-vault__actions">
            <Button variant="primary" disabled={busy} onClick={() => void run("vault_add")}>Add a login</Button>
            {data.touch_ready ? <Status tone="ok"><Fingerprint size={13} aria-hidden="true" /> Touch ID is on</Status> : data.touch_available ? <Button disabled={busy} onClick={() => void run("vault_touch")}><Fingerprint size={14} aria-hidden="true" /> Turn on Touch ID</Button> : <span className="wr-muted">This Mac has no Touch ID: your secret password opens it.</span>}
          </p>
          <form className="wr-inline" onSubmit={(e) => { e.preventDefault(); if (site.trim()) { void run("vault_open_site", { url: site.trim() }); setSite(""); } }}>
            <input aria-label="A website to sign in to" placeholder="Sign in to a website, like accounts.google.com" value={site} onChange={(e) => setSite(e.target.value)} />
            <Button type="submit" disabled={!site.trim() || busy}>Open it</Button>
          </form>
          <p className="wr-muted">A window opens. A saved login is placed after your fingerprint. If you sign in by hand, XENIO offers to save it for next time.</p>
          {data.items.length ? <ul className="wr-files">{data.items.map((i) => <li key={i.id} className="wr-file"><div className="wr-file__line"><strong>{i.host}</strong><span className="wr-muted">{i.label ? `${i.label} · ` : ""}added <Time iso={i.added} /></span><span className="wr-file__do"><button type="button" className="wr-link" aria-label={`Remove the login for ${i.host}`} onClick={() => setRemoving(i.id)}><Trash2 size={14} aria-hidden="true" /></button></span></div></li>)}</ul> : <Empty>No logins yet.</Empty>}
        </>
      )}
      {busy && <p className="wr-muted" role="status">Look for a window from XENIO on your screen.</p>}
      {removing && <ConfirmDialog title="Remove this login?" body="It needs your fingerprint or secret password, and it cannot be undone." confirmLabel="Remove" onCancel={() => setRemoving(null)} onConfirm={() => { const item = removing; setRemoving(null); void run("vault_remove", { item }); }} />}
    </div>
  );
}

function Requirements({ state, act }: { state: FoundryState; act: (c: string, b: Record<string, unknown>) => Promise<unknown> }) {
  const [text, setText] = useState("");
  const wf = state.workflow;
  const open = OPEN.includes(wf.status);
  const add = (e: FormEvent) => { e.preventDefault(); if (!text.trim()) return; void act("constraints", { workflow: wf.id, constraints: [...(wf.constraints ?? []), text.trim()] }); setText(""); };
  return (
    <div className="wr-reqs">
      {wf.constraints?.length ? <ul>{wf.constraints.map((c) => <li key={c}>{c} {open && <button type="button" className="wr-link" aria-label={`Remove ${c}`} onClick={() => void act("constraints", { workflow: wf.id, constraints: wf.constraints.filter((x) => x !== c) })}>remove</button>}</li>)}</ul> : <p className="wr-muted">Nothing beyond the goal.</p>}
      {open && <>
        <form className="wr-inline" onSubmit={add}><input aria-label="Add a requirement" placeholder="Add a requirement" value={text} onChange={(e) => setText(e.target.value)} /><Button type="submit" disabled={!text.trim()}>Add</Button></form>
        <label className="wr-check"><input type="checkbox" checked={Boolean(wf.require_evidence)} onChange={(e) => void act("require_evidence", { workflow: wf.id, required: e.target.checked })} /> Decisions need evidence XENIO can check</label>
      </>}
    </div>
  );
}

// ---- one workflow ----------------------------------------------------------------------------------------------------

function RunningNow({ state, act }: { state: FoundryState; act: (c: string, b: Record<string, unknown>) => Promise<unknown> }) {
  const [busy, setBusy] = useState(false);
  const [shown, setShown] = useState(true);
  const preview = state.preview;
  const wid = state.workflow.id;
  const start = async () => { setBusy(true); await act("preview_start", { workflow: wid }); setBusy(false); };
  if (preview?.state === "running" && preview.url) {
    return (
      <section className="wr-run" aria-label="The app, running">
        <div className="wr-run__bar">
          <span className="wr-run__dot" aria-hidden="true" /><strong>It&rsquo;s running</strong>
          <a className="wr-run__url" href={preview.url} target="_blank" rel="noopener noreferrer">{preview.url} <ExternalLink size={12} aria-hidden="true" /></a>
          <span className="wr-run__actions">
            <button type="button" className="wr-link" onClick={() => setShown(!shown)}>{shown ? "Hide" : "Show"}</button>
            <button type="button" className="wr-icon" aria-label="Start it again" title="Start it again" disabled={busy} onClick={() => void start()}><RotateCw size={14} aria-hidden="true" /></button>
            <button type="button" className="wr-icon" aria-label="Stop it" title="Stop it" onClick={() => void act("preview_stop", { workflow: wid })}><Square size={14} aria-hidden="true" /></button>
          </span>
        </div>
        {shown && <iframe className="wr-run__frame" src={preview.url} title="The app the team built" sandbox="allow-scripts allow-forms allow-same-origin allow-popups" />}
      </section>
    );
  }
  const kind = state.workflow.team?.brief?.kind ?? "software";
  if (NOTHING_TO_RUN.includes(kind)) return null; // a report or a profile edit has nothing to start: offering to "fix it so it starts" sent a real job round again for nothing
  if (OPEN.includes(state.workflow.status) || !state.workflow.run_id) return null;
  const failed = preview?.state === "failed";
  return (
    <div className={`wr-run wr-run--off ${failed ? "wr-run--failed" : ""}`}>
      <div className="wr-run__why">
        <span className={failed ? "" : "wr-muted"}>{busy ? "Starting it, and installing what it needs…" : failed ? <><AlertTriangle size={14} aria-hidden="true" /> It would not start: {preview?.detail}.</> : "It isn’t running right now."}</span>
        {failed && preview?.log && <details><summary>What it said</summary><pre className="wr-pre">{preview.log}</pre></details>}
      </div>
      <span className="wr-run__actions">
        {failed && !busy && <button type="button" className="btn" onClick={() => void act("change", { workflow: wid, text: `It would not start: ${preview?.detail}. Fix it so it starts.` })}>Ask the team to fix it</button>}
        <button type="button" className="btn btn--primary" disabled={busy} onClick={() => void start()}><Play size={14} aria-hidden="true" /> {busy ? "Starting…" : failed ? "Try again" : "Run it"}</button>
      </span>
    </div>
  );
}

function Result({ state }: { state: FoundryState }) {
  const { app } = useApp();
  const wf = state.workflow;
  const [note, setNote] = useState("");
  const [deleting, setDeleting] = useState(false);
  const copy = () => { void navigator.clipboard?.writeText(wf.delivered).then(() => setNote("Copied"), () => setNote("Could not copy")); };
  const leave = async (action: "apply" | "push" | "pull_request") => {
    try {
      await app.api.runAction(wf.run_id, action);
      setNote(action === "apply" ? "Written into your files." : "Done.");
    } catch (e) {
      setNote((e as Error).message);
    }
  };
  const remove = async () => {
    setDeleting(false);
    try {
      await app.api.foundryDo("delete", { workflow: wf.id });
      window.location.hash = href("warroom");
    } catch (e) {
      setNote((e as Error).message);
    }
  };
  const ok = wf.status === "done";
  return (
    <div className={`wr-result ${ok ? "wr-result--ok" : "wr-result--bad"}`}>
      <div className="wr-result__head">
        <h3>{ok ? "Finished" : wf.status === "stopped" ? "Stopped" : "Not finished"}</h3>
        <button type="button" className="wr-link wr-link--quiet" onClick={() => setDeleting(true)}><Trash2 size={13} aria-hidden="true" /> Delete this work</button>
      </div>
      <p>{wf.outcome}</p>
      {wf.delivered && <p className="wr-result__where"><FolderOpen size={15} aria-hidden="true" /> <code>{wf.delivered}</code> <button type="button" className="wr-link" onClick={copy} aria-label="Copy the path"><Copy size={14} aria-hidden="true" /></button></p>}
      {!wf.delivered && !wf.greenfield && wf.run_id && wf.status === "done" && (
        <div className="wr-result__actions">
          <p className="wr-muted">The work is in its own isolated copy. Nothing has touched your project. Each of these asks you first:</p>
          <Button onClick={() => void leave("apply")}>Apply to my files</Button>
          <Button variant="quiet" onClick={() => void leave("push")}>Push the branch</Button>
          <Button variant="quiet" onClick={() => void leave("pull_request")}>Open a pull request</Button>
        </div>
      )}
      {wf.greenfield && wf.delivered && <GitHubPanel state={state} />}
      {note && <p className="wr-muted" role="status">{note}</p>}
      {deleting && <ConfirmDialog title="Delete this work?" body="Its conversation, decisions and graph are removed. Any project it built stays in its folder." confirmLabel="Delete it" onCancel={() => setDeleting(false)} onConfirm={() => void remove()} />}
    </div>
  );
}

function Workflow({ id }: { id: string }) {
  const { app } = useApp();
  const { data: state, error, reload } = useLive(() => app.api.foundryState(id), [id]);
  const { act: raw } = useAct();
  const act = useCallback(async (command: string, body: Record<string, unknown>) => { const out = await raw(command, body); reload(); return out; }, [raw, reload]);
  const [chosen, setTab] = useState<"result" | "talk" | "floor" | "live" | "files" | "decide" | "graph" | "time" | null>(null);
  const [stopping, setStopping] = useState(false);
  const [reviewer, setReviewer] = useState<string | null>(null);
  const moving = state?.team.some((m) => m.state !== "idle") ?? false;
  useEffect(() => { // a long reply gives off no events, and it should still be seen moving
    if (!moving) return;
    const timer = setInterval(reload, 2500);
    return () => clearInterval(timer);
  }, [moving, reload]);
  const [about, setAbout] = useState("");
  const names = useMemo(() => new Map((state?.team ?? []).map((m) => [m.agent, m.name])), [state?.team]);
  if (error && !state) return <ErrorNote message={error} retry={reload} />;
  if (!state) return <p className="wr-muted" role="status">Loading…</p>;
  const wf = state.workflow;
  const open = OPEN.includes(wf.status);
  const live = state.preview?.state === "running";
  const hasResult = !open || live;
  const tab = chosen === "result" && !hasResult ? "talk" : chosen ?? (open ? "talk" : "result"); // finished work opens on what it made, unless you chose otherwise
  const waitingDecision = state.decisions.some((d) => d.status === "needs_human");
  const waitingGate = state.graph.find((n) => n.status === "waiting" && n.type === "approval");
  const counted = state.graph.filter((n) => TASK_NODES.includes(n.type));
  const done = counted.filter((n) => n.status === "done").length;
  const send = async (text: string, to: string, uploads: string[]) => { await act(open ? "message" : "change", open ? { workflow: wf.id, text, to, ...(uploads.length ? { uploads } : {}) } : { workflow: wf.id, text, ...(uploads.length ? { uploads } : {}) }); };
  const working = state.team.some((m) => m.state !== "idle");
  return (
    <div className="wr-flow">
      <header className="wr-top">
        <a className="wr-back" href={href("warroom")}><ChevronLeft size={15} aria-hidden="true" /> All work</a>
        <h2 className="wr-goal" title={wf.goal}>{wf.goal}</h2>
        <div className="wr-top__row">
          <StatusOf value={wf.status === "waiting" ? "waiting" : wf.status} />
          {wf.milestone > 1 && <Status tone="neutral">Milestone {wf.milestone}</Status>}
          {counted.length > 0 && <span className="wr-muted">{done} of {counted.length} tasks done</span>}
          <Spend state={state} />
          {open && <LimitsControl state={state} act={act} />}
          {open && <span className="wr-top__actions">
            {wf.paused ? <Button onClick={() => void act("resume", { workflow: wf.id })}><Play size={14} aria-hidden="true" /> Resume</Button> : <Button onClick={() => void act("pause", { workflow: wf.id })}><Pause size={14} aria-hidden="true" /> Pause</Button>}
            <Button variant="danger" onClick={() => setStopping(true)}><Square size={14} aria-hidden="true" /> Stop</Button>
          </span>}
        </div>
      </header>
      <ApprovalsHere />
      {stopping && <ConfirmDialog title="Stop this work?" body="Agents stop now and nothing else runs. What was finished stays in its own workspace." confirmLabel="Stop it" onCancel={() => setStopping(false)} onConfirm={() => { setStopping(false); void act("stop", { workflow: wf.id }); }} />}
      {open && wf.attention && (
        <div className="wr-attention" role="status">
          <ShieldAlert size={16} aria-hidden="true" />
          <div>
            <p>{wf.attention}</p>
            {waitingGate && <p className="wr-attention__actions"><Button variant="primary" onClick={() => void act("approve", { workflow: wf.id, node: waitingGate.id })}>Approve</Button> <Button variant="danger" onClick={() => void act("reject", { workflow: wf.id, node: waitingGate.id })}>Reject</Button></p>}
            {waitingDecision && <p className="wr-attention__actions"><Button variant="primary" onClick={() => setTab("decide")}>See the decision</Button></p>}
            {wf.pause_reason?.startsWith("out of budget") && <p className="wr-attention__actions"><Button variant="primary" onClick={() => void act("extend_budget", { workflow: wf.id })}>Add more budget</Button></p>}
          </div>
        </div>
      )}
      <div className="wr-columns">
        <main className="wr-main">
          <div className="segmented" role="tablist" aria-label="Show">
            {([...(hasResult ? [["result", open ? "Running" : "Result", 0] as const] : []), ["talk", "Conversation", state.messages.length], ["floor", working ? "Floor •" : "Floor", 0], ["live", "Live", 0], ["files", "Files", 0], ["decide", "Decisions", state.decisions.length], ["graph", "Graph", state.graph.length], ["time", "Timeline", 0]] as const).map(([key, label, n]) => (
              <button key={key} type="button" role="tab" aria-selected={tab === key} className={tab === key ? "is-on" : ""} onClick={() => setTab(key)}>{label}{n ? ` (${n})` : ""}</button>
            ))}
          </div>
          <div className={`wr-pane ${tab === "talk" ? "wr-pane--talk" : ""}`}>
            {tab === "result" && <div className="wr-resulting">{!open && <Result state={state} />}<RunningNow state={state} act={act} /></div>}
            {tab === "talk" && <Conversation state={state} names={names} />}
            {tab === "floor" && <Floor state={state} />}
            {tab === "live" && <LiveView workflow={wf.id} open={open} />}
            {tab === "files" && <Files workflow={wf.id} />}
            {tab === "decide" && (state.decisions.length ? <div className="wr-decisions">{[...state.decisions].reverse().map((d) => <DecisionCard key={d.id} d={d} workflowOpen={open} act={act} />)}</div> : <Empty>No decision has needed a debate yet.</Empty>)}
            {tab === "graph" && <Graph state={state} names={names} act={act} />}
            {tab === "time" && <Timeline state={state} names={names} />}
          </div>
          <Composer team={state.team} send={send} finished={!open} />
        </main>
        <aside className="wr-rail">
          <Section title="The team"><TeamRail state={state} onAsk={setReviewer} /></Section>
          <ModelsUsed state={state} />
          {reviewer && (
            <form className="wr-inline wr-inline--stack" onSubmit={(e) => { e.preventDefault(); void act("review", { workflow: wf.id, agent: reviewer, text: about.trim() || "Review the work so far." }); setReviewer(null); setAbout(""); }}>
              <label>What should {names.get(reviewer) ?? reviewer} review? <input value={about} onChange={(e) => setAbout(e.target.value)} placeholder="The work so far" /></label>
              <span><Button type="submit">Ask</Button> <Button variant="quiet" type="button" onClick={() => setReviewer(null)}>Never mind</Button></span>
            </form>
          )}
          {(wf.orders?.length ?? 0) > 0 && <Section title="Your orders"><Orders orders={wf.orders ?? []} /></Section>}
          <Section title="What you require"><Requirements state={state} act={act} /></Section>
          {state.interventions.length > 0 && <Section title="What you did"><ul className="wr-yours">{[...state.interventions].reverse().slice(0, 8).map((i) => <li key={i.id}><p><strong>{i.kind.replace("_", " ")}</strong>{i.text ? `: ${i.text}` : ""}</p>{i.effect.map((x) => <p key={x} className="wr-muted">{x}</p>)}</li>)}</ul></Section>}
        </aside>
      </div>
    </div>
  );
}

// ---- the deck --------------------------------------------------------------------------------------------------------

function Command({ overview, onStarted }: { overview: FoundryOverview; onStarted: (id: string) => void }) {
  const { act, busy } = useAct();
  const [goal, setGoal] = useState("");
  const [more, setMore] = useState(false);
  const [path, setPath] = useState("");
  const [careful, setCareful] = useState(false);
  const [job, setJob] = useState(shown(overview.limits?.job_tokens));
  const [task, setTask] = useState(shown(overview.limits?.task_tokens));
  const [saved, setSaved] = useState("");
  const pictures = usePictures();
  const go = async (e?: FormEvent) => {
    e?.preventDefault();
    if (!goal.trim() || busy) return;
    const out = await act("start", { goal: goal.trim(), path: path.trim(), autonomy: careful ? "careful" : "supervisor", job_tokens: asTokens(job), task_tokens: asTokens(task), ...(pictures.files.length ? { uploads: pictures.files.map((f) => f.id) } : {}) });
    if (out && typeof out.id === "string") onStarted(out.id);
  };
  const remember = async () => {
    const out = await act("set_limits", { job_tokens: asTokens(job), task_tokens: asTokens(task) });
    if (out) setSaved("Saved: every new job starts with these limits.");
  };
  return (
    <form className="wr-command" onSubmit={(e) => void go(e)}>
      <label htmlFor="wr-goal" className="wr-command__label">What should the team do?</label>
      <textarea id="wr-goal" rows={3} value={goal} onChange={(e) => setGoal(e.target.value)} placeholder="Build a gym app. Research ten competitors and make me a deck. Update my profile on a website. Anything done on a computer." onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) void go(); }} />
      {overview.can_think === false && <p className="wr-error">The team needs a Nabh key to think. Add NABH_API_KEY to XENIO's .env.</p>}
      <Pictures pictures={pictures} />
      <div className="wr-command__row">
        <Button variant="primary" type="submit" disabled={!goal.trim() || busy || overview.can_think === false}>{busy ? "Starting…" : "Start"}</Button>
        <AttachButton pictures={pictures} />
        <button type="button" className="wr-link" aria-expanded={more} onClick={() => setMore(!more)}>{more ? "Fewer options" : "Options"}</button>
        <p className="wr-muted wr-command__hint">You give the command. XENIO runs the team, and only comes to you for what is really yours to decide. Talk to them any time.</p>
      </div>
      {more && (
        <div className="wr-command__more">
          <div className="field"><label htmlFor="wr-path">Work on an existing project (leave empty for a new one)</label><input id="wr-path" value={path} onChange={(e) => setPath(e.target.value)} placeholder="/Users/you/projects/my-app" /></div>
          <label className="wr-check"><input type="checkbox" checked={careful} onChange={(e) => setCareful(e.target.checked)} /> Ask me before building, and on every judgment call</label>
          <LimitFields id="wr-new" job={job} task={task} setJob={(v) => { setJob(v); setSaved(""); }} setTask={(v) => { setTask(v); setSaved(""); }} />
          <p className="wr-command__row"><Button onClick={() => void remember()}>Make these my default</Button>{saved && <span className="wr-muted" role="status">{saved}</span>}</p>
        </div>
      )}
    </form>
  );
}

function WorkList({ overview, reload }: { overview: FoundryOverview; reload: () => void }) {
  const { act } = useAct();
  const [stopping, setStopping] = useState(false);
  const [deleting, setDeleting] = useState<string | null>(null);
  if (!overview.workflows.length) return <Empty>Nothing yet. Give the team its first command.</Empty>;
  const running = overview.workflows.filter((w) => OPEN.includes(w.status)).length;
  return (
    <>
    {running > 0 && <p className="wr-stopall"><span className="wr-muted">{running} running</span> <Button variant="danger" onClick={() => setStopping(true)}><Square size={14} aria-hidden="true" /> Stop everything</Button></p>}
    {deleting && <ConfirmDialog title="Delete this work?" body="Its conversation, decisions and graph are removed. Any project it built stays in its folder." confirmLabel="Delete it" onCancel={() => setDeleting(null)} onConfirm={() => { const id = deleting; setDeleting(null); void act("delete", { workflow: id }).then(reload); }} />}
    {stopping && <ConfirmDialog title="Stop everything?" body="Every agent on every piece of work stops now. What was finished stays in its own workspace." confirmLabel="Stop everything" onCancel={() => setStopping(false)} onConfirm={() => { setStopping(false); void act("stop_all").then(reload); }} />}
    <ul className="wr-list">
      {overview.workflows.map((w) => (
        <li key={w.id} className="wr-list__item">
          <a href={href("warroom", w.id)} className="wr-list__row">
            <span className="wr-list__goal">{w.goal}</span>
            {w.attention && OPEN.includes(w.status) ? <span className="wr-list__needs">{w.attention}</span> : w.outcome ? <span className="wr-muted wr-list__out">{w.outcome}</span> : null}
            <span className="wr-list__meta"><StatusOf value={w.status} /><Time iso={w.updated} />{w.budget ? <span>{tokens(w.budget.tokens_estimated)} tokens</span> : null}</span>
          </a>
          {!OPEN.includes(w.status) && <button type="button" className="wr-icon wr-list__delete" aria-label={`Delete: ${w.goal}`} title="Delete this work" onClick={() => setDeleting(w.id)}><Trash2 size={14} aria-hidden="true" /></button>}
        </li>
      ))}
    </ul>
    </>
  );
}

function Agents({ overview, reload }: { overview: FoundryOverview; reload: () => void }) {
  const { act } = useAct();
  const run = async (c: string, b: Record<string, unknown>) => { await act(c, b); reload(); };
  const [model, setModel] = useState<Record<string, string>>({});
  return (
    <ul className="wr-agents">
      {overview.agents.map((a: FoundryAgent) => (
        <li key={a.id} className={`wr-agent wr-agent--${a.state}`}>
          <Avatar id={a.id} name={a.name} />
          <div className="wr-agent__body">
            <p className="wr-member__name">{a.name} <span className="wr-tag">{a.source === "builtin" ? "built in" : a.kind}</span>{a.state !== "enabled" && <span className="wr-tag wr-tag--off">{a.state}</span>}</p>
            <p className="wr-muted">{a.description.slice(0, 180)}</p>
            <p className="wr-msg__meta">{a.permissions.writes ? "Can change files in its task's scope" : "Read-only"} · {a.permissions.tools.length} tools · {a.performance.runs ? `${a.performance.succeeded} of ${a.performance.runs} tasks succeeded, ${a.performance.workflows} project(s)` : "no work yet"}{a.skills.length ? ` · ${a.skills.length} skill(s)` : ""}</p>
            <div className="wr-agent__actions">
              <label className="wr-reassign">Model <input list="wr-seats" aria-label={`Model for ${a.name}`} value={model[a.id] ?? a.model} placeholder="balanced" onChange={(e) => setModel({ ...model, [a.id]: e.target.value })} onBlur={() => { if (model[a.id] !== undefined && model[a.id] !== a.model) void run("agent_update", { agent: a.id, changes: { model: model[a.id] }, why: "you chose its model" }); }} /></label>
              {a.state === "enabled" ? <Button variant="quiet" onClick={() => void run("agent_disable", { agent: a.id })}>Switch off</Button> : a.state === "disabled" ? <Button variant="quiet" onClick={() => void run("agent_enable", { agent: a.id })}>Switch on</Button> : <Button variant="quiet" onClick={() => void run("agent_restore", { agent: a.id })}>Restore</Button>}
              {a.kind === "temporary" && a.source !== "builtin" && <Button variant="quiet" onClick={() => void run("agent_promote", { agent: a.id })}>Keep permanently</Button>}
            </div>
          </div>
        </li>
      ))}
      <datalist id="wr-seats">{(overview.seats ?? []).map((s) => <option key={s} value={s} />)}</datalist>
    </ul>
  );
}

const RISK: Record<string, string> = { low: "Low risk", medium: "Medium risk", high: "High risk", blocked: "Blocked" };

const JOBS: [string, string][] = [["ui", "Frontend and design"], ["build", "Backend and building"], ["test", "Testing"], ["security", "Security"], ["docs", "Docs"], ["review", "Review"], ["plan", "Planning"]];

function SkillCard({ s, agents, reload }: { s: FoundrySkill; agents: FoundryAgent[]; reload: () => void }) {
  const { act, busy } = useAct();
  const run = async (c: string, b: Record<string, unknown> = {}) => { await act(c, { skill: s.id, ...b }); reload(); };
  const [network, setNetwork] = useState(false);
  const [risky, setRisky] = useState(false);
  const findings = s.assessment?.findings ?? [];
  const serious = findings.filter((f) => f.severity !== "note");
  const noted = findings.length - serious.length;
  const waiting = s.state === "awaiting_approval" || Boolean(s.incoming);
  const wantsNet = s.permissions.some((p) => p.includes("internet"));
  const looksBad = s.state === "blocked" || Boolean(s.update_refused);
  return (
    <article className={`wr-skill wr-skill--${s.risk}`}>
      <header className="wr-decision__head">
        <div><h3>{s.name}</h3><p className="wr-msg__meta">{s.source_text}{s.version ? ` · ${s.version}` : ""}{s.incoming ? ` · new: ${s.incoming.version}` : ""}{s.pinned ? " · pinned" : ""} · {s.kind === "instructions" ? "know-how" : s.kind === "reference" ? "to read" : s.kind === "tool" ? "program" : s.kind}</p></div>
        <span className={`wr-risk wr-risk--${s.risk}`}>{s.state === "enabled" ? "Ready" : looksBad ? "Looks dangerous" : RISK[s.risk] ?? s.risk}</span>
      </header>
      {s.description && <p>{s.description}</p>}
      {looksBad && <p className="wr-error"><ShieldAlert size={14} aria-hidden="true" /> {s.why_blocked ?? s.update_refused}</p>}
      {s.failure && <p className="wr-error"><AlertTriangle size={14} aria-hidden="true" /> It is not active: {s.failure}</p>}
      {s.disabled_because && <p className="wr-muted">Off: {s.disabled_because}.</p>}
      {s.state !== "enabled" && <div className="wr-skill__wants"><h4>What it would do</h4><ul>{s.permissions.map((p) => <li key={p}>{p}</li>)}</ul></div>}
      {findings.length > 0 && s.state !== "enabled" && (
        <details open={waiting && serious.length > 0 && serious.length <= 6}><summary>What XENIO found ({serious.length}{noted ? `, plus ${noted} in files it never runs` : ""})</summary>
          <ul className="wr-findings">{[...serious, ...findings.filter((f) => f.severity === "note").slice(0, 6)].slice(0, 20).map((f, i) => <li key={`${f.kind}${i}`} className={`wr-finding wr-finding--${f.severity}`}><strong>{f.severity === "note" ? "Only a note: " : ""}{f.why}</strong>{f.file && <code> {f.file}{f.line ? `:${f.line}` : ""}</code>}{f.excerpt && <pre className="wr-pre">{f.excerpt}</pre>}</li>)}</ul>
        </details>
      )}
      {waiting && !s.update_refused && (
        <div className="wr-skill__approve">
          <p className="wr-muted">Nothing from it has run. Install scripts stay off, and it only ever runs in the sandbox: your home folder unreadable, no keys, writes only in its own space.</p>
          {wantsNet && !looksBad && <label className="wr-check"><input type="checkbox" checked={network} onChange={(e) => setNetwork(e.target.checked)} /> Let it use the internet</label>}
          {looksBad
            ? <Button variant="danger" disabled={busy} onClick={() => setRisky(true)}>Install anyway, locked down</Button>
            : <Button variant="primary" disabled={busy} onClick={() => void run("skill_install", { network })}>{busy ? "Installing and testing…" : `Add to the skills base${s.wanted_by?.length ? ` for ${s.wanted_by.length} agent(s)` : ""}`}</Button>}
          <Button variant="quiet" disabled={busy} onClick={() => void run("skill_reject")}>No</Button>
        </div>
      )}
      {risky && <ConfirmDialog title="Install this anyway?" body="XENIO found something that looks dangerous. It would run with no internet, no keys and no access to your files, but you have seen what it does and it is your call." confirmLabel="Install it, locked down" onCancel={() => setRisky(false)} onConfirm={() => { setRisky(false); void run("skill_install", { accept_risk: true }); }} />}
      {(s.state === "enabled" || s.state === "disabled") && (
        <>
          <p className="wr-muted wr-skill__use">{(s.use?.used ?? 0) + (s.use?.added ?? 0) === 0
            ? "No agent has needed it yet."
            : `Agents have used it ${s.use!.used} time${s.use!.used === 1 ? "" : "s"}${s.use!.added ? ` and added it to ${s.use!.added} project${s.use!.added === 1 ? "" : "s"}` : ""}: ${s.use!.by.map((id) => agents.find((a) => a.id === id)?.name ?? readable(id)).join(", ")}.`}</p>
          <label className="wr-check wr-skill__everyone"><input type="checkbox" checked={Boolean(s.universal)} onChange={(e) => void run("skill_everyone", { everyone: e.target.checked })} /> Every agent whose job it suits, now and in the future</label>
          {s.universal && (
            <div className="wr-skill__jobs" role="group" aria-label="Which jobs it is for">
              <span className="wr-muted">For:</span>
              {JOBS.map(([key, label]) => {
                const on = (s.for_work ?? []).includes(key);
                return <button key={key} type="button" aria-pressed={on} className={`wr-job ${on ? "is-on" : ""}`} onClick={() => void run("skill_work", { work: on ? (s.for_work ?? []).filter((w) => w !== key) : [...(s.for_work ?? []), key] })}>{label}</button>;
              })}
              {!(s.for_work ?? []).length && <span className="wr-muted">any job, when the task fits</span>}
            </div>
          )}
          {!s.universal && <details open><summary>Who has it ({s.agents.length})</summary><ul className="wr-assign">{agents.filter((a) => a.state === "enabled").map((a) => <li key={a.id}><label className="wr-check"><input type="checkbox" checked={s.agents.includes(a.id)} onChange={(e) => void run(e.target.checked ? "skill_assign" : "skill_unassign", { agents: [a.id] })} /> {a.name}</label></li>)}</ul></details>}
          <div className="wr-agent__actions">
            {s.state === "enabled" ? <Button variant="quiet" onClick={() => void run("skill_disable")}>Switch off</Button> : <Button variant="quiet" onClick={() => void run("skill_enable")}>Switch on</Button>}
            <Button variant="quiet" disabled={busy || s.pinned} onClick={() => void run("skill_update")}>Check for an update</Button>
            <Button variant="quiet" onClick={() => void run("skill_pin", { pinned: !s.pinned })}>{s.pinned ? "Unpin" : "Pin this version"}</Button>
            {(s.versions?.length ?? 0) > 1 && <Button variant="quiet" onClick={() => void run("skill_rollback")}>Roll back</Button>}
            <Button variant="quiet" onClick={() => void run("skill_uninstall")}>Uninstall</Button>
          </div>
        </>
      )}
      {(s.state === "blocked" || s.state === "failed") && !waiting && <div className="wr-agent__actions"><Button variant="quiet" onClick={() => void run("skill_uninstall")}>Remove</Button></div>}
    </article>
  );
}

/** A repository holding many skills: you choose, and each becomes its own entry in the base. */
function CollectionCard({ s, reload }: { s: FoundrySkill; reload: () => void }) {
  const { act, busy } = useAct();
  const items = s.contains ?? [];
  const fine = items.filter((c) => !c.already && c.risk !== "blocked").map((c) => c.folder);
  const [picked, setPicked] = useState<string[]>(fine);
  const toggle = (folder: string) => setPicked((p) => (p.includes(folder) ? p.filter((x) => x !== folder) : [...p, folder]));
  const run = async (c: string, b: Record<string, unknown> = {}) => { await act(c, { skill: s.id, ...b }); reload(); };
  return (
    <article className="wr-skill">
      <header className="wr-decision__head">
        <div><h3>{s.name}</h3><p className="wr-msg__meta">{s.source_text} · {items.length} skills in this repository</p></div>
        <span className={`wr-risk wr-risk--${s.risk}`}>{RISK[s.risk] ?? s.risk}</span>
      </header>
      <ul className="wr-collection">
        {items.map((c) => (
          <li key={c.folder || c.name}>
            <label className="wr-collection__row">
              <input type="checkbox" checked={picked.includes(c.folder)} disabled={c.already} onChange={() => toggle(c.folder)} />
              <span className="wr-collection__body"><strong>{c.name}</strong>{c.description && <span className="wr-muted"> {c.description.slice(0, 160)}</span>}{c.findings.length > 0 && <span className="wr-collection__why">{c.findings[0]!.why}{c.findings[0]!.file ? ` (${c.findings[0]!.file})` : ""}</span>}</span>
              {c.already ? <span className="wr-tag">already here</span> : <span className={`wr-risk wr-risk--${c.risk}`}>{c.risk === "blocked" ? "Looks dangerous" : RISK[c.risk] ?? c.risk}</span>}
            </label>
          </li>
        ))}
      </ul>
      <div className="wr-skill__approve">
        <p className="wr-muted">Nothing has run. Each skill you keep goes in your skills base once and for all, for the agents whose job it suits.</p>
        <Button variant="primary" disabled={busy || !picked.length} onClick={() => void run("skill_install", { picks: picked, accept_risk: items.some((c) => c.risk === "blocked" && picked.includes(c.folder)) })}>{busy ? "Adding them…" : `Add ${picked.length} to the skills base`}</Button>
        <Button variant="quiet" disabled={busy} onClick={() => void run("skill_reject")}>No</Button>
      </div>
    </article>
  );
}

function Skills({ overview, reload }: { overview: FoundryOverview; reload: () => void }) {
  const { act, busy } = useAct();
  const [text, setText] = useState("");
  const go = async (e: FormEvent) => { e.preventDefault(); if (!text.trim()) return; const out = await act("skill_request", { request: text.trim() }); if (out) setText(""); reload(); };
  const ready = overview.skills.filter((s) => s.state === "enabled").length;
  return (
    <div className="wr-skills">
      <p className="wr-muted">Your skills base{ready ? `: ${ready} ready` : ""}. Add a skill once and the agents whose job it suits can use it, with no installing it again.</p>
      <form className="wr-inline wr-inline--wide" onSubmit={(e) => void go(e)}>
        <input aria-label="A skill to install" value={text} onChange={(e) => setText(e.target.value)} placeholder="A GitHub link, owner/repo, or an npx skills add command" />
        <Button variant="primary" type="submit" disabled={busy || !text.trim()}>{busy ? "Reading it…" : "Add"}</Button>
      </form>
      <p className="wr-muted">XENIO fetches it and reads it first. Nothing runs until you have seen what it found.</p>
      {overview.skills.length ? overview.skills.map((s) => (s.kind === "collection" ? <CollectionCard key={s.id} s={s} reload={reload} /> : <SkillCard key={s.id} s={s} agents={overview.agents} reload={reload} />)) : <Empty>No skills yet.</Empty>}
    </div>
  );
}

export function WarRoomPage({ workflowId }: { workflowId: string | null }) {
  const { app } = useApp();
  const { data: overview, error, reload } = useLive(() => app.api.foundry(), [], 1000);
  const [tab, setTab] = useState<"work" | "agents" | "skills" | "cando" | "connectors" | "vault">("work");
  if (workflowId) return <Workflow key={workflowId} id={workflowId} />;
  const needing = overview?.skills.filter((s) => s.state === "awaiting_approval").length ?? 0;
  return (
    <>
      <PageHeader title="War Room" intro="Your digital company. You are the CEO, XENIO is the manager, and the agents do the work." />
      {error && !overview && <ErrorNote message={error} retry={reload} />}
      {overview && !overview.available && <Empty>{overview.reason ?? "The team is switched off."}</Empty>}
      {overview?.available && (
        <>
          <ApprovalsHere />
          <Command overview={overview} onStarted={(id) => { window.location.hash = href("warroom", id); }} />
          <div className="segmented" role="tablist" aria-label="Show">
            {([["work", `Work (${overview.workflows.length})`], ["agents", `Agents (${overview.agents.length})`], ["skills", `Skills (${overview.skills.length})${needing ? ` · ${needing} waiting` : ""}`], ["cando", "What it can do"], ["connectors", `Connectors${overview.mcp?.length ? ` (${overview.mcp.length})` : ""}`], ["vault", "Vault"]] as const).map(([key, label]) => (
              <button key={key} type="button" role="tab" aria-selected={tab === key} className={tab === key ? "is-on" : ""} onClick={() => setTab(key)}>{label}</button>
            ))}
          </div>
          {tab === "work" && <WorkList overview={overview} reload={reload} />}
          {tab === "agents" && <Agents overview={overview} reload={reload} />}
          {tab === "skills" && <Skills overview={overview} reload={reload} />}
          {tab === "cando" && <CanDo />}
          {tab === "connectors" && <Connectors />}
          {tab === "vault" && <Vault />}
        </>
      )}
    </>
  );
}
