"""Teaching in the chat: the model decides when someone wants to learn, gives one step, and offers what could come next as buttons. The line that carries them is never shown or spoken."""

import asyncio
from types import SimpleNamespace

import pytest

from app import followups
from app.followups import StreamFilter, split
from app.orchestrator import Orchestrator
from app.safety import policy
from app.teacher.modes import TEACHING_NOTE


class TestTheLine:
    def test_it_comes_out_and_becomes_choices(self):
        words, choices = split("Recursion is a function calling itself.\n\n<<next: Walk through factorial(3) | Show it as a diagram | Let me try one>>")
        assert words == "Recursion is a function calling itself."
        assert choices == ["Walk through factorial(3)", "Show it as a diagram", "Let me try one"]

    @pytest.mark.parametrize("text", ["No line here.", "", "Compare a << b and c >> d."])
    def test_ordinary_words_are_left_alone(self, text):
        assert split(text) == (text, [])

    def test_at_most_three_short_different_ones(self):
        _, choices = split("x <<next: a | A | b | c | d | " + "long " * 40 + ">>")
        assert choices == ["a", "b", "c"]
        assert all(len(c) <= followups.MAX_CHARS for c in split("x <<next: " + "long " * 40 + ">>")[1])

    def test_a_line_cut_off_at_the_end_is_still_taken_out(self):
        words, choices = split("All of it. <<next: One | Tw")
        assert words == "All of it." and choices == ["One", "Tw"]

    def test_nothing_offered_is_no_buttons(self):
        assert split("Words. <<next: >>") == ("Words.", [])


class TestWhileStreaming:
    def run(self, pieces):
        f = StreamFilter()
        out = "".join(f.feed(p) for p in pieces)
        return out + f.flush()

    @pytest.mark.parametrize("size", [1, 2, 3, 5, 7, 11])
    def test_the_line_never_shows_however_it_is_split(self, size):
        text = "It calls itself.\n<<next: One | Two>>"
        pieces = [text[i:i + size] for i in range(0, len(text), size)]
        assert self.run(pieces) == "It calls itself."

    def test_words_that_only_look_like_it_are_shown(self):
        assert self.run(["a <", "< b ", "and <<", "nope"]) == "a << b and <<nope"

    def test_a_held_back_end_is_not_lost(self):
        assert self.run(["ends with <<"]) == "ends with <<"

    def test_nothing_comes_after_it(self):
        f = StreamFilter()
        assert f.feed("x <<next: a>>") == "x" and f.feed(" more") == "" and f.flush() == ""


class TestTheNote:
    def test_it_tells_the_model_to_decide_and_how_to_offer(self):
        for words in ("decide for yourself", "one idea at a time", "Never ask questions out of nowhere", "mermaid", "## headings", "never LaTeX", "<<next:", "never ask them to start anything", "never invent facts or sources", "xenio_teach show", "When speaking aloud"):
            assert words in TEACHING_NOTE

    def test_it_is_never_taken_for_the_users_words(self):
        assert TEACHING_NOTE.count("]") == 1 and TEACHING_NOTE.endswith("]")
        assert policy.own_words(f"what is gravity\n\n{TEACHING_NOTE}") == "what is gravity"

    def test_it_does_not_look_like_a_request_for_a_picture_or_a_form(self):
        assert not policy.wants_image(f"what is gravity\n\n{TEACHING_NOTE}")

    def test_the_plugin_adds_it_for_people_and_not_for_scheduled_runs(self, monkeypatch):
        import importlib.util
        import os
        import sys

        root = os.path.join(os.path.dirname(__file__), "..")
        monkeypatch.setitem(sys.modules, "app.tools.teacher", SimpleNamespace())
        spec = importlib.util.spec_from_file_location("guard_for_note", os.path.join(root, "hermes_plugin/xenio-guard/__init__.py"), submodule_search_locations=[os.path.join(root, "hermes_plugin/xenio-guard")])
        guard = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, "guard_for_note", guard)
        spec.loader.exec_module(guard)
        assert guard._teaching_note("session-1") == TEACHING_NOTE and guard._teaching_note("cron_1") == ""


class TestInTheApp:
    def test_the_message_carries_the_buttons_and_history_keeps_them(self, make_settings, tmp_path):
        import threading
        import time

        from app.safety.permissions import PermissionGate
        from app.ui.approvals import WebConfirmer
        from app.ui.assistant import AssistantState
        from app.ui.controller import UiController
        from app.ui.events import EventHub

        class Script:
            hermes = SimpleNamespace(agent_info={}, connected=True)
            busy = False

            async def start(self):
                return True

            async def new_session(self):
                return True

            async def process_text(self, text, on_chunk=None, on_tool=None, context=None, **extra):
                return SimpleNamespace(text="Gravity is a pull.", next=["An example", "A diagram"], ok=True, error=None, cancelled=False, sources=[], attachments=[], action_lines=lambda: [])

        loop = asyncio.new_event_loop()
        threading.Thread(target=loop.run_forever, daemon=True).start()
        hub, state = EventHub(tmp_path / "ui" / "a.jsonl"), AssistantState()
        confirmer = WebConfirmer(hub.publish, timeout=5, on_change=lambda n: None)
        ctl = UiController(make_settings(), Script(), PermissionGate(confirmer, timeout=5), confirmer, hub, state, hermes_home=tmp_path / "h")
        asyncio.run_coroutine_threadsafe(ctl.start(), loop).result(10)
        ctl.chat("what is gravity", quiet=True)
        end = time.time() + 3
        while time.time() < end and len(ctl.conversation) < 2:
            time.sleep(0.005)
        assert ctl.conversation[-1]["next"] == ["An example", "A diagram"] and ctl.conversation[-1]["text"] == "Gravity is a pull."
        assert ctl.history.messages(ctl.chat_id)[-1]["next"] == ["An example", "A diagram"]
        loop.call_soon_threadsafe(loop.stop)


class TestThroughTheOrchestrator:
    def run(self, tmp_path, text):
        from app.hermes.client import HermesReply
        from app.orchestrator import Orchestrator

        class Hermes:
            connected = True

            def set_permission_handler(self, handler):
                pass

            async def send_message(self, message, on_chunk=None, **kw):
                for i in range(0, len(text), 5):
                    on_chunk(text[i:i + 5])
                return HermesReply(text=text)

        pieces: list[str] = []
        out = asyncio.run(Orchestrator(Hermes(), None, tmp_path).process_text("what is gravity", on_chunk=pieces.append))
        return out, "".join(pieces)

    def test_the_reply_loses_the_line_and_carries_the_choices(self, tmp_path):
        out, streamed = self.run(tmp_path, "Gravity is a pull.\n\n<<next: An example | A diagram>>")
        assert out.text == "Gravity is a pull." and out.next == ["An example", "A diagram"]
        assert streamed == "Gravity is a pull."  # what is shown, and what would be spoken, never had it

    def test_a_reply_that_offers_nothing_is_as_it_was(self, tmp_path):
        out, streamed = self.run(tmp_path, "Done, it is open.")
        assert out.text == streamed == "Done, it is open." and out.next == []
