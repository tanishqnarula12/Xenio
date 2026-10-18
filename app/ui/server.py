"""The UI's local web server: ``http://127.0.0.1:<port>``, standard library only.

* Listens on 127.0.0.1 only. The ``Host`` header must name this server (no DNS rebinding); a browser request that
  changes something must come from this server's own page (``Origin``).
* Every ``/api/`` call carries the per-start token (``Authorization: Bearer …``). XENIO opens the browser with it in
  the URL fragment (never sent to a server, never logged); the page keeps it for the tab's lifetime only. No other
  page, and no command an agent runs, can call the API.
* The UI's files are served with a strict Content-Security-Policy (no inline script, no other origins).
* ``GET /api/v1/events`` is a Server-Sent-Events stream of ``app.ui.events`` envelopes; ``?since=<seq>`` replays what a
  reconnecting page missed, or sends ``stream.reset`` when that is too old (the page then reloads its snapshot).

Each request runs in its own thread; work on XENIO's components goes through ``UiController``.
"""

from __future__ import annotations

import hmac
import json
import logging
import mimetypes
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from app.ui.approvals import ApprovalError
from app.ui.controller import UiController, UiError, ui_dist

log = logging.getLogger("xenio.ui")

MAX_JSON = 64_000
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
       "media-src 'self' blob:; connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
       "frame-ancestors 'none'; form-action 'self'")
SECURITY_HEADERS = {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
                    "Cross-Origin-Opener-Policy": "same-origin", "Permissions-Policy": "camera=(), geolocation=()"}
KEEPALIVE_SECONDS = 15.0
NOT_BUILT = (b"<!doctype html><meta charset=utf-8><title>XENIO</title><body style='font:16px system-ui;background:"
             b"#090a0c;color:#f5f5f7;padding:48px'><h1>The XENIO interface is not built yet</h1><p>Run <code>npm "
             b"install && npm run build</code> in the <code>ui</code> folder, then reload this page.</p>")


class _HTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        """A page that closes its event stream is normal; anything else is logged once, without request content."""
        import sys

        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError, TimeoutError)):
            return
        log.exception("UI connection failed")


class UiServer:
    def __init__(self, controller: UiController, host: str = "127.0.0.1", port: int = 8765, token: str | None = None,
                 dist: Path | None = None, extra_origins: tuple[str, ...] = ()):
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("the XENIO UI only listens on this computer (127.0.0.1)")
        self.controller = controller
        self.token = token or secrets.token_urlsafe(32)
        self.dist = dist or ui_dist()
        self.extra_origins = tuple(extra_origins)
        self.stopping = threading.Event()
        self.httpd = _HTTPServer((host, port), _handler_for(self))
        self.port = self.httpd.server_address[1]
        self.host = host
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def launch_url(self) -> str:
        return f"{self.url}#token={self.token}"

    def allowed_hosts(self) -> set[str]:
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"[::1]:{self.port}"}

    def allowed_origins(self) -> set[str]:
        return {f"http://{h}" for h in self.allowed_hosts()} | set(self.extra_origins)

    def start(self) -> None:
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="xenio-ui", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.stopping.set()
        self.controller.hub.wake_all()
        self.httpd.shutdown()
        self.httpd.server_close()


