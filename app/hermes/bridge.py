"""Low-level ACP transport: JSON-RPC 2.0, one JSON object per line, over the stdio of ``hermes acp``.

This module knows nothing about sessions or prompts; it moves messages and keeps the child
process healthy. ``adapter.py`` builds the Hermes conversation on top of it.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import itertools
import json
import logging
import os
import shutil
import subprocess
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from app.hermes.client import HermesError, HermesTimeout, HermesUnavailable

log = logging.getLogger("xenio.hermes.bridge")

NotificationHandler = Callable[[str, dict[str, Any]], None]
RequestHandler = Callable[[str, dict[str, Any]], Awaitable[Any]]

_STREAM_LIMIT = 64 * 1024 * 1024  # history replays and tool payloads can be large single lines


class AcpRemoteError(HermesError):
    """Hermes answered a request with a JSON-RPC error object."""

    def __init__(self, method: str, error: Mapping[str, Any]):
        self.code = error.get("code")
        self.data = error.get("data")
        super().__init__(f"{method} failed ({self.code}): {error.get('message', 'unknown error')}")


class MethodNotFound(Exception):
    """Raised by a request handler for agent->client methods XENIO does not implement."""


class AcpConnection:
    def __init__(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        on_notification: NotificationHandler | None = None,
        on_request: RequestHandler | None = None,
    ):
        self._command = list(command)
        self._cwd = cwd
        self._env = dict(env) if env is not None else None
        self._on_notification = on_notification
        self._on_request = on_request
        self._proc: asyncio.subprocess.Process | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, tuple[str, asyncio.Future[Any]]] = {}
        self._write_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._stderr_tail: collections.deque[str] = collections.deque(maxlen=40)
        self._closing = False

    # ---- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        exe = shutil.which(self._command[0])
        if exe is None:
            raise HermesUnavailable(f"Hermes executable not found on PATH: {self._command[0]!r}")
        try:
            self._proc = await asyncio.create_subprocess_exec(
                exe,
                *self._command[1:],
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._cwd) if self._cwd else None,
                env=self._env,
                limit=_STREAM_LIMIT,
            )
        except OSError as exc:
            raise HermesUnavailable(f"Could not start Hermes: {exc}") from exc
        log.debug("Hermes process started", extra={"fields": {"pid": self._proc.pid}})
        self._spawn(self._read_stdout())
        self._spawn(self._read_stderr())

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None and not self._closing

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode if self._proc else None

    def stderr_tail(self, lines: int = 8) -> str:
        return "\n".join(list(self._stderr_tail)[-lines:])

    async def close(self, timeout: float = 10.0) -> None:
        if self._proc is None or self._closing:
            return
        self._closing = True
        proc = self._proc
        # EOF on stdin is Hermes' graceful shutdown signal (it ends its ACP sessions and exits).
        with contextlib.suppress(Exception):
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), timeout)
        except TimeoutError:
            log.info("Hermes still running %ss after stdin EOF; terminating it", timeout)
            await self._kill_tree(proc)
        self._fail_pending(HermesUnavailable("Hermes connection closed"))
        for task in list(self._tasks):
            task.cancel()

    @staticmethod
    async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
        if os.name == "nt":
            # hermes.exe is a launcher; kill its Python child too.
            await asyncio.to_thread(
                subprocess.run,
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
            )
        else:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), 5)

    # ---- outgoing ----------------------------------------------------------

    async def request(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        if not self.alive:
            raise HermesUnavailable("Hermes is not running")
        req_id = next(self._ids)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = (method, future)
        try:
            await self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout)
        except TimeoutError as exc:
            raise HermesTimeout(f"Hermes did not answer {method} within {timeout:.0f}s") from exc
        finally:
            self._pending.pop(req_id, None)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def _send(self, message: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise HermesUnavailable("Hermes is not running")
        data = json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n"
        async with self._write_lock:
            try:
                self._proc.stdin.write(data)
                await self._proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError, RuntimeError) as exc:
                raise HermesUnavailable(f"Lost connection to Hermes: {exc}") from exc

    # ---- incoming ----------------------------------------------------------

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _read_stdout(self) -> None:
        assert self._proc and self._proc.stdout
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    log.debug("Ignoring non-JSON line on Hermes stdout")
                    continue
                if isinstance(message, dict):
                    self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Hermes stdout reader failed")
        finally:
            if self._proc is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self._proc.wait(), 5)
            code = self.returncode
            if not self._closing:
                log.error(
                    "Hermes process exited unexpectedly",
                    extra={"fields": {"exit_code": code, "stderr": self.stderr_tail(3) or "-"}},
                )
            self._fail_pending(HermesUnavailable(f"Hermes process exited (code {code})"))

    async def _read_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        with contextlib.suppress(asyncio.CancelledError):
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    return
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    self._stderr_tail.append(text)
                    log.debug("hermes: %s", text)

    def _dispatch(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        if isinstance(method, str):
            params = message.get("params") or {}
            if "id" in message:
                # Agent -> client request (e.g. a permission prompt). It may wait on the user,
                # so it must never block the reader loop.
                self._spawn(self._answer_request(message["id"], method, params))
            elif self._on_notification is not None:
                try:
                    self._on_notification(method, params)
                except Exception:
                    log.exception("Notification handler failed", extra={"fields": {"method": method}})
            return

        entry = self._pending.get(message.get("id"))  # type: ignore[arg-type]
        if entry is None:
            return
        sent_method, future = entry
        if future.done():
            return
        if "error" in message:
            future.set_exception(AcpRemoteError(sent_method, message["error"] or {}))
        else:
            future.set_result(message.get("result"))

    async def _answer_request(self, req_id: Any, method: str, params: dict[str, Any]) -> None:
        try:
            if self._on_request is None:
                raise MethodNotFound(method)
            result = await self._on_request(method, params)
            reply: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "result": result}
        except MethodNotFound:
            reply = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}
        except Exception as exc:
            log.exception("Request handler failed", extra={"fields": {"method": method}})
            reply = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32603, "message": str(exc)}}
        with contextlib.suppress(HermesUnavailable):
            await self._send(reply)

    def _fail_pending(self, exc: Exception) -> None:
        for _method, future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()
