"""The tool-call audit: every tool call XENIO's guard saw inside Hermes, with what became of it.

The guard plugin (hermes_plugin/xenio-guard) writes one JSON line per step to ``<data dir>/logs/xenio-guard.log``:
TOOL_REQUESTED, PERMISSION_CHECK (level, blocked, summary), CONFIRMATION_REQUESTED / CONFIRMATION_RECEIVED (the
user's answer), and for XENIO's own tools TOOL_EXECUTION_STARTED / COMPLETED / FAILED. Here those steps become one
record per call, so Mission Control can tell requested, blocked, asked, approved, denied, completed and failed calls
apart. Hermes' own tools (terminal, files, browser) report no result to the guard: their last state is "allowed" -
never shown as completed, because nothing proves it.

Read-only, the newest part of the log only (the log is append-only and can be large). Summaries are redacted again
here, and text the user typed (quoted «…» by the policy) never leaves the log.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable

TAIL_BYTES = 768_000
FINAL = {"blocked", "denied", "expired", "completed", "failed"}


def _hermes_name(tool: str) -> str:
    """XENIO's tools log their execution under their own name ("desktop.gui"), Hermes asks for "xenio_gui"."""
    return "xenio_" + tool.split(".", 1)[1] if "." in tool else tool


def _read_tail(path: Path, size: int = TAIL_BYTES) -> list[str]:
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            end = fh.tell()
            fh.seek(max(0, end - size))
            data = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = data.splitlines()
    return lines[1:] if end > size else lines  # the first line of a tail is usually cut


def tool_calls(log_path: Path, redact: Callable[[str], str] = lambda t: t, limit: int = 200) -> list[dict]:
    """Tool calls, newest first: ``{"tool", "at", "status", "level", "summary", "detail"}`` (+ ``ms`` when measured)."""
    calls: list[dict] = []
    open_calls: dict[str, list[dict]] = {}  # tool -> calls still waiting for their next step, oldest first

    def latest(tool: str, *, without: str | None = None) -> dict | None:
        for call in reversed(open_calls.get(tool, [])):
            if without is None or not call.get(without):
                return call
        return None

    def close(call: dict, status: str) -> None:
        call["status"] = status
        if status in FINAL:
            open_calls[call["tool"]] = [c for c in open_calls.get(call["tool"], []) if c is not call]

    for line in _read_tail(log_path):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        kind, at = entry.get("event"), str(entry.get("time") or "")
        tool = _hermes_name(str(entry.get("tool") or entry.get("rule") or ""))
        if not tool:
            continue
        if kind == "TOOL_REQUESTED":
            call = {"tool": tool, "at": at, "status": "requested", "level": None, "summary": "", "detail": ""}
            calls.append(call)
            open_calls.setdefault(tool, []).append(call)
        elif kind == "PERMISSION_CHECK" and (call := latest(tool, without="level")):
            call["level"] = entry.get("level")
            call["summary"] = str(entry.get("summary") or "")
            if entry.get("error"):
                close(call, "failed")
                call["detail"] = "the safety check itself failed, so the call was refused"
            elif entry.get("blocked"):
                close(call, "blocked")
            else:
                call["status"] = "allowed"
        elif kind == "ESCALATED_AFTER_INJECTION" and (call := latest(tool)):
            call["detail"] = "asked first: outside content in this turn tried to give instructions"
        elif kind == "CONFIRMATION_REQUESTED" and (call := latest(tool)):
            call["status"] = "asked"
        elif kind == "CONFIRMATION_RECEIVED" and (call := next((c for c in reversed(open_calls.get(tool, []))
                                                               if c["status"] == "asked"), None)):
            choice = str(entry.get("choice") or "")
            if choice in ("once", "session", "always"):
                call["status"] = "approved"
            else:
                close(call, "expired" if choice == "timeout" else "denied")
        elif kind == "TOOL_EXECUTION_STARTED" and (call := latest(tool)):
            call["status"] = "running"
        elif kind in ("TOOL_EXECUTION_COMPLETED", "TOOL_EXECUTION_FAILED") and (call := latest(tool)):
            close(call, "completed" if kind == "TOOL_EXECUTION_COMPLETED" else "failed")
            if isinstance(entry.get("ms"), int):
                call["ms"] = entry["ms"]
            if entry.get("error"):
                call["detail"] = str(entry["error"])[:200]

    out = []
    for call in reversed(calls[-limit:]):
        clean = {k: (re.sub(r"«[^»]*»", "«…»", redact(v)) if isinstance(v, str) else v) for k, v in call.items()}
        clean["summary"] = clean["summary"][:200]
        out.append(clean)
    return out
