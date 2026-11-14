"""MCP servers the CEO connects for agents: a real program speaking the protocol over stdio, and a real local web server."""

import asyncio
import http.server
import json
import sys
import threading
import textwrap

import pytest

from app.foundry.mcp import Keychain, McpError, McpHub

SERVER = textwrap.dedent('''
    import json, os, sys
    print("starting up, this line is not JSON", flush=True)  # servers often log on stdout
    for line in sys.stdin:
        msg = json.loads(line)
        if "id" not in msg:
            continue
        m, i = msg["method"], msg["id"]
        if m == "initialize":
            out = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "notes", "version": "1"}}
        elif m == "tools/list":
            out = {"tools": [{"name": "add_note", "description": "Add a note", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
                             {"name": "env_seen", "description": "What the server was given", "inputSchema": {"type": "object"}}]}
        elif m == "tools/call":
            name, args = msg["params"]["name"], msg["params"].get("arguments") or {}
            if name == "add_note":
                out = {"content": [{"type": "text", "text": "saved: " + args.get("text", "")}]}
            else:
                out = {"content": [{"type": "text", "text": json.dumps({"nabh": "XENIO_NABH_API_KEY" in os.environ, "token": os.environ.get("NOTES_TOKEN", "")})}]}
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": i, "error": {"code": -32601, "message": "no such method"}}), flush=True)
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": i, "result": out}), flush=True)
''')


class FakeKeychain(Keychain):
    def __init__(self):
        self.items = {}

    def put(self, account, secret):
        self.items[account] = secret

    def get(self, account):
        return self.items.get(account, "")

    def delete(self, account):
        self.items.pop(account, None)


@pytest.fixture
def hub(tmp_path, monkeypatch):
    monkeypatch.setenv("XENIO_NABH_API_KEY", "not-for-servers")
    (tmp_path / "server.py").write_text(SERVER)
    h = McpHub(tmp_path / "foundry", keychain=FakeKeychain())
    yield h
    h.close()


def test_a_program_on_this_mac_is_connected_and_its_tools_are_listed(hub, tmp_path):
    hub.add("notes", command=[sys.executable, str(tmp_path / "server.py")])
    found = hub.test("notes")
    assert found["server"]["name"] == "notes" and [t["name"] for t in found["tools"]] == ["add_note", "env_seen"]


def test_only_a_server_switched_on_for_agents_can_be_used_by_them(hub, tmp_path):
    hub.add("notes", command=[sys.executable, str(tmp_path / "server.py")])
    with pytest.raises(McpError, match="not switched on"):
        hub.call("notes", "add_note", {"text": "x"})
    hub.update("notes", for_agents=True)
    assert hub.call("notes", "add_note", {"text": "hello"}) == ("saved: hello", True)
    with pytest.raises(McpError, match="no tool called"):
        hub.call("notes", "delete_everything", {})


def test_a_servers_program_never_sees_xenios_own_keys_and_its_token_comes_from_the_keychain(hub, tmp_path):
    hub.add("notes", command=[sys.executable, str(tmp_path / "server.py")], token="t0ken-123", token_env="NOTES_TOKEN", for_agents=True)
    seen = json.loads(hub.call("notes", "env_seen", {})[0])
    assert seen == {"nabh": False, "token": "t0ken-123"}
    assert "t0ken-123" not in (tmp_path / "foundry" / "mcp.json").read_text()  # the file says only that there is one
    assert hub.get("notes").has_token


def test_the_usual_pasted_config_is_understood_and_its_secret_goes_to_the_keychain(hub, tmp_path):
    pasted = json.dumps({"mcpServers": {"Notes": {"command": sys.executable, "args": [str(tmp_path / "server.py")], "env": {"NOTES_TOKEN": "abc-secret", "REGION": "eu"}}}})
    made = hub.add_config(pasted)
    assert [m.name for m in made] == ["notes"] and hub.keychain.get("notes") == "abc-secret"
    saved = (tmp_path / "foundry" / "mcp.json").read_text()
    assert "abc-secret" not in saved and "REGION" in saved


