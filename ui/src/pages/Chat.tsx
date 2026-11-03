import { AudioLines, MessageSquarePlus } from "lucide-react";
import { Composer, PendingApprovals, Transcript } from "../components/Conversation";
import { GrantChip, PlansToStart } from "../components/Decisions";
import { IconButton } from "../components/ui";
import { useOrbState } from "../components/useOrbState";
import { useApp, useAppState } from "../state/context";
import { href } from "../state/router";

/**
 * A normal chat, for when talking is not what is wanted: the whole conversation on one page, no orb, and typed replies stay on screen. It is the same
 * conversation as Home and the floating orb, through the same requests. What XENIO is doing is said in words above the box, only while it is doing it.
 */
export function ChatPage() {
  const { app } = useApp();
  const empty = useAppState((s) => s.conversation.length === 0 && !s.streaming);
  const busy = useAppState((s) => Boolean(s.assistant?.turn));
  const approvals = useAppState((s) => s.approvals.length);
  const { sentence } = useOrbState();
  const newConversation = () => void app.api.newConversation().catch((e: Error) =>
    app.store.notify({ tone: "danger", title: "Could not start a new conversation", message: e.message }));

  return (
    <div className="chat-page">
      <h1 className="sr-only">Chat</h1>
      <div className="chat-page__bar">
        <IconButton label="New conversation" icon={MessageSquarePlus} onClick={newConversation} />
        <a className="icon-btn" href={href("home")} aria-label="Talk to XENIO, with the orb" title="Talk to XENIO, with the orb"><AudioLines size={18} aria-hidden="true" /></a>
      </div>
      <div className="chat-page__scroll">
        <div className="chat-page__column">
          {empty ? (
            <div className="chat-page__empty">
              <p className="chat-page__hello">What can I do for you?</p>
              <p className="meta">Ask anything, add a file, ask for a lesson or a picture, or tell XENIO what to do on your Mac.</p>
            </div>
          ) : <Transcript />}
        </div>
      </div>
      <div className="chat-page__dock">
        <div className="chat-page__column">
          <div className="decision-tray" aria-label="Needs your decision">
            <GrantChip />
            {approvals > 0 && <PendingApprovals compact />}
            <PlansToStart />
          </div>
          {busy && !approvals && <p className="chat-status" role="status"><span className="chat-status__dot" aria-hidden="true" /> {sentence}</p>}
          <Composer autoFocus quiet idleHint="Enter to send, Shift+Enter for a new line. Replies stay on screen here." />
        </div>
      </div>
    </div>
  );
}
