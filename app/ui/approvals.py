"""Approvals in the UI: XENIO's PermissionGate asks, the browser answers, the gate decides.

``WebConfirmer`` is a PermissionGate confirmer like the console's and the voice's. The gate has already decided that
the action needs a human (blocked actions never get here, safe ones never ask) and keeps its own timeout. Here:

* every question becomes an approval record (what, who asked, risk, when, until when) and an event;
* only an explicit "approve" counts; HIGH_RISK needs the word "confirm" typed, checked HERE, not in the browser;
* a decision for an unknown, expired or already-answered approval is refused;
* silence is never consent: when the gate's time runs out, the approval ends as "expired" (not approved).

The answer arrives from an HTTP thread; it is handed to XENIO's loop, which owns the waiting gate.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta
from typing import Callable

from app.safety.permissions import NO_CLEAR_ANSWER, PermissionRequest
from app.safety.policy import RiskLevel


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
                  "status": "pending"}
        future: asyncio.Future = self._loop.create_future()
        self._pending[record["id"]] = (record, future)
        self._publish("approval.requested", dict(record))
        self._on_change(len(self._pending))
        outcome = "expired"
        try:
            answer = await future
            outcome = "approved" if answer is True else "denied"
            return True if answer is True else False
        except asyncio.CancelledError:  # the gate's own timeout (or XENIO stopping): never a yes
            outcome = "expired"
            raise
        finally:
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
        if decision not in ("approve", "deny"):
            raise ApprovalError("the decision must be 'approve' or 'deny'", 422)
        if decision == "approve" and record["strict"] and confirm_text.strip().lower() != "confirm":
            raise ApprovalError("this is a high-risk action: type the word confirm to approve it", 422)
        if self._loop is None or future.done():
            raise ApprovalError("this approval is no longer open", 409)
        self._loop.call_soon_threadsafe(lambda: future.done() or future.set_result(decision == "approve"))
        return {"id": record["id"], "decision": decision}

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
