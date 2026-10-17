"""The user's tasks: title, description, status, priority, due date, project, source, timestamps.

Kept small on purpose (no boards, assignees or sub-tasks). Adding/updating/completing is SAFE_WRITE
(it only changes XENIO's own list); deleting a task is CONFIRM_REQUIRED.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

from app.integrations.base import IntegrationError, clip
from app.personal.store import PersonalStore, now_iso

STATUSES = ("pending", "in_progress", "done", "cancelled")
PRIORITIES = ("low", "normal", "high")
OPEN = ("pending", "in_progress")


@dataclass
class Task:
    id: int
    title: str
    description: str
    status: str
    priority: str
    due: str | None
    project: str | None
    source: str
    created: str
    updated: str

    def to_dict(self) -> dict:
        out = {k: v for k, v in self.__dict__.items() if v not in (None, "")}
        if self.overdue():
            out["overdue"] = True
        return out

    def overdue(self, today: date | None = None) -> bool:
        if not self.due or self.status not in OPEN:
            return False
        try:
            return date.fromisoformat(self.due[:10]) < (today or date.today())
        except ValueError:
            return False


def _due(value: str | None) -> str | None:
    if value is None or not str(value).strip():
        return None
    text = str(value).strip()
    lowered = text.lower()
    today = date.today()
    if lowered == "today":
        return today.isoformat()
    if lowered == "tomorrow":
        return date.fromordinal(today.toordinal() + 1).isoformat()
    for parse in (date.fromisoformat, datetime.fromisoformat):
        try:
            parsed = parse(text)
            return parsed.isoformat(timespec="minutes") if isinstance(parsed, datetime) else parsed.isoformat()
        except ValueError:
            continue
    raise IntegrationError(f"Due date {text!r} not understood; use today, tomorrow or 2026-10-05.")


def _choice(value: str | None, allowed: tuple[str, ...], default: str, what: str) -> str:
    value = (value or default).strip().lower().replace(" ", "_").replace("-", "_")
    value = {"open": "pending", "todo": "pending", "completed": "done", "complete": "done", "doing": "in_progress",
             "urgent": "high", "medium": "normal"}.get(value, value)
    if value not in allowed:
        raise IntegrationError(f"{what} must be one of: {', '.join(allowed)}")
    return value


class TaskList:
    def __init__(self, store: PersonalStore | None = None):
        self.store = store or PersonalStore()

    def _row(self, task_id: int) -> Task:
        rows = self.store.query("SELECT * FROM tasks WHERE id = ?", (int(task_id),))
        if not rows:
            raise IntegrationError(f"There is no task #{task_id}.")
        return Task(**rows[0])

    def add(self, title: str, description: str = "", priority: str = "normal", due: str | None = None,
            project: str | None = None, source: str = "user") -> Task:
        title = clip(title, 200)
        if not title:
            raise IntegrationError("A task needs a title.")
        stamp = now_iso()
        task_id, _ = self.store.execute(
            "INSERT INTO tasks (title, description, priority, due, project, source, created, updated) VALUES (?,?,?,?,?,?,?,?)",
            (title, (description or "").strip()[:2000], _choice(priority, PRIORITIES, "normal", "priority"), _due(due),
             (project or "").strip() or None, source if source in ("user", "xenio", "schedule") else "user", stamp, stamp))
        return self._row(task_id)

    def list(self, status: str = "open", project: str | None = None, limit: int = 50) -> list[Task]:
        clauses, params = [], []
        if status in ("open", "", None):
            clauses.append("status IN ('pending','in_progress')")
        elif status != "all":
            clauses.append("status = ?")
            params.append(_choice(status, STATUSES, "pending", "status"))
        if project:
            clauses.append("project = ? COLLATE NOCASE")
            params.append(project.strip())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.store.query(
            f"SELECT * FROM tasks {where} ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, "
            "due IS NULL, due, id LIMIT ?", (*params, max(1, min(int(limit or 50), 200))))
        return [Task(**r) for r in rows]

    def update(self, task_id: int, **changes) -> Task:
        task = self._row(task_id)
        fields = {}
        if changes.get("title") is not None:
            fields["title"] = clip(changes["title"], 200) or task.title
        if changes.get("description") is not None:
            fields["description"] = str(changes["description"]).strip()[:2000]
        if changes.get("status") is not None:
            fields["status"] = _choice(changes["status"], STATUSES, task.status, "status")
        if changes.get("priority") is not None:
            fields["priority"] = _choice(changes["priority"], PRIORITIES, task.priority, "priority")
        if "due" in changes and changes["due"] is not None:
            fields["due"] = _due(changes["due"])
        if changes.get("project") is not None:
            fields["project"] = str(changes["project"]).strip() or None
        if not fields:
            raise IntegrationError("Nothing to change: give a new title, status, priority, due date or project.")
        fields["updated"] = now_iso()
        assignments = ", ".join(f"{k} = ?" for k in fields)
        self.store.execute(f"UPDATE tasks SET {assignments} WHERE id = ?", (*fields.values(), task.id))
        return self._row(task.id)

    def complete(self, task_id: int) -> Task:
        return self.update(task_id, status="done")

    def delete(self, task_id: int) -> Task:
        task = self._row(task_id)
        self.store.execute("DELETE FROM tasks WHERE id = ?", (task.id,))
        return task

    def find(self, text: str) -> list[Task]:
        """Open tasks whose title contains every word (to resolve "finish the CRM report")."""
        words = [w for w in re.findall(r"\w+", (text or "").lower()) if len(w) > 2]
        return [t for t in self.list("open") if all(w in t.title.lower() for w in words)] if words else []

    def counts(self) -> dict:
        rows = self.store.query("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status")
        by = {r["status"]: r["n"] for r in rows}
        open_tasks = self.list("open", limit=200)
        today = date.today().isoformat()
        return {"open": by.get("pending", 0) + by.get("in_progress", 0), "done": by.get("done", 0),
                "overdue": sum(t.overdue() for t in open_tasks),
                "due_today": sum(1 for t in open_tasks if (t.due or "")[:10] == today),
                "high_priority": sum(1 for t in open_tasks if t.priority == "high")}
