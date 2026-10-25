"""Solving a coding problem on an authorized practice page, reliably (2026-10-25, from a live session on GeeksforGeeks).

Measured in that session and reproduced on a local practice page with the real Ace editor in Safari:

* an Esc the page does not use (no completion list open) ends Safari's full screen - and code typing pressed Esc after
  every word, so the first one ended it; the keys typed during the animation were lost ("#include <bits/stdc++.h>" came
  out "#includedc++.h>"), so every program was typed twice (77 s for 483 characters), and out of full screen the Dock
  covered the Compile & Run button;
* GeeksforGeeks refuses pasting (6 of 6 pastes; the user confirmed it), which cost a paste and a model round trip each
  time; a program was pasted into XENIO's own quick-chat box (it holds the keyboard while it is open);
* replies said "compiled with no errors" when the run showed no result at all.

Each fix is tested here with fakes that behave the way the real editor and browser were measured to: no real input
is sent. The live-test guard (no answers in contests, assessments or interviews) is tested to stay in force on every
new path.
"""

import asyncio
import re
import threading
import time
from types import SimpleNamespace

import pytest

from app.desktop import service as service_mod
from app.desktop.input import DesktopController, split_units
from app.desktop.service import DesktopError, DesktopService, code_pieces
from app.desktop.task import EditorTask, TaskState, TransitionError, code_facts, outcome
from app.hermes.client import HermesReply, ToolActivity
from app.orchestrator import MSG_DUPLICATE, Orchestrator, claims_outcome, code_claim_check
from app.safety import policy as p
from app.tools import desktop as desktop_tools
from tests.test_desktop import FakeBackend, FakeWindows, Win, _turn, load, service
from tests.test_editor_writing import CODE, VK, WORDS, EditorBackend, FakeClipboard
from tests.test_orchestrator import FakeHermes, make
from tests.test_screen_code import JudgePage, PageUIA, on_click

GFG = "Range Max Count | Practice | GeeksforGeeks"
SQUASH = re.compile(r"\s+")


def squash(text):
    return SQUASH.sub("", text)


# ---- fakes: the editor reached the macOS way, inside a browser in full screen ------------------------------------------

class StringEditor(EditorBackend):
    """The test editor (auto-pairs, auto-indent, completions that take punctuation and Enter) reached the macOS way:
    one key event can carry a string, which the editor takes as ONE insertion - no bracket closed, no suggestion taken
    inside it; a completion list opens when an insertion ends in a word (measured in Ace). Around it, Safari in full
    screen: an Esc the page does not use - no list open - ends full screen (measured)."""

    inserts_strings = True

    def __init__(self, clipboard, drop=(), on_insert=None, click_exits=False, **kw):
        super().__init__(clipboard, **kw)
        self.fullscreen, self.esc_unhandled, self.inserts = True, 0, 0
        self.drop, self.on_insert, self.click_exits = set(drop), on_insert, click_exits

    def unicode_tap(self, units):
        text = "".join(chr(u) for u in units)
        if len(text) == 1:
            return super().unicode_tap(units)
        FakeBackend.unicode_tap(self, units)
        self.inserts += 1
        if self.on_insert is not None:
            self.on_insert(self.inserts)
        if self.inserts in self.drop or "*" in self.drop:
            text = text[:1]  # keys lost (measured: during the full-screen animation)
        if self._sel():
            self._replace(*self._sel(), "")
        self._replace(self.cur, self.cur, text)
        word = self._word_before()
        self.popup = next((w for w in WORDS if w.startswith(word) and w != word), None) \
            if word and re.match(r"\w", text[-1]) else None

    def key(self, vk, up):
        name = VK.get(vk)
        if not up and name == "esc" and self.popup is None:
            self.esc_unhandled += 1
            self.fullscreen = False
        if not up and name == "backspace":
            FakeBackend.key(self, vk, up)
            self.down.add(name)
            if self._sel():
                self._replace(*self._sel(), "")
            elif self.cur > 0:
                self._replace(self.cur - 1, self.cur, "")
            return
        super().key(vk, up)

    def mouse(self, flags, data=0):
        super().mouse(flags, data)
        if flags == 0x2 and self.click_exits:
            self.fullscreen = False


