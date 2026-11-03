import { Check, Eye, EyeOff, Pencil, Plus, Trash2, X } from "lucide-react";
import { useState, type FormEvent } from "react";
import type { LastForm, ProfileData, ProfileEntry } from "../api/types";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { Button, Empty, ErrorNote, IconButton, Loaded, PageHeader, Section } from "../components/ui";
import { useApp } from "../state/context";
import { useResource } from "../state/useResource";

const FIELD_STATUS: Record<string, string> = {
  filled: "Filled and checked", not_filled: "Could not be entered", ask: "XENIO needs your answer", empty: "Left empty",
  question: "XENIO needs your answer", graded: "Graded quiz: yours to answer",
  keep: "Your text was kept", never: "Yours to do", hidden: "Not visible, left alone", disabled: "Locked by the page",
  unsupported: "Not supported", not_reached: "Stopped before this", not_found: "Saved detail is gone", already: "Already right",
};

function when(iso: string | null | undefined): string {
  if (!iso) return "";
  const at = new Date(iso);
  return Number.isNaN(at.getTime()) ? "" : at.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

function EntryRow({ entry }: { entry: ProfileEntry }) {
  const { app } = useApp();
  const [editing, setEditing] = useState(false);
  const [label, setLabel] = useState(entry.label);
  const [value, setValue] = useState(entry.masked ? "" : entry.value ?? "");
  const [shown, setShown] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);

  const act = async (work: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await work();
      return true;
    } catch (err) {
      setError((err as Error).message);
      return false;
    } finally {
      setBusy(false);
    }
  };
  const save = async (e: FormEvent) => {
    e.preventDefault();
    const change = { action: "update" as const, id: entry.id, label: label.trim(), ...(value.trim() ? { value: value.trim() } : {}) };
    if (await act(() => app.api.changeProfile(change))) setEditing(false);
  };
  const reveal = async () => {
    if (shown !== null) { setShown(null); return; }
    await act(async () => { const out = await app.api.changeProfile({ action: "reveal", id: entry.id }); setShown(out.value ?? ""); });
  };
  const shownValue = shown ?? entry.value ?? "";

  return (
    <li className="memory-entry">
      {editing ? (
        <form className="memory-edit" onSubmit={save} aria-label={`Correct ${entry.label}`}>
          <label className="field-label" htmlFor={`pf-l-${entry.id}`}>Name</label>
          <input id={`pf-l-${entry.id}`} value={label} maxLength={80} onChange={(e) => setLabel(e.target.value)} />
          <label className="field-label" htmlFor={`pf-v-${entry.id}`}>{entry.masked ? "New value (leave empty to keep it)" : "Value"}</label>
          <textarea id={`pf-v-${entry.id}`} value={value} maxLength={2000} rows={2} autoFocus onChange={(e) => setValue(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Escape") setEditing(false); }} />
          <div className="memory-edit__actions">
            <Button variant="primary" type="submit" disabled={busy || !label.trim()}><Check size={14} aria-hidden="true" /> Save</Button>
            <Button variant="quiet" disabled={busy} onClick={() => setEditing(false)}><X size={14} aria-hidden="true" /> Cancel</Button>
          </div>
        </form>
      ) : (
        <>
          <div className="memory-entry__text">
            <strong>{entry.label}</strong>{entry.sensitive && <span className="meta"> · sensitive</span>}
            <p className="profile-value">{shownValue}</p>
            <p className="meta">{entry.source_words}, {when(entry.created)}{entry.corrected ? ` · corrected ${when(entry.updated)}` : ""}</p>
          </div>
          <div className="memory-entry__actions">
            {entry.masked && <IconButton label={shown === null ? `Show ${entry.label}` : `Hide ${entry.label}`} icon={shown === null ? Eye : EyeOff} disabled={busy} onClick={() => void reveal()} />}
            <IconButton label={`Correct ${entry.label}`} icon={Pencil} disabled={busy} onClick={() => setEditing(true)} />
            <IconButton label={`Delete ${entry.label}`} icon={Trash2} disabled={busy} onClick={() => setConfirming(true)} />
          </div>
        </>
      )}
      {error && <ErrorNote message={error} />}
      {confirming && (
        <ConfirmDialog title={`Delete ${entry.label}?`} confirmLabel="Delete" onCancel={() => setConfirming(false)}
          onConfirm={() => { setConfirming(false); void act(() => app.api.changeProfile({ action: "delete", id: entry.id })); }}
          body="It is erased from this Mac and XENIO can no longer put it into any form, even one it is working on." />
      )}
    </li>
  );
}

function AddEntry({ data }: { data: ProfileData }) {
  const { app } = useApp();
  const [open, setOpen] = useState(false);
  const [category, setCategory] = useState("identity");
  const [label, setLabel] = useState("");
  const [value, setValue] = useState("");
  const [sensitive, setSensitive] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const suggestion = data.suggestions.find((s) => s.label.toLowerCase() === label.trim().toLowerCase());
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await app.api.changeProfile({ action: "add", category: suggestion?.category ?? category, label: label.trim(), value: value.trim(),
        ...(suggestion ? { field: suggestion.field } : {}), ...(sensitive ? { sensitive: true } : {}) });
      setLabel("");
      setValue("");
      setSensitive(false);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };
  if (!open) return <Button variant="primary" onClick={() => setOpen(true)}><Plus size={14} aria-hidden="true" /> Add a detail</Button>;
  return (
    <form className="memory-edit" onSubmit={submit} aria-label="Add a detail to your profile">
      <label className="field-label" htmlFor="pf-new-label">Name</label>
      <input id="pf-new-label" list="pf-suggestions" value={label} maxLength={80} autoFocus placeholder="Email, City, LinkedIn…" onChange={(e) => setLabel(e.target.value)} />
      <datalist id="pf-suggestions">{data.suggestions.map((s) => <option key={s.field} value={s.label} />)}</datalist>
      <label className="field-label" htmlFor="pf-new-value">Value</label>
      <input id="pf-new-value" value={value} maxLength={2000} onChange={(e) => setValue(e.target.value)} autoComplete="off" />
      {!suggestion && (
        <>
          <label className="field-label" htmlFor="pf-new-category">Kind</label>
          <select id="pf-new-category" value={category} onChange={(e) => setCategory(e.target.value)}>
            {Object.entries(data.categories).map(([id, name]) => <option key={id} value={id}>{name}</option>)}
          </select>
        </>
      )}
      <label className="profile-check"><input type="checkbox" checked={sensitive} onChange={(e) => setSensitive(e.target.checked)} /> Sensitive: stays hidden here, and XENIO asks before each use</label>
      <div className="memory-edit__actions">
        <Button variant="primary" type="submit" disabled={busy || !label.trim() || !value.trim()}>Save</Button>
        <Button variant="quiet" disabled={busy} onClick={() => setOpen(false)}>Done</Button>
      </div>
      {error && <ErrorNote message={error} />}
    </form>
  );
}

