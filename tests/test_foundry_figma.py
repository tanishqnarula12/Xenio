"""Agents drawing in the CEO's Figma file through XENIO's plugin. A stand-in plugin speaks to the real bridge over HTTP."""

import asyncio
import base64
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from app.foundry.figma_bridge import FigmaBridge, FigmaError


@pytest.fixture
def bridge(tmp_path):
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    b = FigmaBridge(tmp_path, port=port)
    assert b.start()
    yield b
    b.stop()


def ask(bridge, path, token, body=None, host=None):
    req = urllib.request.Request(f"http://127.0.0.1:{bridge.port}{path}", data=json.dumps(body).encode() if body is not None else None, method="POST" if body is not None else "GET",
                                 headers={"X-Xenio-Token": token, "Content-Type": "application/json", **({"Host": host} if host else {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return res.status, json.loads(res.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, None


class Plugin:
    """What the plugin's window does: say hello, then ask for the next drawing and answer it, as Figma would."""

    def __init__(self, bridge, answer):
        self.bridge, self.answer, self.stop, self.got = bridge, answer, False, []
        ask(bridge, "/hello", bridge.token, {"file": "Portfolio", "page": "Page 1", "user": "Tanishq"})
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        while not self.stop:
            status, cmd = ask(self.bridge, "/next", self.bridge.token)
            if status == 200:
                self.got.append(cmd)
                ask(self.bridge, "/result", self.bridge.token, {"type": "result", "id": cmd["id"], **self.answer(cmd)})


def test_the_pairing_code_is_kept_and_needed(bridge, tmp_path):
    assert FigmaBridge(tmp_path).token == bridge.token  # the same code after a restart: the plugin keeps working
    assert ask(bridge, "/next", "wrong")[0] == 403 and ask(bridge, "/hello", "", {"file": "x"})[0] == 403


def test_a_page_that_calls_itself_this_mac_gets_nothing(bridge):
    assert ask(bridge, "/next", bridge.token, host="evil.example:3779")[0] == 403


def test_without_the_plugin_open_nothing_is_sent(bridge):
    assert bridge.connected() is None
    with pytest.raises(FigmaError, match="not running in Figma"):
        bridge.call("read")


def test_a_drawing_reaches_the_plugin_and_figmas_answer_comes_back(bridge):
    plugin = Plugin(bridge, lambda cmd: {"ok": True, "result": {"id": "1:2", "nodes": 5}})
    for _ in range(50):
        if bridge.connected():
            break
        time.sleep(0.05)
    assert bridge.connected()["file"] == "Portfolio"
    out = bridge.call("build", {"tree": {"type": "frame", "name": "Hero"}})
    plugin.stop = True
    assert out == {"id": "1:2", "nodes": 5} and plugin.got[0]["action"] == "build" and plugin.got[0]["args"]["tree"]["name"] == "Hero"


def test_what_figma_refuses_comes_back_as_an_error(bridge):
    plugin = Plugin(bridge, lambda cmd: {"ok": False, "error": "there is no node 9:9 in this file"})
    for _ in range(50):
        if bridge.connected():
            break
        time.sleep(0.05)
    with pytest.raises(FigmaError, match="no node 9:9"):
        bridge.call("update", {"id": "9:9"})
    plugin.stop = True


class TestAgents:
    def tools(self, bridge, tmp_path, yes=True):
        from app.foundry.engine import TeamTools

        asked, events = [], []

        class Toolbox:
            def owns(self, tool):
                return False

            def schemas(self, scope):
                return []

            def _name(self, scope):
                return "UI designer"

            async def _confirm(self, scope, decision):
                asked.append(decision.summary)
                return yes

        engine = type("E", (), {"figma": bridge, "mcp": None, "mcp_allowed": set(), "store": type("S", (), {"event": staticmethod(lambda kind, *a, **k: events.append((kind, k)))})()})()
        return TeamTools(engine, None, Toolbox()), asked, events

    def test_a_designer_draws_after_your_yes_once_per_job_and_looks_at_what_it_drew(self, bridge, tmp_path):
        png = base64.b64encode(b"\x89PNG fake").decode()
        plugin = Plugin(bridge, lambda cmd: {"ok": True, "result": {"png": png, "width": 1440, "height": 900} if cmd["action"] == "export" else {"id": "1:2", "nodes": 7}})
        for _ in range(50):
            if bridge.connected():
                break
            time.sleep(0.05)
        tools, asked, events = self.tools(bridge, tmp_path)
        scope = {"workflow": "w1", "role": "designer", "task": "t", "figma": True, "root": str(tmp_path)}
        assert any(s["function"]["name"] == "figma" for s in tools.schemas(scope))
        first, ok1 = asyncio.run(tools.call("figma", {"action": "build", "tree": {"type": "frame", "name": "Hero"}}, scope))
        second, ok2 = asyncio.run(tools.call("figma", {"action": "update", "id": "1:2", "fill": "#000000"}, scope))
        shot, ok3 = asyncio.run(tools.call("figma", {"action": "export", "id": "1:2"}, scope))
        plugin.stop = True
        assert ok1 and ok2 and ok3 and len(asked) == 1 and "Portfolio" in asked[0] and "undone" in asked[0]
        assert (tmp_path / "figma" / "1-2.png").read_bytes() == b"\x89PNG fake" and "look_at_image" in shot
        assert [k for kind, k in events if kind == "figma.action" and k.get("action") == "build"]

    def test_without_your_yes_nothing_is_drawn(self, bridge, tmp_path):
        plugin = Plugin(bridge, lambda cmd: {"ok": True, "result": {}})
        for _ in range(50):
            if bridge.connected():
                break
            time.sleep(0.05)
        tools, asked, _ = self.tools(bridge, tmp_path, yes=False)
        text, ok = asyncio.run(tools.call("figma", {"action": "build", "tree": {}}, {"workflow": "w1", "figma": True, "root": str(tmp_path)}))
        plugin.stop = True
        assert not ok and "did not let you" in text and plugin.got == []

    def test_a_job_that_is_not_about_figma_is_not_offered_it(self, bridge, tmp_path):
        tools, _, _ = self.tools(bridge, tmp_path)
        assert not [s for s in tools.schemas({"workflow": "w1"}) if s["function"]["name"] == "figma"]


def test_the_plugin_code_parses():
    import shutil
    import subprocess
    from pathlib import Path

    if not shutil.which("node"):
        pytest.skip("needs node")
    root = Path(__file__).resolve().parents[1] / "native" / "figma-plugin"
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["networkAccess"]["devAllowedDomains"] == ["http://localhost:3779"] and manifest["networkAccess"]["allowedDomains"] == ["none"]
    done = subprocess.run(["node", "--check", str(root / "code.js")], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_with_the_plugin_closed_xenio_asks_you_and_waits_and_the_agent_carries_on_when_it_is_back(bridge, tmp_path, monkeypatch):
    """A real job: the plugin was closed, and the agent tried four times, slept, and gave up after 37,000 tokens."""
    from app.foundry import engine as engine_module
    from app.foundry.engine import TeamTools

    monkeypatch.setattr(engine_module, "FIGMA_WAIT", 6)
    told = []
    engine = type("E", (), {"figma": bridge, "mcp": None, "mcp_allowed": set(), "bus": type("B", (), {"send": staticmethod(lambda *a, **k: told.append(a))})(), "store": type("S", (), {"event": staticmethod(lambda *a, **k: None)})()})()

    class Toolbox:
        def owns(self, tool):
            return False

        def _name(self, scope):
            return "UI designer"

        async def _confirm(self, scope, decision):
            return True

    tools = TeamTools(engine, None, Toolbox())
    later = threading.Timer(1.0, lambda: setattr(tools, "plugin", Plugin(bridge, lambda cmd: {"ok": True, "result": {"page": "Page 1", "frames": []}})))
    later.start()
    text, ok = asyncio.run(tools.call("figma", {"action": "read"}, {"workflow": "w1", "figma": True, "root": str(tmp_path)}))
    tools.plugin.stop = True
    assert ok and "Page 1" in text and len(told) == 1 and "Plugins, Development, XENIO" in told[0][4]


def test_if_you_do_not_open_it_the_agent_is_told_to_stop_not_to_retry(bridge, tmp_path, monkeypatch):
    from app.foundry import engine as engine_module
    from app.foundry.engine import TeamTools

    monkeypatch.setattr(engine_module, "FIGMA_WAIT", 2)
    engine = type("E", (), {"figma": bridge, "mcp": None, "mcp_allowed": set(), "bus": type("B", (), {"send": staticmethod(lambda *a, **k: None)})(), "store": type("S", (), {"event": staticmethod(lambda *a, **k: None)})()})()
    tools = TeamTools(engine, None, None)
    text, ok = asyncio.run(tools.call("figma", {"action": "read"}, {"workflow": "w1", "figma": True, "root": str(tmp_path)}))
    assert not ok and "Do not try again" in text
