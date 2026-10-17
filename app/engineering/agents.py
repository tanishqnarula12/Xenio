"""The engineering team's agents (V6): Hermes ACP sessions on a dedicated Hermes process.

Each task runs as one Hermes agent in its own ACP session: ``cwd`` = the task's worktree, the role's model set with
``session/set_model`` (only models the cost policy allows), edits accepted inside the worktree (``accept_edits``; the
xenio-guard plugin has already checked the task's file scope before any edit). Hermes keeps all reasoning, tools,
skills and its free fallback chain; Hermes runs at most 4 agent turns at once per process.

Before a session gets its first prompt XENIO registers its scope (role, tools, terminal mode, worktree, allowed
paths) in ``<data dir>/engineering/sessions.json``, which the guard reads for every tool call of that session. The
entry is removed when the task ends. Permission requests from a session reach the user through XENIO's
PermissionGate, labelled with the task and role that asked.

This process is separate from the user's chat with XENIO, so the chat stays responsive during a run.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app import __version__
from app.config import HermesSettings
from app.hermes.adapter import ACP_PROTOCOL_VERSION, _CLIENT_CAPABILITIES, _pick_option, _text_of, _Turn, guard_environment
from app.hermes.bridge import AcpConnection
from app.hermes.client import HermesError, HermesPermissionRequest, HermesTimeout, HermesUnavailable, ToolActivity
from app.logging_setup import event

log = logging.getLogger("xenio.engineering")

ACCEPT_EDITS = "accept_edits"
# Hermes' own wording when every model in the chain failed (seen live: "OpenRouter sent back an empty or broken reply
# 3 times"): the turn "succeeds" with this text, so it must be recognised as a provider failure, not an answer.
_PROVIDER_FAILURE = ("sent back an empty or broken reply", "Your request was not processed",
                     "upstream provider overloaded", "is temporarily rate-limited upstream", "No available provider")


class ProviderUnavailable(HermesError):
    pass


@dataclass
class AgentTurn:
    text: str
    stop_reason: str
    tools: list[ToolActivity] = field(default_factory=list)
    usage: dict | None = None
    seconds: float = 0.0
    model: str = ""

    @property
    def cancelled(self) -> bool:
        return self.stop_reason == "cancelled"


class SessionRegistry:
    """XENIO's record of which ACP session is which engineering task, read by xenio-guard inside Hermes."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(self.path)

    def add(self, session_id: str, scope: dict) -> None:
        with self._lock:
            data = self._read()
            data[session_id] = scope
            self._write(data)

    def remove(self, session_id: str) -> None:
        with self._lock:
            data = self._read()
            if data.pop(session_id, None) is not None:
                self._write(data)

    def clear_run(self, run_id: str) -> None:
        with self._lock:
            data = {k: v for k, v in self._read().items() if v.get("run") != run_id}
            self._write(data)

    def get(self, session_id: str) -> dict | None:
        return self._read().get(session_id)


PermissionAsker = Callable[[str, HermesPermissionRequest], Awaitable[bool]]