class KeysOnly(StringEditor):
    """The same browser, typed into key by key (the way XENIO typed before: Esc after every word)."""

    inserts_strings = False


class FsWindows(FakeWindows):
    """Windows whose full screen is the browser's (the service reads and restores it)."""

    def __init__(self, wins, browser):
        super().__init__(wins)
        self.browser, self.set_calls, self.maximized = browser, [], []

    def fullscreen(self, hwnd, set_to=None):
        if set_to is not None:
            self.set_calls.append(set_to)
            self.browser.fullscreen = set_to
        return self.browser.fullscreen

    def maximize(self, hwnd):
        self.maximized.append(hwnd)
        return True


class LivePage(PageUIA):
    """The page's text and its editor, which shows what the editor backend holds (also in the read)."""

    def read_text(self, hwnd, max_chars=60000):
        out = super().read_text(hwnd, max_chars)
        out["editors"] = [dict(out["editors"][0], text=self.backend.shown())]
        return out


def practice(tmp_path, title=GFG, wins=None, backend=StringEditor, uia=None, **editor):
    cb = FakeClipboard()
    ed = backend(cb, **editor)
    svc, _ = service(tmp_path, wins or [Win(1, title, "Safari", True)])
    svc._controller, svc.clipboard = DesktopController(ed, sleep=lambda s: None), cb
    svc.windows = FsWindows(svc.windows.wins, ed)
    svc.uia = uia(ed) if uia is not None else LivePage(ed)
    return svc, ed, cb


def write(svc, text=CODE, session="", **extra):
    return svc.gui({"action": "paste", "editor": True, "select_all": True, "text": text, **extra}, session=session)


def presses(ed, key):
    vk = next(v for v, k in VK.items() if k == key)
    return sum(1 for e in ed.events if e == ("key", vk, "down"))


# ---- full screen: no Esc, measured before and after, restored when XENIO's keys end it ------------------------------------

def test_code_is_typed_without_a_single_esc_and_full_screen_is_kept(tmp_path):
    svc, ed, _cb = practice(tmp_path)
    out = write(svc)
    assert ed.buf == CODE and out["verified"] is True
    assert presses(ed, "esc") == 0 and ed.esc_unhandled == 0
    assert out["fullscreen"] == {"before": True, "after": True, "kept": True}  # measured, not assumed
    assert svc.windows.set_calls == []  # nothing to restore


def test_the_old_key_by_key_typing_ended_full_screen_and_xenio_now_puts_it_back(tmp_path):
    """Why: the per-word Esc found no list open after "include" and ended full screen (the root cause)."""
    svc, ed, _cb = practice(tmp_path, backend=KeysOnly)
    out = svc.gui({"action": "type", "code": True, "editor": True, "select_all": True, "text": CODE})
    assert ed.buf == CODE and ed.esc_unhandled >= 1  # the old way still presses Esc (it remains only where strings cannot be sent)
    assert out["fullscreen"]["before"] is True and out["fullscreen"]["restored"] is True and ed.fullscreen is True
    assert out["fullscreen"]["kept"] is False and "put it back" in out["fullscreen"]["note"]


def test_an_esc_the_model_presses_is_undone_but_a_click_that_ends_full_screen_is_only_reported(tmp_path):
    svc, ed, _cb = practice(tmp_path)
    out = svc.gui({"action": "key", "keys": "esc"})
    assert out["fullscreen"]["restored"] is True and ed.fullscreen is True
    svc, ed, _cb = practice(tmp_path / "2", click_exits=True)
    out = svc.gui({"action": "click", "x": 10, "y": 10})
    assert ed.fullscreen is False and "restored" not in out["fullscreen"] and out["fullscreen"]["after"] is False
    assert "left like that" in out["fullscreen"]["note"]  # reported, never hidden


def test_a_pages_own_full_screen_is_reported_never_faked_back(tmp_path):
    """A page's full screen (an editor's own full-screen button) ends on any Esc and only the page can bring it back:
    setting the window's system full screen would be another mode, so it is reported as lost."""
    svc, ed, _cb = practice(tmp_path)

    class PageFs(FsWindows):
        def fullscreen(self, hwnd, set_to=None):
            if set_to is not None:
                self.set_calls.append(set_to)
            return "page" if self.browser.fullscreen else False

    svc.windows = PageFs(svc.windows.wins, ed)
    out = svc.gui({"action": "key", "keys": "esc"})
    assert out["fullscreen"]["after"] is False and "restored" not in out["fullscreen"]
    assert "cannot put a page's full screen back" in out["fullscreen"]["note"] and svc.windows.set_calls == []


