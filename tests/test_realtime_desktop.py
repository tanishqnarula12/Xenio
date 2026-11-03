"""The real-time desktop engine (V4 phase 1): seeing the screen at the lowest cost that can answer, checking actions against
the UI, short sequences that are judged step by step, and honest numbers. No real input and no real screen: fakes behave
the way the real windows were measured to (Electron apps expose almost no text, the Finder desktop has window id 0)."""

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest

from app.desktop import metrics
from app.desktop import service as service_mod
from app.desktop.perception import LiveTestRefused, Perception, needs_pixels
from app.desktop.service import DesktopError
from app.fastpath import FastPath
from app.hermes.client import ToolActivity
from app.language.detect import parse_ui_command
from app.orchestrator import describe_actions, not_confirmed
from app.safety import policy as p
from app.safety.policy import RiskLevel
from app.tools import desktop as desktop_tools
from app.tools import vision as vision_tool
from app.tools.base import ToolContext
from app.ui.assistant import AssistantState
from app.vision import capture as capture_mod
from app.vision.capture import same_sketch
from app.vision.service import ScreenLook, same_question
from tests.test_desktop import FakeBackend, FakeUIA, FakeWindows, Win, _turn, load, service
from tests.test_engineering_service import repo, settings  # noqa: F401 - fixtures for the UI stack
from tests.test_ui_backend import stack  # noqa: F401

GFG = "Range Max Count | Practice | GeeksforGeeks"
PROBLEM = "Range Max Count\nGiven an array A of N integers, print how many times the maximum occurs.\n1 <= N <= 2*10^5"
CONTEST_PAGE = "Count Frequent Elements\nContest Ends in 00 : 01 : 40 : 49\n[Finish Contest]\n[Compile & Run] [Submit]"
XENIO_CHAT = Win(9, "", "XENIO", True)


class Page(FakeUIA):
    """A window's text through accessibility, per window id. Electron apps: nothing (an empty string here)."""

    def __init__(self, pages, elements=(), **kw):
        super().__init__(elements=elements, **kw)
        self.pages, self.reads = dict(pages), []

    def read_text(self, hwnd, max_chars=60000):
        self.reads.append(hwnd)
        text = self.pages.get(hwnd, "")
        return {"text": text, "chars": len(text), "complete": True, "editors": []}


class Screen(FakeWindows):
    """Windows, with the ones really visible (front to back) and the Finder desktop the way macOS lists it: id 0."""

    def __init__(self, wins, visible=None):
        super().__init__(wins)
        self.visible = visible

    def visible_windows(self):
        return list(self.visible if self.visible is not None else self.wins)

    def fullscreen(self, hwnd, set_to=None):
        return False


def desk(tmp_path, wins, pages, ocr=None, visible=None, elements=()):
    svc, backend = service(tmp_path, wins, Page(pages, elements))
    svc.windows = Screen(svc.windows.wins, visible)
    svc._ocr = ocr
    return svc, backend


def finder_desktop():
    return Win(0, "", "Finder", False, rect=(0, 0, 1470, 956))


# ---- the 2026-10-28 failure: "that window no longer exists" ------------------------------------------------------------

def test_with_xenios_chat_in_front_the_finder_desktop_is_never_the_target(tmp_path):
    """The Finder desktop is listed with window id 0, and a task without a window yet had window 0 too: Safari lost to it."""
    safari = Win(5, GFG, "Safari")
    svc, backend = desk(tmp_path, [XENIO_CHAT, finder_desktop(), safari], {5: PROBLEM})
    svc.new_turn(session="s1")
    assert svc.task("s1").window == 0  # no window yet
    assert svc.pick_window({"action": "paste", "editor": True}, "s1").hwnd == 5
    assert svc.probe({"action": "paste", "editor": True, "text": "x"}, "s1")["title"] == GFG  # what the policy judges


def test_only_the_desktop_and_xenio_left_is_said_plainly(tmp_path):
    svc, _b = desk(tmp_path, [XENIO_CHAT, finder_desktop()], {})
    with pytest.raises(DesktopError, match="only XENIO's own window"):
        svc.pick_window({"action": "read"})


def test_named_controls_are_looked_for_behind_xenios_window_too(tmp_path):
    """Measured 2026-10-27: "submit" was clicked in XENIO's own window, because its chat was in front."""
    button = {"name": "Compile & Run", "type": "button", "center": [900, 800], "rect": [0, 0, 1, 1], "enabled": True}
    safari = Win(5, GFG, "Safari")
    svc, backend = desk(tmp_path, [XENIO_CHAT, safari], {5: PROBLEM}, elements=[button])
    out = svc.gui({"action": "click", "element": "Compile & Run"})
    assert out["app"] == "Safari" and svc.windows.focused == [5] and ("move", 900, 800) in backend.events


# ---- the live-test guard, on every route ----------------------------------------------------------------------------------

@pytest.mark.parametrize("title,page", [("Weekly Contest 412 - LeetCode", "Problem"),
                                        ("Count Frequent Elements | GeeksforGeeks", CONTEST_PAGE)])