def _handler_for(server: UiServer):
    ctl = server.controller

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "XENIO"
        sys_version = ""

        def log_message(self, fmt, *args):  # never the URL (it can hold a query) or a body
            log.debug("ui %s %s", self.command, urlsplit(self.path).path)

        # ---- plumbing ----------------------------------------------------------------------------------------------

        def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for key, value in {**SECURITY_HEADERS, **(extra or {})}.items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, data) -> None:
            self._send(status, json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"),
                       "application/json; charset=utf-8", {"Cache-Control": "no-store"})

        def _error(self, status: int, message: str) -> None:
            self._json(status, {"error": message})

        def _host_ok(self) -> bool:
            return (self.headers.get("Host") or "").lower() in server.allowed_hosts()

        def _origin_ok(self) -> bool:
            origin = self.headers.get("Origin")
            return origin is None or origin in server.allowed_origins()

        def _token_ok(self) -> bool:
            auth = self.headers.get("Authorization") or ""
            given = auth[7:] if auth.startswith("Bearer ") else ""
            return bool(given) and hmac.compare_digest(given.encode(), server.token.encode())

        def _body(self, limit: int = MAX_JSON) -> bytes:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise UiError("bad Content-Length", 400) from None
            if length > limit:
                raise UiError("the request is too large", 413)
            return self.rfile.read(length) if length > 0 else b""

        def _payload(self) -> dict:
            raw = self._body()
            if not raw:
                return {}
            try:
                data = json.loads(raw)
            except ValueError:
                raise UiError("the request body is not valid JSON", 400) from None
            if not isinstance(data, dict):
                raise UiError("the request body must be a JSON object", 400)
            return data

        # ---- methods -----------------------------------------------------------------------------------------------

        def do_GET(self):
            self._dispatch("GET")

        def do_HEAD(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

        def do_DELETE(self):
            self._dispatch("DELETE")

        def _dispatch(self, method: str) -> None:
            if not self._host_ok():
                self._error(421, "this server only answers requests addressed to 127.0.0.1")
                return
            parts = urlsplit(self.path)
            path, query = parts.path, parse_qs(parts.query)
            if not path.startswith("/api/"):
                if method != "GET":
                    self._error(405, "method not allowed")
                    return
                self._static(path)
                return
            if method != "GET" and not self._origin_ok():
                self._error(403, "requests that change something must come from XENIO's own page")
                return
            if not self._token_ok():
                self._error(401, "missing or wrong XENIO UI token: open the address XENIO printed when it started")
                return
            try:
                self._api(method, path, query)
            except UiError as exc:
                self._error(exc.status, str(exc))
            except ApprovalError as exc:
                self._error(exc.status, str(exc))
            except (BrokenPipeError, ConnectionResetError):
                pass
            except TimeoutError:
                self._error(504, "XENIO did not finish that in time")
            except Exception:  # noqa: BLE001 - never a stack trace to the browser
                log.exception("UI request failed: %s %s", method, path)
                self._error(500, "XENIO hit an internal error; details are in XENIO's log")

        # ---- the API -------------------------------------------------------------------------------------------------

        def _api(self, method: str, path: str, query: dict) -> None:
            q = lambda key, default="": (query.get(key) or [default])[0]  # noqa: E731
            route = path[len("/api/v1"):] if path.startswith("/api/v1/") else None
            if route is None:
                raise UiError("unknown API version", 404)
            m = re.fullmatch
            if method == "GET" and route == "/snapshot":
                return self._json(200, ctl.snapshot())
            if method == "GET" and route == "/events":
                return self._events(int(q("since", "0") or 0))
            if method == "POST" and route == "/chat":
                p = self._payload()
                return self._json(202, ctl.chat(str(p.get("text") or ""), "voice" if p.get("via") == "voice" else "text"))
            if method == "POST" and route == "/chat/cancel":
                return self._json(200, ctl.cancel())
            if method == "POST" and route == "/chat/new":
                return self._json(200, ctl.new_conversation())
            if method == "POST" and route == "/assistant/pause":
                return self._json(200, ctl.set_paused(bool(self._payload().get("paused"))))
            if method == "POST" and route == "/assistant/dismiss":
                return self._json(200, ctl.dismiss_error())
            if method == "GET" and route == "/approvals":
                return self._json(200, ctl.approvals())
            if method == "POST" and (hit := m(r"/approvals/([0-9a-f]{6,32})", route)):
                p = self._payload()
                return self._json(200, ctl.decide(hit.group(1), str(p.get("decision") or ""),
                                                  str(p.get("confirm_text") or "")))
            if method == "GET" and route == "/engineering/runs":
                return self._json(200, ctl.runs())
            if method == "GET" and (hit := m(r"/engineering/runs/([\w-]{4,40})", route)):
                return self._json(200, ctl.run_detail(hit.group(1)))
            if method == "GET" and (hit := m(r"/engineering/runs/([\w-]{4,40})/report", route)):
                return self._json(200, ctl.run_report(hit.group(1)))
            if method == "POST" and (hit := m(r"/engineering/runs/([\w-]{4,40})/(approve|cancel|apply|push|pull_request)",
                                              route)):
                return self._json(200, ctl.run_action(hit.group(1), hit.group(2)))
            if method == "GET" and route == "/agents":
                return self._json(200, ctl.agents())
            if method == "GET" and route == "/tasks":
                return self._json(200, ctl.tasks(q("status", "all")))
            if method == "POST" and route == "/tasks":
                return self._json(201, ctl.task_create(self._payload()))
            if method == "POST" and (hit := m(r"/tasks/(\d{1,9})", route)):
                return self._json(200, ctl.task_update(int(hit.group(1)), self._payload()))
            if method == "DELETE" and (hit := m(r"/tasks/(\d{1,9})", route)):
                return self._json(200, ctl.task_delete(int(hit.group(1))))
            if method == "GET" and route == "/projects":
                return self._json(200, ctl.projects())
            if method == "GET" and route == "/schedules":
                return self._json(200, ctl.schedules())
            if method == "GET" and route == "/memory":
                return self._json(200, ctl.memory_entries(q("q")[:200]))
            if method == "GET" and route == "/activity":
                return self._json(200, ctl.activity(max(1, min(int(q("limit", "200") or 200), 500))))
            if method == "GET" and route == "/health":
                return self._json(200, ctl.health(force=q("force") == "1"))
            if method == "GET" and route == "/tools":
                return self._json(200, ctl.tools())
            if method == "GET" and route == "/settings":
                return self._json(200, ctl.ui_settings())
            if method == "PUT" and route == "/settings":
                return self._json(200, ctl.save_ui_settings(self._payload()))
            if method == "POST" and route == "/voice/listen":
                return self._json(200, ctl.voice_listen())
            if method == "POST" and route == "/voice/finish":
                return self._json(200, ctl.voice_finish())
            if method == "GET" and route == "/voice/level":
                return self._json(200, ctl.voice_level())
            if method == "POST" and route == "/voice/transcribe":
                audio = self._body(limit=12_000_000)
                return self._json(200, ctl.transcribe(audio, self.headers.get("Content-Type") or "audio/wav"))
            if method == "POST" and route == "/voice/speak":
                audio = ctl.speak(str(self._payload().get("text") or ""))
                return self._send(200, audio, "audio/wav", {"Cache-Control": "no-store"})
            raise UiError("not found", 404)

        def _events(self, since: int) -> None:
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            for key, value in SECURITY_HEADERS.items():
                self.send_header(key, value)
            self.end_headers()
            seq = since
            events, gap = ctl.hub.since(seq)
            try:
                if gap:
                    self._sse({"v": 1, "seq": ctl.hub.seq, "id": "reset", "type": "stream.reset", "data": {}})
                    seq, events = ctl.hub.seq, []
                while not server.stopping.is_set():
                    for event in events:
                        self._sse(event)
                        seq = max(seq, event["seq"])
                    if not events:
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
                    events, gap = ctl.hub.wait(seq, KEEPALIVE_SECONDS)
                    if gap:
                        self._sse({"v": 1, "seq": ctl.hub.seq, "id": "reset", "type": "stream.reset", "data": {}})
                        seq, events = ctl.hub.seq, []
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        def _sse(self, event: dict) -> None:
            data = json.dumps(event, ensure_ascii=False, default=str)
            self.wfile.write(f"id: {event['seq']}\ndata: {data}\n\n".encode("utf-8"))
            self.wfile.flush()

        # ---- the page ------------------------------------------------------------------------------------------------

        def _static(self, path: str) -> None:
            dist = server.dist
            index = dist / "index.html"
            if not index.is_file():
                self._send(200, NOT_BUILT, "text/html; charset=utf-8", {"Content-Security-Policy": CSP})
                return
            rel = path.lstrip("/") or "index.html"
            target = (dist / rel).resolve()
            if not target.is_relative_to(dist.resolve()) or not target.is_file():
                target = index  # the app routes itself (hash routes); unknown paths get the page
            kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if kind.startswith("text/") or kind in ("application/javascript", "application/json"):
                kind += "; charset=utf-8"
            cache = "no-cache" if target == index else "public, max-age=31536000, immutable"
            self._send(200, target.read_bytes(), kind, {"Content-Security-Policy": CSP, "Cache-Control": cache})

    return Handler
