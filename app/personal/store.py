"""XENIO's personal database: tasks, projects (with their notes and decisions) and notifications.

One SQLite file, ``<data dir>/personal.db`` - standard library, safe across the three processes that
use it (XENIO, its Hermes process, a scheduled check). Deliberately separate from Hermes' memory:
memory is a few curated facts injected into every prompt; tasks and project notes are records that
are looked up when needed.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from app.integrations.base import data_dir

DB_FILE = "personal.db"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',      -- pending | in_progress | done | cancelled
    priority TEXT NOT NULL DEFAULT 'normal',     -- low | normal | high
    due TEXT,                                    -- YYYY-MM-DD or YYYY-MM-DDTHH:MM
    project TEXT,
    source TEXT NOT NULL DEFAULT 'user',         -- user | xenio | schedule
    created TEXT NOT NULL,
    updated TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS projects (
    name TEXT PRIMARY KEY COLLATE NOCASE,
    description TEXT NOT NULL DEFAULT '',
    repository TEXT NOT NULL DEFAULT '',         -- owner/name on GitHub
    path TEXT NOT NULL DEFAULT '',               -- local folder
    status TEXT NOT NULL DEFAULT 'active',       -- active | paused | done
    focus TEXT NOT NULL DEFAULT '',              -- what the user is working on in it right now
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    last_active TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS project_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL COLLATE NOCASE,
    kind TEXT NOT NULL,                          -- fact | decision | tech | file | issue
    text TEXT NOT NULL,
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT UNIQUE,                             -- de-duplication: the same event is stored once
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    priority TEXT NOT NULL DEFAULT 'normal',     -- low | normal | high
    source TEXT NOT NULL,
    created TEXT NOT NULL,
    action_required INTEGER NOT NULL DEFAULT 0,
    delivered TEXT
);
CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS notes_project ON project_notes(project, kind);
"""


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class PersonalStore:
    def __init__(self, path: Path | None = None):
        self.path = path or data_dir() / DB_FILE
        self._ready = False

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        if not self._ready:
            conn.executescript(_SCHEMA)
            self._ready = True
        return conn

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        conn = self.connect()
        try:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def execute(self, sql: str, params: tuple = ()) -> tuple[int, int]:
        """(lastrowid, rowcount), committed."""
        conn = self.connect()
        try:
            with conn:
                cur = conn.execute(sql, params)
                return cur.lastrowid or 0, cur.rowcount
        finally:
            conn.close()
