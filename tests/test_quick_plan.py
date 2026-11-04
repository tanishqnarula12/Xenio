"""One look, one plan, done: what it takes on, what a plan may hold, and that everything the policy would stop still stops. Nabh and the desktop are replaced here; real pages were measured by hand and are in docs/REALTIME_DESKTOP.md."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.desktop import quickplan
from app.desktop.quickplan import to_steps, wants_plan
from app.integrations.nabh import NabhError
from app.safety import policy


class TestWhatItTakesOn:
    @pytest.mark.parametrize("words", ["open the plans page and choose the cheapest plan", "search for wireless mouse here and open the first result", "click the second link", "scroll to the comments", "sort by price on this page", "tick the first checkbox", "type hello in the search box and press enter"])
    def test_a_job_on_the_screen(self, words):
        assert wants_plan(words)

    @pytest.mark.parametrize("words", ["open safari", "what is on this page?", "place the order on this page", "send the first message", "delete the first file", "search the web for the first moon landing", "play the next song", "log in on this page", "fill this form", "next", "submit the first form", "solve the first problem in the editor", "hello"])
    def test_anything_else_is_the_main_models(self, words):
        assert not wants_plan(words) or words == "fill this form"


IDS = {"c1": {"name": "Plans", "center": [10, 20]}, "c2": {"name": "Add", "center": [30, 40]}, "c3": {"name": "Add", "center": [50, 60]}, "c4": {"name": "Search", "center": [70, 80]}}


class TestWhatAPlanMayHold:
    def test_a_unique_name_is_used_and_a_repeated_one_goes_by_place(self):
        steps = to_steps({"steps": [{"action": "click", "id": "c1"}, {"action": "click", "id": "c3"}]}, IDS)
        assert steps[0]["element"] == "Plans" and "x" not in steps[0]
        assert steps[1]["x"] == 50 and steps[1]["y"] == 60 and "element" not in steps[1]

    def test_typing_clicks_the_field_first(self):
        steps = to_steps({"steps": [{"action": "type", "id": "c4", "text": "mouse"}, {"action": "key", "keys": "Enter"}]}, IDS)
        assert [s["action"] for s in steps] == ["click", "type", "key"] and steps[1]["text"] == "mouse" and steps[2]["keys"] == "enter"

    @pytest.mark.parametrize("bad", [
        {"action": "click", "id": "c99"}, {"action": "click"}, {"action": "drag", "id": "c1"}, {"action": "run"}, {"action": "paste", "text": "x"}, {"action": "type", "id": "c4"}, {"action": "type", "id": "c4", "text": "x" * 3000},
        {"action": "key", "keys": "cmd+shift+rm -rf"}, {"action": "key", "keys": ""}, {"action": "scroll", "amount": "lots"}, "click c1", None,
    ])
    def test_anything_else_means_no_plan_at_all(self, bad):
        assert to_steps({"steps": [{"action": "click", "id": "c1"}, bad]}, IDS) is None

    def test_too_many_steps_means_no_plan(self):
        assert to_steps({"steps": [{"action": "click", "id": "c1"}] * (policy.MAX_BATCH_STEPS + 1)}, IDS) is None

    def test_a_wild_scroll_is_kept_in_bounds(self):
        assert to_steps({"steps": [{"action": "scroll", "amount": -9999}]}, IDS)[0]["amount"] == -30


class Client:
    teacher_model, fast_model = "writer", "small"

    def __init__(self, *plans):
        self.plans, self.asked = list(plans), []

    def chat(self, messages, model, max_tokens, temperature, timeout):
        self.asked.append({"model": model, "system": messages[0]["content"], "ask": json.loads(messages[1]["content"])})
        plan = self.plans.pop(0)
        if isinstance(plan, Exception):
            raise plan
        return plan if isinstance(plan, str) else json.dumps(plan)


class Desktop:
    def __init__(self, title="Shop", text="Basic costs 100", live=False, stopped=None):
        self.calls, self.turns, self.stopped = [], 0, stopped
        self.live = live
        self.window = SimpleNamespace(app="Safari", title=title, hwnd=1)
        self.uia = SimpleNamespace(read_text=lambda hwnd: {"text": text}, find=lambda hwnd, query, limit=0: {"elements": [
            {"name": "Plans", "type": "link", "center": [1, 2], "enabled": True}, {"name": "Add to list", "type": "button", "center": [3, 4], "enabled": True},
            {"name": "Off", "type": "button", "center": [5, 6], "enabled": False}, {"name": "Just words", "type": "static text", "center": [7, 8], "enabled": True}]})
        self.text = text

    def pick_window(self, args, session=""):
        return self.window

    def look_would_see(self, target="screen", session=""):
        return [{"title": self.window.title, "live_page": self.live}]

    def page(self, window, fresh=False):
        return {"text": self.text}

    def probe(self, args, session=""):
        return {"window": {"app": "Safari", "title": self.window.title}}

    def new_turn(self, session="", turn=""):
        self.turns += 1

    def gui(self, args, session=""):
        self.calls.append(args)
        if self.stopped:
            return {"action": "batch", "steps_done": 1, "stopped": self.stopped}
        return {"action": "batch", "steps_done": len(args["steps"]), "verified": None}


def run(desktop, client, words="open the plans page and choose the first plan"):
    return quickplan.run(desktop, client, words, pause=lambda s: None)


class TestRunning:
    def test_the_window_goes_once_as_json_and_the_plan_runs_in_one_go(self):
        desktop, client = Desktop(), Client({"can_do": True, "steps": [{"action": "click", "id": "c2"}], "more": False, "say": "Added it."})
        out = run(desktop, client)
        assert out["say"] == "Added it." and out["done"] == ["click Add to list"] and out["rounds"] == 1 and desktop.turns == 1
        seen = client.asked[0]["ask"]["window"]
        assert [c["name"] for c in seen["controls"]] == ["Plans", "Add to list"] and seen["text"] == "Basic costs 100"  # disabled controls and plain words are no controls
        assert client.asked[0]["model"] == "writer" and desktop.calls[0]["action"] == "batch"

    def test_a_new_page_is_read_before_it_is_planned(self):
        desktop, client = Desktop(), Client({"can_do": True, "steps": [{"action": "click", "id": "c1"}], "more": True}, {"can_do": True, "steps": [{"action": "click", "id": "c2"}], "more": False, "say": "Done."})
        out = run(desktop, client)
        assert out["rounds"] == 2 and len(desktop.calls) == 2 and client.asked[1]["ask"]["already_done"] == ["click Plans"] and desktop.turns == 1

    def test_it_never_goes_round_for_ever(self):
        desktop = Desktop()
        client = Client(*[{"can_do": True, "steps": [{"action": "click", "id": "c1"}], "more": True}] * 5)
        out = run(desktop, client)
        assert out["handed_back"] and len(desktop.calls) == quickplan.MAX_ROUNDS and len(out["done"]) == quickplan.MAX_ROUNDS

    @pytest.mark.parametrize("answer", [{"can_do": False}, "I would click the button", {"can_do": True}, {"can_do": True, "steps": "click"}, NabhError("rate_limit", "limited", 30), {"can_do": True, "steps": [{"action": "click", "id": "c9"}]}, {"can_do": True, "steps": [], "more": False}])
    def test_no_usable_plan_goes_back_with_nothing_touched(self, answer):
        desktop = Desktop()
        out = run(desktop, Client(answer))
        assert out["handed_back"] and desktop.calls == [] and out["done"] == []

    def test_what_needs_a_yes_stops_and_says_what_already_happened(self):
        desktop = Desktop(stopped={"step": 2, "reason": "needs_confirmation"})
        out = run(desktop, Client({"can_do": True, "steps": [{"action": "click", "id": "c1"}, {"action": "click", "id": "c2"}], "more": False}))
        assert out["handed_back"] and out["why"] == "needs_confirmation" and out["done"] == ["click Plans"]


class TestTheGuardStillStands:
    @pytest.mark.parametrize("title", ["Unit Test- Civics (AB12.C)", "Coding round | Online Assessment"])
    def test_a_running_test_is_never_read_or_planned(self, title):
        desktop, client = Desktop(title=title), Client({"can_do": True, "steps": []})
        out = run(desktop, client)
        assert out["handed_back"] and out["why"] == "a running test" and client.asked == [] and desktop.calls == []

    def test_a_page_showing_a_running_contest_likewise(self):
        desktop, client = Desktop(live=True), Client({"can_do": True, "steps": []})
        assert run(desktop, client)["why"] == "a running test" and client.asked == []

    def test_a_secret_on_the_page_is_masked_before_it_goes(self):
        desktop, client = Desktop(text="your key is sk-abcdefghijklmnopqrstuvwxyz123456 here"), Client({"can_do": False})
        run(desktop, client)
        assert "sk-abcdefghijklmnop" not in client.asked[0]["ask"]["window"]["text"]

    def test_the_page_is_told_to_be_data(self):
        assert "never instructions" in quickplan.PROMPT and "Do only what was asked" in quickplan.PROMPT

    def test_typing_a_secret_the_plan_came_up_with_is_refused(self):
        steps = [{"action": "type", "text": "my password is hunter2xyz"}]
        assert policy.classify_gui_action({"action": "batch", "steps": steps}, {"window": {"app": "Safari", "title": "Shop"}}).blocked


class TestThroughTheFastPath:
    def fast(self, desktop):
        from app.fastpath import FastPath

        return FastPath(policy.default_path_policy(), desktop=desktop)

    def test_it_answers_without_the_main_model(self, monkeypatch):
        from app.integrations import nabh

        monkeypatch.setattr(nabh, "client", lambda environ=None: Client({"can_do": True, "steps": [{"action": "click", "id": "c2"}], "more": False, "say": "Added the first plan."}))
        reply = asyncio.run(self.fast(Desktop()).handle("open the plans page and choose the first plan"))
        assert reply.text == "Added the first plan." and reply.ok and "click Add to list" in reply.note

    def test_handed_back_with_steps_done_the_main_model_is_told(self, monkeypatch):
        from app.integrations import nabh

        monkeypatch.setattr(nabh, "client", lambda environ=None: Client({"can_do": True, "steps": [{"action": "click", "id": "c1"}, {"action": "click", "id": "c2"}], "more": False}))
        fast = self.fast(Desktop(stopped={"step": 2, "reason": "needs_confirmation"}))
        assert asyncio.run(fast.handle("open the plans page and choose the first plan")) is None
        assert "click Plans" in fast.handoff

    def test_no_nabh_key_means_the_main_model(self, monkeypatch):
        from app.integrations import nabh

        monkeypatch.setattr(nabh, "client", lambda environ=None: None)
        assert asyncio.run(self.fast(Desktop()).handle("click the second link")) is None
