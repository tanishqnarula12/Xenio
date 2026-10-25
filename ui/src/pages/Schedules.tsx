import { BellRing, Trash2 } from "lucide-react";
import { useState, type FormEvent } from "react";
import type { Reminder } from "../api/types";
import { Button, Empty, IconButton, Loaded, PageHeader, Section, Status, StatusOf, Time } from "../components/ui";
import { useApp } from "../state/context";
import { useResource } from "../state/useResource";

const RESULT: Record<string, string> = { message: "Reported something", silent: "Nothing to report", failed: "Failed" };

function NewReminder({ onAdded }: { onAdded: () => void }) {
  const { app } = useApp();
  const [text, setText] = useState("");
  const [when, setWhen] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const made = await app.api.createReminder(text, when);
      app.store.notify({ tone: "ok", title: "Reminder set", message: `${made.text} - ${made.when}` });
      setText("");
      setWhen("");
      onAdded();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <form className="inline-form" onSubmit={submit} aria-label="New reminder">
      <div className="field">
        <label htmlFor="reminder-text">Remind me to</label>
        <input id="reminder-text" value={text} onChange={(e) => setText(e.target.value)} maxLength={300} placeholder="call mom" />
      </div>
      <div className="field">
        <label htmlFor="reminder-when">When</label>
        <input id="reminder-when" value={when} onChange={(e) => setWhen(e.target.value)} maxLength={60}
          placeholder="at 5 pm · in 20 minutes · every day at 8 am" />
      </div>
      <Button type="submit" variant="primary" disabled={busy || !text.trim() || !when.trim()}>{busy ? "Setting…" : "Set reminder"}</Button>
      {error && <p className="error-text" role="alert">{error}</p>}
    </form>
  );
}

function ReminderRow({ reminder, onRemoved }: { reminder: Reminder; onRemoved: () => void }) {
  const { app } = useApp();
  const remove = async () => {
    try {
      await app.api.deleteReminder(reminder.id);
      onRemoved();
    } catch (e) {
      app.store.notify({ tone: "danger", title: "Could not remove it", message: (e as Error).message });
    }
  };
  return (
    <li className={`task ${reminder.done ? "task--done" : ""}`}>
      <BellRing size={16} aria-hidden="true" className="task__icon" />
      <span className="task__title">{reminder.text}</span>
      <span className="task__meta">
        {reminder.done ? <Status tone="ok">Reminded</Status> : <span className="meta">{reminder.schedule && !reminder.next_run ? reminder.schedule : ""}</span>}
        <Time iso={reminder.done ? reminder.last_run : reminder.next_run} prefix={reminder.done ? "" : "next"} />
      </span>
      <IconButton label={`Remove the reminder: ${reminder.text}`} icon={Trash2} onClick={() => void remove()} />
    </li>
  );
}

export function SchedulesPage() {
  const { app } = useApp();
  const schedules = useResource((s) => app.api.schedules(s), ["schedules", "activity"]);
  return (
    <>
      <PageHeader title="Schedules" intro={<>Reminders pop up on your Mac and XENIO says them out loud. Scheduled checks run on Hermes' own scheduler, read-only and guarded: ask XENIO for one, like "check my inbox every morning at 8".</>} />
      <Section title="Reminders" id="reminders">
        <NewReminder onAdded={schedules.reload} />
        <Loaded resource={schedules} what="reminders">
          {(data) => {
            const reminders = data.reminders ?? [];
            if (!reminders.length) return <Empty>No reminders. Set one above, or say "Hey Xenio, remind me at 5 to call mom".</Empty>;
            return <ul className="tasks">{reminders.map((r) => <ReminderRow key={r.id} reminder={r} onRemoved={schedules.reload} />)}</ul>;
          }}
        </Loaded>
      </Section>
      {schedules.data && (
      <Section title="Scheduled checks" id="checks">
        {((data) => {
          if (!data.jobs.length) return <Empty>{data.note ? "No scheduled checks yet." : "No scheduled checks."} Ask XENIO to set one up.</Empty>;
          return (
            <>
              <p className="meta">
                {data.ticking ? "Checks run while XENIO is open (or a Hermes gateway runs them)." : "Nothing is running the checks right now: start XENIO to run them."}
                {data.timezone ? ` Times are in ${data.timezone}.` : ""}
              </p>
              <ul className="schedules">
                {data.jobs.map((j) => (
                  <li key={j.id} className="schedule">
                    <div className="schedule__head">
                      <h2 className="schedule__name">{j.name}</h2>
                      {j.enabled ? <Status tone="ok">On</Status> : <StatusOf value="disabled" />}
                      {j.last_status === "error" && <Status tone="danger">Last run failed</Status>}
                    </div>
                    <p>{j.task}</p>
                    <dl className="facts">
                      <dt>When</dt><dd>{j.schedule ?? "unknown"}</dd>
                      <dt>Next run</dt><dd><Time iso={j.next_run} /></dd>
                      <dt>Last run</dt><dd><Time iso={j.last_run} /></dd>
                    </dl>
                    {j.last_error && <p className="error-text">{j.last_error}</p>}
                    {!!j.runs.length && (
                      <details>
                        <summary>Recent runs</summary>
                        <ol className="timeline">
                          {j.runs.map((r) => (
                            <li key={r.at} className="timeline__item">
                              <p className="timeline__text">{RESULT[r.result] ?? r.result}{r.message ? `: ${r.message}` : ""}</p>
                              <span className="meta">{r.at}</span>
                            </li>
                          ))}
                        </ol>
                      </details>
                    )}
                  </li>
                ))}
              </ul>
            </>
          );
        })(schedules.data)}
      </Section>
      )}
    </>
  );
}