def test_a_running_contest_is_refused_on_every_route(tmp_path, monkeypatch, title, page):
    """By its title, or - the 2026-10-28 page - by its countdown and "Finish Contest" under an ordinary title."""
    _m, ctx, _ws = load(monkeypatch, tmp_path)
    svc, backend = desk(tmp_path / "svc", [XENIO_CHAT, Win(5, title, "Safari")], {5: page})
    desktop_tools.set_service(svc)
    try:
        pre = ctx.hooks["pre_tool_call"]
        note = str((_turn(ctx, "solve this problem on my screen") or {}).get("context") or "")
        assert "Contest Ends" not in note and "Problem\n" not in note  # nothing of it reaches the model
        for tool, args in (("xenio_gui", {"action": "paste", "editor": True, "select_all": True, "text": "int main() {}"}),
                           ("xenio_gui", {"action": "type", "code": True, "editor": True, "text": "int main() {}"}),
                           ("xenio_gui", {"action": "read"}), ("xenio_gui", {"action": "run"}),
                           ("xenio_gui", {"action": "copy", "editor": True, "select_all": True}),
                           ("xenio_find_ui", {"query": "problem"}),
                           ("xenio_look_at_screen", {"question": "read the problem"}),
                           ("xenio_look_at_screen", {"question": "show me the diagram", "need_pixels": True})):
            out = pre(tool_name=tool, args=args, session_id="s1")
            assert out and out["action"] == "block" and "Blocked by XENIO policy" in out["message"], (tool, args)
        assert backend.events == []
    finally:
        desktop_tools.set_service(None)


def test_a_contest_beside_another_window_is_seen_too(tmp_path):
    front, contest = Win(4, "notes.md - Code", "Code", True), Win(5, "Problem | GeeksforGeeks", "Safari")
    svc, _b = desk(tmp_path, [front, contest], {4: "some notes in the editor, long enough to read", 5: CONTEST_PAGE})
    assert any(w["live_page"] for w in svc.look_would_see("screen"))
    with pytest.raises(LiveTestRefused):
        Perception(svc).observe("what is on my screen")


@pytest.mark.parametrize("title,text", [
    ("How exams work", "Students who finish the exam early may end the test and leave the room."),
    ("Two Sum | Practice | GeeksforGeeks", "Weekly Contest starts in 2 days. Register now"),
    ("Two Sum | Practice | GeeksforGeeks", "[Add note] [Start Timer] 00:05:12 [Compile & Run] [Submit]"),
    ("Virtual Contest - Weekly 412", CONTEST_PAGE),
])
def test_ordinary_pages_and_practice_are_not_taken_for_a_live_test(title, text):
    assert not p.live_test_page(text, title)


def test_a_screen_look_is_only_refused_for_what_it_would_see():
    look = p.read("vision.look_at_screen", "look at your screen")
    assert p.screen_look_on_live_test(look, [{"title": "Two Sum | Practice", "live_page": False}]) is look
    assert p.screen_look_on_live_test(look, [{"title": "x", "live_page": True}]).blocked
    assert p.screen_look_on_live_test(look, [{"title": "Online Assessment", "live_page": False}]).blocked


# ---- perception: the lowest cost that can answer ----------------------------------------------------------------------------

def test_a_question_about_text_is_answered_from_the_tree_without_a_model(tmp_path):
    svc, _b = desk(tmp_path, [Win(5, GFG, "Safari", True)], {5: PROBLEM})
    seen = Perception(svc).observe("Read the problem statement exactly")
    assert seen["read_by"] == "accessibility" and "2*10^5" in seen["text"] and seen["windows"][0]["app"] == "Safari"
    assert seen["windows"][0]["fullscreen"] is False  # "is Safari in full screen?" needs no screenshot either


def test_an_app_without_a_tree_is_read_on_the_device(tmp_path):
    """Measured: an Electron IDE and the ChatGPT app expose 0 characters through accessibility."""
    calls = []

    def ocr(hwnd, rect):
        calls.append(hwnd)
        return {"text": "const answer = 42; // the file open in the editor", "lines": [], "capture_ms": 9, "ocr_ms": 150}

    svc, _b = desk(tmp_path, [Win(7, "main.py - Code", "Code", True)], {7: ""}, ocr)
    seen = Perception(svc).observe("what does the editor show?")
    assert seen["read_by"] == "ocr" and "answer = 42" in seen["text"] and calls == [7] and seen["timing_ms"]["ocr"] == 150


def test_nothing_readable_goes_on_to_the_vision_model(tmp_path):
    svc, _b = desk(tmp_path, [Win(7, "Paint", "Paint", True)], {7: ""}, lambda h, r: {"text": "", "lines": []})
    assert Perception(svc).observe("what is on my screen?") is None


def test_only_what_pixels_can_answer_goes_to_the_model(tmp_path):
    svc, _b = desk(tmp_path, [Win(5, GFG, "Safari", True)], {5: PROBLEM})
    perceive = Perception(svc)
    assert perceive.observe("what does the chart show?") is None
    assert perceive.observe("read the text", need_pixels=True) is None  # the model may always ask for pixels


def test_all_the_questions_really_asked_stay_on_text():
    """A sample of the 59 real questions: none of them needs a screenshot."""
    for question in ("What compilation error is shown? Read the error message exactly.", "Is the Safari window in full screen or windowed?",
                     "What language is selected in the code editor?", "Read every visible question and option text now.",
                     "Which app window is in front right now?", "Where is the exact text 'code here'?"):
        assert not needs_pixels(question), question
    for question in ("what does this chart show", "what colour is the button", "describe this photo", "how does the layout look"):
        assert needs_pixels(question), question


def test_the_whole_screen_means_the_windows_on_it_never_xenios(tmp_path):
    left, right = Win(4, "Docs", "Safari", True), Win(5, "main.py - Code", "Code")
    svc, _b = desk(tmp_path, [left, right], {4: "the documentation, long enough to count as text", 5: "the program, long enough to count as text"},
                   visible=[XENIO_CHAT, left, right])
    seen = Perception(svc).observe("what is on my screen")
    assert [w["app"] for w in seen["windows"]] == ["Safari", "Code"] and "=== Safari: Docs ===" in seen["text"]
    assert "XENIO" not in seen["text"]
    alone = Perception(svc).observe("what is on my screen", target="window")
    assert [w["app"] for w in alone["windows"]] == ["Safari"]


