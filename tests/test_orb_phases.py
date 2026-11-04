"""The orb says what is true now, and nothing can leave it behind. Each of these once could: a tool finishing under another title, tools side by side, a turn
that ended without saying so."""

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from app.hermes.client import ToolActivity
from app.safety import policy as p
from app.safety.permissions import PermissionGate
from app.ui.approvals import WebConfirmer
from app.ui.assistant import AssistantState
from app.ui.controller import UiController
from app.ui.events import EventHub

PATHS = p.default_path_policy()


def turn() -> AssistantState:
    state = AssistantState(connection="connected")
    state.turn_started("t1")
    return state


class TestToolPhases:
    def test_a_call_that_finishes_under_another_title_still_ends(self):
        state = turn()
        state.tool_changed("terminal", "execute", "in_progress", "c1")
        assert state.derive() == "EXECUTING"
        state.tool_changed("terminal_v2: ls -la", "execute", "completed", "c1")
        assert state.derive() == "THINKING" and state.tool == ""

    @pytest.mark.parametrize("ending", ["completed", "failed"])
    def test_side_by_side_it_shows_one_that_is_really_running(self, ending):
        state = turn()
        state.tool_changed("web_search: a", "search", "in_progress", "c1")
        state.tool_changed("xenio_gui: click", "other", "in_progress", "c2")
        assert state.derive() == "EXECUTING" and state.tool == "xenio_gui"
        state.tool_changed("xenio_gui: click", "other", ending, "c2")
        assert state.derive() == "SEARCHING" and state.tool == "web_search"
        state.tool_changed("web_search: a", "search", ending, "c1")
        assert state.derive() == "THINKING" and state.tool == ""

    def test_the_first_one_ending_does_not_hide_the_other(self):
        state = turn()
        state.tool_changed("web_search: a", "search", "in_progress", "c1")
        state.tool_changed("xenio_gui: click", "other", "in_progress", "c2")
        state.tool_changed("web_search: a", "search", "completed", "c1")
        assert state.derive() == "EXECUTING" and state.tool == "xenio_gui"

    def test_an_update_without_a_title_changes_nothing(self):
        state = turn()
        state.tool_changed("web_search: a", "search", "pending", "c1")
        state.tool_changed("", "", "in_progress", "c1")
        assert state.derive() == "SEARCHING" and state.tool == "web_search"

    def test_a_tool_never_changes_anything_after_the_turn(self):
        state = turn()
        state.turn_finished(True)
        state.tool_changed("web_search: a", "search", "in_progress", "c1")
        assert state.derive() == "COMPLETED" and state.running == {}

    def test_the_next_turn_starts_clean(self):
        state = turn()
        state.tool_changed("web_search: a", "search", "in_progress", "c1")
        state.turn_finished(False, "it broke")
        assert state.derive() == "ERROR"
        state.turn_started("t2")
        assert state.derive() == "PROCESSING" and state.running == {} and state.error == ""

    def test_a_hands_step_goes_with_its_tool(self):
        state = turn()
        state.tool_changed("xenio_gui: click", "other", "in_progress", "c1")
        state.action = {"state": "acting"}
        state.tool_changed("xenio_gui: click", "other", "completed", "c1")
        assert state.action is None

    def test_done_runs_its_course(self):
        now = [100.0]
        state = AssistantState(connection="connected", clock=lambda: now[0])
        state.turn_started("t1")
        state.turn_finished(True)
        assert state.derive() == "COMPLETED"
        now[0] += 10
        assert state.derive() == "IDLE"


class Orchestrator:
    hermes = SimpleNamespace(agent_info={}, connected=True)

    def __init__(self, script):
        self.script, self.busy = script, False
        self.fast = None

    async def start(self):
        return True

    async def process_text(self, text, on_chunk=None, on_tool=None, context=None, **extra):
        return await self.script(on_chunk, on_tool)


@pytest.fixture
def stack(make_settings, tmp_path):
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    made = []

    def build(script):
        hub, state = EventHub(tmp_path / "ui" / "a.jsonl"), AssistantState()
        confirmer = WebConfirmer(hub.publish, timeout=5, on_change=lambda n: None)
        ctl = UiController(make_settings(), Orchestrator(script), PermissionGate(confirmer, timeout=5), confirmer, hub, state, hermes_home=tmp_path / "h")
        asyncio.run_coroutine_threadsafe(ctl.start(), loop).result(10)
        made.append(ctl)
        return SimpleNamespace(ctl=ctl, state=state, hub=hub, loop=loop)

    yield build
    loop.call_soon_threadsafe(loop.stop)


def states_published(hub) -> list[str]:
    out = []
    for event in hub.since(0)[0]:
        if event["type"] == "assistant.state" and (not out or out[-1] != event["data"]["state"]):
            out.append(event["data"]["state"])
    return out