function LastFormCard({ form }: { form: LastForm }) {
  const counts = form.fields.filter((f) => f.status === "filled").length;
  const outcome = form.submitted === true ? "Submitted: the page confirmed it." : form.submitted === null ? "Button pressed; the page did not clearly confirm it."
    : form.stopped ? `Stopped (${form.stopped}). What is listed as filled was done.` : "Not submitted.";
  return (
    <Section title="Last form" id="pf-last">
      <p><strong>{form.heading || form.window || "A form"}</strong></p>
      <p className="meta tabular">{counts} filled · {outcome}{form.timing_ms != null ? ` · ${(form.timing_ms / 1000).toFixed(1)} s` : ""}</p>
      {form.fields.length > 0 && (
        <ul className="profile-form-fields">
          {form.fields.map((f, i) => (
            <li key={`${f.label}-${i}`}><span>{f.label}</span><span className="meta">{FIELD_STATUS[f.status] ?? f.status}{f.from ? ` · from ${f.from}` : ""}</span></li>
          ))}
        </ul>
      )}
      <p className="meta">Names and outcomes only. The form on your screen shows the values.</p>
    </Section>
  );
}

function Stored({ data }: { data: ProfileData }) {
  const groups = Object.entries(data.categories).map(([id, name]) => ({ id, name, entries: data.entries.filter((e) => e.category === id) }));
  return (
    <>
      <Section title="Add" id="pf-add"><AddEntry data={data} /></Section>
      {data.entries.length === 0 && <Empty>Nothing saved yet. Only what you add here or ask XENIO to save is kept.</Empty>}
      {groups.filter((g) => g.entries.length > 0).map((g) => (
        <Section key={g.id} title={g.name} id={`pf-${g.id}`}>
          <ul className="memory">{g.entries.map((e) => <EntryRow key={`${e.id}-${e.updated}`} entry={e} />)}</ul>
        </Section>
      ))}
      <Section title="How it is kept" id="pf-how">
        <dl className="facts facts--wide">
          <dt>On this Mac</dt><dd>Encrypted with {data.protected_by}. It is not part of Memory and is not in every conversation.</dd>
          <dt>Filling forms</dt><dd>XENIO puts values straight into the form: the AI model sees the names of fields and details, not the values.</dd>
          <dt>In a conversation</dt><dd>If you ask what an ordinary detail says, XENIO reads it for you, and that answer goes through the model. Sensitive ones never do.</dd>
          <dt>Never stored</dt><dd>Passwords, codes, tokens and payment details are refused here, even if you ask.</dd>
          <dt>Submitting</dt><dd>XENIO always asks you first, and asks you to confirm for money, legal, identity or health forms.</dd>
        </dl>
      </Section>
    </>
  );
}

export function ProfilePage() {
  const { app } = useApp();
  const profile = useResource((s) => app.api.profile(s), ["profile"], []);
  return (
    <>
      <PageHeader title="Profile" intro="Details XENIO can put into forms for you. You decide what is here; every entry can be corrected or deleted." />
      <Loaded resource={profile} what="your profile">
        {(data) => (
          <>
            {data.available ? <Stored data={data} /> : <Empty>{data.reason ?? "The profile cannot be read right now."}</Empty>}
            {data.last_form && <LastFormCard form={data.last_form} />}
          </>
        )}
      </Loaded>
    </>
  );
}