def test_secrets_on_screen_are_masked_like_in_the_vision_path(tmp_path):
    svc, _b = desk(tmp_path, [Win(5, ".env - Code", "Code", True)], {5: "OPENROUTER_API_KEY=sk-or-v1-abcdefghijklmnopqrstuvwxyz012345 in the file"})
    assert "sk-or-v1" not in Perception(svc).observe("read the file")["text"]


def test_an_unchanged_window_is_not_sent_twice_in_one_request(tmp_path):
    svc, _b = desk(tmp_path, [Win(5, GFG, "Safari", True)], {5: PROBLEM})
    perceive = Perception(svc)
    svc.new_turn(session="s1")
    first = perceive.observe("read the problem", session="s1")
    again = perceive.observe("what are the constraints?", session="s1")
    assert first["text"] and first["changed_since_last_look"] is None
    assert again["unchanged"] is True and again["text"] == "" and again["changed_since_last_look"] is False
    assert perceive.observe("read it again", session="s1", fresh=True)["text"]  # the model no longer has it
    svc.new_turn(session="s1", turn="t2")
    later = perceive.observe("read the problem", session="s1")
    assert later["text"] and later["changed_since_last_look"] is False  # a new request gets the text again
    svc.uia.pages[5] = PROBLEM + "\nCompilation Error"
    svc._pages = {}
    changed = perceive.observe("read the problem", session="s1")
    assert changed["changed_since_last_look"] is True and "Compilation Error" in changed["text"]


def test_the_guard_and_the_look_read_each_window_once(tmp_path):
    left, right = Win(4, "Docs", "Safari", True), Win(5, GFG, "Safari")
    svc, _b = desk(tmp_path, [left, right], {4: "the documentation, long enough to count as text", 5: PROBLEM})
    svc.look_would_see("screen")
    Perception(svc).observe("read it")
    assert sorted(svc.uia.reads) == [4, 5]
    probe_then_read = desk(tmp_path / "2", [Win(5, GFG, "Safari", True)], {5: PROBLEM})[0]
    probe_then_read.probe({"action": "read"})
    probe_then_read.read_window("")
    assert probe_then_read.uia.reads == [5]


def test_finding_controls_on_an_ordinary_page_is_still_free(monkeypatch, tmp_path):
    _m, ctx, _ws = load(monkeypatch, tmp_path)
    svc, _b = desk(tmp_path / "svc", [Win(5, GFG, "Safari", True)], {5: PROBLEM})
    desktop_tools.set_service(svc)
    try:
        _turn(ctx, "click the run button")
        assert ctx.hooks["pre_tool_call"](tool_name="xenio_find_ui", args={"query": "run"}, session_id="s1") is None
    finally:
        desktop_tools.set_service(None)


# ---- the look tool: consent and fallback unchanged ------------------------------------------------------------------------

class CountingLook:
    def __init__(self):
        self.calls = 0

    def look(self, question, target):
        self.calls += 1
        return {"source": target, "description": "a picture of a cat", "timing_ms": {}}


@pytest.fixture
def tool_ctx():
    return ToolContext(paths=p.default_path_policy(), session="s1")


def test_the_tool_answers_from_the_window_and_calls_no_model(tmp_path, tool_ctx):
    svc, _b = desk(tmp_path, [Win(5, GFG, "Safari", True)], {5: PROBLEM})
    desktop_tools.set_service(svc)
    look = CountingLook()
    vision_tool.set_service(look)
    try:
        out = vision_tool.look_at_screen({"question": "read the problem"}, tool_ctx)
        assert out.success and out.data["read_by"] == "accessibility" and look.calls == 0
        assert out.data["untrusted_content"] is True and "need_pixels" in out.data["note"]
        assert p.find_injection(out.data["note"]) is None  # XENIO's own words never read as an injected order
        pixels = vision_tool.look_at_screen({"question": "read the problem", "need_pixels": True}, tool_ctx)
        assert pixels.data["description"] == "a picture of a cat" and look.calls == 1
    finally:
        desktop_tools.set_service(None)
        vision_tool.set_service(None)


