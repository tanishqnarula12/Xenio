"""Hermes over ACP (Agent Client Protocol) — the integration discovered on this machine.

``hermes acp`` runs Hermes Agent as a long-lived JSON-RPC server on stdio. XENIO is the ACP
*client*: it opens one conversation session, sends prompts, streams the reply, forwards
Hermes' permission prompts to the XENIO safety layer, and sends ``session/cancel`` for
barge-in. Hermes keeps full ownership of reasoning, tools, memory, skills and subagents.

See docs/HERMES_INTEGRATION.md for the protocol evidence and timings.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app import __version__
from app.config import PROJECT_ROOT, HermesSettings, default_hermes_home
from app.hermes.model_info import read_model_setup
from app.safety.policy import POLICY_VERSION
from app.hermes.bridge import AcpConnection, AcpRemoteError, MethodNotFound
from app.hermes.client import (
    ChunkHandler,
    HermesClient,
    HermesError,
    HermesPermissionRequest,
    HermesReply,
    HermesTimeout,
    HermesUnavailable,
    PermissionHandler,
    ToolActivity,
)
from app.logging_setup import event

log = logging.getLogger("xenio.hermes")

ACP_PROTOCOL_VERSION = 1
# XENIO does not offer its filesystem/terminal to Hermes: Hermes uses its own tools.
_CLIENT_CAPABILITIES = {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}


@dataclass
class _Turn:
    on_chunk: ChunkHandler | None
    parts: list[str] = field(default_factory=list)
    last_message_id: str | None = None
    tools: dict[str, ToolActivity] = field(default_factory=dict)
    on_tool: Callable[[ToolActivity], None] | None = None

    def tool_changed(self, activity: ToolActivity) -> None:
        if self.on_tool is not None:
            try:
                self.on_tool(activity)
            except Exception:
                log.exception("Tool handler failed")

    def add_text(self, text: str, message_id: str | None) -> None:
        # A turn can hold several assistant messages (e.g. before and after a tool call).
        if message_id and self.last_message_id and message_id != self.last_message_id and self.parts:
            self._emit("\n\n")
        if message_id:
            self.last_message_id = message_id
        self._emit(text)

    def _emit(self, text: str) -> None:
        self.parts.append(text)
        if self.on_chunk is not None:
            try:
                self.on_chunk(text)
            except Exception:
                log.exception("Chunk handler failed")

    @property
    def text(self) -> str:
        return "".join(self.parts).strip()


class HermesAcpClient(HermesClient):
    def __init__(
        self,
        settings: HermesSettings,
        session_file: Path | None = None,
        connection_factory: type[AcpConnection] = AcpConnection,
        hermes_home: Path | None = None,
    ):
        self._settings = settings
        self._session_file = session_file  # None = never resume or persist (health checks, tests)
        self._data_dir = session_file.parent if session_file is not None else None
        self._factory = connection_factory
        self._conn: AcpConnection | None = None
        self._session_id: str | None = None
        self._turn: _Turn | None = None
        self._turn_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._permission_handler: PermissionHandler | None = None
        self._hermes_home = hermes_home if hermes_home is not None else default_hermes_home()
        self.agent_info: dict[str, Any] = {}
        self.extra_env: dict[str, str] = {}  # e.g. the engineering service's endpoint (this chat's Hermes only)

    # ---- lifecycle ---------------------------------------------------------

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def connected(self) -> bool:
        return self._conn is not None and self._conn.alive

    def set_permission_handler(self, handler: PermissionHandler | None) -> None:
        self._permission_handler = handler

    async def start(self, open_session: bool = True) -> None:
        async with self._start_lock:
            if self.connected and (self._session_id or not open_session):
                return
            if not self.connected:
                await self._connect()
            if open_session and not self._session_id:
                await self._open_session(fresh=False)

    def guard_env(self) -> dict[str, str]:
        """Environment of every Hermes process XENIO starts (its ACP agent and scheduled-check ticks)."""
        return {**guard_environment(self._settings, self._data_dir), **self.extra_env}

    async def _connect(self) -> None:
        await self._drop_connection()
        # A new Hermes process re-opens the conversation through _open_session (saved id first).
        self._session_id = None
        workspace = self._settings.workspace
        workspace.mkdir(parents=True, exist_ok=True)
        env = self.guard_env()
        conn = self._factory(
            self._settings.command,
            cwd=workspace,
            env=env,
            on_notification=self._on_notification,
            on_request=self._on_request,
        )
        event(log, "Starting Hermes", command=" ".join(self._settings.command))
        started = time.monotonic()
        await conn.start()
        try:
            init = await conn.request(
                "initialize",
                {
                    "protocolVersion": ACP_PROTOCOL_VERSION,
                    "clientCapabilities": _CLIENT_CAPABILITIES,
                    "clientInfo": {"name": "xenio", "version": __version__},
                },
                timeout=self._settings.startup_timeout,
            )
        except HermesError as exc:
            detail = conn.stderr_tail(3)
            await conn.close(timeout=5)
            raise HermesUnavailable(f"{exc}" + (f" | {detail}" if detail else "")) from exc
        if not isinstance(init, dict):
            await conn.close(timeout=5)
            raise HermesUnavailable("Hermes returned an invalid initialize response")
        if init.get("protocolVersion") != ACP_PROTOCOL_VERSION:
            log.warning("Unexpected ACP protocol version", extra={"fields": {"version": init.get("protocolVersion")}})
        self.agent_info = dict(init.get("agentInfo") or {})
        self._conn = conn
        event(
            log,
            "Hermes connection established",
            agent=self.agent_info.get("name", "?"),
            version=self.agent_info.get("version", "?"),
            ms=int((time.monotonic() - started) * 1000),
        )

    async def _open_session(self, fresh: bool) -> None:
        assert self._conn is not None
        cwd = str(self._settings.workspace)
        saved = None if fresh or not self._settings.resume_session else self._load_saved_session()
        if saved:
            try:
                # session/load replays history as notifications before it answers; with no turn
                # active those are ignored. It returns null when the session no longer exists.
                result = await self._conn.request(
                    "session/load",
                    {"sessionId": saved, "cwd": cwd, "mcpServers": []},
                    timeout=self._settings.startup_timeout,
                )
                if result is not None:
                    self._session_id = saved
                    event(log, "Hermes session resumed", session=saved)
                    return
                log.info("Saved Hermes session no longer exists; starting a new one")
            except AcpRemoteError as exc:
                log.warning("Could not resume Hermes session", extra={"fields": {"error": str(exc)}})
        result = await self._conn.request(
            "session/new", {"cwd": cwd, "mcpServers": []}, timeout=self._settings.startup_timeout
        )
        session_id = result.get("sessionId") if isinstance(result, dict) else None
        if not session_id:
            raise HermesError("Hermes did not return a session id")
        self._session_id = session_id
        self._save_session(session_id)
        event(log, "Hermes session created", session=session_id)

    async def new_session(self) -> None:
        await self.start(open_session=False)
        async with self._turn_lock:
            self._session_id = None
            await self._open_session(fresh=True)

    async def health_check(self) -> bool:
        try:
            await self.start(open_session=False)
        except HermesError as exc:
            log.warning("Hermes health check failed", extra={"fields": {"error": str(exc)}})
            return False
        return self.connected

    async def close(self) -> None:
        await self._drop_connection()
        self._session_id = None

    async def _drop_connection(self) -> None:
        if self._conn is not None:
            conn, self._conn = self._conn, None
            await conn.close()

    # ---- turns -------------------------------------------------------------

    async def send_message(self, message: str, on_chunk: ChunkHandler | None = None,
                           on_tool: Callable[[ToolActivity], None] | None = None) -> HermesReply:
        if not message.strip():
            raise ValueError("message is empty")
        if not self.connected:
            # First use, or Hermes died since the last turn: (re)start once.
            await self.start()
        assert self._conn is not None

        async with self._turn_lock:
            if not self._session_id:
                await self._open_session(fresh=False)
            turn, result = await self._prompt(message, on_chunk, on_tool)
            if self._lost_session(turn, result):
                # Hermes answers "refusal" with no text when it no longer knows the session id (seen live
                # minutes after a successful session/load). Start a fresh session and send the request again.
                event(log, "Hermes lost the session; starting a new one", logging.WARNING, session=self._session_id)
                self._session_id = None
                await self._open_session(fresh=True)
                turn, result = await self._prompt(message, on_chunk, on_tool)
                if self._lost_session(turn, result):
                    self._session_id = None
                    raise HermesError("Hermes rejected the prompt (session not found or refused)")

        if not isinstance(result, dict):
            raise HermesError("Hermes returned an invalid prompt response")
        stop_reason = str(result.get("stopReason") or "end_turn")
        return HermesReply(
            text=turn.text,
            stop_reason=stop_reason,
            tool_calls=list(turn.tools.values()),
            usage=result.get("usage"),
        )

    async def _prompt(self, message: str, on_chunk: ChunkHandler | None,
                      on_tool: Callable[[ToolActivity], None] | None = None) -> tuple[_Turn, Any]:
        assert self._conn is not None
        turn = _Turn(on_chunk, on_tool=on_tool)
        self._turn = turn
        try:
            result = await self._conn.request(
                "session/prompt",
                {"sessionId": self._session_id, "prompt": [{"type": "text", "text": message}]},
                timeout=self._settings.turn_timeout,
            )
        except HermesTimeout:
            await self.cancel()
            raise
        finally:
            self._turn = None
        return turn, result

    @staticmethod
    def _lost_session(turn: _Turn, result: Any) -> bool:
        return isinstance(result, dict) and result.get("stopReason") == "refusal" and not turn.text and not turn.tools

    async def cancel(self) -> None:
        if self._conn is not None and self._conn.alive and self._session_id and self._turn is not None:
            event(log, "Cancelling Hermes turn", session=self._session_id)
            try:
                await self._conn.notify("session/cancel", {"sessionId": self._session_id})
            except HermesUnavailable:
                pass

    # ---- agent -> client -----------------------------------------------------

    def _on_notification(self, method: str, params: dict[str, Any]) -> None:
        if method != "session/update":
            return
        turn = self._turn
        if turn is None or params.get("sessionId") != self._session_id:
            return  # history replay, or another session
        update = params.get("update") or {}
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            content = update.get("content") or {}
            if content.get("type") == "text" and content.get("text"):
                turn.add_text(content["text"], update.get("messageId"))
        elif kind in ("tool_call", "tool_call_update"):
            tool_id = str(update.get("toolCallId") or "")
            if tool_id.startswith(("perm-check-", "edit-approval-")):
                return  # synthetic bubbles for permission prompts, not real tool runs
            new = tool_id not in turn.tools
            activity = turn.tools.setdefault(tool_id, ToolActivity(tool_call_id=tool_id))
            previous = activity.status
            activity.title = update.get("title") or activity.title
            activity.kind = update.get("kind") or activity.kind
            activity.status = update.get("status") or activity.status
            if activity.status == "failed":
                activity.detail = _text_of(update.get("content")) or activity.detail
            label = (activity.title or tool_id)[:160]
            if new:
                event(log, "TOOL_REQUESTED", tool=label, kind=activity.kind or "-")
            if new or activity.status != previous:
                turn.tool_changed(activity)
            if activity.status != previous:
                if activity.status == "in_progress":
                    event(log, "TOOL_EXECUTION_STARTED", tool=label)
                elif activity.status == "completed":
                    event(log, "TOOL_EXECUTION_COMPLETED", tool=label)
                elif activity.status == "failed":
                    event(log, "TOOL_EXECUTION_FAILED", logging.WARNING, tool=label, error=activity.detail[:300])

    async def _on_request(self, method: str, params: dict[str, Any]) -> Any:
        if method == "session/request_permission":
            return await self._handle_permission(params)
        raise MethodNotFound(method)

    async def _handle_permission(self, params: dict[str, Any]) -> dict[str, Any]:
        tool_call = params.get("toolCall") or {}
        options = [o for o in params.get("options") or [] if isinstance(o, dict)]
        request = HermesPermissionRequest(
            title=str(tool_call.get("title") or ""),
            kind=str(tool_call.get("kind") or "other"),
            raw_input=tool_call.get("rawInput") or {},
            content=tool_call.get("content") or [],
            options=options,
        )
        approved = False
        if self._permission_handler is None:
            log.warning("Hermes asked for permission but no handler is set; denying")
        else:
            try:
                approved = bool(
                    await asyncio.wait_for(self._permission_handler(request), self._settings.permission_timeout)
                )
            except TimeoutError:
                log.warning("Permission request timed out; denying", extra={"fields": {"title": request.title}})
            except Exception:
                log.exception("Permission handler failed; denying")
        option_id = _pick_option(options, approved)
        # text XENIO would type or send (between « ») stays out of the log
        title = re.sub(r"«[^»]*»", "«…»", request.title or "")
        event(log, "Permission answered", title=title, approved=approved, option=option_id or "cancelled")
        if option_id is None:
            return {"outcome": {"outcome": "cancelled"}}
        return {"outcome": {"outcome": "selected", "optionId": option_id}}

    # ---- session persistence ----------------------------------------------

    def _load_saved_session(self) -> str | None:
        if self._session_file is None or not self._session_file.is_file():
            return None
        try:
            data = json.loads(self._session_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if data.get("workspace") != str(self._settings.workspace):
            return None
        # Hermes freezes a session's system prompt (incl. XENIO's rules) at creation: a new policy
        # version needs a new session.
        if data.get("policy_version") != POLICY_VERSION:
            log.info("XENIO policy changed since the saved session; starting a new one")
            return None
        # Hermes also keeps the model a session was created with: after `hermes model` (or a new
        # fallback chain) the saved conversation would keep using the old model.
        current = self._model_signature()
        if current and data.get("model", "") != current:
            event(log, "Hermes model changed since the saved session; starting a new one", model=current)
            return None
        return data.get("session_id") or None

    def _model_signature(self) -> str:
        setup = read_model_setup(self._hermes_home)
        if not setup.signature:
            return ""  # model unknown (no readable config): never a reason to drop the conversation
        return "|".join([setup.signature, setup.reasoning_effort, *(f"{p}:{m}" for p, m in setup.fallbacks)])

    def _save_session(self, session_id: str) -> None:
        if self._session_file is None:
            return
        try:
            self._session_file.parent.mkdir(parents=True, exist_ok=True)
            self._session_file.write_text(
                json.dumps({"session_id": session_id, "workspace": str(self._settings.workspace),
                            "policy_version": POLICY_VERSION, "model": self._model_signature()}),
                encoding="utf-8",
            )
        except OSError:
            log.warning("Could not save Hermes session id", exc_info=True)


def guard_environment(settings: HermesSettings, data_dir: Path | None) -> dict[str, str]:
    """Environment of every Hermes process XENIO starts (chat, engineering, scheduled-check ticks).

    XENIO_GUARD=1 switches on the xenio-guard Hermes plugin for that process only; the other
    variables tell it where XENIO's policy/tools live and which folders are the user's."""
    return {
        **os.environ,
        "XENIO_GUARD": "1",
        "XENIO_ROOT": str(PROJECT_ROOT),
        "XENIO_WORKSPACE": str(settings.workspace),
        "XENIO_ALLOWED_DIRS": ";".join(str(p) for p in settings.allowed_dirs),
        # Read by Hermes' config as tools.tool_search.enabled: ${XENIO_TOOL_SEARCH} (set by
        # --install-guard): keeps XENIO's tools directly visible instead of behind tool_search.
        "XENIO_TOOL_SEARCH": "off",
        "XENIO_DOWNLOAD_MAX_MB": str(settings.download_max_mb),
        **({"XENIO_DATA_DIR": str(data_dir)} if data_dir else {}),
        **({"XENIO_GITHUB_USER": settings.github_user} if settings.github_user else {}),
        **({"XENIO_HIMALAYA_ACCOUNT": settings.himalaya_account} if settings.himalaya_account else {}),
        # Hermes' own browser is invisible unless this is set (Hermes reads AGENT_BROWSER_HEADED when
        # browser.headed is not in its config). Off by default: xenio_open_url shows pages to the user.
        **({"AGENT_BROWSER_HEADED": "1"} if settings.show_browser else {}),
        **(settings.vision.env() if settings.vision else {}),
    }


def _text_of(content: Any) -> str:
    """Plain text of ACP tool-call content blocks ({"type": "content", "content": {"type": "text", ...}})."""
    parts: list[str] = []
    for block in content if isinstance(content, list) else []:
        inner = block.get("content") if isinstance(block, dict) and isinstance(block.get("content"), dict) else block
        if isinstance(inner, dict) and inner.get("type") == "text" and isinstance(inner.get("text"), str):
            parts.append(inner["text"])
    return " ".join(" ".join(parts).split())[:500]


def _pick_option(options: list[dict[str, Any]], approved: bool) -> str | None:
    """Approve with a one-time option only — XENIO never grants standing permissions."""
    wanted = "allow_once" if approved else "reject_once"
    for option in options:
        if option.get("kind") == wanted and option.get("optionId"):
            return str(option["optionId"])
    return None  # -> "cancelled", which Hermes treats as a denial


def create_hermes_client(settings: HermesSettings, data_dir: Path | None) -> HermesClient:
    session_file = data_dir / "hermes_session.json" if data_dir is not None else None
    return HermesAcpClient(settings, session_file=session_file)
