import { useEffect, useState } from "react";
import type { Agent, AgentWork } from "../api/types";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { Button, Empty, Loaded, PageHeader, StatusOf, Time } from "../components/ui";
import { useApp } from "../state/context";
import { href } from "../state/router";
import { useResource } from "../state/useResource";

/** "for 4 min" from XENIO's own start time; nothing when XENIO did not record one. */
export function elapsed(since: string | null | undefined, now = Date.now()): string | null {
  if (!since) return null;
  const at = Date.parse(since);
  if (Number.isNaN(at)) return null;
  const s = Math.max(0, Math.round((now - at) / 1000));
  return s < 60 ? `for ${s} s` : s < 3600 ? `for ${Math.round(s / 60)} min` : `for ${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min`;
}

function useNow(active: boolean): number {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => setNow(Date.now()), 5000);
    return () => clearInterval(timer);
  }, [active]);
  return now;
}

function WorkLine({ work, now }: { work: AgentWork; now: number }) {
  const { app } = useApp();
  const [confirm, setConfirm] = useState(false);
  const [state, setState] = useState<string | null>(null);
  const stop = async () => {
    setConfirm(false);
    setState("Stopping…");
    try {
      await app.api.runAction(work.run, "cancel");
      setState("Stop requested: no new task starts; running ones finish their current step.");
    } catch (e) {
      setState((e as Error).message);
    }
  };
  return (
    <div className="agent-work">
      <p>
        <a className="link" href={href("orchestration", work.run)}>{work.title}</a> <StatusOf value={work.status} />
        {work.status === "running" && work.since && <span className="meta tabular"> {elapsed(work.since, now)}</span>}
      </p>
      {work.run_request && <p className="meta">Run {work.run}: {work.run_request}</p>}
      {work.latest && <p className="meta">Latest: {work.latest.text} · <Time iso={work.latest.at} /></p>}
      {work.failure && <p className="error-text">{work.failure}</p>}
      {work.status === "running" && (
        <Button variant="quiet" onClick={() => setConfirm(true)} disabled={state === "Stopping…"}>Stop this run…</Button>
      )}
      {state && <p className="meta" role="status">{state}</p>}
      {confirm && (
        <ConfirmDialog title="Stop the run?" confirmLabel="Stop the run" onCancel={() => setConfirm(false)} onConfirm={() => void stop()}
          body={`Run ${work.run} stops: no new task starts and the work done so far is kept in its workspace. Your own files are not touched.`} />
      )}
    </div>
  );
}

function AgentRow({ agent, now }: { agent: Agent; now: number }) {
  const [open, setOpen] = useState(false);
  const panel = `agent-${agent.id}`;
  const first = agent.current[0];
  return (
    <li className={`agent agent--${agent.state}`}>
      <button type="button" className="agent__summary" aria-expanded={open} aria-controls={panel} onClick={() => setOpen((o) => !o)}>
        <span className="agent__name">{agent.name}</span>
        <StatusOf value={agent.state} />
        <span className="agent__now">
          {first ? <>{first.title}{first.since && <span className="meta tabular"> · {elapsed(first.since, now)}</span>}</>
            : agent.blocked[0] ? `Waiting: ${agent.blocked[0].title}` : "No task right now"}
        </span>
      </button>
      <div id={panel} className="agent__detail" hidden={!open}>
        <p>{agent.description}</p>
        <dl className="facts">
          <dt>Model</dt><dd>{first?.model ?? agent.model}</dd>
          <dt>May use</dt><dd>{agent.capabilities.length ? agent.capabilities.join(", ") : "nothing yet"}</dd>
          <dt>Tools</dt><dd>{agent.tools.length ? agent.tools.join(", ") : "none"}</dd>
        </dl>
        {[...agent.current, ...agent.blocked].map((w) => <WorkLine key={`${w.run}-${w.task}`} work={w} now={now} />)}
        <p className="meta">XENIO enforces these limits on every tool call the agent makes; the agent cannot widen them.</p>
      </div>
    </li>
  );
}

export function AgentsPage() {
  const { app } = useApp();
  const agents = useResource((s) => app.api.agents(s), ["agents", "runs"]);
  const working = agents.data?.agents.some((a) => a.state === "working") ?? false;
  const now = useNow(working);
  return (
    <>
      <PageHeader title="Agents" intro="XENIO's engineering team: specialist roles, each limited to its own tools and its task's files. An agent shows as working only while it really has a running session; planners and reviewers count too." />
      <Loaded resource={agents} what="agents">
        {(data) => {
          if (!data.available) return <Empty>{data.reason ?? "The engineering team is switched off."}</Empty>;
          if (!data.agents.length) return <Empty>No roles are configured.</Empty>;
          const count = data.agents.filter((a) => a.state === "working").length;
          return (
            <>
              <p className="lead tabular">{count ? `${count} of ${data.agents.length} agents working right now.` : "No agent is working right now."}</p>
              <ul className="agents">{data.agents.map((a) => <AgentRow key={a.id} agent={a} now={now} />)}</ul>
            </>
          );
        }}
      </Loaded>
    </>
  );
}