def test_vision_still_works_when_reading_fails_and_can_be_forced(tmp_path, tool_ctx, monkeypatch):
    svc, _b = desk(tmp_path, [Win(5, GFG, "Safari", True)], {5: PROBLEM})
    desktop_tools.set_service(svc)
    look = CountingLook()
    vision_tool.set_service(look)
    try:
        monkeypatch.setattr(Perception, "observe", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        assert vision_tool.look_at_screen({"question": "read it"}, tool_ctx).data["description"] and look.calls == 1
        monkeypatch.undo()
        monkeypatch.setenv("XENIO_PERCEPTION", "vision")
        assert vision_tool.look_at_screen({"question": "read it"}, tool_ctx).data["description"] and look.calls == 2
    finally:
        desktop_tools.set_service(None)
        vision_tool.set_service(None)


def test_consent_is_the_same_as_before(monkeypatch, tmp_path):
    _m, ctx, _ws = load(monkeypatch, tmp_path)
    svc, _b = desk(tmp_path / "svc", [Win(5, GFG, "Safari", True)], {5: PROBLEM})
    desktop_tools.set_service(svc)
    try:
        pre = ctx.hooks["pre_tool_call"]
        _turn(ctx, "play some music")
        assert pre(tool_name="xenio_look_at_screen", args={"question": "x"}, session_id="s1")["action"] == "approve"
        _turn(ctx, "look at my screen", turn="t2")
        assert pre(tool_name="xenio_look_at_screen", args={"question": "x"}, session_id="s1") is None
        assert pre(tool_name="xenio_look_at_screen", args={"question": "x"}, session_id="cron_1_2")["action"] == "block"
    finally:
        desktop_tools.set_service(None)


# ---- repeated looks with the vision model ----------------------------------------------------------------------------------

def test_a_question_said_differently_about_the_same_screen_is_one_model_call():
    assert same_question("What is the error message?", "Read the error message exactly")
    assert not same_question("What is the error message?", "What language is selected?")


def test_the_menu_bar_clock_is_no_change_but_a_new_line_of_text_is():
    screen = bytes([20] * 16000)
    assert same_sketch(screen, screen)
    line = bytearray(screen)
    line[8000:8060] = bytes([60] * 60)  # a new line of text in a dark editor
    assert not same_sketch(screen, bytes(line))
    assert not same_sketch(b"", b"")  # nothing to compare: never "the same"
    caret = bytearray(screen)
    caret[100] = 50
    assert not same_sketch(screen, bytes(caret))  # strict on purpose: a wrong reuse is worse than one more call


def test_repeated_observations_do_not_call_the_model_again():
    answers = []

    class Provider:
        def analyze(self, image, prompt, timeout=90.0):
            answers.append(prompt)
            return SimpleNamespace(text="an error on line 3", provider="p", model="m", ms=21000)

    sketch = bytes([9] * 100)
    image = lambda digest, sk=sketch: capture_mod.CapturedImage(b"x", 10, 10, "screen", (10, 10), digest=digest, sketch=sk)  # noqa: E731

    class Capture:
        def __init__(self):
            self.next = image("one")

        def capture_screen(self):
            return self.next

    capture = Capture()
    look = ScreenLook(Provider(), capture)
    assert look.look("What is the error?")["description"]
    capture.next = image("two")  # other pixels in the menu bar: the sketch leaves it out
    assert look.look("What is the error?")["reused"] is True
    assert look.look("Read the error exactly")["reused"] is True  # said differently
    assert len(answers) == 1
    look.look("Which language is selected?")  # another question: a real call
    assert len(answers) == 2
    capture.next = image("three", bytes([200] * 100))  # the screen really changed
    assert "reused" not in look.look("What is the error?") and len(answers) == 3


# ---- actions are checked against the UI -----------------------------------------------------------------------------------

def test_expect_confirms_from_the_window_without_screenshots(tmp_path):
    svc, backend = desk(tmp_path, [Win(5, GFG, "Safari", True)], {5: "Compilation Completed\nYour Output: 3"})
    digests = []
    svc._digest = lambda: digests.append(1) or "x"
    out = svc.gui({"action": "click", "x": 10, "y": 10, "target": "Run", "expect": {"text": "compilation completed"}})
    assert out["verified"] is True and "read by accessibility" in out["verification"] and digests == []


def test_expect_says_no_when_the_ui_does_not_show_it(tmp_path):
    svc, _b = desk(tmp_path, [Win(5, GFG, "Safari", True)], {5: "the page as it was"})
    out = svc.gui({"action": "key", "keys": "enter", "expect": {"text": "Order placed", "timeout": 0.2}})
    assert out["verified"] is False and "did NOT show" in out["verification"] and "Order placed" in out["verification"]
    assert "the page as it was" not in json.dumps(out)  # what the window holds stays out of it


def test_expect_waits_for_a_slow_page(tmp_path):
    svc, _b = desk(tmp_path, [Win(5, GFG, "Safari", True)], {5: "Compiling"})
    naps = []

    def sleep(seconds):
        naps.append(seconds)
        if len(naps) == 3:
            svc.uia.pages[5] = "Result: Accepted"

    svc._sleep = sleep
    assert svc.gui({"action": "click", "x": 1, "y": 1, "target": "Run", "expect": {"text": "Accepted", "gone": "Compiling"}})["verified"] is True
    assert naps[:3] == [0.05, 0.1, 0.2]  # no blind wait


def test_expect_covers_titles_controls_and_what_only_ocr_can_read(tmp_path):
    button = {"name": "Log out", "type": "button", "center": [1, 1], "rect": [0, 0, 1, 1], "enabled": True}
    svc, _b = desk(tmp_path, [Win(5, "Dashboard", "Safari", True)], {5: ""}, elements=[button],
                   ocr=lambda h, r: {"text": "Welcome back, this is drawn on a canvas", "lines": []})
    target = svc.windows.wins[0]
    assert svc._expect(target, {"title": "dashboard"})["verified"] is True
    assert svc._expect(target, {"element": "Log out"})["verified"] is True
    assert "read by ocr" in svc._expect(target, {"text": "welcome back"})["verification"]
    assert svc._expect(target, {})["verified"] is None  # nothing to check is never "confirmed"


def test_a_stop_ends_the_wait(tmp_path):
    svc, _b = desk(tmp_path, [Win(5, GFG, "Safari", True)], {5: "page"})
    out = svc._expect(svc.windows.wins[0], {"text": "never", "timeout": 10}, ended=lambda: True)
    assert out["verified"] is None and out["stopped_waiting"] is True  # at once, and never called a failed check


def test_a_stop_during_a_batchs_wait_is_a_cancellation(tmp_path):
    """Measured on the demo page: it stopped in 0.86 s but said "not as expected"."""
    from app.desktop.stop import request_stop

    svc, _b = form(tmp_path, [])
    naps = []

    def sleep(seconds):
        naps.append(seconds)
        if len(naps) == 2:
            request_stop("voice", tmp_path)

    svc._sleep = sleep
    out = svc.gui({"action": "batch", "steps": [{"action": "wait", "expect": {"text": "never", "timeout": 10}},
                                                {"action": "scroll", "amount": -1}]})
    assert out["stopped"]["reason"] == "cancelled" and out["steps_done"] == 0 and out["verified"] is None
    assert len(out["remaining"]) == 1 and out["remaining"][0]["action"] == "scroll"


def test_a_very_long_window_also_says_what_is_on_screen(tmp_path):
    """Measured: an IDE's tree held 60 000 characters, and a cut at the limit may not reach the file on screen."""
    from app.desktop.perception import MAX_TEXT

    tree = "sidebar item\n" * (MAX_TEXT // 10)
    svc, _b = desk(tmp_path, [Win(7, "main.py - Code", "Code", True)], {7: tree},
                   lambda h, r: {"text": "def visible_function(): pass", "lines": [], "ocr_ms": 150, "capture_ms": 9})
    seen = Perception(svc).observe("what does the editor show?")
    assert seen["read_by"] == "accessibility" and seen["text"].index("visible_function") < seen["text"].index("sidebar item")
    assert seen["truncated"] is True and seen["timing_ms"]["ocr"] == 150


def test_and_is_how_an_ampersand_is_said():
    from app.desktop.common import score_name

    assert score_name("Compile and Run", "Compile & Run") == 1.0


# ---- batches ---------------------------------------------------------------------------------------------------------------

def form(tmp_path, elements=(), focused=None, pages=None):
    named = [{"name": n, "type": "button", "center": [100 + i * 10, 100], "rect": [0, 0, 1, 1], "enabled": True}
             for i, n in enumerate(elements)]
    svc, backend = desk(tmp_path, [Win(5, "Settings", "Safari", True)], pages or {5: "the page"}, elements=named)
    svc.uia._focused = focused or {"found": True, "name": "Search", "type": "edit", "value": ""}
    return svc, backend


def test_a_batch_runs_its_steps_and_checks_them(tmp_path):
    svc, backend = form(tmp_path, ["Filters", "Apply"], pages={5: "Filters applied"})
    out = svc.gui({"action": "batch", "steps": [
        {"action": "click", "element": "Filters"}, {"action": "scroll", "amount": -3},
        {"action": "click", "element": "Apply", "expect": {"text": "Filters applied"}},
        {"action": "wait", "expect": {"text": "applied"}}]})
    assert out["steps_done"] == 4 and out["steps_total"] == 4 and out["verified"] is True and "stopped" not in out
    assert ("move", 100, 100) in backend.events and ("move", 110, 100) in backend.events


def test_steps_without_a_check_are_not_called_verified(tmp_path):
    svc, _b = form(tmp_path, ["Filters"])
    out = svc.gui({"action": "batch", "steps": [{"action": "click", "element": "Filters"}]})
    assert out["verified"] is None and "nothing checked" in out["verification"]


def test_a_batch_stops_before_anything_that_needs_a_yes(tmp_path):
    svc, backend = form(tmp_path, ["Open", "Send"])
    out = svc.gui({"action": "batch", "steps": [{"action": "click", "element": "Open"}, {"action": "click", "element": "Send"},
                                                {"action": "click", "element": "Open"}]})
    assert out["steps_done"] == 1 and out["stopped"]["reason"] == "needs_confirmation" and "by itself" in out["stopped"]["how"]
    assert len(out["remaining"]) == 2 and out["remaining"][0]["element"] == "Send"
    assert ("move", 110, 100) not in backend.events  # Send was never clicked


def test_every_step_is_judged_on_the_ui_as_it_is_then(tmp_path):
    """Between steps a password field took the focus: typing stops there, though the batch looked harmless when it began."""
    svc, backend = form(tmp_path, ["Next"])
    original = svc.uia.find

    def find(hwnd, query, limit=8, **kw):
        svc.uia._focused = {"found": True, "name": "Password", "type": "edit", "is_password": True}
        return original(hwnd, query, limit, **kw)

    svc.uia.find = find
    out = svc.gui({"action": "batch", "steps": [{"action": "click", "element": "Next"}, {"action": "type", "text": "hello"}]})
    assert out["stopped"]["reason"] == "blocked" and "password" in out["stopped"]["why"].lower()
    assert not [e for e in backend.events if e[0] == "char"]


def test_a_batch_stops_at_the_first_surprise_and_says_what_is_left(tmp_path):
    svc, _b = form(tmp_path, ["Open", "Next"])
    out = svc.gui({"action": "batch", "steps": [{"action": "click", "element": "Open", "expect": {"text": "Details", "timeout": 0.2}},
                                                {"action": "click", "element": "Next"}]})
    assert out["verified"] is False and out["stopped"]["reason"] == "not_as_expected" and out["steps_done"] == 0
    assert out["remaining"][0]["element"] == "Next" and len(out["remaining"]) == 1


def test_a_control_not_there_yet_is_looked_for_once_more(tmp_path):
    svc, _b = form(tmp_path, [])
    naps = []

    def sleep(seconds):
        naps.append(seconds)
        if seconds == 0.4:  # the UI settled meanwhile
            svc.uia.elements = [{"name": "Next", "type": "button", "center": [50, 50], "rect": [0, 0, 1, 1], "enabled": True}]

    svc._sleep = sleep
    out = svc.gui({"action": "batch", "steps": [{"action": "click", "element": "Next"}]})
    assert out["recovered"] == 1 and "second look" in out["steps"][0]["recovered"] and out["steps_done"] == 1
    gone = svc.gui({"action": "batch", "steps": [{"action": "click", "element": "Nowhere"}]})
    assert gone["stopped"]["reason"] == "failed" and "Nowhere" in gone["stopped"]["why"] and gone["steps_done"] == 0


def test_a_new_request_cancels_a_batch(tmp_path):
    svc, backend = form(tmp_path, ["Open"])
    svc.new_turn(session="s1")
    original = backend.mouse

    def mouse(flags, data=0):
        original(flags, data)
        if flags == 0x4:
            svc.new_turn(session="s1", turn="t2")

    backend.mouse = mouse
    out = svc.gui({"action": "batch", "steps": [{"action": "click", "element": "Open"}, {"action": "click", "element": "Open"}]}, session="s1")
    assert out["stopped"]["reason"] == "cancelled" and out["steps_done"] == 1


def test_batches_hold_the_one_input_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(service_mod, "BUSY_WAIT", 0.01)
    svc, _b = form(tmp_path, ["Open"])
    svc._input.acquire()
    try:
        with pytest.raises(DesktopError, match="^BUSY"):
            svc.gui({"action": "batch", "steps": [{"action": "click", "element": "Open"}]})
    finally:
        svc._input.release()


def test_the_policy_refuses_what_a_batch_can_never_do():
    info = {"app": "safari", "title": "Settings"}
    ok = p.classify_gui_action({"action": "batch", "steps": [{"action": "click", "target": "Open"}, {"action": "wait", "expect": {"text": "x"}}]}, info)
    assert ok.level is RiskLevel.SAFE_WRITE and "2 steps" in ok.summary
    too_many = {"action": "batch", "steps": [{"action": "scroll"}] * 9}
    assert p.classify_gui_action(too_many, info).blocked
    assert p.classify_gui_action({"action": "batch", "steps": [{"action": "paste", "text": "x"}]}, info).blocked
    secret = {"action": "batch", "steps": [{"action": "type", "text": "sk-or-v1-abcdefghijklmnopqrstuvwxyz012345"}]}
    assert p.classify_gui_action(secret, info).blocked
    assert p.classify_gui_action({"action": "batch", "steps": [{"action": "key", "keys": "ctrl+alt+delete"}]}, info).blocked
    live = {"action": "batch", "steps": [{"action": "type", "code": True, "text": "int main() {}"}]}
    assert p.classify_gui_action(live, {"app": "safari", "title": "Weekly Contest 412"}).blocked
    assert p.classify_gui_action(live, {"app": "safari", "title": "x", "live_page": True}).blocked


def test_the_plugin_counts_a_batch_as_what_it_does(monkeypatch, tmp_path):
    _m, ctx, _ws = load(monkeypatch, tmp_path)
    svc, _b = form(tmp_path / "svc", ["Open"])
    desktop_tools.set_service(svc)
    try:
        pre = ctx.hooks["pre_tool_call"]
        _turn(ctx, "work through the settings")
        results = [pre(tool_name="xenio_gui", args={"action": "batch", "steps": [{"action": "scroll", "amount": -1 - i}] * 8},
                       session_id="s1") for i in range(6)]
        assert results[:5] == [None] * 5 and results[5]["action"] == "block" and "more than 40" in results[5]["message"]
    finally:
        desktop_tools.set_service(None)


# ---- finding what the tree does not expose ----------------------------------------------------------------------------------

def test_a_control_only_visible_in_the_pixels_is_found_by_its_words(tmp_path):
    def ocr(hwnd, rect):
        return {"text": "Run\nStop", "lines": [{"text": "Run", "center": [300, 40]}, {"text": "Stop", "center": [340, 40]}]}

    svc, backend = desk(tmp_path, [Win(7, "main.py - Code", "Code", True)], {7: ""}, ocr)
    found = svc.find_ui("run")
    assert found["level"] == "ocr" and found["elements"][0]["center"] == [300, 40]
    svc.gui({"action": "click", "element": "Run"})
    assert ("move", 300, 40) in backend.events


def test_a_word_inside_a_sentence_is_not_clicked(tmp_path):
    def ocr(hwnd, rect):
        return {"text": "", "lines": [{"text": "you can run this later if you want", "center": [300, 40]}]}

    svc, backend = desk(tmp_path, [Win(7, "main.py - Code", "Code", True)], {7: ""}, ocr)
    with pytest.raises(DesktopError, match="No control named"):
        svc.gui({"action": "click", "element": "run"})
    assert not [e for e in backend.events if e[0] == "move"]


# ---- the fast path --------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("words,expected", [
    ("click Compile and Run", {"action": "click", "element": "Compile and Run"}),
    ("Xenio, click on the Run button", {"action": "click", "element": "Run"}),
    ("Run pe click karo", {"action": "click", "element": "Run"}),
    ("scroll down", {"action": "scroll", "amount": -5}), ("upar scroll karo", {"action": "scroll", "amount": 5}),
    ("press enter", {"action": "key", "keys": "enter"}), ("escape dabao", {"action": "key", "keys": "esc"}),
    ("click it", None), ("click the third result after you read the page and tell me", None),
    ("press the big red button", None), ("what happens when I click run?", None), ("solve this problem", None),
])
def test_only_short_complete_commands_are_taken(words, expected):
    assert parse_ui_command(words) == expected