def wait_until(check, seconds=3.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if check():
            return True
        time.sleep(0.01)
    return False


def reply(text="ok", ok=True):
    return SimpleNamespace(text=text, ok=ok, error=None if ok else "failed", cancelled=False, sources=[], attachments=[], action_lines=lambda: [])


class TestAWholeTurn:
    def test_every_phase_is_published_in_order_and_it_ends(self, stack):
        async def script(on_chunk, on_tool):
            await asyncio.sleep(0.01)
            on_tool(ToolActivity("c1", "web_search: x", "search", "in_progress"))
            on_tool(ToolActivity("c1", "web_search: x", "search", "completed"))
            on_tool(ToolActivity("c2", "xenio_gui: click", "other", "in_progress"))
            on_tool(ToolActivity("c2", "something else", "other", "completed"))
            on_chunk("hello")
            return reply()

        s = stack(script)
        s.ctl.chat("hi", quiet=True)
        assert wait_until(lambda: s.state.turn is None)
        seen = states_published(s.hub)
        at = seen.index("PROCESSING")
        assert seen[at:] == ["PROCESSING", "SEARCHING", "THINKING", "EXECUTING", "THINKING", "COMPLETED"]

    def test_a_failed_turn_says_so(self, stack):
        async def script(on_chunk, on_tool):
            raise RuntimeError("boom")

        s = stack(script)
        s.ctl.chat("hi", quiet=True)
        assert wait_until(lambda: s.state.turn is None)
        assert states_published(s.hub)[-1] == "ERROR" and s.ctl.dismiss_error()["state"] != "ERROR"

    def test_a_turn_cancelled_underneath_does_not_leave_the_orb_thinking(self, stack):
        gate = asyncio.Event()

        async def script(on_chunk, on_tool):
            await gate.wait()

        s = stack(script)
        s.ctl.chat("hi", quiet=True)
        assert wait_until(lambda: s.state.turn is not None and s.ctl._turn_task is not None)
        s.loop.call_soon_threadsafe(s.ctl._turn_task.cancel)
        assert wait_until(lambda: s.state.turn is None), "the orb stayed on the turn"
        assert s.state.snapshot()["state"] == "IDLE"
        done = [e for e in s.hub.since(0)[0] if e["type"] == "chat.done"]
        assert done and done[-1]["data"]["cancelled"] is True  # the page stops showing it as streaming too
        assert not s.ctl.turn_running()

    def test_the_watcher_clears_a_turn_that_ended_without_saying_so(self, stack):
        async def script(on_chunk, on_tool):
            return reply()

        s = stack(script)
        s.ctl.chat("hi", quiet=True)
        assert wait_until(lambda: s.state.turn is None)
        s.state.turn_started("a turn nobody finished")
        watcher = asyncio.run_coroutine_threadsafe(s.ctl.watch(), s.loop)
        try:
            assert wait_until(lambda: s.state.turn is None), "still stuck"
        finally:
            watcher.cancel()

    def test_the_watcher_never_ends_a_turn_that_is_running(self, stack):
        gate = asyncio.Event()

        async def script(on_chunk, on_tool):
            await gate.wait()
            return reply()

        s = stack(script)
        s.ctl.chat("hi", quiet=True)
        assert wait_until(lambda: s.state.turn is not None)
        watcher = asyncio.run_coroutine_threadsafe(s.ctl.watch(), s.loop)
        time.sleep(0.2)
        try:
            assert s.state.turn is not None
        finally:
            watcher.cancel()
            s.loop.call_soon_threadsafe(gate.set)

    def test_a_lesson_tells_the_orb_it_is_being_written(self, stack, tmp_path):
        from app.fastpath import FastPath
        from app.teacher.engine import Teacher
        from app.teacher.store import LearningStore
        from tests.test_teacher import FakeNabh

        teacher = Teacher(LearningStore(tmp_path / "l.db"), nabh_factory=lambda: FakeNabh())
        teacher.review_in_background = False
        fast = FastPath(PATHS, teacher=teacher)
        seen = []

        async def script(on_chunk, on_tool):
            def tell(activity):
                seen.append((activity.title, activity.status))
                on_tool(activity)

            fast.progress, fast.on_tool = on_chunk, tell
            made = await fast.handle("make a full lesson on photosynthesis")
            on_chunk(made.text)
            return reply(made.text)

        s = stack(script)
        s.ctl.chat("make a full lesson on photosynthesis", quiet=True)
        assert wait_until(lambda: s.state.turn is None, 8)
        assert seen == [("xenio_teach: writing the lesson", "in_progress"), ("xenio_teach: writing the lesson", "completed")]
        published = states_published(s.hub)
        assert "EXECUTING" in published and published[-1] == "COMPLETED"
