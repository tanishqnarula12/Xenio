// What waits for the user's decision, wherever the conversation is (Home's tray, the quick chat): the task grant in
// force, and engineering plans waiting for a go-ahead. Approvals themselves are ApprovalCard.

import { GitBranch, ShieldCheck } from "lucide-react";
import { useEffect, useState } from "react";
import { useApp, useAppState } from "../state/context";
import { href } from "../state/router";
import { useResource } from "../state/useResource";
import { Button } from "./ui";

/** "Allow for this task" in force: XENIO works on its own inside its workspace until it ends - shown, and endable. */
export function GrantChip() {
  const { app } = useApp();
  const grant = useAppState((s) => s.snapshot?.grant ?? null);
  const [, tick] = useState(0);
  useEffect(() => {
    if (!grant) return;
    const timer = setInterval(() => tick((n) => n + 1), 15_000);
    return () => clearInterval(timer);
  }, [grant]);
  if (!grant) return null;
  const minutes = Math.max(0, Math.ceil((Date.parse(grant.until) - Date.now()) / 60_000));
  return (
    <div className="grant" role="status">
      <ShieldCheck size={16} aria-hidden="true" />
      <p className="grant__text">
        Working on its own in XENIO's workspace · {minutes} min left
        {grant.covered ? ` · ${grant.covered} step${grant.covered > 1 ? "s" : ""} without asking` : ""}
      </p>
      <Button variant="quiet" onClick={() => void app.api.endGrant().catch((e: Error) =>
        app.store.notify({ tone: "danger", title: "Could not end it", message: e.message }))}>End</Button>
    </div>
  );
}

/** Engineering runs whose plan waits for the user's go-ahead, right where the conversation is. */
export function PlansToStart() {
  const { app } = useApp();
  const runs = useResource((s) => app.api.runs(s), ["runs"]);
  const [busy, setBusy] = useState<string | null>(null);
  const waiting = runs.data?.available ? runs.data.runs.filter((r) => r.live && r.status === "planned") : [];
  if (!waiting.length) return null;
  const act = async (id: string, action: "approve" | "cancel") => {
    setBusy(id);
    try {
      await app.api.runAction(id, action);
    } catch (e) {
      app.store.notify({ tone: "danger", title: "The engineering team did not take it", message: (e as Error).message });
    } finally {
      setBusy(null);
    }
  };
  return (
    <>
      {waiting.map((r) => {
        const total = Object.values(r.tasks).reduce((a, b) => a + (b ?? 0), 0);
        return (
          <article key={r.id} className="plan-card" aria-label={`Plan ready: ${r.request}`}>
            <GitBranch size={16} aria-hidden="true" className="plan-card__icon" />
            <div className="plan-card__body">
              <p className="plan-card__what">The engineering team has a plan{total ? ` (${total} task${total > 1 ? "s" : ""})` : ""}: {r.request}</p>
              <div className="approval__actions">
                <Button variant="primary" disabled={busy === r.id} onClick={() => void act(r.id, "approve")}>Start it</Button>
                <Button variant="secondary" disabled={busy === r.id} onClick={() => void act(r.id, "cancel")}>Cancel</Button>
                <a className="link plan-card__link" href={href("orchestration", r.id)}>See the plan</a>
              </div>
            </div>
          </article>
        );
      })}
    </>
  );
}
