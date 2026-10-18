import { useState } from "react";
import type { Agent } from "../api/types";
import { Empty, Loaded, PageHeader, StatusOf } from "../components/ui";
import { useApp } from "../state/context";
import { href } from "../state/router";
import { useResource } from "../state/useResource";

function AgentRow({ agent }: { agent: Agent }) {
  const [open, setOpen] = useState(false);
  const panel = `agent-${agent.id}`;
  return (
    <li className={`agent agent--${agent.state}`}>
      <button type="button" className="agent__summary" aria-expanded={open} aria-controls={panel} onClick={() => setOpen((o) => !o)}>
        <span className="agent__name">{agent.name}</span>
        <StatusOf value={agent.state} />
        <span className="agent__now">
          {agent.current[0] ? `${agent.current[0].title}` : agent.blocked[0] ? `Waiting: ${agent.blocked[0].title}` : "No task right now"}
        </span>
      </button>
      <div id={panel} className="agent__detail" hidden={!open}>
        <p>{agent.description}</p>
        <dl className="facts">
          <dt>Model</dt><dd>{agent.model}</dd>
          <dt>May use</dt><dd>{agent.capabilities.length ? agent.capabilities.join(", ") : "nothing yet"}</dd>
          <dt>Tools</dt><dd>{agent.tools.length ? agent.tools.join(", ") : "none"}</dd>
        </dl>
        {[...agent.current, ...agent.blocked].map((w) => (
          <p key={`${w.run}-${w.task}`}>
            <a className="link" href={href("orchestration", w.run)}>{w.title}</a> <StatusOf value={w.status} />
            {w.failure && <span className="error-text"> {w.failure}</span>}
          </p>
        ))}
        <p className="meta">XENIO enforces these limits on every tool call the agent makes; the agent cannot widen them.</p>
      </div>
    </li>
  );
}

export function AgentsPage() {
  const { app } = useApp();
  const agents = useResource((s) => app.api.agents(s), ["agents", "runs"]);
  return (
    <>
      <PageHeader title="Agents" intro="XENIO's engineering team: eight specialist roles, each limited to its own tools and its task's files. An agent is shown as working only while it really has a running session." />
      <Loaded resource={agents} what="agents">
        {(data) => {
          if (!data.available) return <Empty>{data.reason ?? "The engineering team is switched off."}</Empty>;
          if (!data.agents.length) return <Empty>No roles are configured.</Empty>;
          const working = data.agents.filter((a) => a.state === "working").length;
          return (
            <>
              <p className="lead tabular">{working ? `${working} of ${data.agents.length} agents working right now.` : "No agent is working right now."}</p>
              <ul className="agents">{data.agents.map((a) => <AgentRow key={a.id} agent={a} />)}</ul>
            </>
          );
        }}
      </Loaded>
    </>
  );
}
