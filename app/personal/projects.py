"""Project context: what the user is working on, and what is known about each project.

A project has a name, description, GitHub repository, local folder, status and a current focus
("the analytics feature"). Notes hold what was learned along the way, each with a kind:
``fact``, ``decision``, ``tech`` (stack, database...), ``file`` (relevant files) or ``issue``.

Nothing here is inferred: a project exists only because the user (or XENIO at the user's request)
recorded it, so "what am I working on?" is answered from records - or with "I don't know yet".
"""

from __future__ import annotations

from app.integrations.base import IntegrationError, clip
from app.personal.store import PersonalStore, now_iso
from app.personal.tasks import TaskList

NOTE_KINDS = ("fact", "decision", "tech", "file", "issue")
STATUSES = ("active", "paused", "done")


class Projects:
    def __init__(self, store: PersonalStore | None = None):
        self.store = store or PersonalStore()

    def _get(self, name: str) -> dict | None:
        rows = self.store.query("SELECT * FROM projects WHERE name = ?", (name.strip(),))
        return rows[0] if rows else None

    def resolve(self, name: str | None) -> dict:
        """The project the user means: exact name, else a unique partial match, else a clear error
        (several matches -> the caller asks the user which one; none -> it is not recorded)."""
        if not name or not name.strip():
            current = self.current()
            if current is None:
                raise IntegrationError("No current project is recorded. Tell me which project you're working on.")
            return current
        found = self._get(name)
        if found:
            return found
        needle = name.strip().lower()
        known = self.list(include_done=True)
        # "crm analytics" -> "CRM Analytics Dashboard"; only if nothing contains the words: "the crm backend" -> "CRM"
        matches = [p for p in known if needle in p["name"].lower()] or [p for p in known if p["name"].lower() in needle]
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise IntegrationError("Several projects match " + repr(name) + ": " + ", ".join(p["name"] for p in matches)
                                   + ". Ask the user which one.")
        raise IntegrationError(f"No project called {name!r} is recorded.")

    def upsert(self, name: str, description: str | None = None, repository: str | None = None, path: str | None = None,
               status: str | None = None, focus: str | None = None, make_current: bool = False) -> dict:
        name = clip(name, 80)
        if not name:
            raise IntegrationError("A project needs a name.")
        if status is not None and status not in STATUSES:
            raise IntegrationError(f"status must be one of: {', '.join(STATUSES)}")
        stamp = now_iso()
        existing = self._get(name)
        if existing is None:
            self.store.execute(
                "INSERT INTO projects (name, description, repository, path, status, focus, created, updated, last_active) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (name, (description or "").strip()[:500], (repository or "").strip(), (path or "").strip(), status or "active",
                 (focus or "").strip()[:300], stamp, stamp, stamp if make_current or focus else "1970-01-01T00:00:00"))
        else:
            fields = {k: v.strip()[:500] for k, v in (("description", description), ("repository", repository),
                                                       ("path", path), ("focus", focus)) if v is not None}
            if status is not None:
                fields["status"] = status
            fields["updated"] = stamp
            if make_current or focus:
                fields["last_active"] = stamp
            self.store.execute(f"UPDATE projects SET {', '.join(f'{k} = ?' for k in fields)} WHERE name = ?",
                               (*fields.values(), existing["name"]))
        return self._get(name) or {}

    def set_current(self, name: str, focus: str | None = None) -> dict:
        project = self._get(name) or self.upsert(name)
        return self.upsert(project["name"], focus=focus, make_current=True)

    def current(self) -> dict | None:
        rows = self.store.query("SELECT * FROM projects WHERE status = 'active' AND last_active > '1970-01-02' "
                                "ORDER BY last_active DESC LIMIT 1")
        return rows[0] if rows else None

    def list(self, include_done: bool = False) -> list[dict]:
        where = "" if include_done else "WHERE status != 'done'"
        return self.store.query(f"SELECT * FROM projects {where} ORDER BY last_active DESC, name")

    def add_note(self, project: str, kind: str, text: str) -> dict:
        target = self.resolve(project)
        kind = (kind or "fact").strip().lower()
        if kind not in NOTE_KINDS:
            raise IntegrationError(f"kind must be one of: {', '.join(NOTE_KINDS)}")
        text = " ".join(str(text or "").split())[:600]
        if not text:
            raise IntegrationError("The note is empty.")
        duplicate = self.store.query("SELECT id FROM project_notes WHERE project = ? AND kind = ? AND lower(text) = lower(?)",
                                     (target["name"], kind, text))
        if not duplicate:
            self.store.execute("INSERT INTO project_notes (project, kind, text, created) VALUES (?,?,?,?)",
                               (target["name"], kind, text, now_iso()))
        self.store.execute("UPDATE projects SET updated = ? WHERE name = ?", (now_iso(), target["name"]))
        return {"project": target["name"], "kind": kind, "text": text, "already_known": bool(duplicate)}

    def notes(self, project: str, kind: str | None = None, limit: int = 30) -> list[dict]:
        clause, params = ("AND kind = ?", (kind,)) if kind else ("", ())
        return self.store.query(f"SELECT kind, text, created FROM project_notes WHERE project = ? {clause} "
                                "ORDER BY id DESC LIMIT ?", (project, *params, limit))

    def context(self, name: str | None = None, tasks: TaskList | None = None) -> dict:
        """Everything recorded about one project (the current one by default)."""
        project = self.resolve(name)
        notes = self.notes(project["name"], limit=60)
        by_kind: dict[str, list] = {}
        for note in notes:
            by_kind.setdefault(note["kind"], []).append({"text": note["text"], "when": note["created"][:10]})
        decisions = by_kind.get("decision", [])
        open_tasks = (tasks or TaskList(self.store)).list("open", project=project["name"], limit=20)
        current = self.current()
        return {"project": project["name"], "is_current": bool(current and current["name"] == project["name"]),
                "status": project["status"], "focus": project["focus"], "description": project["description"],
                "repository": project["repository"], "path": project["path"],
                "last_decision": decisions[0] if decisions else None,
                "decisions": decisions[:10], "tech": by_kind.get("tech", [])[:15], "facts": by_kind.get("fact", [])[:15],
                "files": by_kind.get("file", [])[:15], "issues": by_kind.get("issue", [])[:10],
                "open_tasks": [t.to_dict() for t in open_tasks]}
