"""XENIO Orb (the floating orb and summon shortcut): XENIO starts it with the session's address on its stdin (never on
the command line), restarts it after a crash but not forever, stops it by closing that pipe, and reports in words what
it could set up. The native helper's own triple-Space logic is checked by its built-in self-test."""

import io
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from app import overlay as O
from tests.test_ui_backend import call, stack  # noqa: F401 - fixtures
from tests.test_engineering_service import repo, settings  # noqa: F401 - fixtures


class FakeProc:
    def __init__(self, exits_with=None, err=""):
        self.stdin = io.StringIO()
        self.stdin.close = lambda: setattr(self, "closed", True)  # keep what was written readable
        self.stderr = io.StringIO(err)
        self.pid = 4242
        self.closed = False
        self._code = exits_with
        self._done = threading.Event()
        if exits_with is not None:
            self._done.set()

    def poll(self):
        return self._code if self._done.is_set() else None

    def wait(self, timeout=None):
        if not self._done.wait(timeout if timeout is not None else 5):
            raise subprocess.TimeoutExpired("orb", timeout)
        return self._code

    def kill(self):
        self._code = -9
        self._done.set()


def helper(tmp_path, procs, **kw):
    made = []

    def popen(args, **options):
        proc = procs.pop(0) if procs else FakeProc()
        made.append((args, options, proc))
        return proc

    h = O.OrbHelper("http://127.0.0.1:8765/?view=orb#token=SECRET", tmp_path / "orb.lock", binary=tmp_path / "XenioOrb",
                    popen=popen, **kw)
    h.restart_delay = 0
    return h, made


def test_starts_with_the_address_on_stdin_and_stops_by_closing_it(tmp_path, monkeypatch):
    monkeypatch.setattr(O, "supported", lambda: True)
    h, made = helper(tmp_path, [])
    assert h.start() and h.running and h.info()["running"]
    args, options, proc = made[0]
    assert args == [str(tmp_path / "XenioOrb")] and "SECRET" not in " ".join(args)  # never on the command line
    sent = json.loads(proc.stdin.getvalue().strip())
    assert sent == {"url": "http://127.0.0.1:8765/?view=orb#token=SECRET", "lock": str(tmp_path / "orb.lock")}
    proc._code = 0
    threading.Timer(0.05, proc._done.set).start()
    h.stop()
    assert proc.closed and not h.running


def test_restarts_after_a_crash_but_not_forever(tmp_path, monkeypatch):
    monkeypatch.setattr(O, "supported", lambda: True)
    crashing = [FakeProc(exits_with=1, err="boom") for _ in range(6)]
    h, made = helper(tmp_path, crashing)
    h.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and "kept stopping" not in h.problem:
        time.sleep(0.02)
    assert "kept stopping" in h.problem and len(made) == O.MAX_RESTARTS + 1


def test_a_second_orb_is_reported_not_restarted(tmp_path, monkeypatch):
    monkeypatch.setattr(O, "supported", lambda: True)
    h, made = helper(tmp_path, [FakeProc(exits_with=3, err="XENIO Orb: already running\n")])
    h.start()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not h.problem:
        time.sleep(0.02)
    assert h.problem == "another XENIO's orb is already running" and len(made) == 1


def test_build_problems_are_reported_in_words(tmp_path, monkeypatch):
    monkeypatch.setattr(O, "supported", lambda: True)
    monkeypatch.setattr(O, "BINARY", tmp_path / "XenioOrb")  # the default binary: built on first use
    h = O.OrbHelper("u", tmp_path / "l", binary=tmp_path / "XenioOrb", builder=lambda: "xcode-select --install needed")
    monkeypatch.setattr(O, "needs_build", lambda: True)
    assert not h.start() and "could not be built: xcode-select" in h.problem
    monkeypatch.setattr(O, "supported", lambda: False)
    assert not O.OrbHelper("u", tmp_path / "l").start()


