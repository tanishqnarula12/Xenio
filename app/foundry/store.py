"""Everything The Foundry must remember, in one SQLite file in XENIO's data folder, readable by you alone. Schema changes are numbered migrations, so an older file is brought up to date.

Every change also goes in the event log: Mission Control follows it live, and a restart can read back where things stood. Free text is redacted on the way in, so a secret that slips into a
message or a tool result is not kept. Keys are never stored here at all.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from app.safety.policy import redact_secrets

MIGRATIONS: list[tuple[int, str]] = [
    (1, """
    CREATE TABLE agents (id TEXT PRIMARY KEY, body TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL, source TEXT NOT NULL, workflow_id TEXT NOT NULL DEFAULT '', version INTEGER NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
    CREATE TABLE agent_versions (agent_id TEXT NOT NULL, version INTEGER NOT NULL, body TEXT NOT NULL, why TEXT NOT NULL, actor TEXT NOT NULL, at REAL NOT NULL, PRIMARY KEY (agent_id, version));
    CREATE TABLE agent_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL, workflow_id TEXT NOT NULL, task_id TEXT NOT NULL DEFAULT '', outcome TEXT NOT NULL, score REAL, seconds REAL, calls INTEGER, at REAL NOT NULL);
    CREATE INDEX agent_runs_agent ON agent_runs (agent_id);
    CREATE TABLE memory (id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, owner TEXT NOT NULL, text TEXT NOT NULL, approved INTEGER NOT NULL DEFAULT 0, at REAL NOT NULL);
    CREATE INDEX memory_scope ON memory (scope, owner);
    """),
    (2, """
    CREATE TABLE workflows (id TEXT PRIMARY KEY, goal TEXT NOT NULL, status TEXT NOT NULL, body TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
    CREATE TABLE nodes (workflow_id TEXT NOT NULL, id TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL, updated REAL NOT NULL, PRIMARY KEY (workflow_id, id));
    CREATE TABLE messages (id TEXT PRIMARY KEY, seq INTEGER NOT NULL, workflow_id TEXT NOT NULL, task_id TEXT NOT NULL DEFAULT '', sender TEXT NOT NULL, recipient TEXT NOT NULL, type TEXT NOT NULL, correlation_id TEXT NOT NULL DEFAULT '', body TEXT NOT NULL, at REAL NOT NULL);
    CREATE INDEX messages_workflow ON messages (workflow_id, seq);
    CREATE TABLE decisions (id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, status TEXT NOT NULL, body TEXT NOT NULL, at REAL NOT NULL);
    CREATE INDEX decisions_workflow ON decisions (workflow_id);
    CREATE TABLE loop_iterations (workflow_id TEXT NOT NULL, loop_id TEXT NOT NULL, iteration INTEGER NOT NULL, body TEXT NOT NULL, at REAL NOT NULL, PRIMARY KEY (workflow_id, loop_id, iteration));
    CREATE TABLE interventions (id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, body TEXT NOT NULL, at REAL NOT NULL);
    CREATE INDEX interventions_workflow ON interventions (workflow_id);
    CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL, type TEXT NOT NULL, workflow_id TEXT NOT NULL DEFAULT '', agent_id TEXT NOT NULL DEFAULT '', task_id TEXT NOT NULL DEFAULT '', body TEXT NOT NULL, at REAL NOT NULL);
    CREATE INDEX events_workflow ON events (workflow_id, seq);
    """),
    (3, """
    CREATE TABLE skills (id TEXT PRIMARY KEY, body TEXT NOT NULL, state TEXT NOT NULL, version TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL);
    CREATE TABLE skill_history (id INTEGER PRIMARY KEY AUTOINCREMENT, skill_id TEXT NOT NULL, action TEXT NOT NULL, version TEXT NOT NULL, body TEXT NOT NULL, at REAL NOT NULL);
    CREATE INDEX skill_history_skill ON skill_history (skill_id);
    """),
]


def iso(seconds: float | None) -> str:
    return datetime.fromtimestamp(seconds).isoformat(timespec="seconds") if seconds else ""


def scrub(value):
    """Secrets out of anything about to be kept, however deep."""
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v) for v in value]
    return value


class FoundryStore:
    def __init__(self, path: Path, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self._lock = threading.RLock()
        self.on_event = None  # Mission Control's live stream
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.schema = self._migrate()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _migrate(self) -> int:
        with self._lock, self.connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
            row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
            current = int(row["v"] or 0)
            for version, sql in MIGRATIONS:
                if version > current:
                    conn.executescript(sql)
                    conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
                    current = version
            return current

    def execute(self, sql: str, params: tuple = ()) -> tuple[int, int]:
        with self._lock, self.connect() as conn:
            cur = conn.execute(sql, params)
            return int(cur.lastrowid or 0), int(cur.rowcount)

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple = ()) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def event(self, event_type: str, workflow: str = "", agent: str = "", task: str = "", /, **payload) -> dict:
        """Something happened. Kept, and passed on to whoever is watching. A watcher's failure never stops the work."""
        clean = scrub(payload)
        at = self.clock()
        event_id = uuid.uuid4().hex
        seq, _ = self.execute("INSERT INTO events (id, type, workflow_id, agent_id, task_id, body, at) VALUES (?,?,?,?,?,?,?)",
                              (event_id, event_type, workflow, agent, task, json.dumps(clean, ensure_ascii=False, default=str), at))
        out = {"id": event_id, "seq": seq, "type": event_type, "workflow": workflow, "agent": agent, "task": task, "at": iso(at), "data": clean}
        if self.on_event is not None:
            try:
                self.on_event(out)
            except Exception:  # noqa: BLE001
                pass
        return out

    def events(self, workflow: str = "", since: int = 0, limit: int = 500) -> list[dict]:
        rows = (self.query("SELECT * FROM events WHERE workflow_id = ? AND seq > ? ORDER BY seq LIMIT ?", (workflow, since, limit)) if workflow
                else self.query("SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT ?", (since, limit)))
        return [{"id": r["id"], "seq": r["seq"], "type": r["type"], "workflow": r["workflow_id"], "agent": r["agent_id"], "task": r["task_id"], "at": iso(r["at"]), "data": json.loads(r["body"])} for r in rows]
