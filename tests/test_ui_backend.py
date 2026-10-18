"""XENIO UI V1 backend: the local server's security, the assistant state the orb shows, chat over the real
orchestrator interface, approvals through the real PermissionGate, the V6 team, tasks, settings and voice.
Fakes stand in for Hermes and the speech services; the gate, the engineering service and the stores are real."""

import asyncio
import http.client
import json
import os
import threading
import time
from types import SimpleNamespace

import pytest

from app.safety.permissions import PermissionGate, PermissionRequest
from app.safety.policy import RiskLevel
from app.ui.approvals import ApprovalError, WebConfirmer
from app.ui.assistant import AssistantState, phase_for_tool
from app.ui.controller import UiController, UiError
from app.ui.events import EventHub
from app.ui.server import UiServer
from tests.test_engineering_service import repo, service as eng_service, settings  # noqa: F401 - fixtures


# ---- fakes ---------------------------------------------------------------------------------------------------------

class FakeOrchestrator:
    def __init__(self, reply="Done. Your folder is ready.", tools=(), ok=True, gate=None, ask=None):
        self.hermes = SimpleNamespace(agent_info={"name": "hermes-agent", "version": "0.22.2"}, connected=True)
        self.reply, self.tools, self.ok, self.gate, self.ask = reply, list(tools), ok, gate, ask
        self._busy = asyncio.Lock()
        self.started = self.interrupted = 0
        self.release = None

    @property
    def busy(self):
        return self._busy.locked()

    async def start(self):
        self.started += 1
        return True

    async def process_text(self, text, on_chunk=None, on_tool=None, context=None):
        async with self._busy:
            for title, kind in self.tools:
                on_tool(SimpleNamespace(tool_call_id=title, title=title, kind=kind, status="in_progress", detail=""))
                on_tool(SimpleNamespace(tool_call_id=title, title=title, kind=kind, status="completed", detail=""))
            if self.ask is not None:
                await self.gate.check(self.ask)
            if self.release is not None:
                await self.release.wait()
            on_chunk(self.reply)
            return SimpleNamespace(text=self.reply, ok=self.ok, error=None if self.ok else "hermes_error", cancelled=False,
                                   sources=[], action_lines=lambda: ["[done] xenio_create_directory: Projects"])

    async def interrupt(self):
        self.interrupted += 1

    async def new_session(self):
        return True

    async def close(self):
        pass


class FakeSTT:
    async def transcribe(self, audio, filename="audio.wav", content_type="audio/wav", **kw):
        self.got = (len(audio), content_type)
        return "  what time is it  "


class FakeTTS:
    async def synthesize(self, text):
        return b"RIFF" + b"\0" * 40 + text.encode()