def fast(svc):
    return FastPath(p.default_path_policy(), desktop=svc)


def run_fast(path, words):
    return asyncio.run(path.handle(words))


def test_a_fast_click_says_only_what_the_screen_confirmed(tmp_path):
    svc, backend = form(tmp_path, ["Run"])
    svc._digest = iter(["a", "b"]).__next__
    reply = run_fast(fast(svc), "click Run")
    assert reply.text == "Clicked Run." and reply.ok and ("move", 100, 100) in backend.events
    svc._digest = lambda: "same"
    assert "nothing visibly changed" in run_fast(fast(svc), "click Run").text


def test_a_fast_click_that_needs_a_yes_goes_to_the_model(tmp_path):
    svc, backend = form(tmp_path, ["Send", "Submit"])
    assert run_fast(fast(svc), "click Send") is None and run_fast(fast(svc), "click Submit") is None
    assert not [e for e in backend.events if e[0] == "move"]


def test_a_control_not_found_goes_to_the_model(tmp_path):
    svc, backend = form(tmp_path, [])
    assert run_fast(fast(svc), "click Nowhere") is None and backend.events == []


def test_a_fast_key_or_scroll_runs(tmp_path):
    svc, backend = form(tmp_path, [])
    assert run_fast(fast(svc), "scroll down").text == "Scrolled."
    assert run_fast(fast(svc), "press tab").text == "Pressed tab."


