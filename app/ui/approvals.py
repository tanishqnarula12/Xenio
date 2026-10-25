"""Approvals in the UI: XENIO's PermissionGate asks, the browser answers, the gate decides.

``WebConfirmer`` is a PermissionGate confirmer like the console's and the voice's. The gate has already decided that
the action needs a human (blocked actions never get here, safe ones never ask) and keeps its own timeout. Here:

* every question becomes an approval record (what, why it is asked, who asked - the assistant or which engineering
  agent -, on what, the risk, what a yes allows and for how long, when, until when) and an event;
* only an explicit "approve" counts; HIGH_RISK needs the word "confirm" typed, checked HERE, not in the browser;
* "approve_task" (allow the rest of this task in XENIO's workspace) is accepted only for a request the policy found
  confined to the workspace - checked HERE; the gate then opens a time-limited task grant (app/safety/permissions.py);
* the question can also be asked out loud (the talk engine); the first answer, clicked or said, decides;
* a decision for an unknown, expired or already-answered approval is refused;
* silence is never consent: when the gate's time runs out, the approval ends as "expired" (not approved).

The answer arrives from an HTTP thread; it is handed to XENIO's loop, which owns the waiting gate.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta
from typing import Awaitable, Callable

from app.safety.permissions import APPROVE_TASK, NO_CLEAR_ANSWER, PermissionRequest
from app.safety.policy import RiskLevel


# Why XENIO asks, by kind of action (the first matching word in the policy's action name wins). Plain words for the
# approval card: the policy decided THAT it asks; this only explains it.
_WHY = (
    (("account_or_payment",), "it may act on an account or a payment"),
    (("credentials", "secret", "password", "token", "_auth", "security_settings", "reveal_secret", "security_off"),
     "it touches passwords, keys or security settings"),
    (("delete", "empty_trash", "terminal.empty", "robocopy_mirror"), "it permanently deletes something"),
    (("force_push", "push", "publish", "pull_request", "pr_merge", "deploy", "gh_api_write"),
     "it publishes or changes something outside this Mac"),
    (("send", "submit", "upload"), "it sends something on your behalf"),
    (("install", "download_program", "pipe_to_shell", "remote_script", "untrusted_program"),
     "it installs or runs software that comes from outside"),
    (("download",), "it downloads a file to your Mac"),
    (("engineering.apply",), "it writes the engineering team's changes into your own files"),
    (("engineering", "xenio_engineering"), "it starts or changes the engineering team's work"),
    (("calendar_create", "schedule", "reminder", "scheduled_task", "cronjob"), "it sets up something that acts later"),
    (("look_at_screen", "vision.", "screen.read"), "it looks at your screen"),
    (("desktop.", "screen.", "browser.click_xy", "xenio_gui", "xenio_window", "xenio_open_app", "xenio_find_ui"),
     "it uses your Mac's apps, mouse or keyboard"),
    (("browser",), "it acts on a web page"),
    (("memory", "skill_manage"), "it changes what XENIO remembers or one of its skills"),
    (("filesystem.", "xenio_move_path", "xenio_create_directory", "write_file", "patch", "terminal.move",
      "terminal.copy", "terminal.mkdir", "terminal.touch", "terminal.sed", "terminal.permissions"), "it changes files"),
    (("code.", "execute_code", "terminal.python", "terminal.node_eval", "terminal.eval", "terminal.script"),
     "it runs code on your Mac"),
    (("terminal", "process."), "it runs a command on your Mac"),
)


def why_asked(action: str, strict: bool) -> str:
    reason = next((why for words, why in _WHY if any(w in action for w in words)), "XENIO's rules ask before this kind of action")
    return f"High risk: {reason}." if strict else f"Asked because {reason}."


def targets_of(request: PermissionRequest) -> list[str]:
    """What the action reaches: files, the command, the tool, destructive targets (at most five, shortened)."""
    details = request.details or {}
    out = [f"file: {p}" for p in details.get("paths") or []]
    if details.get("command"):
        out.append(f"command: {details['command']}")
    for scope in request.scopes or ():
        kind, _, target = str(scope).partition(":")
        out.append(f"{kind}: {target}" if target else kind)
    if details.get("tool") and not out:
        out.append(f"tool: {details['tool']}")
    return [t if len(t) <= 200 else t[:199] + "…" for t in dict.fromkeys(out)][:5]


def requested_by(request: PermissionRequest) -> str:
    team = (request.details or {}).get("engineering")
    if isinstance(team, dict) and team.get("role"):
        return f"the {team['role']} agent, engineering task {team.get('task')} (run {team.get('run')})"
    return {"hermes": "XENIO, for your request", "xenio": "XENIO itself"}.get(request.source, request.source)


def scope_note(confined: bool, strict: bool) -> str:
    if strict:
        return "A yes allows only this action, after you type confirm."
    if confined:
        return ("Allow once: only this action. Allow for this task: XENIO may go on inside its own workspace for 30 "
                "minutes without asking (never a high-risk action, never outside the workspace); you can end it any time.")
    return "A yes allows only this action; XENIO asks again next time."


class ApprovalError(Exception):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


class WebConfirmer:
    def __init__(self, publish: Callable[[str, dict], None], timeout: float = 120.0,
                 on_change: Callable[[int], None] | None = None):
        self._publish = publish
        self._timeout = timeout
        self._on_change = on_change or (lambda count: None)
        self._pending: dict[str, tuple[dict, asyncio.Future]] = {}
        self._history: list[dict] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        # optional: ask the question out loud too (the talk engine), and announce it on the desktop. Whichever answer
        # comes first - clicked or said - decides; both go through the same checks.
        self.ask_aloud: Callable[[PermissionRequest, bool, str], Awaitable[None]] | None = None
        self.on_request: Callable[[dict], None] | None = None

    def pending(self) -> list[dict]:
        return [dict(record) for record, _ in self._pending.values()]

    def history(self, limit: int = 50) -> list[dict]:
        return [dict(r) for r in self._history[-limit:]][::-1]

    async def confirm(self, request: PermissionRequest, *, strict: bool) -> bool:
        self._loop = asyncio.get_running_loop()
        now = datetime.now()
        record = {"id": uuid.uuid4().hex[:12], "action": request.action, "description": request.description,
                  "risk": request.risk.name, "strict": strict, "source": request.source,
                  "requested_at": now.isoformat(timespec="seconds"),
                  "expires_at": (now + timedelta(seconds=self._timeout)).isoformat(timespec="seconds"),
                  "status": "pending",
                  # "allow for this task" is offered only when everything it writes stays in XENIO's workspace
                  "confined": bool(getattr(request, "confined", False)) and not strict}
        record |= {"why": why_asked(request.action, strict), "targets": targets_of(request),
                   "requested_by": requested_by(request), "scope": scope_note(record["confined"], strict)}
        future: asyncio.Future = self._loop.create_future()
        self._pending[record["id"]] = (record, future)
        self._publish("approval.requested", dict(record))
        self._on_change(len(self._pending))
        if self.on_request is not None:
            try:
                self.on_request(dict(record))
            except Exception:  # noqa: BLE001 - an announcement must never block the question
                pass
        aloud = asyncio.ensure_future(self.ask_aloud(request, strict, record["id"])) if self.ask_aloud else None
        outcome = "expired"
        try:
            answer = await future
            outcome = ("approved for the task" if answer is APPROVE_TASK else "approved" if answer is True
                       else "denied")
            return answer if answer is APPROVE_TASK else answer is True
        except asyncio.CancelledError:  # the gate's own timeout (or XENIO stopping): never a yes
            outcome = "expired"
            raise
        finally:
            if aloud is not None and not aloud.done():
                aloud.cancel()  # answered on screen (or expired): stop asking out loud
            self._pending.pop(record["id"], None)
            record = {**record, "status": outcome, "decided_at": datetime.now().isoformat(timespec="seconds")}
            self._history = (self._history + [record])[-100:]
            self._publish("approval.settled", record)
            self._on_change(len(self._pending))

    def decide(self, approval_id: str, decision: str, confirm_text: str = "") -> dict:
        """Called from an HTTP thread. Raises ApprovalError for anything that is not a valid, current decision."""
        entry = self._pending.get(str(approval_id))
        if entry is None:
            raise ApprovalError("this approval is no longer open (answered, expired or unknown)", 404)
        record, future = entry
        if decision not in ("approve", "deny", "approve_task"):
            raise ApprovalError("the decision must be 'approve', 'approve_task' or 'deny'", 422)
        if decision == "approve_task" and not record.get("confined"):
            raise ApprovalError("this action reaches outside XENIO's workspace, so it can only be allowed once", 422)
        if decision == "approve" and record["strict"] and confirm_text.strip().lower() != "confirm":
            raise ApprovalError("this is a high-risk action: type the word confirm to approve it", 422)
        if self._loop is None or future.done():
            raise ApprovalError("this approval is no longer open", 409)
        value = APPROVE_TASK if decision == "approve_task" else decision == "approve"
        self._loop.call_soon_threadsafe(lambda: future.done() or future.set_result(value))
        return {"id": record["id"], "decision": decision}

    def answer(self, approval_id: str, value) -> bool:
        """An answer heard out loud (on XENIO's loop): True, False or APPROVE_TASK. False if already answered."""
        entry = self._pending.get(str(approval_id))
        if entry is None or entry[1].done():
            return False
        record, future = entry
        if value is APPROVE_TASK and not record.get("confined"):
            value = True  # outside the workspace: a "yes to all" counts as one yes
        future.set_result(value)
        return True

    def cancel_all(self) -> None:
        """XENIO is stopping: every open question ends unanswered (= not approved)."""
        for _record, future in list(self._pending.values()):
            if self._loop is not None and not future.done():
                self._loop.call_soon_threadsafe(lambda f=future: f.done() or f.set_result(NO_CLEAR_ANSWER))


def risk_rank(level: str) -> int:
    try:
        return int(RiskLevel[level])
    except KeyError:
        return 0
