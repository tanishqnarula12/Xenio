"""Hermes' memory files, changed by the user from Mission Control - the way Hermes itself changes them.

Hermes keeps memory in ``<HERMES_HOME>/memories/MEMORY.md`` (XENIO's notes) and ``USER.md`` (about the user): short
entries separated by a line holding ``§``. Its memory tool edits them under an exclusive lock on a separate
``<file>.lock`` file, re-reads the file under that lock, and replaces it atomically (tools/memory_tool_store.py in
Hermes 0.22). This module does exactly the same, so an edit here and a save by Hermes can never lose each other's
entries, and Hermes keeps reading the file as its own (no "drift").

Only the user's own explicit actions call this (correct, delete, add in Mission Control); the model still saves
through Hermes' tool, under the guard's memory policy. Every change goes through the same policy: secrets are refused
(even when the user typed them), entries stay short, and the store's size limit holds. A running Hermes session keeps
the memory it started with; the guard plugin tells it, on the next turn, which entries the user removed or corrected.
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
from pathlib import Path

from app.config import default_hermes_home

try:
    import fcntl
except ImportError:  # Windows: Hermes uses msvcrt there; XENIO's memory editing is macOS/Linux only
    fcntl = None  # type: ignore[assignment]

ENTRY_DELIMITER = "\n§\n"
STORES = {"memory": "MEMORY.md", "user": "USER.md"}
DEFAULT_LIMITS = {"memory": 2200, "user": 1375}  # Hermes' memory.memory_char_limit / user_char_limit defaults


class MemoryEditError(Exception):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def parse(raw: str) -> list[str]:
    """Hermes' parse: split on the full delimiter, strip, drop empties, first occurrence wins."""
    return list(dict.fromkeys(e for e in (x.strip() for x in raw.split(ENTRY_DELIMITER)) if e))


def configured_limits(hermes_home: Path) -> dict[str, int]:
    """The char limits set in Hermes' config.yaml (memory.memory_char_limit / user_char_limit), else Hermes' defaults."""
    limits = dict(DEFAULT_LIMITS)
    try:
        text = (hermes_home / "config.yaml").read_text(encoding="utf-8")
    except OSError:
        return limits
    block = re.search(r"(?m)^memory:\s*\n((?:[ \t]+.*\n?)*)", text)
    for key, store in (("memory_char_limit", "memory"), ("user_char_limit", "user")):
        hit = re.search(rf"(?m)^\s+{key}:\s*(\d+)\s*$", block.group(1)) if block else None
        if hit:
            limits[store] = int(hit.group(1))
    return limits


class MemoryFiles:
    def __init__(self, hermes_home: Path | None = None):
        self.home = hermes_home or default_hermes_home()

    @property
    def folder(self) -> Path:
        return self.home / "memories"

    def path(self, store: str) -> Path:
        if store not in STORES:
            raise MemoryEditError("the store must be 'memory' or 'user'")
        return self.folder / STORES[store]

    def entries(self, store: str) -> list[str]:
        try:
            return parse(self.path(store).read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return []
        except (OSError, UnicodeDecodeError) as exc:
            raise MemoryEditError(f"Hermes' memory file could not be read ({exc})", 503) from None

    def usage(self) -> dict[str, dict]:
        limits = configured_limits(self.home)
        out = {}
        for store in STORES:
            try:
                used = len(ENTRY_DELIMITER.join(self.entries(store)))
            except MemoryEditError:
                used = None
            out[store] = {"used": used, "limit": limits[store]}
        return out

    # ---- changes ------------------------------------------------------------------------------------------------

    @contextlib.contextmanager
    def _locked(self, path: Path):
        """Hermes' lock: an exclusive flock on ``<file>.lock`` (owner-only), so the memory file can be replaced."""
        path.parent.mkdir(parents=True, exist_ok=True)
        if fcntl is None:
            raise MemoryEditError("editing memory needs macOS or Linux", 501)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path.with_suffix(path.suffix + ".lock"), flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _write(self, path: Path, entries: list[str]) -> None:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".mem_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(ENTRY_DELIMITER.join(entries))
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def _change(self, store: str, change) -> dict:
        path = self.path(store)
        with self._locked(path):
            try:
                raw = path.read_text(encoding="utf-8-sig") if path.exists() else ""
            except (OSError, UnicodeDecodeError) as exc:
                raise MemoryEditError(f"Hermes' memory file could not be read, so it was not changed ({exc})", 503) from None
            before = parse(raw)
            after, result = change(before)
            limit = configured_limits(self.home)[store]
            if len(ENTRY_DELIMITER.join(after)) > max(limit, len(ENTRY_DELIMITER.join(before))):
                raise MemoryEditError(f"that would go over Hermes' limit for this list ({limit} characters): shorten it "
                                      "or delete an entry first")
            self._write(path, after)
        return result

    @staticmethod
    def _check(text: str, others: list[str]) -> str:
        from app.safety.policy import MEMORY_ENTRY_MAX, assess_memory_write

        clean = " ".join((text or "").split())
        if not clean:
            raise MemoryEditError("a memory entry cannot be empty")
        if len(clean) > MEMORY_ENTRY_MAX:
            raise MemoryEditError(f"memory holds short facts: at most {MEMORY_ENTRY_MAX} characters")
        verdict, why = assess_memory_write({"action": "add", "content": clean}, others, True)
        if verdict == "SECRET":
            raise MemoryEditError("that looks like a password, key or other secret: XENIO never stores those, not "
                                  "even when you ask")
        if verdict == "DUPLICATE":
            raise MemoryEditError("memory already holds an entry saying the same thing")
        if verdict != "HIGH_VALUE":
            raise MemoryEditError(f"not saved: {why}")
        return clean

    @staticmethod
    def _find(entries: list[str], text: str) -> int:
        if text in entries:
            return entries.index(text)
        raise MemoryEditError("that entry is not in memory any more (Hermes may have changed it): reload the page", 409)

    def add(self, store: str, text: str) -> dict:
        def change(entries):
            clean = self._check(text, entries)
            return entries + [clean], {"store": store, "action": "added", "text": clean}
        return self._change(store, change)

    def replace(self, store: str, old: str, new: str) -> dict:
        def change(entries):
            index = self._find(entries, old)
            clean = self._check(new, entries[:index] + entries[index + 1:])
            return entries[:index] + [clean] + entries[index + 1:], {"store": store, "action": "corrected", "text": clean}
        return self._change(store, change)

    def remove(self, store: str, old: str) -> dict:
        def change(entries):
            index = self._find(entries, old)
            return entries[:index] + entries[index + 1:], {"store": store, "action": "deleted"}
        return self._change(store, change)