@pytest.fixture
def stack(settings, tmp_path):  # noqa: F811
    """A real loop in a thread, the real gate with the web confirmer, the controller and the server."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    hub = EventHub(tmp_path / "ui" / "activity.jsonl")
    state = AssistantState()
    confirmer = WebConfirmer(hub.publish, timeout=5, on_change=lambda n: setattr(state, "approvals", n))
    gate = PermissionGate(confirmer, timeout=5)
    orch = FakeOrchestrator(gate=gate)
    ctl = UiController(settings, orch, gate, confirmer, hub, state, stt=FakeSTT(), tts=FakeTTS(),
                       hermes_home=tmp_path / "hermes")
    asyncio.run_coroutine_threadsafe(ctl.start(), loop).result(10)
    server = UiServer(ctl, port=0)
    server.start()
    yield SimpleNamespace(ctl=ctl, server=server, hub=hub, orch=orch, gate=gate, loop=loop, state=state)
    server.stop()
    loop.call_soon_threadsafe(loop.stop)


def call(server, method, path, body=None, token=True, host=None, origin="default", headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    h = {"Host": host or f"127.0.0.1:{server.port}"}
    if token:
        h["Authorization"] = f"Bearer {server.token if token is True else token}"
    if origin == "default" and method != "GET":
        h["Origin"] = f"http://127.0.0.1:{server.port}"
    elif origin not in ("default", None):
        h["Origin"] = origin
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        h["Content-Type"] = "audio/wav" if isinstance(body, bytes) else "application/json"
    h.update(headers or {})
    conn.request(method, path, body=data, headers=h)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    try:
        return resp.status, json.loads(raw), dict(resp.getheaders())
    except ValueError:
        return resp.status, raw, dict(resp.getheaders())


def wait_for(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ---- the server's boundary -----------------------------------------------------------------------------------------

def test_only_this_page_with_the_token_reaches_the_api(stack):
    s = stack.server
    assert call(s, "GET", "/api/v1/snapshot", token=False)[0] == 401
    assert call(s, "GET", "/api/v1/snapshot", token="wrong-token")[0] == 401
    assert call(s, "GET", "/api/v1/snapshot", host="evil.example:80")[0] == 421  # DNS rebinding
    status, body, _ = call(s, "POST", "/api/v1/chat", {"text": "hi"}, origin="https://evil.example")
    assert status == 403 and "XENIO's own page" in body["error"]
    status, snap, headers = call(s, "GET", "/api/v1/snapshot")
    assert status == 200 and snap["assistant"]["state"] == "IDLE" and headers["Cache-Control"] == "no-store"
    assert snap["demo"] is False and s.token not in json.dumps(snap)


def test_the_page_is_served_with_a_strict_policy_and_never_outside_its_folder(stack, tmp_path):
    s = stack.server
    status, body, headers = call(s, "GET", "/", token=False)
    assert status == 200 and "script-src 'self'" in headers["Content-Security-Policy"]
    assert headers["X-Frame-Options"] == "DENY"
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>XENIO</title>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    (tmp_path / "secret.txt").write_text("nope")
    s.dist = dist
    assert call(s, "GET", "/assets/app.js", token=False)[2]["Content-Type"].startswith("text/javascript")
    status, body, _ = call(s, "GET", "/../secret.txt", token=False)
    assert b"nope" not in body
    assert call(s, "DELETE", "/", token=False)[0] == 405


def test_errors_are_messages_never_stack_traces(stack):
    s = stack.server
    status, body, _ = call(s, "POST", "/api/v1/chat", b"{not json", headers={"Content-Type": "application/json"})
    assert status == 400 and "JSON" in body["error"]
    assert call(s, "GET", "/api/v2/snapshot")[0] == 404
    assert call(s, "POST", "/api/v1/chat", {"text": "x" * 70000})[0] == 413


# ---- chat and the orb's states -------------------------------------------------------------------------------------

def test_a_request_streams_real_states_and_keeps_no_text_in_the_activity_log(stack):
    s, hub = stack.server, stack.hub
    stack.orch.tools = [("web_search: weather Delhi", "search"), ("xenio_create_directory: Projects", "edit")]
    status, body, _ = call(s, "POST", "/api/v1/chat", {"text": "Create my secret-plans folder"})
    assert status == 202 and body["turn"]
    assert wait_for(lambda: any(e["type"] == "chat.done" for e in hub.since(0)[0]))
    events = hub.since(0)[0]
    states = [e["data"]["state"] for e in events if e["type"] == "assistant.state"]
    assert "PROCESSING" in states and "SEARCHING" in states and "EXECUTING" in states
    assert states.index("SEARCHING") < states.index("EXECUTING")
    done = next(e for e in events if e["type"] == "chat.done")["data"]
    assert done["ok"] and done["text"] == "Done. Your folder is ready." and done["actions"]
    assert stack.state.derive() == "COMPLETED"  # a short moment, then IDLE
    stack.state.completed_at -= 10
    assert stack.state.derive() == "IDLE"
    activity = (stack.ctl.settings.data_dir.parent / "ui" / "activity.jsonl")
    log = (stack.hub._log_path).read_text()
    assert "secret-plans" not in log and '"chars": 29' in log


def test_one_request_at_a_time_and_empty_or_paused_requests_are_refused(stack):
    s = stack.server
    assert call(s, "POST", "/api/v1/chat", {"text": "   "})[0] == 422
    stack.orch.release = asyncio.Event()
    assert call(s, "POST", "/api/v1/chat", {"text": "first"})[0] == 202
    assert wait_for(lambda: stack.orch.busy)
    status, body, _ = call(s, "POST", "/api/v1/chat", {"text": "second"})
    assert status == 409 and "still answering" in body["error"]
    assert call(s, "POST", "/api/v1/chat/cancel", {})[1] == {"cancelled": True} and stack.orch.interrupted == 1
    stack.loop.call_soon_threadsafe(stack.orch.release.set)
    assert wait_for(lambda: not stack.orch.busy)
    assert call(s, "POST", "/api/v1/assistant/pause", {"paused": True})[1]["state"] == "PAUSED"
    assert call(s, "POST", "/api/v1/chat", {"text": "hello"})[0] == 409
    assert call(s, "POST", "/api/v1/assistant/pause", {"paused": False})[1]["state"] != "PAUSED"


def test_a_failed_request_shows_an_error_until_the_next_one(stack):
    stack.orch.ok, stack.orch.reply = False, "Hermes is unavailable."
    call(stack.server, "POST", "/api/v1/chat", {"text": "hi"})
    assert wait_for(lambda: stack.state.derive() == "ERROR")
    time.sleep(0.05)
    assert stack.state.derive() == "ERROR"  # not hidden behind a calm animation
    assert call(stack.server, "POST", "/api/v1/assistant/dismiss", {})[1]["state"] == "IDLE"


def test_state_priorities_and_tool_phases():
    s = AssistantState(connection="connected")
    assert s.derive() == "IDLE"
    assert phase_for_tool("web_extract: https://x") == "SEARCHING" and phase_for_tool("xenio_gui: click") == "EXECUTING"
    assert phase_for_tool("xenio_engineering: start") == "PLANNING" and phase_for_tool("custom", "read") == "SEARCHING"
    s.set_background([{"status": "running", "running": [{"task": "a"}, {"task": "b"}]}])
    assert s.derive() == "ORCHESTRATING" and s.snapshot()["background"]["agents"] == 2
    s.turn_started("t1")
    assert s.derive() == "PROCESSING"  # the foreground wins; the background stays reported
    s.approvals = 1
    assert s.derive() == "WAITING_APPROVAL"
    s.paused = True
    assert s.derive() == "PAUSED"
    s.connection = "disconnected"
    assert s.derive() == "DISCONNECTED"
    quiet = AssistantState(connection="connected")
    quiet.set_background([{"status": "planning", "running": []}])
    assert quiet.derive() == "PLANNING"
    quiet.set_background([{"status": "done", "running": []}])
    assert quiet.derive() == "IDLE"  # nothing running: never shown as busy


# ---- approvals: the gate decides ------------------------------------------------------------------------------------

def test_approvals_are_backend_decisions(stack):
    s = stack.server
    request = PermissionRequest("terminal.install", "run: pip install requests", RiskLevel.CONFIRM_REQUIRED, "hermes")
    future = asyncio.run_coroutine_threadsafe(stack.gate.check(request), stack.loop)
    assert wait_for(lambda: len(stack.ctl.confirmer.pending()) == 1)
    assert stack.state.derive() == "WAITING_APPROVAL"
    pending = call(s, "GET", "/api/v1/approvals")[1]["pending"][0]
    assert pending["description"] == "run: pip install requests" and pending["risk"] == "CONFIRM_REQUIRED"
    assert call(s, "POST", f"/api/v1/approvals/{pending['id']}", {"decision": "maybe"})[0] == 422
    assert call(s, "POST", f"/api/v1/approvals/{pending['id']}", {"decision": "deny"})[0] == 200
    assert future.result(5).approved is False
    assert call(s, "POST", f"/api/v1/approvals/{pending['id']}", {"decision": "approve"})[0] == 404  # already answered
    assert call(s, "GET", "/api/v1/approvals")[1]["history"][0]["status"] == "denied"


def test_high_risk_needs_the_word_confirm_and_silence_is_never_a_yes(stack):
    s = stack.server
    request = PermissionRequest("terminal.delete", "permanently delete ~/old", RiskLevel.HIGH_RISK, "hermes")
    future = asyncio.run_coroutine_threadsafe(stack.gate.check(request), stack.loop)
    assert wait_for(lambda: stack.ctl.confirmer.pending())
    aid = stack.ctl.confirmer.pending()[0]["id"]
    status, body, _ = call(s, "POST", f"/api/v1/approvals/{aid}", {"decision": "approve"})
    assert status == 422 and "confirm" in body["error"]
    assert call(s, "POST", f"/api/v1/approvals/{aid}", {"decision": "approve", "confirm_text": "Confirm"})[0] == 200
    assert future.result(5).approved is True
    quiet = PermissionRequest("web.form", "submit the form", RiskLevel.CONFIRM_REQUIRED, "hermes")
    stack.gate._timeout = 0.3
    assert asyncio.run_coroutine_threadsafe(stack.gate.check(quiet), stack.loop).result(5).approved is False
    assert stack.ctl.confirmer.history()[0]["status"] == "expired"


def test_typed_text_never_reaches_the_activity_file(tmp_path):
    hub = EventHub(tmp_path / "activity.jsonl")
    hub.publish("approval.requested", {"description": "send the message «my bank PIN is 4711» in WhatsApp (press enter)"})
    stored = (tmp_path / "activity.jsonl").read_text()
    assert "4711" not in stored and "«…»" in stored
    assert hub.since(0)[0][0]["data"]["description"].endswith("(press enter)")  # the live card still shows it


def test_an_unknown_approval_is_refused():
    confirmer = WebConfirmer(lambda *a: None)
    with pytest.raises(ApprovalError):
        confirmer.decide("abcdef", "approve")


# ---- the event stream ----------------------------------------------------------------------------------------------

def test_the_stream_replays_what_a_reconnecting_page_missed(stack):
    hub = stack.hub
    first = hub.publish("test.one", {})
    hub.publish("test.two", {})
    conn = http.client.HTTPConnection("127.0.0.1", stack.server.port, timeout=5)
    conn.request("GET", f"/api/v1/events?since={first['seq']}", headers={
        "Host": f"127.0.0.1:{stack.server.port}", "Authorization": f"Bearer {stack.server.token}"})
    resp = conn.getresponse()
    assert resp.status == 200 and resp.getheader("Content-Type").startswith("text/event-stream")
    line = b""
    while b"test.two" not in line:
        line += resp.fp.readline()
    conn.close()
    assert b"test.one" not in line
    small = EventHub(keep=3)
    for _ in range(10):
        small.publish("x", {})
    assert small.since(1)[1] is True  # too old: the page reloads its snapshot


# ---- the engineering team ------------------------------------------------------------------------------------------

def test_runs_and_agents_come_from_the_v6_service(settings, tmp_path, repo):  # noqa: F811
    svc = eng_service(settings, tmp_path / "svc")

    async def go():
        await svc.dispatch("start", {"request": "Fix the typo in README.md", "path": str(repo)})
        for _ in range(200):
            if svc.runs and next(iter(svc.runs.values())).status in ("done", "failed"):
                break
            await asyncio.sleep(0.05)

    asyncio.run(go())
    hub, state = EventHub(), AssistantState(connection="connected")
    ctl = UiController(settings, FakeOrchestrator(), None, WebConfirmer(hub.publish), hub, state, engineering=svc,
                       hermes_home=tmp_path / "hermes")
    runs = ctl.runs()["runs"]
    assert runs[0]["status"] == "done" and runs[0]["tasks"] == {"succeeded": 1} and runs[0]["live"] is True
    detail = ctl.run_detail(runs[0]["id"])
    assert detail["tasks"][0]["role"] and detail["events"] and "repo_state" in detail
    agents = ctl.agents()["agents"]
    assert len(agents) == 8 and all(a["state"] in ("idle", "working", "blocked", "disabled") for a in agents)
    assert not any(a["state"] == "working" for a in agents)  # nothing is running: nobody is shown as working
    with pytest.raises(UiError):
        ctl.run_detail("no-such-run")
    ctl._watch_engineering()
    assert state.derive() == "IDLE" and any(e["type"] == "engineering.run" for e in hub.since(0)[0])


# ---- tasks, settings, voice, secrets ---------------------------------------------------------------------------------

def test_tasks_are_xenios_own_store(stack, tmp_path, monkeypatch):
    monkeypatch.setenv("XENIO_DATA_DIR", str(tmp_path / "personal"))
    s = stack.server
    assert call(s, "POST", "/api/v1/tasks", {"title": ""})[0] == 422
    assert call(s, "POST", "/api/v1/tasks", {"title": "x", "due": "tomorrow"})[0] == 422
    status, task, _ = call(s, "POST", "/api/v1/tasks", {"title": "Renew passport", "priority": "high", "due": "2026-11-02"})
    assert status == 201 and task["status"] == "pending"
    assert call(s, "POST", f"/api/v1/tasks/{task['id']}", {"action": "complete"})[1]["status"] == "done"
    assert call(s, "GET", "/api/v1/tasks")[1]["tasks"][0]["title"] == "Renew passport"
    assert call(s, "DELETE", f"/api/v1/tasks/{task['id']}")[0] == 200
    assert call(s, "DELETE", f"/api/v1/tasks/{task['id']}")[0] == 404


def test_settings_are_validated_and_kept(stack):
    s = stack.server
    assert call(s, "PUT", "/api/v1/settings", {"motion": "wild"})[0] == 422
    assert call(s, "PUT", "/api/v1/settings", {"api_key": "x"})[0] == 422
    assert call(s, "PUT", "/api/v1/settings", {"motion": "reduced", "speak_replies": False})[1]["motion"] == "reduced"
    assert call(s, "GET", "/api/v1/settings")[1]["speak_replies"] is False


def test_voice_uses_the_existing_speech_services(stack):
    s = stack.server
    status, body, _ = call(s, "POST", "/api/v1/voice/transcribe", b"RIFF....WAVEfmt ")
    assert status == 200 and body == {"text": "what time is it"}
    status, audio, headers = call(s, "POST", "/api/v1/voice/speak", {"text": "**Hello** there"})
    assert status == 200 and headers["Content-Type"] == "audio/wav" and audio.startswith(b"RIFF")
    assert b"**" not in audio  # markdown is never read aloud
    stack.ctl.stt = None
    assert call(s, "POST", "/api/v1/voice/transcribe", b"RIFF")[0] == 503


def test_secrets_never_reach_the_page(stack, monkeypatch):
    secret = "sk-or-v1-0123456789abcdef0123456789"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)
    from app.logging_setup import Redactor
    from app.safety.policy import secret_env_values

    stack.ctl.redact = Redactor(secret_env_values())
    stack.orch.tools = [(f"terminal: echo {secret}", "execute")]
    stack.orch.reply = f"Your key is {secret}"
    call(stack.server, "POST", "/api/v1/chat", {"text": "show my key"})
    assert wait_for(lambda: any(e["type"] == "chat.done" for e in stack.hub.since(0)[0]))
    stream = json.dumps(stack.hub.since(0)[0])
    assert secret not in json.dumps([e for e in stack.hub.since(0)[0] if e["type"] in ("chat.tool", "chat.done")])
    assert secret not in json.dumps(call(stack.server, "GET", "/api/v1/snapshot")[1])
