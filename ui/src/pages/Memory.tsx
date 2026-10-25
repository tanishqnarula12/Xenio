import { Check, Pencil, Plus, Trash2, X } from "lucide-react";
import { useDeferredValue, useState, type FormEvent } from "react";
import type { MemoryEntry, MemoryList } from "../api/types";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { Button, Empty, ErrorNote, IconButton, Loaded, PageHeader, Section } from "../components/ui";
import { useApp } from "../state/context";
import { href } from "../state/router";
import { useResource } from "../state/useResource";

const STORES: { id: "user" | "memory"; title: string; what: string; empty: string }[] = [
  { id: "user", title: "About you", what: "Your name, preferences and standing instructions.", empty: "Nothing about you yet." },
  { id: "memory", title: "XENIO's notes", what: "Facts about your setup, conventions and lessons learned.", empty: "No notes yet." },
];

function Usage({ used, limit }: { used: number | null; limit: number }) {
  if (used == null) return null;
  const share = Math.min(1, used / limit);
  return (
    <span className={`usage ${share >= 0.9 ? "usage--full" : ""}`} title="Hermes keeps memory small on purpose: every entry is in every conversation">
      <span className="usage__bar" aria-hidden="true"><span style={{ width: `${Math.round(share * 100)}%` }} /></span>
      <span className="meta tabular">{used.toLocaleString()} of {limit.toLocaleString()} characters</span>
    </span>
  );
}

function EntryRow({ entry, editable }: { entry: MemoryEntry; editable: boolean }) {
  const { app } = useApp();
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(entry.text);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);

  const save = async (e?: FormEvent) => {
    e?.preventDefault();
    if (!text.trim() || text.trim() === entry.text) { setEditing(false); return; }
    setBusy(true);
    setError(null);
    try {
      await app.api.changeMemory({ action: "correct", id: entry.id, text: text.trim() });
      setEditing(false);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const remove = async () => {
    setConfirming(false);
    setBusy(true);
    setError(null);
    try {
      await app.api.changeMemory({ action: "delete", id: entry.id });
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  };

  return (
    <li className="memory-entry">
      {editing ? (
        <form className="memory-edit" onSubmit={save} aria-label="Correct this memory">
          <label className="sr-only" htmlFor={`mem-${entry.id}`}>Corrected text</label>
          <textarea id={`mem-${entry.id}`} value={text} maxLength={400} rows={2} autoFocus onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Escape") { setText(entry.text); setEditing(false); } }} />
          <div className="memory-edit__actions">
            <Button variant="primary" type="submit" disabled={busy || !text.trim()}><Check size={14} aria-hidden="true" /> Save</Button>
            <Button variant="quiet" disabled={busy} onClick={() => { setText(entry.text); setEditing(false); }}><X size={14} aria-hidden="true" /> Cancel</Button>
            <span className="meta tabular">{text.trim().length} / 400</span>
          </div>
        </form>
      ) : (
        <>
          <p className="memory-entry__text">{entry.text}</p>
          {editable && (
            <div className="memory-entry__actions">
              <IconButton label="Correct this memory" icon={Pencil} disabled={busy} onClick={() => setEditing(true)} />
              <IconButton label="Delete this memory" icon={Trash2} disabled={busy} onClick={() => setConfirming(true)} />
            </div>
          )}
        </>
      )}
      {error && <ErrorNote message={error} />}
      {confirming && (
        <ConfirmDialog title="Delete this memory?" confirmLabel="Delete" onCancel={() => setConfirming(false)}
          onConfirm={() => void remove()}
          body={`XENIO will no longer know: “${entry.text}”. A conversation that is already running is told at once.`} />
      )}
    </li>
  );
}

function AddEntry({ store }: { store: "user" | "memory" }) {
  const { app } = useApp();
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await app.api.changeMemory({ action: "add", store, text: text.trim() });
      setText("");
      setOpen(false);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };
  if (!open) return <Button variant="quiet" onClick={() => setOpen(true)}><Plus size={14} aria-hidden="true" /> Add</Button>;
  return (
    <form className="memory-edit" onSubmit={submit} aria-label={`Add to ${store === "user" ? "about you" : "XENIO's notes"}`}>
      <label className="sr-only" htmlFor={`mem-new-${store}`}>New memory</label>
      <textarea id={`mem-new-${store}`} value={text} maxLength={400} rows={2} autoFocus placeholder={store === "user"
        ? "For example: I prefer short answers in the morning" : "For example: the home server is called atlas"}
        onChange={(e) => setText(e.target.value)} onKeyDown={(e) => { if (e.key === "Escape") setOpen(false); }} />
      <div className="memory-edit__actions">
        <Button variant="primary" type="submit" disabled={busy || !text.trim()}>Remember it</Button>
        <Button variant="quiet" disabled={busy} onClick={() => setOpen(false)}>Cancel</Button>
      </div>
      {error && <ErrorNote message={error} />}
    </form>
  );
}

function Stores({ data, query }: { data: MemoryList; query: string }) {
  const editable = Boolean(data.editable);
  return (
    <>
      {STORES.map((st) => {
        const entries = data.entries.filter((e) => e.store === st.id);
        const usage = data.usage?.[st.id];
        return (
          <Section key={st.id} title={st.title} id={`mem-${st.id}`} action={usage ? <Usage used={usage.used} limit={usage.limit} /> : undefined}>
            <p className="meta">{st.what}</p>
            {entries.length ? <ul className="memory">{entries.map((e) => <EntryRow key={e.id} entry={e} editable={editable} />)}</ul>
              : <Empty>{query ? "Nothing here matches that." : st.empty}</Empty>}
            {editable && !query && <AddEntry store={st.id} />}
          </Section>
        );
      })}
    </>
  );
}

export function MemoryPage() {
  const { app } = useApp();
  const [query, setQuery] = useState("");
  const deferred = useDeferredValue(query);
  const memory = useResource((s) => app.api.memory(deferred, s), ["memory"], [deferred]);
  return (
    <>
      <PageHeader title="Memory" intro="What XENIO remembers across conversations (Hermes keeps it; every entry is part of every conversation). Correct or delete anything here. Passwords, keys and other secrets are never stored, even if you ask." />
      <div className="toolbar">
        <input type="search" className="search" placeholder="Search memory" aria-label="Search memory" value={query}
          onChange={(e) => setQuery(e.target.value)} />
      </div>
      <Loaded resource={memory} what="memory">
        {(data) => data.available ? <Stores data={data} query={query} />
          : <Empty>{data.reason ?? "Memory is not available."}</Empty>}
      </Loaded>
      <Section title="What is not memory" id="mem-other">
        <dl className="facts facts--wide">
          <dt>Projects and tasks</dt>
          <dd>Kept by XENIO as records, looked up only when needed: see <a className="link" href={href("tasks")}>Tasks & projects</a>.</dd>
          <dt>Conversations</dt>
          <dd>Hermes keeps each conversation in its own session history; it is not memory. A new conversation starts empty.</dd>
          <dt>Your screen</dt>
          <dd>Never kept: a look at your screen becomes a few lines of text in that conversation, and the picture is discarded.</dd>
          <dt>Activity</dt>
          <dd>What XENIO did (never what you typed or said), kept as long as Settings says: see <a className="link" href={href("activity")}>Activity</a>.</dd>
        </dl>
      </Section>
    </>
  );
}