# ---- the orb and Mission Control follow the real state --------------------------------------------------------------------------

def test_hands_publish_what_they_really_do(tmp_path):
    svc, _b = form(tmp_path, ["Open"], pages={5: "Details"})
    seen = []
    original = svc.publish
    svc.publish = lambda state, **facts: (seen.append((state, facts.get("step"))), original(state, **facts))[1]
    svc.gui({"action": "batch", "steps": [{"action": "click", "element": "Open", "expect": {"text": "Details"}}]})
    assert ("acting", 1) in seen and ("verifying", None) in seen and seen[-1][0] == "done"
    state = json.loads((tmp_path / service_mod.STATE_FILE).read_text())
    assert state["state"] == "done" and state["verified"] is True and "Details" not in json.dumps(state)
    with pytest.raises(DesktopError):
        svc.gui({"action": "click", "element": "Nowhere"})
    assert json.loads((tmp_path / service_mod.STATE_FILE).read_text())["state"] == "failed"


def test_the_snapshot_carries_the_action_until_the_tool_ends():
    state = AssistantState(connection="connected")
    state.turn_started("t1")
    state.tool_changed("xenio_gui: click", "other", "in_progress")
    state.action = {"state": "verifying", "action": "check", "step": None, "total": None, "verified": None}
    assert state.snapshot()["action"]["state"] == "verifying" and state.snapshot()["state"] == "EXECUTING"
    state.tool_changed("xenio_gui: click", "other", "completed")
    assert state.snapshot()["action"] is None