@pytest.mark.skipif(sys.platform != "darwin" or not O.BINARY.exists(), reason="needs the built helper (native/orb/build.sh)")
def test_the_built_helpers_triple_space_logic():
    out = subprocess.run([str(O.BINARY), "--selftest"], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0 and "selftest ok" in out.stdout, out.stdout


# ---- what the helper reports reaches Settings and --health ----------------------------------------------------------

def test_status_and_timings_reach_diagnostics(stack):  # noqa: F811
    stack.ctl.overlay = O.OrbHelper("u", Path("/tmp/x"), binary=Path("/nonexistent"))
    status, _, _ = call(stack.server, "POST", "/api/v1/overlay/status",
                        {"shortcut": "triple_space", "shortcut_status": "needs_accessibility", "accessibility": False,
                         "screens": 2, "evil": "<script>"})
    assert status == 200
    info = stack.ctl.overlay_info()
    assert info["status"] == {"shortcut": "triple_space", "shortcut_status": "needs_accessibility",
                              "accessibility": False, "screens": 2}
    for ms in (180, 120, 150):
        assert call(stack.server, "POST", "/api/v1/overlay/metric", {"name": "listening_to_orb_ms", "ms": ms})[0] == 200
    assert call(stack.server, "POST", "/api/v1/overlay/metric", {"name": "anything", "ms": 1})[0] == 422
    assert stack.ctl.overlay_info()["timings"]["listening_to_orb_ms"] == {"last": 150, "median": 150, "n": 3}
    checks = {c["name"]: c for c in stack.ctl.health(force=True)["checks"]}
    if sys.platform == "darwin":
        assert checks["Summon shortcut"]["status"] == "WARN" and "Accessibility" in checks["Summon shortcut"]["detail"]
        assert checks["Floating orb"]["status"] == "WARN"


def test_orb_settings_are_validated(stack):  # noqa: F811
    ok = call(stack.server, "PUT", "/api/v1/settings", {"summon_shortcut": "ctrl_option_space", "summon_interval_ms": 600,
                                                         "orb_overlay_mode": "always"})
    assert ok[0] == 200 and ok[1]["summon_interval_ms"] == 600
    assert call(stack.server, "PUT", "/api/v1/settings", {"summon_interval_ms": 50})[0] == 422
    assert call(stack.server, "PUT", "/api/v1/settings", {"summon_shortcut": "cmd_q"})[0] == 422


def test_control_c_in_xenios_terminal_is_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(O, "supported", lambda: True)
    h, made = helper(tmp_path, [FakeProc(exits_with=-2), FakeProc(exits_with=-15)])  # SIGINT reaches the whole group
    h.start()
    time.sleep(0.2)
    assert len(made) == 1 and not h.problem  # not restarted while XENIO stops, not reported as a failure


def test_xenio_can_summon_the_quick_chat_through_the_helpers_stdin(tmp_path, monkeypatch, stack):  # noqa: F811
    monkeypatch.setattr(O, "supported", lambda: True)
    h, made = helper(tmp_path, [])
    assert call(stack.server, "POST", "/api/v1/overlay/summon", {"mode": "chat"})[0] == 409  # no orb running
    stack.ctl.overlay = h
    h.start()
    proc = made[0][2]
    assert call(stack.server, "POST", "/api/v1/overlay/summon", {"mode": "chat"})[0] == 200
    assert call(stack.server, "POST", "/api/v1/overlay/summon", {})[0] == 200
    assert call(stack.server, "POST", "/api/v1/overlay/summon", {"mode": "rm -rf"})[0] == 422
    lines = [json.loads(line) for line in proc.stdin.getvalue().strip().splitlines()]
    assert lines[1:] == [{"type": "summon", "mode": "chat"}, {"type": "summon"}]
    with pytest.raises(ValueError):
        h.send({"type": "terminate"})  # only the helper's own commands
    proc._code = 0
    proc._done.set()
    h.stop()
    assert not h.send({"type": "summon"})
