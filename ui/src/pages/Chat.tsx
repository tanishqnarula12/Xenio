import { AudioLines, History, MessageSquarePlus, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import type { ChatSummary } from "../api/types";
import { Composer, PendingApprovals, Transcript } from "../components/Conversation";
import { GrantChip, PlansToStart } from "../components/Decisions";
import { Button, IconButton, Loaded } from "../components/ui";
import { useOrbState } from "../components/useOrbState";
import { useApp, useAppState } from "../state/context";
import { href } from "../state/router";
import { useResource } from "../state/useResource";

function whenOf(iso: string, now = new Date()): string {
  const day = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const days = Math.round((day(now) - day(new Date(iso))) / 86_400_000);
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days < 8) return "Previous 7 days";
  if (days < 31) return "Previous 30 days";
  return "Earlier";
}

/** Your conversations, newest first: open one and carry on, as in Claude or ChatGPT. A search looks through their words too. */
function ChatList({ onPicked, onNew }: { onPicked: () => void; onNew: () => void }) {
  const { app } = useApp();
  const busy = useAppState((s) => Boolean(s.assistant?.turn));
  const [typed, setTyped] = useState("");
  const [query, setQuery] = useState("");
  const [asking, setAsking] = useState<string | null>(null);
  const [clearing, setClearing] = useState(false);
  useEffect(() => { const t = setTimeout(() => setQuery(typed.trim()), 250); return () => clearTimeout(t); }, [typed]);
  const list = useResource((signal) => app.api.chats(query, signal), ["chats"], [query]);
  const failed = (title: string) => (e: Error) => app.store.notify({ tone: "danger", title, message: e.message });
  const open = (chat: ChatSummary) => {
    if (busy) return;
    void app.api.changeChat(chat.id, "open").then(onPicked, failed("That chat could not be opened"));
  };
  const remove = (id: string) => {
    setAsking(null);
    void app.api.changeChat(id, "delete").catch(failed("That chat could not be deleted"));
  };
  return (
    <aside className="chat-side" aria-label="Your chats">
      <Button variant="quiet" className="chat-side__new" onClick={onNew}><MessageSquarePlus size={16} aria-hidden="true" /> New chat</Button>
      <input className="chat-side__search" type="search" value={typed} placeholder="Search your chats" aria-label="Search your chats" onChange={(e) => setTyped(e.target.value)} />
      <div className="chat-side__list">
        <Loaded resource={list} what="your chats">
          {(data) => {
            if (!data.chats.length) return <p className="meta chat-group">{query ? "No chat has those words." : "Your chats will be kept here."}</p>;
            let group = "";
            return data.chats.map((chat) => {
              const when = whenOf(chat.updated);
              const heading = when !== group ? when : null;
              group = when;
              return (
                <div key={chat.id}>
                  {heading && <p className="chat-group">{heading}</p>}
                  {asking === chat.id ? (
                    <div className="chat-item__confirm" role="group" aria-label={`Delete ${chat.title}?`}>
                      <span>Delete this chat?</span>
                      <Button variant="quiet" onClick={() => remove(chat.id)}>Delete</Button>
                      <Button variant="quiet" onClick={() => setAsking(null)}>Keep</Button>
                    </div>
                  ) : (
                    <div className={`chat-item ${chat.id === data.current ? "chat-item--current" : ""}`}>
                      <button type="button" className="chat-item__open" aria-disabled={busy} aria-current={chat.id === data.current ? "true" : undefined} title={chat.title} onClick={() => open(chat)}>{chat.title}</button>
                      <IconButton className="chat-item__delete" label={`Delete ${chat.title}`} icon={Trash2} onClick={() => setAsking(chat.id)} />
                    </div>
                  )}
                </div>
              );
            });
          }}
        </Loaded>
      </div>
      <div className="chat-side__foot">
        <span>Kept on this Mac only.</span>
        {clearing ? (
          <span role="group" aria-label="Delete all chats?">Delete every chat? <button type="button" onClick={() => { setClearing(false); void app.api.deleteAllChats().catch(failed("The chats could not be deleted")); }}>Yes, delete all</button> · <button type="button" onClick={() => setClearing(false)}>Keep them</button></span>
        ) : <button type="button" onClick={() => setClearing(true)}>Delete all chats</button>}
      </div>
    </aside>
  );
}

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
  const [side, setSide] = useState(() => window.innerWidth > 820);
  const newConversation = () => void app.api.newConversation().catch((e: Error) =>
    app.store.notify({ tone: "danger", title: "Could not start a new conversation", message: e.message }));

  return (
    <div className={`chat-layout ${side ? "chat-layout--side" : ""}`}>
    {side && <ChatList onPicked={() => { if (window.innerWidth <= 820) setSide(false); }} onNew={() => { newConversation(); if (window.innerWidth <= 820) setSide(false); }} />}
    <div className="chat-page">
      <h1 className="sr-only">Chat</h1>
      <div className="chat-page__bar">
        <IconButton label={side ? "Hide your chats" : "Show your chats"} icon={History} aria-pressed={side} onClick={() => setSide((v) => !v)} />
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
          ) : <Transcript quiet />}
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
    </div>
  );
}
