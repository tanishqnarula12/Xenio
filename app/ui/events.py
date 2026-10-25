"""The UI's event stream: one versioned envelope for everything the browser shows as it happens.

``{"v": 1, "seq": 42, "id": "…", "type": "chat.chunk", "at": "2026-10-18T02:10:00", "data": {…}}``

* ``seq`` grows by one per event: a reconnecting browser asks for everything after the last one it saw (missed
  updates are replayed from memory; older ones come from the persisted activity log or a fresh snapshot);
* ``id`` is unique, so a duplicate delivery can be dropped;
* activity events (requests, engineering milestones, approvals, scheduled results, health changes, task, memory, skill
  and settings changes, XENIO starting, stopping and recovering) are also appended to ``<data dir>/ui/activity.jsonl``,
  redacted and WITHOUT what the user typed or said, so the Activity page shows real history after a reload or a
  restart. It is paged newest first (``offset``) and filtered by kind on the server; old entries are pruned by the
  retention the user chose.

Thread-safe: XENIO's asyncio loop publishes, the HTTP server's threads wait for new events.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

VERSION = 1
# what the Activity page keeps across restarts (never chat text: requests are recorded by size only)
ACTIVITY_TYPES = {"request.received", "engineering.milestone", "engineering.run", "approval.requested",
                  "approval.settled", "notification", "health.changed", "assistant.error", "schedule.result",
                  "task.changed", "memory.changed", "skill.changed", "settings.changed", "system.event"}
# the Activity page's filters, decided here so that paging works per filter
ACTIVITY_KINDS = {
    "requests": {"request.received"},
    "engineering": {"engineering.milestone", "engineering.run"},
    "approvals": {"approval.requested", "approval.settled"},
    "scheduled": {"notification", "schedule.result"},
    "changes": {"task.changed", "memory.changed", "skill.changed", "settings.changed"},
    "system": {"health.changed", "assistant.error", "system.event"},
}
MAX_LOG_BYTES = 2_000_000


class EventHub:
    def __init__(self, log_path: Path | None = None, keep: int = 2000, redact: Callable[[str], str] | None = None):
        self._cond = threading.Condition()
        self._events: deque[dict] = deque(maxlen=keep)
        self._seq = 0
        self._log_path = log_path
        self._redact = redact or (lambda text: text)
        self._file = threading.Lock()  # appends and pruning never interleave
        self.listeners = 0  # live views connected to the stream right now (System page)
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def seq(self) -> int:
        with self._cond:
            return self._seq

    def publish(self, type_: str, data: dict[str, Any] | None = None) -> dict:
        with self._cond:
            self._seq += 1
            event = {"v": VERSION, "seq": self._seq, "id": uuid.uuid4().hex, "type": type_,
                     "at": datetime.now().isoformat(timespec="seconds"), "data": data or {}}
            self._events.append(event)
            self._cond.notify_all()
        if type_ in ACTIVITY_TYPES:
            self._persist(event)
        return event

    def since(self, seq: int) -> tuple[list[dict], bool]:
        """Events after ``seq``, and whether some were already dropped from memory (then the browser reloads)."""
        with self._cond:
            events = [e for e in self._events if e["seq"] > seq]
            gap = bool(self._events) and seq < self._events[0]["seq"] - 1 and seq < self._seq
            return events, gap

    def wait(self, seq: int, timeout: float) -> tuple[list[dict], bool]:
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._seq <= seq:
                left = deadline - time.monotonic()
                if left <= 0:
                    return [], False
                self._cond.wait(left)
        return self.since(seq)

    def attach(self) -> None:
        with self._cond:
            self.listeners += 1

    def detach(self) -> None:
        with self._cond:
            self.listeners = max(0, self.listeners - 1)

    def wake_all(self) -> None:
        with self._cond:
            self._cond.notify_all()

    # ---- persisted activity -------------------------------------------------------------------------------------

    def _persist(self, event: dict) -> None:
        if self._log_path is None:
            return
        # typed text (quoted «…» by the policy, e.g. a message about to be sent) never reaches the file, as in the guard's log
        line = re.sub(r"«[^»]*»", "«…»", self._redact(json.dumps(event, ensure_ascii=False, default=str)))
        try:
            with self._file:
                if self._log_path.exists() and self._log_path.stat().st_size > MAX_LOG_BYTES:
                    self._log_path.replace(self._log_path.with_suffix(".old.jsonl"))
                with self._log_path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except OSError:
            pass  # activity history is a convenience; the live stream still works

    def _persisted(self) -> list[dict]:
        lines: list[str] = []
        for path in (self._log_path.with_suffix(".old.jsonl"), self._log_path):
            try:
                lines.extend(path.read_text(encoding="utf-8").splitlines())
            except OSError:
                continue
        out = []
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("type") in ACTIVITY_TYPES:
                out.append(event)
        return out

    def activity(self, limit: int = 200, offset: int = 0, kind: str = "all") -> list[dict]:
        """Persisted activity, newest first (this run's and earlier runs'): ``offset`` events of ``kind`` skipped."""
        if self._log_path is None:
            return []
        wanted = ACTIVITY_KINDS.get(kind)
        events = [e for e in reversed(self._persisted()) if wanted is None or e.get("type") in wanted]
        return events[offset:offset + limit]

    def prune(self, keep_days: int) -> int:
        """Drop persisted activity older than ``keep_days`` (the user's retention). Returns how many were dropped."""
        if self._log_path is None or keep_days <= 0:
            return 0
        with self._file:
            return self._prune(datetime.now().timestamp() - keep_days * 86400)

    def _prune(self, cutoff: float) -> int:
        kept, dropped = [], 0
        for event in self._persisted():
            try:
                fresh = datetime.fromisoformat(str(event.get("at"))).timestamp() >= cutoff
            except ValueError:
                fresh = True
            if fresh:
                kept.append(json.dumps(event, ensure_ascii=False, default=str))
            else:
                dropped += 1
        if not dropped:
            return 0
        tmp = self._log_path.with_suffix(".tmp")
        try:
            tmp.write_text("".join(line + "\n" for line in kept), encoding="utf-8")
            os.replace(tmp, self._log_path)
            self._log_path.with_suffix(".old.jsonl").unlink(missing_ok=True)
        except OSError:
            return 0
        return dropped