def test_maximize_leaves_a_full_screen_window_alone(tmp_path):
    svc, _ed, _cb = practice(tmp_path)
    out = svc.window("maximize", "Safari")
    assert out["result"] == "already full screen" and svc.windows.maximized == []


def test_lines_are_typed_in_pieces_that_never_end_in_a_word_or_after_equals():
    line = "        for (int idx = 0; idx < (int)values.size(); idx++) { total += values[idx]; }"
    pieces = code_pieces(line)
    assert "".join(pieces) == line and all(len(split_units(piece)) == 1 for piece in pieces)
    assert all(not re.fullmatch(r"[\w=]", piece[-1]) for piece in pieces[:-1])
    assert code_pieces("x" * 45) == ["x" * 20, "x" * 20, "x" * 5]  # one very long word: cut anyway
    assert code_pieces("😀" * 15)[0] == "😀" * 10  # an emoji is two units: never split
    assert split_units("a😀b", 2) == ["a", "😀", "b"]


def test_a_line_ending_in_a_word_gets_a_space_then_backspace_so_enter_takes_no_suggestion(tmp_path):
    """Measured in Ace: "return x" then Enter came out "return xor"."""
    program = "int main() {\n    int possible = 0, pos = 1\n    return pos\n    double m2 =\n}"
    svc, ed, _cb = practice(tmp_path)
    out = write(svc, program)
    assert "possible = 0, pos = 1\n" in ed.buf and "return pos\n" in ed.buf and "return possible" not in ed.buf
    assert "double m2 = \n" in ed.buf  # after "=" the space stays: Backspace could bring macOS's offer back
    assert out["verified"] is True and squash(ed.buf) == squash(program)


# ---- paste refusal, write ledger, evidence-based retries -------------------------------------------------------------------

def test_geeksforgeeks_gets_the_program_typed_without_a_paste_attempt(tmp_path):
    svc, ed, cb = practice(tmp_path, allow_paste=False, allow_copy=False)
    out = write(svc)
    assert ed.buf == CODE and out["verified"] is True and "does not accept pasting" in out["typed_instead"]
    assert presses(ed, "v") == 0 and cb.text == "user"  # no paste tried; the clipboard untouched
    assert presses(ed, "c") == 0  # nor a copy it refuses (measured: 1.5 s waiting, every time)
    assert out["outcome"]["code_entered"] is True and out["outcome"]["editor_verified"] is True


def test_a_refused_paste_elsewhere_is_typed_in_the_same_call_and_remembered(tmp_path):
    title = "Two Sum - LeetCode"
    svc, ed, _cb = practice(tmp_path, title=title, allow_paste=False)
    out = write(svc)
    assert ed.buf == CODE and out["verified"] is True and "refused the paste" in out["typed_instead"]
    assert presses(ed, "v") == 1
    svc.new_turn()
    ed.buf, ed.cur = "// code here", 12
    write(svc)
    assert presses(ed, "v") == 1  # learned: the next request types it directly


def test_the_same_program_is_not_written_twice_in_one_request(tmp_path):
    svc, ed, _cb = practice(tmp_path)
    first = write(svc)
    typed = len(ed.events)
    again = write(svc, run=False)
    assert first["verified"] is True and again["already_written"] is True and again["verified"] is True
    assert len(ed.events) == typed  # not one key
    assert svc.task().public()["writes"] == 1 and "not written again" in again["verification"]


def test_an_inconclusive_check_is_no_reason_to_write_again(tmp_path):
    """Nothing could read the editor back (no copy, no accessibility): the write stands."""
    svc, ed, _cb = practice(tmp_path, title="Notes", allow_copy=False, uia=lambda ed: PageUIA(ed, editor_found=False))
    first = svc.gui({"action": "type", "code": True, "select_all": True, "text": CODE})
    assert first["verified"] is None and first["outcome"]["inconclusive"] is True
    typed = len(ed.events)
    again = svc.gui({"action": "type", "code": True, "select_all": True, "text": CODE})
    assert again["already_written"] is True and len(ed.events) == typed