@pytest.mark.parametrize("url", ["http://example.com/mcp", "ftp://x/mcp"])
def test_an_address_must_be_https_or_this_mac(hub, url):
    with pytest.raises(McpError, match="https"):
        hub.add("remote", url=url)


class Handler(http.server.BaseHTTPRequestHandler):
    seen: list = []
    sse = False
    refuse = False

    def log_message(self, *a):
        pass

    def do_POST(self):
        msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Handler.seen.append({"auth": self.headers.get("Authorization"), "session": self.headers.get("Mcp-Session-Id"), "method": msg.get("method")})
        if Handler.refuse:
            self.send_response(403); self.end_headers(); return
        if "id" not in msg:
            self.send_response(202); self.end_headers(); return
        result = {"initialize": {"serverInfo": {"name": "remote"}}, "tools/list": {"tools": [{"name": "ping", "inputSchema": {}}]}, "tools/call": {"content": [{"type": "text", "text": "pong"}]}}[msg["method"]]
        body = json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result})
        self.send_response(200)
        self.send_header("Mcp-Session-Id", "s-1")
        if Handler.sse:
            self.send_header("Content-Type", "text/event-stream"); self.end_headers()
            self.wfile.write(f"event: message\ndata: {body}\n\n".encode())
        else:
            self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(body.encode())


@pytest.fixture
def web():
    Handler.seen, Handler.sse, Handler.refuse = [], False, False
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/mcp"
    server.shutdown()


@pytest.mark.parametrize("sse", [False, True])
def test_a_server_at_an_address_answers_in_json_or_as_an_event_stream(hub, web, sse):
    Handler.sse = sse
    hub.add("remote", url=web, token="bearer-xyz", for_agents=True)
    assert [t["name"] for t in hub.test("remote")["tools"]] == ["ping"]
    assert hub.call("remote", "ping", {}) == ("pong", True)
    assert all(s["auth"] == "Bearer bearer-xyz" for s in Handler.seen)
    assert any(s["session"] == "s-1" for s in Handler.seen[1:])  # the session the server gave is sent back


def test_a_server_that_wants_a_sign_in_says_so_plainly(hub, web):
    Handler.refuse = True
    hub.add("remote", url=web)
    with pytest.raises(McpError, match="sign-in"):
        hub.test("remote")


