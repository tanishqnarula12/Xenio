"""Thin facade over Hermes' native memory. XENIO keeps no memory database of its own.

Hermes stores curated memory in two files under ``HERMES_HOME/memories``: ``MEMORY.md``
(agent notes) and ``USER.md`` (user profile), entries separated by a line holding ``§``.

* ``recall()`` reads those files directly — read-only, no LLM call.
* ``remember()`` asks Hermes to save the fact with its own memory tool, so Hermes keeps
  curating and size-limiting it. XENIO never writes Hermes' memory files.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from app.config import default_hermes_home
from app.hermes.client import HermesClient, HermesError

log = logging.getLogger("xenio.memory")

ENTRY_DELIMITER = "\n§\n"
STORES = {"memory": "MEMORY.md", "user": "USER.md"}


@dataclass
class MemoryEntry:
    store: str  # "memory" | "user"
    text: str


class MemoryManager:
    def __init__(self, hermes: HermesClient, hermes_home: Path | None = None):
        self._hermes = hermes
        self._home = hermes_home or default_hermes_home()

    @property
    def memory_dir(self) -> Path:
        return self._home / "memories"

    async def recall(self, query: str = "", limit: int = 20) -> list[MemoryEntry]:
        words = [w for w in query.lower().split() if w]
        entries: list[MemoryEntry] = []
        for store, filename in STORES.items():
            path = self.memory_dir / filename
            try:
                raw = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                continue
            except OSError:
                log.warning("Could not read Hermes memory file", extra={"fields": {"file": filename}})
                continue
            for chunk in raw.replace("\r\n", "\n").split(ENTRY_DELIMITER):
                text = chunk.strip()
                if text and all(w in text.lower() for w in words):
                    entries.append(MemoryEntry(store, text))
        return entries[:limit]

    async def remember(self, fact: str, about_user: bool = False) -> bool:
        """Ask Hermes to store ``fact``. True only if Hermes reports a completed memory tool call."""
        target = "user profile (USER.md)" if about_user else "memory (MEMORY.md)"
        try:
            reply = await self._hermes.send_message(
                f"Use your memory tool to save this to your {target}, then reply 'saved': {fact}"
            )
        except HermesError:
            log.warning("Hermes unavailable; memory not saved")
            return False
        saved = any(
            "memory" in (call.title or "").lower() and call.status == "completed" for call in reply.tool_calls
        )
        if not saved:
            log.warning("Hermes did not confirm a memory write")
        return saved
