"""Everyone has their own arrow, and they work at once. This is the real thing on the real Mac: real windows far off the screen, the real Accessibility API, and the real mouse position checked before and after. Nothing here moves your mouse or takes your keyboard."""

import asyncio
import ctypes
import json
import subprocess
import sys
import time

import pytest

from app.foundry import hands as hands_module
from app.foundry.computer import Computer, CursorLayer, YouWatcher

on_a_trusted_mac = pytest.mark.skipif(sys.platform != "darwin" or not hands_module.BINARY.is_file(), reason="needs a Mac and native/build/xenio-hands")


def pointer() -> tuple[float, float]:
    class Point(ctypes.Structure):
        _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]

    cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    cg.CGEventCreate.restype = ctypes.c_void_p
    cg.CGEventCreate.argtypes = [ctypes.c_void_p]
    cg.CGEventGetLocation.restype = Point
    cg.CGEventGetLocation.argtypes = [ctypes.c_void_p]
    event = cg.CGEventCreate(None)
    at = cg.CGEventGetLocation(event)
    return at.x, at.y


def trusted() -> bool:
    from app.desktop import macos

    return bool(macos.trusted())


class Target:
    def __init__(self, title, offset):
        self.proc = subprocess.Popen([str(hands_module.BINARY), "testwindow", "--title", title, "--offset", str(offset)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        assert json.loads(self.proc.stdout.readline())["ready"]
        self.title = title

    def close(self):
        self.proc.kill()
        self.proc.wait()

    def said(self):
        import select

        out = []
        while select.select([self.proc.stdout], [], [], 0.2)[0]:
            line = self.proc.stdout.readline()
            if not line:
                break
            out.append(json.loads(line))
        return out


@on_a_trusted_mac
class TestTheRealThing:
    @pytest.fixture
    def machine(self, tmp_path):
        if not trusted():
            pytest.skip("this app has no Accessibility permission")
        from app.desktop import macos

        if macos.session_locked():
            pytest.skip("the Mac is locked: XENIO touches nothing on a locked Mac, so there is nothing to test")
        from app.desktop.service import DesktopService

        async def yes(session, scope, decision):
            return True

        drawn = []

        class Arrows(CursorLayer):
            def show(self, name, x, y, action="move"):
                drawn.append((name, x, y, action))
                super().show(name, x, y, action)

        arrows = Arrows()
        computer = Computer(lambda: DesktopService(data_dir=tmp_path), yes, watcher=YouWatcher(idle=lambda: 0.0), cursors=arrows)  # you are always "busy" here: background work must not care
        computer.allowed.add("w")
        computer.drawn = drawn
        yield computer
        computer.close()

    def test_two_agents_work_at_once_each_with_their_own_arrow_and_your_mouse_stays_put(self, machine):
        a, b = Target("XENIO test one", 0), Target("XENIO test two", 700)
        time.sleep(0.5)
        before = pointer()
        try:
            async def agent(name, title, note):
                scope = {"workflow": "w", "role": name.lower(), "task": "t", "name": name}
                typed = await machine.act("screen_type", {"window": title, "field": "Note", "text": note}, scope)
                pressed = await machine.act("screen_click", {"window": title, "element": "Save"}, scope)
                return typed, pressed

            async def both():
                started = time.monotonic()
                done = await asyncio.gather(agent("Asha", a.title, "from asha"), agent("Ben", b.title, "from ben"))
                return done, time.monotonic() - started

            (first, second), seconds = asyncio.run(both())
            assert first[0][1] and first[1][1], first
            assert second[0][1] and second[1][1], second
            assert "mouse was not touched" in first[1][0] and "read back" in first[0][0]
            assert a.said() == [{"pressed": 1, "field": "from asha"}]  # the real app really did it
            assert b.said() == [{"pressed": 1, "field": "from ben"}]
            assert pointer() == before  # your mouse did not move
            names = {d[0] for d in machine.drawn}
            assert names == {"Asha", "Ben"}  # each has its own
            state = machine.state()
            assert {x["name"] for x in state["arrows"]} == {"Asha", "Ben"} and state["holder"] is None and all(x["how"] == "without touching your mouse" for x in state["arrows"])
        finally:
            a.close()
            b.close()

    def test_a_password_box_is_never_filled_this_way(self, machine):
        a = Target("XENIO test three", 1400)
        time.sleep(0.3)
        try:
            text, ok = asyncio.run(machine.act("screen_type", {"window": a.title, "field": "Password", "text": "x"}, {"workflow": "w", "role": "asha", "task": "t", "name": "Asha"}))
            assert not a.said()
        finally:
            a.close()