class AgentSessions:
    """One Hermes process, many sessions. ``ask(session_id, request)`` answers Hermes' permission requests."""

    def __init__(self, settings: HermesSettings, data_dir: Path, ask: PermissionAsker | None = None,
                 connection_factory=AcpConnection, turn_timeout: float = 1800.0):
        self._settings = settings
        self._data_dir = data_dir
        self._ask = ask
        self._factory = connection_factory
        self.turn_timeout = turn_timeout
        self._conn: AcpConnection | None = None
        self._turns: dict[str, _Turn] = {}
        self._start_lock = asyncio.Lock()
        self.models: dict[str, str] = {}  # session -> model it was set to ("" = Hermes' main model)
        self.agent_info: dict[str, Any] = {}

    @property
    def connected(self) -> bool:
        return self._conn is not None and self._conn.alive

    async def start(self) -> None:
        async with self._start_lock:
            if self.connected:
                return
            cwd = self._settings.workspace
            cwd.mkdir(parents=True, exist_ok=True)
            conn = self._factory(self._settings.command, cwd=cwd, env=guard_environment(self._settings, self._data_dir),
                                 on_notification=self._on_notification, on_request=self._on_request)
            event(log, "Starting Hermes for the engineering team", command=" ".join(self._settings.command))
            await conn.start()
            try:
                init = await conn.request("initialize", {"protocolVersion": ACP_PROTOCOL_VERSION,
                                                         "clientCapabilities": _CLIENT_CAPABILITIES,
                                                         "clientInfo": {"name": "xenio-engineering", "version": __version__}},
                                          timeout=self._settings.startup_timeout)
            except HermesError as exc:
                detail = conn.stderr_tail(3)
                await conn.close(timeout=5)
                raise HermesUnavailable(f"{exc}" + (f" | {detail}" if detail else "")) from exc
            self.agent_info = dict((init or {}).get("agentInfo") or {}) if isinstance(init, dict) else {}
            guarded, why = self._guard_check()
            if guarded is False:  # sessions would run in a Hermes process without XENIO's policy: never
                await conn.close(timeout=5)
                raise HermesUnavailable(f"refusing to run engineering agents: {why}")
            self._conn = conn

    def _guard_check(self) -> tuple[bool | None, str]:
        from app.hermes.guard import guard_live

        return guard_live(self._data_dir)

    async def open(self, cwd: Path, model: str = "", provider: str = "") -> str:
        """A new session working in ``cwd``. ``model`` "" = Hermes' main model."""
        await self.start()
        assert self._conn is not None
        result = await self._conn.request("session/new", {"cwd": str(cwd), "mcpServers": []},
                                          timeout=self._settings.startup_timeout)
        session_id = result.get("sessionId") if isinstance(result, dict) else None
        if not session_id:
            raise HermesError("Hermes did not return a session id")
        try:
            await self._conn.request("session/set_mode", {"sessionId": session_id, "modeId": ACCEPT_EDITS},
                                     timeout=30)
        except HermesError as exc:  # older Hermes: every in-scope edit then asks through the gate instead
            log.warning("Could not set accept_edits mode", extra={"fields": {"error": str(exc)}})
        if model:
            model_id = f"{provider}:{model}" if provider and ":" not in model.split("/")[0] else model
            try:
                await self._conn.request("session/set_model", {"sessionId": session_id, "modelId": model_id},
                                         timeout=60)
            except HermesError as exc:
                raise ProviderUnavailable(f"Hermes could not switch this session to {model}: {exc}") from exc
        self.models[session_id] = model
        return session_id

    async def prompt(self, session_id: str, text: str, on_tool: Callable[[ToolActivity], None] | None = None,
                     timeout: float | None = None) -> AgentTurn:
        if not self.connected:
            raise HermesUnavailable("the engineering Hermes process is not running")
        assert self._conn is not None
        turn = _Turn(None, on_tool=on_tool)
        self._turns[session_id] = turn
        started = time.monotonic()
        try:
            result = await self._conn.request("session/prompt", {"sessionId": session_id,
                                                                 "prompt": [{"type": "text", "text": text}]},
                                              timeout=timeout or self.turn_timeout)
        except HermesTimeout:
            await self.cancel(session_id)
            raise
        finally:
            self._turns.pop(session_id, None)
        stop = str((result or {}).get("stopReason") or "end_turn") if isinstance(result, dict) else "end_turn"
        out = AgentTurn(turn.text, stop, list(turn.tools.values()),
                        (result or {}).get("usage") if isinstance(result, dict) else None,
                        round(time.monotonic() - started, 1), self.models.get(session_id, ""))
        if stop != "cancelled" and not out.tools and len(out.text) < 800 and \
                any(s.lower() in out.text.lower() for s in _PROVIDER_FAILURE):
            raise ProviderUnavailable(" ".join(out.text.split())[:300])
        return out

    async def cancel(self, session_id: str) -> bool:
        """Ask Hermes to stop this session's turn. True if a turn was running and the request was sent."""
        if self._conn is None or not self._conn.alive or session_id not in self._turns:
            return False
        try:
            await self._conn.notify("session/cancel", {"sessionId": session_id})
            return True
        except HermesUnavailable:
            return False

    def running(self) -> list[str]:
        return list(self._turns)

    async def close(self) -> None:
        if self._conn is not None:
            conn, self._conn = self._conn, None
            await conn.close()

    # ---- agent -> client ------------------------------------------------------------------------------------------

    def _on_notification(self, method: str, params: dict[str, Any]) -> None:
        if method != "session/update":
            return
        turn = self._turns.get(str(params.get("sessionId") or ""))
        if turn is None:
            return
        update = params.get("update") or {}
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            content = update.get("content") or {}
            if content.get("type") == "text" and content.get("text"):
                turn.add_text(content["text"], update.get("messageId"))
        elif kind in ("tool_call", "tool_call_update"):
            tool_id = str(update.get("toolCallId") or "")
            if tool_id.startswith(("perm-check-", "edit-approval-")):
                return
            new = tool_id not in turn.tools
            activity = turn.tools.setdefault(tool_id, ToolActivity(tool_call_id=tool_id))
            previous = activity.status
            activity.title = update.get("title") or activity.title
            activity.kind = update.get("kind") or activity.kind
            activity.status = update.get("status") or activity.status
            if activity.status == "failed":
                activity.detail = _text_of(update.get("content")) or activity.detail
            if new or activity.status != previous:
                turn.tool_changed(activity)

    async def _on_request(self, method: str, params: dict[str, Any]) -> Any:
        if method != "session/request_permission":
            from app.hermes.bridge import MethodNotFound

            raise MethodNotFound(method)
        tool_call = params.get("toolCall") or {}
        options = [o for o in params.get("options") or [] if isinstance(o, dict)]
        request = HermesPermissionRequest(title=str(tool_call.get("title") or ""), kind=str(tool_call.get("kind") or "other"),
                                          raw_input=tool_call.get("rawInput") or {}, content=tool_call.get("content") or [],
                                          options=options)
        approved = False
        if self._ask is not None:
            try:
                approved = bool(await asyncio.wait_for(self._ask(str(params.get("sessionId") or ""), request),
                                                       self._settings.permission_timeout))
            except TimeoutError:
                log.warning("Engineering permission request timed out; denying")
            except Exception:  # noqa: BLE001
                log.exception("Engineering permission handler failed; denying")
        option_id = _pick_option(options, approved)
        if option_id is None:
            return {"outcome": {"outcome": "cancelled"}}
        return {"outcome": {"outcome": "selected", "optionId": option_id}}