def test_mission_control_follows_a_running_desktop_tool(tmp_path):
    from app.ui import controller as ctl

    published = []
    host = SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path), state=AssistantState(connection="connected"), _hands_task=None,
                           publish_state=lambda: published.append(host.state.snapshot()["action"]))
    host._read_hands = lambda since: ctl.UiController._read_hands(host, since)
    host._stop_following = lambda: ctl.UiController._stop_following(host)

    async def scenario():
        (tmp_path / "desktop_state.json").write_text(json.dumps({"state": "old", "at": 1.0}))
        ctl.UiController._follow_hands(host, "xenio_gui: click", "in_progress")
        await asyncio.sleep(0.2)
        (tmp_path / "desktop_state.json").write_text(json.dumps({"state": "acting", "action": "click", "step": 2, "total": 4, "at": time.time()}))
        await asyncio.sleep(0.25)
        ctl.UiController._follow_hands(host, "xenio_gui: click", "completed")
        assert host._hands_task is None
        ctl.UiController._follow_hands(host, "web_search: x", "in_progress")  # other tools start nothing
        assert host._hands_task is None

    asyncio.run(scenario())
    assert published and published[-1]["state"] == "acting" and published[-1]["step"] == 2  # the stale state was ignored


def test_a_stopped_turn_stops_following_its_tool(stack):  # noqa: F811
    """A stopped turn never reports its running tool as ended: the follower must not read on forever."""
    orch = stack.orch
    original = orch.process_text

    async def process_text(text, on_chunk=None, on_tool=None, context=None):
        on_tool(SimpleNamespace(tool_call_id="t1", title="xenio_gui: click", kind="other", status="in_progress", detail=""))
        await asyncio.sleep(0.05)
        assert stack.ctl._hands_task is not None
        return await original(text, on_chunk=on_chunk, on_tool=lambda a: None, context=context)

    orch.process_text = process_text
    stack.ctl.run(stack.ctl._begin("t1", "click run", "text"))
    deadline = time.monotonic() + 5
    while stack.ctl.turn_running() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not stack.ctl.turn_running() and stack.ctl._hands_task is None


def test_an_unverified_result_is_never_shown_as_done():
    report = "✅ xenio_gui completed - **message:** done - **status:** UNVERIFIED - **data:**"
    unverified = ToolActivity("t1", title="xenio_gui: click", status="completed", result=report)
    done = ToolActivity("t2", title="xenio_gui: click", status="completed", result="✅ xenio_gui completed - **status:** SUCCEEDED")
    assert not_confirmed(unverified) and not not_confirmed(done)
    lines = describe_actions([unverified, done])
    assert "[NOT CONFIRMED]" in lines[1] and "[done]" in lines[2]


# ---- the numbers ------------------------------------------------------------------------------------------------------------

@pytest.fixture
def measuring(monkeypatch):
    monkeypatch.setattr(metrics, "_enabled", True)


