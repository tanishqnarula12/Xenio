"""XENIO's end of the XENIO plugin in the CEO's Figma desktop app: agents draw in the file the CEO has open.

Figma lets only apps on its own list use its MCP server, so XENIO goes the way Figma itself offers to anyone: a plugin. The
plugin, running in the CEO's file, asks this bridge for the next drawing and sends back what happened. The bridge listens on
this Mac only, and only to a plugin holding the pairing code the CEO pasted into it once: a web page in a browser cannot drive it.
"""

from __future__ import annotations

import json
import os
import queue
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = 3779
SEEN_FOR = 30.0  # seconds since the plugin last asked: after this it counts as closed
POLL = 20.0
MAX_BODY = 8_000_000


class FigmaError(Exception):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


class FigmaBridge:
    def __init__(self, root: Path, port: int = PORT):
        self.file = Path(root) / "figma.json"
        self.port = port
        self.token = self._token()
        self._queue: queue.Queue = queue.Queue()
        self._results: dict[str, dict] = {}
        self._done = threading.Condition()
        self._seen = 0.0
        self.info: dict = {}
        self._server: ThreadingHTTPServer | None = None
        self.error = ""

    def _token(self) -> str:
        try:
            token = json.loads(self.file.read_text(encoding="utf-8")).get("token", "")
            if token:
                return token
        except (OSError, ValueError):
            pass
        token = secrets.token_urlsafe(18)
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_text(json.dumps({"token": token}), encoding="utf-8")
        os.chmod(self.file, 0o600)
        return token

    # ---- the listening end ------------------------------------------------------------------------------------------

    def start(self) -> bool:
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _cors(self):
                self.send_header("Access-Control-Allow-Origin", "*")  # a plugin's window has no origin of its own; the pairing code is what lets it in
                self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Xenio-Token")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

            def _send(self, code: int, body: dict | None = None):
                data = json.dumps(body).encode() if body is not None else b""
                self.send_response(code)
                self._cors()
                if body is not None:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if data:
                    self.wfile.write(data)

            def _allowed(self) -> bool:
                host = (self.headers.get("Host") or "").split(":")[0]
                if host not in ("localhost", "127.0.0.1"):  # a page that renamed its own address to this Mac gets nothing
                    self._send(403, {"error": "not here"})
                    return False
                if not secrets.compare_digest(self.headers.get("X-Xenio-Token") or "", bridge.token):
                    self._send(403, {"error": "the pairing code is not right: copy it from XENIO's War Room, Connectors"})
                    return False
                return True

            def do_OPTIONS(self):
                self._send(204)

            def do_GET(self):
                if not self._allowed():
                    return
                if self.path.startswith("/next"):
                    bridge._seen = time.monotonic()
                    try:
                        command = bridge._queue.get(timeout=POLL)
                    except queue.Empty:
                        return self._send(204)
                    return self._send(200, command)
                self._send(404, {"error": "no such thing"})

            def do_POST(self):
                if not self._allowed():
                    return
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    return self._send(413, {"error": "too big"})
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    return self._send(400, {"error": "not JSON"})
                bridge._seen = time.monotonic()
                if self.path.startswith("/hello"):
                    bridge.info = {k: str(body.get(k) or "")[:200] for k in ("file", "page", "user")}
                    return self._send(200, {"ok": True})
                if self.path.startswith("/result"):
                    with bridge._done:
                        bridge._results[str(body.get("id") or "")] = body
                        bridge._done.notify_all()
                    return self._send(200, {"ok": True})
                self._send(404, {"error": "no such thing"})

        try:
            self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        except OSError as exc:
            self.error = f"port {self.port} is taken on this Mac ({exc.strerror})"
            return False
        threading.Thread(target=self._server.serve_forever, daemon=True, name="figma-bridge").start()
        return True

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server = None

    # ---- XENIO's end ------------------------------------------------------------------------------------------------

    def connected(self) -> dict | None:
        if self._server is None or time.monotonic() - self._seen > SEEN_FOR:
            return None
        return dict(self.info)

    def status(self) -> dict:
        live = self.connected()
        return {"listening": self._server is not None, "error": self.error, "connected": live is not None, "file": (live or {}).get("file", ""), "page": (live or {}).get("page", ""), "token": self.token, "plugin": str(Path(__file__).resolve().parents[2] / "native" / "figma-plugin" / "manifest.json")}

    def call(self, action: str, args: dict | None = None, timeout: float = 90.0) -> dict:
        """One drawing step in the open file. What comes back is what Figma itself reports."""
        if self.connected() is None:
            raise FigmaError("the XENIO plugin is not running in Figma: open your file, then Plugins, Development, XENIO", 409)
        command_id = secrets.token_hex(8)
        self._queue.put({"id": command_id, "action": action, "args": args or {}})
        end = time.monotonic() + timeout
        with self._done:
            while command_id not in self._results:
                left = end - time.monotonic()
                if left <= 0:
                    raise FigmaError(f"Figma did not answer within {int(timeout)} s: is the plugin still open?", 504)
                self._done.wait(left)
            result = self._results.pop(command_id)
        if not result.get("ok"):
            raise FigmaError(str(result.get("error") or "Figma refused it")[:400], 422)
        return result.get("result") or {}
