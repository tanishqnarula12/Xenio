"""What is being learned and how it is going: one SQLite file, ``<data dir>/learning.db``, standard library only, safe across XENIO and
its Hermes process. Deliberately not Hermes' memory: progress is records to look up and correct, not lines in every prompt."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from app.integrations.base import data_dir

DB_FILE = "learning.db"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE COLLATE NOCASE, goal TEXT NOT NULL DEFAULT '', level TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT 'teach', language TEXT NOT NULL DEFAULT '', concept_id INTEGER, created TEXT NOT NULL, last_active TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS concepts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, topic_id INTEGER NOT NULL, name TEXT NOT NULL COLLATE NOCASE, position INTEGER NOT NULL DEFAULT 0,
    prerequisites TEXT NOT NULL DEFAULT '[]', UNIQUE(topic_id, name));
CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT, topic_id INTEGER NOT NULL, concept_id INTEGER, mode TEXT NOT NULL, pack TEXT NOT NULL, model TEXT NOT NULL DEFAULT '',
    ms INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, topic_id INTEGER NOT NULL, concept_id INTEGER, lesson_id INTEGER, kind TEXT NOT NULL, question TEXT NOT NULL,
    options TEXT NOT NULL DEFAULT '[]', answer TEXT NOT NULL DEFAULT '', explanation TEXT NOT NULL DEFAULT '', hints TEXT NOT NULL DEFAULT '[]',
    wrong_ideas TEXT NOT NULL DEFAULT '{}', hints_used INTEGER NOT NULL DEFAULT 0, revealed INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'open', created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, question_id INTEGER NOT NULL, topic_id INTEGER NOT NULL, concept_id INTEGER, answer TEXT NOT NULL, credit REAL NOT NULL,
    graded_by TEXT NOT NULL, feedback TEXT NOT NULL DEFAULT '', misconception TEXT NOT NULL DEFAULT '', hints_used INTEGER NOT NULL DEFAULT 0, revealed INTEGER NOT NULL DEFAULT 0,
    corrected INTEGER NOT NULL DEFAULT 0, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS materials (
    id INTEGER PRIMARY KEY AUTOINCREMENT, topic_id INTEGER NOT NULL, title TEXT NOT NULL, path TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL, pages INTEGER NOT NULL DEFAULT 0,
    chars INTEGER NOT NULL DEFAULT 0, unread_pages INTEGER NOT NULL DEFAULT 0, ocr_pages INTEGER NOT NULL DEFAULT 0, embedded INTEGER NOT NULL DEFAULT 0, added TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, material_id INTEGER NOT NULL, topic_id INTEGER NOT NULL, page INTEGER NOT NULL DEFAULT 0, text TEXT NOT NULL, embedding BLOB);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text, content='chunks', content_rowid='id', tokenize='unicode61');
CREATE TABLE IF NOT EXISTS prefs (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS attempts_concept ON attempts(concept_id, at);
CREATE INDEX IF NOT EXISTS questions_topic ON questions(topic_id, state);
CREATE INDEX IF NOT EXISTS chunks_topic ON chunks(topic_id);
"""


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class LearningStore:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else data_dir() / DB_FILE
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

    def one(self, sql: str, params: tuple = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: tuple = ()) -> tuple[int, int]:
        conn = self.connect()
        try:
            with conn:
                cur = conn.execute(sql, params)
                return cur.lastrowid or 0, cur.rowcount
        finally:
            conn.close()

    def many(self, statements: list[tuple[str, tuple]]) -> None:
        conn = self.connect()
        try:
            with conn:
                for sql, params in statements:
                    conn.execute(sql, params)
        finally:
            conn.close()

    def pref(self, key: str, default: str = "") -> str:
        row = self.one("SELECT value FROM prefs WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_pref(self, key: str, value: str) -> None:
        self.execute("INSERT INTO prefs (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
