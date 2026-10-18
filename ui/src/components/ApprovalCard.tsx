import { ShieldAlert } from "lucide-react";
import { useEffect, useId, useState } from "react";
import type { Approval } from "../api/types";
import { useApp } from "../state/context";
import { Button, Status, Time } from "./ui";

const RISK: Record<string, [string, "attention" | "danger"]> = {
  CONFIRM_REQUIRED: ["Needs your yes", "attention"],
  HIGH_RISK: ["High risk: type confirm", "danger"],
};

function secondsLeft(iso: string): number {
  return Math.max(0, Math.round((Date.parse(iso) - Date.now()) / 1000));
}

/** One question from XENIO's PermissionGate. The decision is made and checked by XENIO; this only sends the answer. */
export function ApprovalCard({ approval, compact = false }: { approval: Approval; compact?: boolean }) {
  const { app } = useApp();
  const [confirmText, setConfirmText] = useState("");
  const [busy, setBusy] = useState<"approve" | "deny" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [left, setLeft] = useState(() => secondsLeft(approval.expires_at));
  const inputId = useId();
  const [riskLabel, riskTone] = RISK[approval.risk] ?? ["Needs your yes", "attention"];

  useEffect(() => {
    const timer = setInterval(() => setLeft(secondsLeft(approval.expires_at)), 1000);
    return () => clearInterval(timer);
  }, [approval.expires_at]);

  const decide = async (decision: "approve" | "deny") => {
    setBusy(decision);
    setError(null);
    try {
      await app.api.decide(approval.id, decision, confirmText);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  const strictReady = !approval.strict || confirmText.trim().toLowerCase() === "confirm";
  return (
    <article className={`approval ${compact ? "approval--compact" : ""}`} aria-label={`Approval needed: ${approval.description}`}>
      <div className="approval__head">
        <ShieldAlert size={18} aria-hidden="true" className="approval__icon" />
        <Status tone={riskTone}>{riskLabel}</Status>
        <span className="meta approval__expiry" aria-live="off">
          {left > 0 ? `Expires in ${left >= 60 ? `${Math.ceil(left / 60)} min` : `${left} s`}` : "Expiring"}
        </span>
      </div>
      <p className="approval__what">XENIO wants to {approval.description}.</p>
      {!compact && (
        <p className="meta">Asked by {approval.source === "hermes" ? "XENIO's assistant" : approval.source} · <Time iso={approval.requested_at} /></p>
      )}
      {approval.strict && (
        <div className="field">
          <label htmlFor={inputId}>Type <strong>confirm</strong> to allow this high-risk action</label>
          <input id={inputId} value={confirmText} onChange={(e) => setConfirmText(e.target.value)} autoComplete="off"
            spellCheck={false} />
        </div>
      )}
      {error && <p className="error-text" role="alert">{error}</p>}
      <div className="approval__actions">
        <Button variant="primary" onClick={() => void decide("approve")} disabled={busy !== null || !strictReady}
          aria-describedby={approval.strict && !strictReady ? inputId : undefined}>
          {busy === "approve" ? "Allowing…" : "Allow once"}
        </Button>
        <Button variant="secondary" onClick={() => void decide("deny")} disabled={busy !== null}>
          {busy === "deny" ? "Declining…" : "Decline"}
        </Button>
      </div>
    </article>
  );
}