def test_measurements_hold_numbers_never_content(tmp_path, measuring):
    svc, _b = desk(tmp_path, [Win(5, GFG, "Safari", True)], {5: PROBLEM + " SECRET_WORD"})
    Perception(svc).observe("read the problem SECRET_QUESTION")
    svc.gui({"action": "key", "keys": "enter", "expect": {"text": "SECRET_WORD"}})
    raw = (tmp_path / "logs" / metrics.FILE).read_text()
    assert "SECRET" not in raw and GFG not in raw and "Safari" not in raw
    stats = metrics.summary(tmp_path)
    assert stats["perception_levels"] == {"accessibility": 1} and stats["answered_without_a_model"] == 1
    assert stats["actions"]["verified"] == 1 and stats["actions"]["total"] == 1 and stats["stages"]["perception"]["n"] == 1


def test_the_summary_tells_failures_recoveries_and_models_apart(tmp_path, measuring):
    for stage, ms, fields in (("model", 21000, {}), ("model", 0, {"reused": True}), ("perception", 300, {"level": "accessibility"}),
                              ("perception", 200, {"level": "ocr", "unchanged": True}), ("perception", 100, {"level": "escalated"}),
                              ("perception", 50, {"level": "accessibility", "kind": "find"}), ("action", 500, {"ok": True, "recovered": 1}),
                              ("action", 90, {"ok": False, "cancelled": True}), ("action", 80, {"ok": False}),
                              ("verification", 40, {"verified": True}), ("verification", 40, {"verified": False}), ("task", 9000, {})):
        metrics.record(stage, ms, tmp_path, **fields)
    stats = metrics.summary(tmp_path)
    assert stats["looks"] == 3 and stats["answered_without_a_model"] == 2 and stats["vision_answers_reused"] == 1
    assert stats["unchanged_observations"] == 1 and stats["failure_rate"] == round(2 / 3, 3) and stats["recovery_rate"] == round(1 / 3, 3)
    assert stats["actions"]["cancelled"] == 1 and stats["actions"]["not_as_expected"] == 1
    assert "Looks: 3" in metrics.describe(stats)


def test_nothing_measured_is_said_as_such(tmp_path):
    stats = metrics.summary(tmp_path)
    assert stats["stages"] == {} and stats["failure_rate"] is None and "Nothing measured" in metrics.describe(stats)


def test_the_log_stays_small(tmp_path, measuring, monkeypatch):
    monkeypatch.setattr(metrics, "MAX_BYTES", 2000)
    for _ in range(200):
        metrics.record("action", 1, tmp_path, ok=True)
    assert (tmp_path / "logs" / metrics.FILE).stat().st_size <= 2100
    assert metrics.summary(tmp_path)["actions"]["total"] > 0


def test_a_measurement_never_breaks_what_it_measures(tmp_path, measuring):
    blocked = tmp_path / "file"
    blocked.write_text("x")
    metrics.record("action", 1, blocked)  # the folder cannot exist: nothing raised


def test_concurrent_recording_keeps_whole_lines(tmp_path, measuring):
    threads = [threading.Thread(target=lambda: [metrics.record("action", 1, tmp_path, ok=True) for _ in range(50)]) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert metrics.summary(tmp_path)["actions"]["total"] == 200


# ---- on-device OCR's reading order -----------------------------------------------------------------------------------------

def test_ocr_runs_in_the_helper_and_rests_after_a_failure(monkeypatch):
    """Measured: a window capture inside Hermes' Python blocked for 30 s; in the helper it takes 10 ms."""
    from app.desktop import macos, ocr

    calls = []

    class Helper:
        def __init__(self, fail=False):
            self.fail = fail

        def call(self, cmd, timeout=None, **args):
            calls.append((cmd, timeout, args))
            if self.fail:
                raise macos.MacError("the helper timed out")
            return {"text": "hello", "lines": []}

    monkeypatch.setattr(ocr, "available", lambda: True)
    monkeypatch.setattr(ocr, "_resting_until", 0.0)
    monkeypatch.setattr(macos, "helper", lambda: Helper())
    clock = [100.0]
    assert ocr.read_window(7, (0, 0, 10, 10), clock=lambda: clock[0])["text"] == "hello"
    assert calls[-1] == ("ocr", ocr.READ_TIMEOUT, {"id": 7, "rect": [0, 0, 10, 10]})
    monkeypatch.setattr(macos, "helper", lambda: Helper(fail=True))
    with pytest.raises(ocr.OcrError, match="timed out"):
        ocr.read_window(7, (0, 0, 10, 10), clock=lambda: clock[0])
    calls.clear()
    with pytest.raises(ocr.OcrError, match="resting"):
        ocr.read_window(7, (0, 0, 10, 10), clock=lambda: clock[0])  # no look waits for it twice
    assert calls == []
    clock[0] += ocr.REST_SECONDS + 1
    monkeypatch.setattr(macos, "helper", lambda: Helper())
    assert ocr.read_window(7, (0, 0, 10, 10), clock=lambda: clock[0])["text"] == "hello"


def test_two_columns_are_not_interleaved():
    from app.desktop.ocr import columns, in_reading_order

    lines = [{"text": "Problem statement", "box": (0.02, 0.1, 0.3, 0.02)}, {"text": "int main() {", "box": (0.55, 0.1, 0.3, 0.02)},
             {"text": "Find the maximum", "box": (0.02, 0.2, 0.3, 0.02)}, {"text": "return 0;", "box": (0.55, 0.2, 0.3, 0.02)}]
    assert len(columns(lines)) == 2
    assert in_reading_order(lines) == "Problem statement\nFind the maximum\n\nint main() {\nreturn 0;"
    prose = [{"text": "one long line across the window", "box": (0.05, 0.1, 0.9, 0.02)}, {"text": "and another", "box": (0.05, 0.2, 0.3, 0.02)}]
    assert len(columns(prose)) == 1


