import { Trash2 } from "lucide-react";
import { useState, type FormEvent } from "react";
import type { PersonalTask } from "../api/types";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { Button, Empty, ErrorNote, IconButton, Loaded, PageHeader, Section, Status, StatusOf, Time } from "../components/ui";
import { useApp } from "../state/context";
import { href } from "../state/router";
import { useResource } from "../state/useResource";

function NewTask({ onSaved }: { onSaved: () => void }) {
  const { app } = useApp();
  const [title, setTitle] = useState("");
  const [priority, setPriority] = useState("normal");
  const [due, setDue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!title.trim()) { setError("A task needs a title."); return; }
    setSaving(true);
    setError(null);
    try {
      await app.api.createTask({ title: title.trim(), priority, due: due || undefined });
      setTitle(""); setDue(""); setPriority("normal");
      onSaved();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };
  return (
    <form className="inline-form" onSubmit={submit} aria-label="Add a task">
      <div className="field field--grow"><label htmlFor="task-title">New task</label>
        <input id="task-title" value={title} onChange={(e) => setTitle(e.target.value)} maxLength={200} placeholder="What needs doing" /></div>
      <div className="field"><label htmlFor="task-priority">Priority</label>
        <select id="task-priority" value={priority} onChange={(e) => setPriority(e.target.value)}>
          <option value="low">Low</option><option value="normal">Normal</option><option value="high">High</option>
        </select></div>
      <div className="field"><label htmlFor="task-due">Due</label>
        <input id="task-due" type="date" value={due} onChange={(e) => setDue(e.target.value)} /></div>
      <Button type="submit" variant="primary" disabled={saving}>{saving ? "Adding…" : "Add task"}</Button>
      {error && <p className="error-text" role="alert">{error}</p>}
    </form>
  );
}

function TaskRow({ task, onChanged, onDelete }: { task: PersonalTask; onChanged: () => void; onDelete: (t: PersonalTask) => void }) {
  const { app } = useApp();
  const [error, setError] = useState<string | null>(null);
  const done = task.status === "done";
  const toggle = async () => {
    try {
      await app.api.updateTask(task.id, done ? "reopen" : "complete");
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };
  return (
    <li className={`task ${done ? "task--done" : ""}`}>
      <input type="checkbox" id={`task-${task.id}`} checked={done} onChange={() => void toggle()} />
      <label htmlFor={`task-${task.id}`} className="task__title">{task.title}</label>
      <span className="task__meta">
        {task.priority === "high" && <Status tone="attention">High priority</Status>}
        {task.overdue && <Status tone="danger">Overdue</Status>}
        {task.due && <span className="meta">Due {new Date(`${task.due}T00:00:00`).toLocaleDateString()}</span>}
        {task.project && <span className="meta">{task.project}</span>}
        {done && <Time iso={task.updated} prefix="done" />}
      </span>
      <IconButton label={`Delete "${task.title}"`} icon={Trash2} onClick={() => onDelete(task)} />
      {error && <p className="error-text" role="alert">{error}</p>}
    </li>
  );
}

export function TasksPage() {
  const { app } = useApp();
  const tasks = useResource((s) => app.api.tasks("all", s), ["tasks"]);
  const projects = useResource((s) => app.api.projects(s), ["tasks"]);
  const [deleting, setDeleting] = useState<PersonalTask | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const confirmDelete = async () => {
    const task = deleting;
    setDeleting(null);
    if (!task) return;
    try {
      await app.api.deleteTask(task.id);
      tasks.reload();
    } catch (e) {
      setDeleteError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <>
      <PageHeader title="Tasks & projects" intro="Your own list, kept by XENIO on this Mac. XENIO also adds and finishes tasks when you ask it to." />
      <Section title="Tasks" id="tasks">
        <NewTask onSaved={tasks.reload} />
        {deleteError && <ErrorNote message={deleteError} />}
        <Loaded resource={tasks} what="tasks">
          {(data) => {
            const open = data.tasks.filter((t) => t.status !== "done" && t.status !== "cancelled");
            const done = data.tasks.filter((t) => t.status === "done");
            if (!data.tasks.length) return <Empty>No tasks yet. Add one above, or tell XENIO "remind me to renew my passport".</Empty>;
            return (
              <>
                {open.length ? <ul className="tasks">{open.map((t) => <TaskRow key={t.id} task={t} onChanged={tasks.reload} onDelete={setDeleting} />)}</ul>
                  : <Empty>Nothing open. Well done.</Empty>}
                {!!done.length && (
                  <details className="done-tasks">
                    <summary>{done.length} finished</summary>
                    <ul className="tasks">{done.map((t) => <TaskRow key={t.id} task={t} onChanged={tasks.reload} onDelete={setDeleting} />)}</ul>
                  </details>
                )}
              </>
            );
          }}
        </Loaded>
      </Section>
      <Section title="Built by XENIO" id="workspace-projects">
        <Loaded resource={projects} what="projects">
          {(data) => (data.workspace ?? []).length ? (
            <>
              {data.workspace_path && <p className="meta">In XENIO's workspace, {data.workspace_path}</p>}
              <ul className="rows">
                {(data.workspace ?? []).map((p) => (
                  <li key={p.path} className="row">
                    <span className="row-main"><strong>{p.name}</strong> <span className="meta">{p.kind} · {p.files} item{p.files === 1 ? "" : "s"}{p.git ? " · git" : ""}</span>
                      {p.last_run && <span className="meta"> · last team run: {p.last_run.request}</span>}
                      {p.last_run?.next && <span className="project-next">Next: {p.last_run.next}
                        {p.last_run.left_to_fix ? ` (${p.last_run.left_to_fix} left to fix)` : ""}</span>}</span>
                    <span className="row-side">
                      {p.last_run && <a className="link" href={href("orchestration", p.last_run.id)}><StatusOf value={p.last_run.status} /></a>}
                      <Time iso={p.updated} prefix="changed" />
                    </span>
                  </li>
                ))}
              </ul>
            </>
          ) : <Empty>Nothing built yet. Ask XENIO to make something, like "build me a snake game"; it works in its own workspace.</Empty>}
        </Loaded>
      </Section>
      <Section title="Your projects" id="projects">
        <Loaded resource={projects} what="projects">
          {(data) => data.projects.length ? (
            <ul className="rows">
              {data.projects.map((p) => (
                <li key={p.name} className="row">
                  <span className="row-main"><strong>{p.name}</strong>{p.description ? ` — ${p.description}` : ""}
                    {p.focus && <span className="meta"> Working on: {p.focus}</span>}</span>
                  <span className="row-side">
                    {data.current === p.name && <Status tone="running">Current</Status>}
                    {p.status && <StatusOf value={p.status === "active" ? "in_progress" : p.status} />}
                    {p.path && <span className="meta">{p.path}</span>}
                  </span>
                </li>
              ))}
            </ul>
          ) : <Empty>No projects yet. Tell XENIO about one: "my CRM project is in ~/code/crm".</Empty>}
        </Loaded>
      </Section>
      {deleting && (
        <ConfirmDialog title="Delete this task?" body={`"${deleting.title}" will be removed from your list. This cannot be undone.`}
          confirmLabel="Delete task" onConfirm={() => void confirmDelete()} onCancel={() => setDeleting(null)} />
      )}
    </>
  );
}
