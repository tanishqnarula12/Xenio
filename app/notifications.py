"""Notifications: things XENIO wants to tell the user without being asked.

A notification has a title, message, priority (low | normal | high), source ("schedule", "xenio"...),
timestamp and whether it needs the user to act. They are stored in the personal database (so nothing
is lost while XENIO is closed) under a de-duplication key: the same scheduled run or event is stored
once, however often it is seen.

Delivery channels in V4 are the existing interfaces: the console prints them, voice mode speaks them
when it is idle. Desktop pop-ups, Telegram or WhatsApp are later channels behind the same ``Channel``
shape.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Protocol

from app.personal.store import PersonalStore, now_iso

PRIORITIES = ("low", "normal", "high")


@dataclass
class Notification:
    title: str
    message: str
    priority: str = "normal"
    source: str = "xenio"
    created: str = ""
    action_required: bool = False
    key: str | None = None
    id: int | None = None
    delivered: str | None = None

    def spoken(self) -> str:
        return f"{self.title}. {self.message}" if self.title and not self.message.startswith(self.title) else self.message

    def line(self) -> str:
        flag = " [action needed]" if self.action_required else ""
        mark = "!!" if self.priority == "high" else "**"
        return f"{mark} {self.title}{flag}: {self.message}"


class Channel(Protocol):
    async def deliver(self, notification: Notification) -> bool: ...


class NotificationCenter:
    def __init__(self, store: PersonalStore | None = None):
        self.store = store or PersonalStore()

    def add(self, title: str, message: str, priority: str = "normal", source: str = "xenio",
            action_required: bool = False, key: str | None = None) -> Notification | None:
        """Store a notification; returns None when one with the same key already exists (duplicate)."""
        note = Notification(title=" ".join(title.split())[:120], message=message.strip()[:2000],
                            priority=priority if priority in PRIORITIES else "normal", source=source[:40],
                            created=now_iso(), action_required=action_required, key=key)
        try:
            note.id, _ = self.store.execute(
                "INSERT INTO notifications (key, title, message, priority, source, created, action_required) "
                "VALUES (?,?,?,?,?,?,?)", (key, note.title, note.message, note.priority, note.source, note.created,
                                           int(action_required)))
        except sqlite3.IntegrityError:
            return None
        return note

    def seen(self, key: str) -> bool:
        return bool(self.store.query("SELECT 1 FROM notifications WHERE key = ?", (key,)))

    def pending(self, limit: int = 20) -> list[Notification]:
        rows = self.store.query("SELECT * FROM notifications WHERE delivered IS NULL ORDER BY "
                                "CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, id LIMIT ?", (limit,))
        return [_from_row(r) for r in rows]

    def recent(self, limit: int = 10) -> list[Notification]:
        return [_from_row(r) for r in self.store.query("SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (limit,))]

    def mark_delivered(self, ids: list[int]) -> None:
        for note_id in ids:
            self.store.execute("UPDATE notifications SET delivered = ? WHERE id = ? AND delivered IS NULL", (now_iso(), note_id))

    def clear(self) -> int:
        _, count = self.store.execute("UPDATE notifications SET delivered = ? WHERE delivered IS NULL", (now_iso(),))
        return count


def _from_row(row: dict) -> Notification:
    return Notification(title=row["title"], message=row["message"], priority=row["priority"], source=row["source"],
                        created=row["created"], action_required=bool(row["action_required"]), key=row["key"],
                        id=row["id"], delivered=row["delivered"])
