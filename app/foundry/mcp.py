"""MCP servers the CEO connects, for agents to use: a program on this Mac (stdio) or an address (Streamable HTTP).

Only the CEO adds, changes or removes a server. A token goes to the macOS Keychain and nowhere else: not to a file, a log, a
model or the screen. A server's program gets a clean environment, never XENIO's own keys. Agents reach all servers through one
tool, so a server with fifty tools does not cost fifty descriptions on every step, and the first use in a job asks the CEO.
Whatever a server sends back is data from outside, never instructions.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

PROTOCOL = "2025-06-18"
KEYCHAIN_SERVICE = "XENIO MCP"
CALL_TIMEOUT = 60.0
RESULT_CHARS = 20_000  # what one call may put in front of an agent
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


class McpError(Exception):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


@dataclass
class Server:
    name: str
    transport: str  # "stdio" or "http"
    command: list[str] = field(default_factory=list)
    url: str = ""
    env: dict[str, str] = field(default_factory=dict)  # not secrets: those live in the Keychain
    token_env: str = ""  # for a program: the variable its token goes in, read from the Keychain when it starts
    has_token: bool = False
    for_agents: bool = False
    added: float = field(default_factory=time.time)

    def public(self) -> dict:
        return {k: v for k, v in asdict(self).items() if k != "env"} | {"env": sorted(self.env)}


# ---- the Keychain ------------------------------------------------------------------------------------------------------


class Keychain:
    """Tokens in the macOS login Keychain. Written through `security -i` on stdin, so a token never appears in a process list."""

    def __init__(self, runner=subprocess.run):
        self._run = runner

    def put(self, account: str, secret: str) -> None:
        if not secret or any(c in secret for c in "\n\r\"'"):
            raise McpError("that token has characters the Keychain cannot take here", 422)
        done = self._run(["/usr/bin/security", "-i"], input=f'add-generic-password -U -s "{KEYCHAIN_SERVICE}" -a "{account}" -w "{secret}"\n', capture_output=True, text=True, timeout=15)
        if done.returncode != 0:
            raise McpError("the Keychain did not take the token", 500)

    def get(self, account: str) -> str:
        done = self._run(["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account, "-w"], capture_output=True, text=True, timeout=15)
        return done.stdout.strip() if done.returncode == 0 else ""

    def delete(self, account: str) -> None:
        self._run(["/usr/bin/security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account], capture_output=True, text=True, timeout=15)


# ---- talking to one server ---------------------------------------------------------------------------------------------


class _Stdio:
    """A local server: JSON-RPC, one message per line, over its stdin and stdout."""

    def __init__(self, server: Server, token: str):
        env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "TMPDIR", "USER") if k in os.environ}  # never XENIO's own keys
        env.update(server.env)
        if server.token_env and token:
            env[server.token_env] = token
        try:
            self.proc = subprocess.Popen(server.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, text=True, bufsize=1)
        except OSError as exc:
            raise McpError(f"{server.command[0]} could not be started: {exc.strerror or exc}") from None
        self.lock = threading.Lock()

    def request(self, message: dict, timeout: float) -> dict | None:
        with self.lock:
            if self.proc.poll() is not None:
                raise McpError("the server stopped")
            self.proc.stdin.write(json.dumps(message) + "\n")
            self.proc.stdin.flush()
            if "id" not in message:
                return None
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                line = _readline(self.proc.stdout, end - time.monotonic())
                if line is None:
                    break
                try:
                    data = json.loads(line)
                except ValueError:
                    continue  # a server's own logging on stdout
                if isinstance(data, dict) and data.get("id") == message["id"]:
                    return data
            raise McpError(f"the server did not answer within {int(timeout)} s")

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def _readline(stream, timeout: float) -> str | None:
    box: list[str] = []
    reader = threading.Thread(target=lambda: box.append(stream.readline()), daemon=True)
    reader.start()
    reader.join(max(timeout, 0.01))
    if not box or not box[0]:
        return None
    return box[0]


class _Http:
    """A server at an address: Streamable HTTP, answering with JSON or with an event stream."""

    def __init__(self, server: Server, token: str, opener=urllib.request.urlopen):
        self.url, self.token, self.session, self._open = server.url, token, "", opener

    def request(self, message: dict, timeout: float) -> dict | None:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": PROTOCOL}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        req = urllib.request.Request(self.url, data=json.dumps(message).encode(), headers=headers, method="POST")
        try:
            with self._open(req, timeout=timeout) as res:
                self.session = res.headers.get("Mcp-Session-Id") or self.session
                kind = res.headers.get("Content-Type", "")
                body = res.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise McpError("the server wants a sign-in XENIO does not have: add its access token, or it accepts only apps it has approved" + (" (Figma lets only apps on its own list in)" if "figma.com" in self.url else ""), 401) from None
            raise McpError(f"the server answered {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise McpError(f"the server could not be reached: {getattr(exc, 'reason', exc)}") from None
        if "id" not in message:
            return None
        if "text/event-stream" in kind:
            for block in body.split("\n\n"):
                data = "\n".join(line[5:].lstrip() for line in block.splitlines() if line.startswith("data:"))
                try:
                    parsed = json.loads(data) if data else None
                except ValueError:
                    continue
                if isinstance(parsed, dict) and parsed.get("id") == message["id"]:
                    return parsed
            raise McpError("the server's answer had no reply to this request")
        try:
            parsed = json.loads(body or "{}")
        except ValueError:
            raise McpError("the server's answer was not JSON") from None
        return parsed if isinstance(parsed, dict) else {}

    def close(self) -> None:
        pass


class Connection:
    def __init__(self, server: Server, token: str, opener=None):
        self.server = server
        self.wire = _Stdio(server, token) if server.transport == "stdio" else _Http(server, token, **({"opener": opener} if opener else {}))
        self._id = 0
        answer = self._ask("initialize", {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "XENIO", "version": "1"}}, 30.0)
        self.info = answer.get("serverInfo") or {}
        self.wire.request({"jsonrpc": "2.0", "method": "notifications/initialized"}, 10.0)

    def _ask(self, method: str, params: dict, timeout: float = CALL_TIMEOUT) -> dict:
        self._id += 1
        reply = self.wire.request({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}, timeout) or {}
        if reply.get("error"):
            raise McpError(str((reply["error"] or {}).get("message") or "the server refused")[:300])
        return reply.get("result") or {}

    def tools(self) -> list[dict]:
        out, cursor = [], None
        for _ in range(20):
            page = self._ask("tools/list", {"cursor": cursor} if cursor else {})
            out += [t for t in page.get("tools") or [] if isinstance(t, dict) and t.get("name")]
            cursor = page.get("nextCursor")
            if not cursor:
                break
        return out

    def call(self, tool: str, arguments: dict) -> tuple[str, bool]:
        result = self._ask("tools/call", {"name": tool, "arguments": arguments})
        parts = []
        for item in result.get("content") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            elif item.get("type") == "resource":
                parts.append(str((item.get("resource") or {}).get("text") or "(a resource)"))
            else:
                parts.append(f"({item.get('type')} content)")
        if result.get("structuredContent") and not parts:
            parts.append(json.dumps(result["structuredContent"])[:RESULT_CHARS])
        text = "\n".join(parts)[:RESULT_CHARS] or "(no content)"
        return text, not result.get("isError")

    def close(self) -> None:
        self.wire.close()


# ---- all servers --------------------------------------------------------------------------------------------------------


class McpHub:
    def __init__(self, root: Path, keychain: Keychain | None = None, connect=Connection):
        self.file = Path(root) / "mcp.json"
        self.keychain = keychain or Keychain()
        self._connect = connect
        self._open: dict[str, Connection] = {}
        self._tools: dict[str, list[dict]] = {}
        self._lock = threading.Lock()

    # what is configured

    def servers(self) -> list[Server]:
        try:
            raw = json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [Server(**{k: v for k, v in s.items() if k in Server.__dataclass_fields__}) for s in raw if isinstance(s, dict)]

    def _save(self, servers: list[Server]) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_text(json.dumps([asdict(s) for s in servers], indent=1), encoding="utf-8")
        os.chmod(self.file, 0o600)

    def get(self, name: str) -> Server:
        for server in self.servers():
            if server.name == name:
                return server
        raise McpError(f"there is no connector called {name}", 404)

    def public(self) -> list[dict]:
        return [{**s.public(), "tools": [{"name": t["name"], "about": str(t.get("description") or "")[:160]} for t in self._tools.get(s.name, [])]} for s in self.servers()]

    def add(self, name: str, *, command: str | list[str] = "", url: str = "", token: str = "", token_env: str = "", env: dict | None = None, for_agents: bool = False) -> Server:
        name = re.sub(r"[^a-z0-9-]+", "-", str(name or "").strip().lower()).strip("-")[:40]
        if not NAME.match(name or ""):
            raise McpError("give the connector a short name, like github or notion", 422)
        if any(s.name == name for s in self.servers()):
            raise McpError(f"there is already a connector called {name}", 409)
        if url and command:
            raise McpError("a connector is a program or an address, not both", 422)
        if url:
            if not (url.startswith("https://") or re.match(r"^http://(127\.0\.0\.1|localhost)(:\d+)?/", url)):
                raise McpError("an address must be https, or http on this Mac only", 422)
            server = Server(name, "http", url=url)
        else:
            argv = command if isinstance(command, list) else _split(str(command))
            if not argv:
                raise McpError("say the command that starts the server, or its address", 422)
            server = Server(name, "stdio", command=[str(a) for a in argv], env={str(k): str(v) for k, v in (env or {}).items()}, token_env=str(token_env or ""))
        if token:
            self.keychain.put(name, token)
            server.has_token = True
        server.for_agents = bool(for_agents)
        self._save([*self.servers(), server])
        return server

    def add_config(self, pasted: str, token: str = "") -> list[Server]:
        """The usual pasted form, {"mcpServers": {"name": {"command", "args", "env"} or {"url"}}}. Values in env that look like keys go to the Keychain."""
        try:
            data = json.loads(pasted)
        except ValueError:
            raise McpError("that is not JSON: paste the mcpServers block as it is", 422) from None
        block = data.get("mcpServers") or data.get("servers") or data if isinstance(data, dict) else {}
        made = []
        for raw_name, spec in (block or {}).items():
            if not isinstance(spec, dict):
                continue
            env = {str(k): str(v) for k, v in (spec.get("env") or {}).items()}
            secret_env = [k for k in env if re.search(r"KEY|TOKEN|SECRET|PASSWORD", k, re.I)]
            secret = env.pop(secret_env[0]) if secret_env else ""
            if spec.get("url"):
                made.append(self.add(raw_name, url=str(spec["url"]), token=token))
            else:
                made.append(self.add(raw_name, command=[str(spec.get("command") or ""), *[str(a) for a in spec.get("args") or []]], env=env, token=secret or token, token_env=secret_env[0] if secret_env else ""))
        if not made:
            raise McpError("no servers were found in that", 422)
        return made

    def update(self, name: str, *, for_agents: bool | None = None, token: str | None = None) -> Server:
        servers = self.servers()
        for server in servers:
            if server.name == name:
                if for_agents is not None:
                    server.for_agents = bool(for_agents)
                if token:
                    self.keychain.put(name, token)
                    server.has_token = True
                    self._drop(name)
                self._save(servers)
                return server
        raise McpError(f"there is no connector called {name}", 404)

    def remove(self, name: str) -> None:
        servers = self.servers()
        if not any(s.name == name for s in servers):
            raise McpError(f"there is no connector called {name}", 404)
        self._drop(name)
        self.keychain.delete(name)
        self._tools.pop(name, None)
        self._save([s for s in servers if s.name != name])

    # using them

    def _drop(self, name: str) -> None:
        with self._lock:
            connection = self._open.pop(name, None)
        if connection is not None:
            connection.close()

    def _connection(self, name: str) -> Connection:
        with self._lock:
            open_ = self._open.get(name)
        if open_ is not None:
            return open_
        server = self.get(name)
        token = self.keychain.get(name) if server.has_token else ""
        connection = self._connect(server, token)
        with self._lock:
            self._open[name] = connection
        return connection

    def test(self, name: str) -> dict:
        """Connect, and list what it offers. The list is kept, so agents are told about it without connecting again."""
        self._drop(name)
        connection = self._connection(name)
        tools = connection.tools()
        self._tools[name] = tools
        return {"name": name, "server": connection.info, "tools": [{"name": t["name"], "about": str(t.get("description") or "")[:160]} for t in tools]}

    def tools_of(self, name: str) -> list[dict]:
        if name not in self._tools:
            self._tools[name] = self._connection(name).tools()
        return self._tools[name]

    def for_agents(self) -> list[Server]:
        return [s for s in self.servers() if s.for_agents]

    def call(self, name: str, tool: str, arguments: dict) -> tuple[str, bool]:
        server = self.get(name)
        if not server.for_agents:
            raise McpError(f"{name} is not switched on for agents", 403)
        known = {t["name"] for t in self.tools_of(name)}
        if tool not in known:
            raise McpError(f"{name} has no tool called {tool}. It has: {', '.join(sorted(known))[:600]}", 404)
        try:
            return self._connection(name).call(tool, arguments if isinstance(arguments, dict) else {})
        except McpError:
            self._drop(name)  # a broken connection is opened again next time
            raise

    def close(self) -> None:
        for name in list(self._open):
            self._drop(name)


def _split(command: str) -> list[str]:
    import shlex

    try:
        return shlex.split(command)
    except ValueError:
        raise McpError("that command has an unclosed quote", 422) from None