def test_a_changed_editor_is_evidence_and_rewrites_stay_bounded(tmp_path):
    svc, ed, _cb = practice(tmp_path)
    write(svc)
    ed.buf, ed.cur = "// someone typed here", 21
    out = write(svc)
    assert ed.buf == CODE and "already_written" not in out and svc.task().public()["writes"] == 2
    ed.buf, ed.cur = "// and again", 12
    with pytest.raises(DesktopError, match="NOT WRITTEN: this exact program was already written 2 times"):
        write(svc)
    assert ed.buf == "// and again"  # bounded: nothing typed


def test_the_same_snippet_typed_in_at_the_caret_may_repeat(tmp_path):
    """"Add the same line three times" is three writes: only whole-program writes are kept from repeating."""
    svc, ed, _cb = practice(tmp_path, text="")
    for _ in range(3):
        out = svc.gui({"action": "type", "code": True, "text": "x++;"})  # on a new line (it clears the caret's line)
        assert "already_written" not in out
        svc.gui({"action": "key", "keys": "enter"})
    assert ed.buf.count("x++;") == 3 and svc.task().public()["writes"] == 3


def test_an_unforeseen_error_never_leaves_the_task_in_flight(tmp_path, monkeypatch):
    svc, ed, _cb = practice(tmp_path)
    monkeypatch.setattr(svc, "_verify_write", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        write(svc)
    assert svc.task().state is TaskState.FAILED and not svc.task().in_flight
    monkeypatch.undo()
    assert write(svc)["verified"] is True  # the request can still write


def test_a_new_request_writes_normally(tmp_path):
    svc, ed, _cb = practice(tmp_path)
    write(svc, session="s1")
    svc.new_turn(session="s1", turn="t2")
    ed.buf, ed.cur = "// code here", 12
    out = write(svc, session="s1")
    assert ed.buf == CODE and "already_written" not in out and svc.task("s1").public()["writes"] == 1


def test_corruption_is_named_and_repaired_once(tmp_path):
    svc, ed, _cb = practice(tmp_path, drop={2})  # keys lost in the first line's second piece
    out = write(svc)
    assert ed.buf == CODE and out["verified"] is True and "typed a second time" in out["retyped"]
    assert out["corruption"]["line"] == 1 and out["corruption"]["kind"] == "missing characters"
    assert out["corruption"]["expected"] == "#include <bits/stdc++.h>"


def test_a_write_that_stays_wrong_is_retried_exactly_once_and_reported_as_wrong(tmp_path):
    svc, ed, _cb = practice(tmp_path, drop={"*"})
    out = write(svc)
    assert presses(ed, "a") == 2  # two typing passes (select all + type), never more
    assert out["verified"] is False and out["outcome"]["editor_verified"] is False
    assert "does NOT hold it exactly" in out["outcome"]["say"] and svc.task().state is TaskState.FAILED


# ---- one action at a time; cancellation; deadline ------------------------------------------------------------------------

def test_a_second_action_while_one_runs_is_refused_without_a_keystroke(tmp_path, monkeypatch):
    monkeypatch.setattr(service_mod, "BUSY_WAIT", 0.01)
    svc, ed, _cb = practice(tmp_path)
    started, release, errors = threading.Event(), threading.Event(), []

    def hold(n):
        if n == 1:
            started.set()
            release.wait(5)

    ed.on_insert = hold
    first = threading.Thread(target=lambda: write(svc))
    first.start()
    assert started.wait(5)
    try:
        write(svc)
    except DesktopError as exc:
        errors.append(str(exc))
    release.set()
    first.join(5)
    assert errors and errors[0].startswith("BUSY:") and "do not repeat it" in errors[0]
    assert ed.buf == CODE  # the first one finished alone, not mixed with a second


def test_a_newer_request_cancels_the_typing_of_the_older_one(tmp_path):
    svc, ed, _cb = practice(tmp_path)
    old = svc.task("s1")
    ed.on_insert = lambda n: svc.new_turn(session="s1", turn="t2") if n == 3 else None
    with pytest.raises(DesktopError, match=r"^CANCELLED at line \d+: a newer request started"):
        write(svc, session="s1")
    assert old.state is TaskState.CANCELLED and svc.task("s1") is not old
    assert ed.inserts == 3  # stopped at its next keystroke


def test_another_sessions_request_does_not_cancel_it(tmp_path):
    svc, ed, _cb = practice(tmp_path)
    ed.on_insert = lambda n: svc.new_turn(session="cron_1") if n == 3 else None
    assert write(svc, session="s1")["verified"] is True and ed.buf == CODE


def test_an_action_past_its_deadline_times_out(tmp_path, monkeypatch):
    monkeypatch.setattr(service_mod, "ACTION_DEADLINE", -1.0)
    svc, ed, _cb = practice(tmp_path)
    with pytest.raises(DesktopError, match="^TIMED_OUT"):
        write(svc)
    assert svc.task().state is TaskState.TIMED_OUT
    assert service_mod.ACTION_DEADLINE < 420  # below Hermes' own per-call deadline (then it abandons the thread)


# ---- XENIO's own window; the live-test guard stays in force on every path -----------------------------------------------

def test_code_never_goes_into_xenios_own_quick_chat(tmp_path):
    """Measured: a program for the GeeksforGeeks editor was pasted into the quick chat and reported verified."""
    wins = [Win(9, "", "XENIO", True), Win(1, GFG, "Safari")]
    svc, ed, _cb = practice(tmp_path, wins=wins)
    assert svc.probe({"action": "paste", "editor": True, "text": CODE})["title"] == GFG  # what the policy judges
    out = write(svc)
    assert out["app"] == "Safari" and svc.windows.focused == [1] and ed.buf == CODE
    assert DesktopService.is_own(SimpleNamespace(process="Something", bundle_id="local.xenio.orb"))


@pytest.mark.parametrize("title", ["Weekly Contest 412 - LeetCode", "Online Assessment | HackerRank",
                                   "Coding Round - Codility", "Proctored Test", "Interview Round 2 - CoderPad"])
def test_the_live_test_guard_still_blocks_every_coding_action(tmp_path, monkeypatch, title):
    """Also when XENIO's own window is in front: the policy is shown the window the action would really work in."""
    module, ctx, _ws = load(monkeypatch, tmp_path)
    svc, ed, _cb = practice(tmp_path / "svc", wins=[Win(9, "", "XENIO", True), Win(1, title, "Safari")])
    desktop_tools.set_service(svc)
    try:
        pre = ctx.hooks["pre_tool_call"]
        note = _turn(ctx, "solve this problem on my screen")
        assert "1<=n<=10^5" not in str((note or {}).get("context") or "")  # its questions are not even read
        assert not [c for c in svc.uia.calls if c[0] == "read_text"]
        for args in ({"action": "paste", "editor": True, "select_all": True, "text": CODE},
                     {"action": "type", "code": True, "editor": True, "select_all": True, "text": CODE},
                     {"action": "read"}, {"action": "run"}, {"action": "copy", "editor": True, "select_all": True}):
            out = pre(tool_name="xenio_gui", args=args, session_id="s1")
            assert out and out["action"] == "block" and "Blocked by XENIO policy" in out["message"], (title, args)
        assert ed.events == [] and ed.buf == "// code here"
    finally:
        desktop_tools.set_service(None)
    assert p.live_test(title) and not p.live_test("Mock Interview: Graphs")


def test_a_practice_page_behind_xenios_window_is_an_ordinary_write(tmp_path, monkeypatch):
    module, ctx, _ws = load(monkeypatch, tmp_path)
    svc, ed, _cb = practice(tmp_path / "svc", wins=[Win(9, "", "XENIO", True), Win(1, GFG, "Safari")])
    desktop_tools.set_service(svc)
    try:
        note = _turn(ctx, "solve this question on my screen")["context"]
        assert "Problem" in note and "editor holds now" in note  # read for the turn, with the editor's code
        assert pre_allowed(ctx, {"action": "paste", "editor": True, "select_all": True, "text": CODE})
        assert svc.task("s1").state is TaskState.SOLVING and svc.task("s1").window == 1
    finally:
        desktop_tools.set_service(None)


def pre_allowed(ctx, args):
    return ctx.hooks["pre_tool_call"](tool_name="xenio_gui", args=args, session_id="s1") is None


# ---- running the code: what the result really says ------------------------------------------------------------------------

def judge(tmp_path, result, at=None, press=None, **editor):
    class Page(JudgePage):
        def at(self, x, y):
            return at or {"found": False}

    if press is not None:
        Page.press = lambda self, hwnd, name: press(self, name)
    svc, ed, _cb = practice(tmp_path, uia=lambda ed: Page(result, backend=ed), **editor)
    if press is None:
        on_click(ed, svc.uia)
    return svc, ed


@pytest.mark.parametrize("result,status,compiled,tests,state", [
    ("Compilation Completed\nYour Output:\n3\n\nExpected Output:\n3", "matches_expected", True, True,
     TaskState.RESULT_CONFIRMED),
    ("Compilation Error\nprog.cpp:9:9: error: 'possible' was not declared in this scope", "error", False, False,
     TaskState.RESULT_FAILED),
    ("Compilation Results Custom Input\nYour program took more time than expected.\nHint : Please optimize your code",
     "error", True, False, TaskState.RESULT_FAILED),  # GeeksforGeeks' time limit (it was reported as "ran")
    ("Compilation Results Custom Input\n\nTime Limit Exceeded Test Cases Passed: 10010 /10014", "error", True, False,
     TaskState.RESULT_FAILED),
    ("Compilation Completed\nYour Output:\n2\n\nExpected Output:\n3", "wrong_output", True, False, TaskState.RESULT_FAILED),
])
def test_compiled_and_passed_come_only_from_the_run(tmp_path, result, status, compiled, tests, state):
    svc, _ed = judge(tmp_path, result)
    out = write(svc, run=True)
    assert out["run"]["status"] == status and out["outcome"]["compiled"] is compiled
    assert out["outcome"]["tests_passed"] is tests and svc.task().state is state
    assert code_facts(f"- **message:** {out['outcome']['say']} - **status:** x")["compiled"] is compiled


def test_no_result_on_the_page_is_inconclusive_never_compiled(tmp_path):
    """Measured twice: status no_output, and the reply said "compiled with no errors"."""
    svc, ed = judge(tmp_path, "never shown")
    svc.uia.ran = 0
    ed.mouse = FakeBackend.mouse.__get__(ed)  # the click does nothing on this page
    out = write(svc, run=True)
    assert out["run"]["status"] == "no_output" and out["outcome"]["compiled"] is None
    assert out["outcome"]["inconclusive"] is True and "NOT known whether it compiled" in out["outcome"]["say"]
    assert svc.task().state is TaskState.INCONCLUSIVE


def test_test_counts_on_the_page_decide_tests_passed(tmp_path):
    svc, _ed = judge(tmp_path, "Compilation Results\nTime Limit Exceeded\nTest Cases Passed: 10010 /10014")
    out = write(svc, run=True)
    assert out["run"]["tests"] == {"passed": 10010, "total": 10014} and out["outcome"]["tests_passed"] is False
    assert "test cases passed: 10010/10014" in out["outcome"]["say"]


def test_a_covered_run_button_is_pressed_through_accessibility(tmp_path):
    """Measured: out of full screen the Dock covered Compile & Run; the click went to the Dock."""
    pressed = []

    def press(page, name):
        pressed.append(name)
        page.ran += 1
        return {"found": True, "pressed": True}

    svc, ed = judge(tmp_path, "Compilation Completed\nYour Output:\n3\n\nExpected Output:\n3", press=press,
                    at={"found": True, "name": "Spotify", "pid": 4242, "parents": ["Dock"]})
    out = svc.gui({"action": "run"})
    assert pressed == ["Compile & Run"] and out["clicked_by"] == "accessibility" and out["status"] == "matches_expected"
    assert not [e for e in ed.events if e[0] == "mouse"]  # nothing clicked on the Dock
    assert out["outcome"]["say"].startswith("It compiled and its output matches")


def test_a_covered_run_button_that_cannot_be_pressed_runs_nothing(tmp_path):
    svc, ed = judge(tmp_path, "Accepted", at={"found": True, "name": "Finder", "pid": 4242, "parents": []},
                    press=lambda page, name: {"found": False, "pressed": False})
    with pytest.raises(DesktopError, match="covered .* nothing was run"):
        svc.gui({"action": "run"})
    assert not [e for e in ed.events if e[0] == "mouse"] and svc.task().state is TaskState.FAILED


def test_the_button_is_clicked_when_the_point_really_is_the_button(tmp_path):
    svc, ed = judge(tmp_path, "Compilation Completed\nYour Output:\n3\n\nExpected Output:\n3",
                    at={"found": True, "name": "Compile & Run", "pid": None, "parents": []})
    assert svc.gui({"action": "run"})["clicked_by"] == "mouse" and ("move", 900, 800) in ed.events


# ---- the task's states ----------------------------------------------------------------------------------------------------

def test_the_task_follows_its_states_and_refuses_invalid_steps():
    t = EditorTask()
    for state in ("INSPECTING", "SOLVING", "WRITING", "VERIFYING", "VERIFIED", "RUNNING", "RESULT_CONFIRMED"):
        t.to(TaskState(state))
    assert t.public()["steps"][-1] == "RESULT_CONFIRMED"
    for bad in (TaskState.VERIFYING, TaskState.VERIFIED, TaskState.RESULT_FAILED):
        with pytest.raises(TransitionError):
            t.to(bad)  # e.g. a result without a run, verification without a write
    t.to(TaskState.WRITING)
    with pytest.raises(TransitionError):
        t.to(TaskState.WRITING)  # a second write while one is in flight
    with pytest.raises(TransitionError, match="still running"):
        t.admit_write("x", 1)
    t.end(TaskState.CANCELLED)
    assert t.state is TaskState.CANCELLED and not t.can(TaskState.WRITING)  # the next request is a new task
    t.end(TaskState.FAILED)  # an ending never raises
    assert t.state is TaskState.CANCELLED


def test_the_ledger_keeps_digests_never_the_program():
    t = EditorTask()
    w = t.record_write("int main() {}", 7, "typed", True)
    assert "main" not in str(w) and t.last_write("int  main()  {}", 7) is w  # spacing aside
    assert t.last_write("int main() {}", 8) is None  # another window


@pytest.mark.parametrize("entered,verified,run,expect", [
    (True, True, None, "The code was entered and verified in the editor; it has not been compiled or run."),
    (True, None, None, "could not be read back"),
    (False, None, None, "The code was NOT entered"),
    (True, True, {"ran": False}, "it was not run"),
    (None, None, {"ran": True, "status": "matches_expected"}, "It compiled and its output matches"),
])
def test_four_facts_are_never_merged(entered, verified, run, expect):
    out = outcome(entered, verified, run)
    assert expect in out["say"] and out["say"].endswith(".")
    if run is None or not run.get("ran"):
        assert out["compiled"] is None and out["tests_passed"] is None  # nothing ran: nothing compiled or passed


# ---- duplicate requests and honest replies (orchestrator) ------------------------------------------------------------------

async def test_the_same_request_twice_while_it_runs_is_run_once():
    hermes = FakeHermes(delay=0.05)
    orch = make(hermes)
    first, second = await asyncio.gather(orch.process_text("solve this on my screen"),
                                         orch.process_text("  Solve this on my  SCREEN "))
    assert len(hermes.messages) == 1 and first.ok and second.error == "duplicate" and second.text == MSG_DUPLICATE
    assert first.command_id and second.command_id and first.command_id != second.command_id
    again = await orch.process_text("solve this on my screen")  # it ended: a new request runs normally
    assert again.ok and len(hermes.messages) == 2
    a, b = await asyncio.gather(orch.process_text("open notes"), orch.process_text("what time is it"))
    assert a.ok and b.ok and len(hermes.messages) == 4  # different requests: queued, both run


async def test_a_stop_also_stops_xenios_hands(tmp_path):
    from pathlib import Path

    hermes = FakeHermes(delay=0.2)
    orch = Orchestrator(hermes, None, Path(tmp_path))
    orch.stop_dir = tmp_path
    turn = asyncio.ensure_future(orch.process_text("type the program"))
    await asyncio.sleep(0.02)
    await orch.interrupt()
    await turn
    assert hermes.cancelled == 1 and (tmp_path / "gui_stop").read_text().split()[1] == "cancel"
    quiet = Orchestrator(FakeHermes(delay=0.2), None, Path(tmp_path / "w"))  # no folder given: never writes one
    turn = asyncio.ensure_future(quiet.process_text("x"))
    await asyncio.sleep(0.02)
    await quiet.interrupt()
    await turn
    assert not (tmp_path / "w" / "gui_stop").exists()


def gui_call(say):
    return ToolActivity("t1", title="xenio_gui: #include <bits/stdc++.h> ...", status="completed",
                        result=f"✅ xenio_gui completed - **message:** {say} - **status:** UNVERIFIED")


NO_RESULT = outcome(True, True, {"ran": True, "status": "no_output"})["say"]


@pytest.mark.parametrize("reply,calls,corrected", [
    # the session's own replies (2026-10-25)
    ("It typed successfully into the editor and compiled with no errors.", [gui_call(NO_RESULT)], True),
    ("Wrote the complete C++ program in the editor and stayed full screen. Compile finished clean.",
     [gui_call(NO_RESULT)], True),
    ("Done — pasted full C++ solution in the right-side editor: finds max and counts in range per query.", [], True),
    ("Confirmed — 10014/10014 passed, 8/8 points, solved successfully.", [], False),  # the user's own Submit, seen
    ("I typed your note into the editor.", [ToolActivity("t2", title="xenio_gui: type", status="completed",
                                                         result="✅ xenio_gui completed - **message:** done")], False),
    ("Still TLE — brute-force scan is too slow.", [gui_call(outcome(True, True, {"ran": True, "status": "error",
                                                                                 "kind": "time_limit",
                                                                                 "kind_text": "time limit"})["say"])],
     False),
    ("It compiled and the example passed.", [gui_call(outcome(True, True, {"ran": True,
                                                                           "status": "matches_expected"})["say"])], False),
])
def test_replies_claiming_more_than_the_run_showed_are_corrected(reply, calls, corrected):
    note = code_claim_check(reply, calls)
    assert bool(note) is corrected, note
    if corrected:
        assert note.startswith("(XENIO correction: ") and ("NOT known" in note or "nothing was written" in note)


async def test_the_correction_reaches_the_reply_and_voice_holds_the_claim_back():
    reply = HermesReply("It typed successfully into the editor and compiled with no errors.",
                        tool_calls=[gui_call(NO_RESULT)])
    orch = make(FakeHermes(reply=reply))
    out = await orch.process_text("solve this on my screen")
    assert "XENIO correction" in out.text and "NOT known whether it compiled" in out.text
    assert claims_outcome("compiled with no errors") and claims_outcome("all test cases passed")


def test_hermes_report_of_a_finished_call_is_kept(make_settings, tmp_path):
    from app.hermes.adapter import HermesAcpClient, _Turn

    client = HermesAcpClient(make_settings().hermes, session_file=tmp_path / "s.json", hermes_home=tmp_path / "h")
    client._session_id, client._turn = "s1", _Turn(on_chunk=None)
    base = {"sessionId": "s1"}
    client._on_notification("session/update", {**base, "update": {"sessionUpdate": "tool_call", "toolCallId": "t1",
                                                                  "title": "xenio_gui: x", "status": "in_progress"}})
    text = f"✅ xenio_gui completed\n- **message:** {NO_RESULT}"
    client._on_notification("session/update", {**base, "update": {
        "sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "completed",
        "content": [{"type": "content", "content": {"type": "text", "text": text}}]}})
    activity = client._turn.tools["t1"]
    assert code_facts(activity.result)["compiled"] is None and "NOT known" in code_facts(activity.result)["say"]
    asyncio.run(client.close())


# ---- the UI: two quick submits are one request -----------------------------------------------------------------------------

from tests.test_engineering_service import repo, settings  # noqa: E402,F401 - fixtures for the UI stack
from tests.test_ui_backend import stack  # noqa: E402,F401


def test_two_quick_submits_start_one_turn(stack):  # noqa: F811
    from app.ui.controller import UiError

    stack.orch.release = asyncio.Event()
    stack.ctl.run(stack.ctl._begin("t1", "solve this on my screen", "text"))
    with pytest.raises(UiError, match="still answering"):
        # both passed the check in their own request threads before either turn started (the race)
        stack.ctl.run(stack.ctl._begin("t2", "solve this on my screen", "text"))
    stack.loop.call_soon_threadsafe(stack.orch.release.set)
    deadline = time.monotonic() + 5
    while stack.ctl.turn_running() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not stack.ctl.turn_running()