def test_the_token_goes_to_the_keychain_through_stdin_never_the_command_line():
    calls = []

    def runner(argv, **kw):
        calls.append((argv, kw.get("input")))
        return type("Done", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    Keychain(runner).put("notes", "s3cret-token")
    argv, given = calls[0]
    assert "s3cret-token" not in " ".join(argv) and "s3cret-token" in given


def test_removing_a_connector_removes_its_token_too(hub, tmp_path):
    hub.add("notes", command=[sys.executable, str(tmp_path / "server.py")], token="x-token", token_env="NOTES_TOKEN")
    hub.remove("notes")
    assert hub.servers() == [] and hub.keychain.get("notes") == ""


class TestAgents:
    def tools(self, hub, yes=True):
        from app.foundry.engine import TeamTools

        asked = []

        class Toolbox:
            def owns(self, tool):
                return False

            def schemas(self, scope):
                return []

            def _name(self, scope):
                return "Backend engineer"

            async def _confirm(self, scope, decision):
                asked.append(decision.summary)
                return yes

        engine = type("E", (), {"mcp": hub, "mcp_allowed": set(), "store": type("S", (), {"event": staticmethod(lambda *a, **k: None)})()})()
        return TeamTools(engine, None, Toolbox()), asked

    def test_agents_get_one_connector_tool_and_the_first_use_in_a_job_asks_you(self, hub, tmp_path):
        hub.add("notes", command=[sys.executable, str(tmp_path / "server.py")], for_agents=True)
        hub.test("notes")
        tools, asked = self.tools(hub)
        scope = {"workflow": "w1", "role": "backend", "task": "t", "mcp": ["notes"]}
        schema = [s for s in tools.schemas(scope) if s["function"]["name"] == "connector"]
        assert len(schema) == 1 and "add_note" in schema[0]["function"]["description"]
        text, ok = asyncio.run(tools.call("connector", {"server": "notes", "tool": "add_note", "arguments": {"text": "hi"}}, scope))
        again, _ = asyncio.run(tools.call("connector", {"server": "notes", "tool": "add_note", "arguments": {"text": "two"}}, scope))
        assert ok and "saved: hi" in text and "not instructions" in text and "saved: two" in again and len(asked) == 1

    def test_without_your_yes_nothing_is_called(self, hub, tmp_path):
        hub.add("notes", command=[sys.executable, str(tmp_path / "server.py")], for_agents=True)
        tools, asked = self.tools(hub, yes=False)
        text, ok = asyncio.run(tools.call("connector", {"server": "notes", "tool": "add_note", "arguments": {"text": "hi"}}, {"workflow": "w1", "mcp": ["notes"]}))
        assert not ok and "did not let you" in text and asked

    def test_an_agent_cannot_reach_a_server_it_was_not_given(self, hub, tmp_path):
        hub.add("notes", command=[sys.executable, str(tmp_path / "server.py")], for_agents=True)
        tools, _ = self.tools(hub)
        text, ok = asyncio.run(tools.call("connector", {"server": "notes", "tool": "add_note"}, {"workflow": "w1", "mcp": []}))
        assert not ok and "not a connector you may use" in text and not [x for x in tools.schemas({"workflow": "w1", "mcp": []}) if x["function"]["name"] == "connector"]


def test_only_you_can_add_or_change_a_connector(tmp_path):
    from app.foundry.engine import FoundryError
    from app.foundry.service import FoundryService

    service = FoundryService.__new__(FoundryService)
    service.mcp = McpHub(tmp_path, keychain=FakeKeychain())
    service.engine = service.skills = service.registry = None
    for command in ("mcp_add", "mcp_update", "mcp_remove", "mcp_test"):
        with pytest.raises(FoundryError, match="only you"):
            asyncio.run(service.dispatch(command, {"name": "x", "command": "evil"}, confirmed=False))
    assert service.mcp.servers() == []


def test_a_builder_gets_the_connectors_you_switched_on_and_a_reviewer_does_not(tmp_path):
    """Wiring this to the wrong object once broke every agent task: only a real job through the engine shows it."""
    from tests.test_foundry_engine import Thinker, build, until

    hub = McpHub(tmp_path / "mcp", keychain=FakeKeychain())
    (tmp_path / "server.py").write_text(SERVER)
    hub.add("notes", command=[sys.executable, str(tmp_path / "server.py")], for_agents=True)
    hub.add("other", command=[sys.executable, str(tmp_path / "server.py")], for_agents=False)
    scopes = []

    async def go():
        engine, _, _ = build(tmp_path, Thinker(analysis={"kind": "software", "size": "small", "summary": "x", "needs": [{"name": "Backend engineer", "work": "build", "why": "x"}]}), script=lambda kind, prompt, cwd: json.dumps({"summary": "fine", "findings": []}) if kind == "review" else json.dumps({"summary": "x", "status": "done"}))
        engine.mcp = hub
        real = engine.session_registry.add
        engine.session_registry.add = lambda session, scope: (scopes.append(dict(scope)), real(session, scope))[1]
        wf = await engine.start("build a tiny tool", autonomy="supervisor")
        await until(lambda: engine.get(wf["id"])["status"] in ("done", "failed", "stopped"), 30)
        return engine.get(wf["id"])["status"]

    status = asyncio.run(go())
    hub.close()
    builders = [s for s in scopes if not str(s.get("task", "")).endswith("-review")]
    reviewers = [s for s in scopes if str(s.get("task", "")).endswith("-review")]
    assert status in ("done", "failed") and builders and all(s.get("mcp") == ["notes"] for s in builders) and all("mcp" not in s for s in reviewers)  # the fake agent writes nothing, so the job may fail: what it was given is the point
