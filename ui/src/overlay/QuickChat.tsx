// The quick chat: XENIO's conversation right in the floating panel, like the Option-Space windows of ChatGPT and
// Claude. It is the same conversation as Mission Control (one backend, one transcript), so the user can switch
// between typing here, talking (the microphone, or "Hey Xenio") and the full window without losing anything. Typed
// requests are answered on screen only; spoken ones are also answered aloud by XENIO's talk engine.

import { Maximize2, MessageSquarePlus, X } from "lucide-react";
import { type KeyboardEvent, type MouseEvent, type PointerEvent } from "react";
import { Composer, PendingApprovals, Transcript } from "../components/Conversation";
import { GrantChip, PlansToStart } from "../components/Decisions";
import { useOrbState } from "../components/useOrbState";
import { useReducedMotion } from "../motion/motion";
import { Orb } from "../orb/Orb";
import { useApp, useAppState } from "../state/context";

interface QuickChatProps {
  onClose: () => void;
  onOpenApp: (route?: string) => void;
  onDragStart: () => void;
}

export function QuickChat({ onClose, onOpenApp, onDragStart }: QuickChatProps) {
  const { app } = useApp();
  const orb = useOrbState();
  const reduced = useReducedMotion();
  const approvals = useAppState((s) => s.approvals.length);

  const onKey = (e: KeyboardEvent<HTMLElement>) => {
    if (e.key === "Escape" && !e.nativeEvent.isComposing) {
      e.preventDefault();
      onClose();
    }
  };
  // links to XENIO's own pages ("See the plan") open in Mission Control, not inside this small panel
  const onClick = (e: MouseEvent<HTMLElement>) => {
    const link = (e.target as HTMLElement).closest?.("a[href^='#/']");
    if (!link) return;
    e.preventDefault();
    onOpenApp(link.getAttribute("href")!.slice(1));
  };
  const onHeadDown = (e: PointerEvent<HTMLElement>) => {
    if (!(e.target as HTMLElement).closest("button")) onDragStart();  // the header moves the panel
  };
  const newConversation = () => void app.api.newConversation().catch((e: Error) =>
    app.store.notify({ tone: "danger", title: "Could not start a new conversation", message: e.message }));

  return (
    <section className="qc" aria-label="Chat with XENIO" onKeyDown={onKey} onClick={onClick}>
      <header className="qc-head" onPointerDown={onHeadDown}>
        <span className="qc-orb" aria-hidden="true">
          <Orb state={orb.state} label={orb.label} size={30} detail="low" reducedMotion={reduced} level={orb.level} agents={orb.agents} />
        </span>
        <div className="qc-title">
          <p className="qc-name">XENIO</p>
          <p className="qc-state" aria-live="polite">{orb.label}</p>
        </div>
        <div className="ov-actions">
          <button type="button" className="ov-icon" onClick={newConversation} aria-label="New conversation" title="New conversation">
            <MessageSquarePlus size={15} aria-hidden="true" /></button>
          <button type="button" className="ov-icon" onClick={() => onOpenApp()} aria-label="Open Mission Control" title="Open Mission Control">
            <Maximize2 size={14} aria-hidden="true" /></button>
          <button type="button" className="ov-icon" onClick={onClose} aria-label="Close the chat" title="Close (Esc)">
            <X size={15} aria-hidden="true" /></button>
        </div>
      </header>
      <div className="qc-scroll">
        <Transcript limit={30} />
      </div>
      <div className="qc-tray" aria-label="Needs your decision">
        <GrantChip />
        {approvals > 0 && <PendingApprovals compact />}
        <PlansToStart />
      </div>
      <Composer autoFocus quiet idleHint="Enter to send · Esc to close · the microphone switches to voice" />
    </section>
  );
}
