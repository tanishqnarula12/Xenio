"""What XENIO is doing right now, from real signals only (the orb's backend half).

LISTENING and SPEAKING come from whoever really holds the microphone or the speaker: XENIO's talk engine (``voice``,
see app/ui/voice.py), or the browser when it records or plays itself. Everything else is decided here:

    DISCONNECTED / CONNECTING / INITIALIZING   the Hermes connection
    PAUSED                                     the user paused XENIO
    WAITING_APPROVAL                           a confirmation is open in the PermissionGate
    SPEAKING / LISTENING / PROCESSING          the talk engine is playing a reply / recording / transcribing
    PROCESSING -> THINKING / SEARCHING /       a chat turn: started, then by the tool Hermes is really running
      EXECUTING / PLANNING
    ERROR                                      the last turn failed (shown until the next request or a dismissal)
    COMPLETED                                  the last turn finished well (a short moment, then idle)
    PLANNING / ORCHESTRATING / EXECUTING       nothing in the foreground, but the V6 team is really working
    IDLE

Foreground and background are reported separately (``background``), so "speaking while two agents work" is shown
without contradiction.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

STATES = ("DISCONNECTED", "CONNECTING", "INITIALIZING", "IDLE", "LISTENING", "PROCESSING", "THINKING", "SEARCHING",
          "PLANNING", "ORCHESTRATING", "EXECUTING", "SPEAKING", "WAITING_APPROVAL", "PAUSED", "COMPLETED", "ERROR")
COMPLETED_SECONDS = 2.5

# tools that look things up (files, the web, mail, the screen) -> SEARCHING; everything else that acts -> EXECUTING
_LOOKUP_TOOLS = {"web_search", "web_extract", "read_file", "search_files", "session_search", "skills_list", "skill_view",
                 "xenio_list_directory", "xenio_inspect_path", "xenio_email_search", "xenio_email_read",
                 "xenio_calendar_events", "xenio_github", "xenio_daily_briefing", "xenio_project", "xenio_tasks",
                 "xenio_notifications", "xenio_integrations", "xenio_find_ui", "xenio_list_apps", "xenio_look_at_screen",
                 "vision_analyze", "browser_snapshot", "browser_vision", "todo_list"}
_LOOKUP_KINDS = {"read", "search", "fetch"}


def tool_name(title: str) -> str:
    return (title or "").split(":", 1)[0].strip()


def phase_for_tool(title: str, kind: str = "") -> str:
    name = tool_name(title)
    if name == "xenio_engineering":
        return "PLANNING"
    if name in _LOOKUP_TOOLS or (kind or "").lower() in _LOOKUP_KINDS:
        return "SEARCHING"
    if (kind or "").lower() == "think":
        return "THINKING"
    return "EXECUTING"


@dataclass
class AssistantState:
    connection: str = "connecting"  # connecting | initializing | connected | disconnected
    paused: bool = False
    approvals: int = 0
    turn: str | None = None  # the running chat turn's id
    phase: str = "PROCESSING"
    tool: str = ""  # the tool the running turn is using (its name only)
    error: str = ""
    completed_at: float = 0.0
    background: dict = field(default_factory=lambda: {"state": None, "runs": 0, "agents": 0})
    voice: str = ""  # the talk engine: listening | transcribing | speaking ("" = not using the mic or speaker)
    voice_since: float = 0.0  # when that phase began (epoch ms): the orb measures how fast it appeared
    clock: object = time.monotonic

    def derive(self) -> str:
        if self.connection == "disconnected":
            return "DISCONNECTED"
        if self.connection == "connecting":
            return "CONNECTING"
        if self.connection == "initializing":
            return "INITIALIZING"
        if self.paused:
            return "PAUSED"
        if self.approvals > 0:
            return "WAITING_APPROVAL"
        if self.voice == "speaking":
            return "SPEAKING"  # also while Hermes is still writing: the reply is spoken sentence by sentence
        if self.voice == "listening":
            return "LISTENING"
        if self.voice == "transcribing" and self.turn is None:
            return "PROCESSING"
        if self.turn is not None:
            return self.phase
        if self.error:
            return "ERROR"
        if self.completed_at and self.clock() - self.completed_at < COMPLETED_SECONDS:
            return "COMPLETED"
        return self.background.get("state") or "IDLE"

    def snapshot(self) -> dict:
        return {"state": self.derive(), "connection": self.connection, "paused": self.paused,
                "approvals": self.approvals, "turn": self.turn, "tool": self.tool or None,
                "error": self.error or None, "background": dict(self.background), "voice": self.voice or None,
                "voice_since": int(self.voice_since) or None}

    # ---- transitions (each returns nothing; the controller publishes the new snapshot) -------------------------

    def turn_started(self, turn_id: str) -> None:
        self.turn, self.phase, self.tool, self.error, self.completed_at = turn_id, "PROCESSING", "", "", 0.0

    def first_output(self) -> None:
        if self.turn is not None and self.phase == "PROCESSING":
            self.phase = "THINKING"

    def tool_changed(self, title: str, kind: str, status: str) -> None:
        if self.turn is None:
            return
        if status in ("pending", "in_progress"):
            self.phase, self.tool = phase_for_tool(title, kind), tool_name(title)
        elif status in ("completed", "failed") and tool_name(title) == self.tool:
            self.phase, self.tool = "THINKING", ""

    def turn_finished(self, ok: bool, error: str = "", cancelled: bool = False) -> None:
        self.turn, self.tool = None, ""
        if cancelled:
            self.error, self.completed_at = "", 0.0
        elif ok:
            self.error, self.completed_at = "", self.clock()
        else:
            self.error, self.completed_at = error or "the request failed", 0.0

    def dismiss_error(self) -> None:
        self.error = ""

    def set_background(self, runs: list[dict]) -> None:
        """From the V6 service: planning runs, and how many agents are really working."""
        active = [r for r in runs if r.get("status") in ("planning", "running", "verifying", "reviewing")]
        agents = sum(len(r.get("running") or []) for r in active)
        state = None
        if agents > 1:
            state = "ORCHESTRATING"
        elif any(r.get("status") == "planning" for r in active):
            state = "PLANNING"  # the planner's turn is the one agent working
        elif agents == 1 or any(r.get("status") in ("verifying", "reviewing") for r in active):
            state = "EXECUTING"
        self.background = {"state": state, "runs": len(active), "agents": agents}
