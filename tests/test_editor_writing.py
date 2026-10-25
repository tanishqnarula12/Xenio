"""Writing and reading code in the user's own apps and web editors (V3): paste through the clipboard (the user's clipboard
put back), code typed line by line for editors that refuse pastes, copy to read text exactly - each checked by reading
the editor back - and the rules: no secrets, no password fields, nothing into tests, contests or interviews.

The editor here is simulated with the habits real ones have (measured live on Ace and Monaco): brackets closed
automatically, lines auto-indented, a closing brace re-indented, a completion list that takes punctuation and Enter,
and pages that refuse pastes or copies. No real keys are sent."""

import json
import re

import pytest

from app.desktop import clipboard as C
from app.desktop.input import _VK
from app.desktop.service import DesktopError
from app.safety import policy as p
from app.safety.policy import RiskLevel
from app.tools import desktop as desktop_tools
from tests.test_desktop import FakeBackend, FakeUIA, Win, _turn, controller, load, service  # noqa: F401

CODE = """#include <bits/stdc++.h>
using namespace std;

int add(vector<int>& a) {
    int s = 0, pos = 0;
    for (int i = 0; i < (int)a.size(); i++) {
        if (a[i] > 0) s += a[i];
    }
    string t = "x(y)";
    return s;
}

int main() {
    vector<int> v = {1, 2, 3};
    cout << add(v) << '\\n';
    return 0;
}"""

VK = {v: k for k, v in _VK.items() if k in ("shift", "cmd", "enter", "esc", "right", "left", "down", "up", "pagedown",
                                            "a", "c", "v", "forwarddelete", "backspace")}
PAIRS = {"(": ")", "[": "]", "{": "}", '"': '"', "'": "'"}
WORDS = ["include", "integer", "just", "justify", "vector", "size", "string", "return", "cout", "main", "namespace",
         "possible"]


class FakeClipboard:
    def __init__(self, items=None):
        self.items, self.change, self.text = items if items is not None else [[["public.utf8-plain-text", "dXNlcg=="]]], 7, "user"

    def change_count(self):
        return self.change

    def save(self):
        return C.Saved(self.change, None if self.items is None else [list(i) for i in self.items])

    def set_text(self, text):
        self.text, self.items, self.change = text, [[["public.utf8-plain-text", "x"]]], self.change + 1
        return self.change

    def get_text(self):
        return self.text

    def restore(self, saved, expect):
        if saved.items is None or self.change != expect:
            return False
        self.items, self.text, self.change = saved.items, "user", self.change + 1
        return True


class EditorBackend(FakeBackend):
    """A code editor driven by key events: auto-pairs, auto-indent, electric '}', completions, refusals."""

    def __init__(self, clipboard, text="// code here", allow_paste=True, allow_copy=True, view=None, on_paste=None):
        super().__init__()
        self.cb, self.allow_paste, self.allow_copy = clipboard, allow_paste, allow_copy
        self.view, self.top = view, None  # lines drawn at a time (None: all); top line, None = follow the caret
        self.on_paste = on_paste or (lambda text: text)
        self.buf, self.cur, self.anchor = text, len(text), None
        self.auto = set()  # positions of closers the editor inserted itself (typed over, like Ace and Monaco)
        self.down, self.popup = set(), None

    # -- helpers
    def _sel(self):
        return (min(self.anchor, self.cur), max(self.anchor, self.cur)) if self.anchor is not None and self.anchor != self.cur else None

    def _replace(self, a, b, s):
        self.buf = self.buf[:a] + s + self.buf[b:]
        self.auto = {x + len(s) - (b - a) if x >= b else x for x in self.auto if not a <= x < b}
        self.cur, self.anchor = a + len(s), None

    def _line_start(self, pos):
        return self.buf.rfind("\n", 0, pos) + 1

    def _word_before(self):
        m = re.search(r"\w+$", self.buf[:self.cur])
        return m.group() if m else ""

    def shown(self):
        """The lines the editor draws (what the accessibility tree has): ``view`` lines around the caret, or from
        where the user scrolled."""
        lines = self.buf.split("\n")
        if self.view is None:
            return self.buf
        top = self.top if self.top is not None else max(0, self.buf[:self.cur].count("\n") - self.view + 1)
        return "\n".join(lines[top:top + self.view])

    # -- typing
    def unicode_tap(self, units):
        super().unicode_tap(units)
        ch = "".join(chr(u) for u in units)
        if self._sel():
            self._replace(*self._sel(), "")
        if self.popup and not re.match(r"\w", ch):
            # punctuation takes the suggestion (measured: Ace, "j)" -> "just)"), and on GeeksforGeeks a space too
            # ("pos == len" -> "possible == len")
            word = self._word_before()
            self._replace(self.cur - len(word), self.cur, self.popup)
            self.popup = None
        if ch in ")]}\"'" and self.cur in self.auto and self.buf[self.cur:self.cur + 1] == ch:
            self.auto.discard(self.cur)
            self.cur += 1
            return
        if ch == "}" and not self.buf[self._line_start(self.cur):self.cur].strip():
            # electric brace (Ace's autoOutdent, Monaco's bracket indentation): the line takes the indentation of the
            # line holding the matching "{"
            depth, pos = 0, self.cur - 1
            while pos >= 0 and not (self.buf[pos] == "{" and depth == 0):
                depth += {"}": 1, "{": -1}.get(self.buf[pos], 0)
                pos -= 1
            if pos >= 0:
                opener = self.buf[self._line_start(pos):pos]
                start = self._line_start(self.cur)
                self._replace(start, self.cur, " " * (len(opener) - len(opener.lstrip(" "))))
        self._replace(self.cur, self.cur, ch)
        if ch in PAIRS:
            self._replace(self.cur, self.cur, PAIRS[ch])
            self.cur -= 1
            self.auto.add(self.cur)
        if re.match(r"\w", ch):
            word = self._word_before()
            self.popup = next((w for w in WORDS if w.startswith(word) and w != word), None)

    def key(self, vk, up):
        super().key(vk, up)
        name = VK.get(vk)
        if up:
            self.down.discard(name)
            return
        self.down.add(name)
        mods = self.down - {name}
        if name in ("cmd", "shift"):
            return
        if name == "a" and "cmd" in mods:
            self.anchor, self.cur = 0, len(self.buf)
        elif name == "c" and "cmd" in mods:
            if self.allow_copy and self._sel():
                self.cb.text, self.cb.change = self.buf[slice(*self._sel())], self.cb.change + 1
        elif name == "v" and "cmd" in mods:
            if self.allow_paste:
                self._replace(*(self._sel() or (self.cur, self.cur)), self.on_paste(self.cb.text))
                self.top = None
        elif name == "down" and {"cmd", "shift"} <= mods:
            self.anchor, self.cur = self.cur, len(self.buf)
        elif name == "down" and mods == {"cmd"}:
            self.anchor, self.cur, self.top = None, len(self.buf), None
        elif name == "up" and mods == {"cmd"}:
            self.anchor, self.cur, self.top = None, 0, 0
        elif name == "pagedown" and self.view:
            self.top = min((self.top or 0) + self.view, max(0, self.buf.count("\n") - self.view + 1))
        elif name == "left" and {"cmd", "shift"} <= mods:
            start = self._line_start(self.cur)
            first = start + len(self.buf[start:self.cur]) - len(self.buf[start:self.cur].lstrip(" "))
            self.anchor, self.cur = self.cur, (start if first == self.cur else first)
        elif name == "forwarddelete":
            if self._sel():
                self._replace(*self._sel(), "")
            elif self.cur < len(self.buf):
                self._replace(self.cur, self.cur + 1, "")
        elif name == "right":
            self.cur, self.anchor = (self._sel()[1] if self._sel() else min(len(self.buf), self.cur + 1)), None
        elif name == "esc":
            self.popup = None
        elif name == "enter":
            if self.popup:  # Enter takes the suggestion, no new line
                word = self._word_before()
                self._replace(self.cur - len(word), self.cur, self.popup)
                self.popup = None
                return
            start = self._line_start(self.cur)
            indent = len(self.buf[start:self.cur]) - len(self.buf[start:self.cur].lstrip(" "))
            extra = 4 if self.buf[:self.cur].rstrip(" ").endswith("{") else 0
            if self.buf[self.cur:self.cur + 1] == "}":  # "{|}" + Enter: the brace goes to its own line below
                self._replace(self.cur, self.cur, "\n" + " " * (indent + extra) + "\n" + " " * indent)
                self.cur -= indent + 1
            else:
                self._replace(self.cur, self.cur, "\n" + " " * (indent + extra))


def editor_service(tmp_path, **editor):
    cb = FakeClipboard()
    backend = EditorBackend(cb, **editor)
    from app.desktop.input import DesktopController

    c = DesktopController(backend, sleep=lambda s: None)
    svc, _ = service(tmp_path, [Win(1, "Polynomial Multiplication | Practice | GeeksforGeeks", "Safari", True)])
    svc._controller, svc.clipboard = c, cb
    return svc, backend, cb


# ---- the mechanics ---------------------------------------------------------------------------------------------------

def test_a_naively_typed_program_comes_out_mangled_in_such_an_editor(tmp_path):
    """Why code mode exists: the same editor, plain typing."""
    svc, editor, _cb = editor_service(tmp_path, text="")
    svc.gui({"action": "type", "text": CODE})
    assert editor.buf != CODE


def test_code_typing_gets_exactly_the_program_into_an_editor_that_refuses_pastes(tmp_path):
    svc, editor, cb = editor_service(tmp_path, allow_paste=False)
    pasted = svc.gui({"action": "paste", "text": CODE, "select_all": True})
    assert pasted["verified"] is False and "type it instead" in pasted["hint"] and editor.buf == "// code here"
    assert cb.text == "user"  # the user's clipboard is back
    out = svc.gui({"action": "type", "code": True, "text": CODE, "select_all": True})
    assert editor.buf == CODE and out["verified"] is True and out["typed_lines"] == CODE.count("\n") + 1
    assert editor.anchor is None  # never left with everything selected


def test_paste_replaces_the_stub_verifies_and_puts_the_clipboard_back(tmp_path):
    svc, editor, cb = editor_service(tmp_path)
    out = svc.gui({"action": "paste", "text": CODE, "select_all": True})
    assert editor.buf == CODE and out["verified"] is True and out["clipboard"] == "put back as it was"
    assert cb.text == "user" and editor.anchor is None


def test_a_page_that_refuses_copying_is_reported_unverified_never_done(tmp_path):
    svc, editor, _cb = editor_service(tmp_path, allow_copy=False)
    out = svc.gui({"action": "paste", "text": CODE, "select_all": True})
    assert editor.buf == CODE and out["verified"] is None and "look_at_screen" in out["verification"]
    assert editor.anchor is None  # measured on GFG: the refused copy used to leave the whole editor selected


def test_copy_reads_exactly_and_marks_the_text_as_untrusted(tmp_path):
    svc, editor, cb = editor_service(tmp_path, text="Given two polynomials A and B")
    out = svc.gui({"action": "copy", "select_all": True})
    assert out["text"] == "Given two polynomials A and B" and out["untrusted_content"] is True and cb.text == "user"
    svc2, _e, _c = editor_service(tmp_path / "2", allow_copy=False)
    assert svc2.gui({"action": "copy", "select_all": True})["copied"] is False


def test_a_clipboard_too_big_to_keep_is_never_overwritten(tmp_path):
    svc, editor, cb = editor_service(tmp_path)
    cb.items = None  # e.g. a huge copied image
    with pytest.raises(DesktopError, match="clipboard is untouched"):
        svc.gui({"action": "paste", "text": CODE, "select_all": True})
    assert cb.text == "user" and editor.buf == "// code here"


def test_code_typing_says_where_it_stopped_when_another_app_takes_the_front(tmp_path):
    svc, editor, _cb = editor_service(tmp_path)
    wins = svc.windows.wins
    calls = {"n": 0}
    original = svc.windows.foreground_window

    def front():
        calls["n"] += 1
        if calls["n"] > 40:  # a notification app takes the front part-way
            wins[0].foreground = False
            return Win(9, "Dynamic Island", "Dynamic Island", True)
        return original()

    svc.windows.foreground_window = front
    with pytest.raises(DesktopError, match=r"stopped at line \d+ .*select_all=true"):
        svc.gui({"action": "type", "code": True, "text": CODE, "select_all": True})


def test_jxa_clipboard_script_and_limits():
    assert "org.nspasteboard.TransientType" in C._JXA  # clipboard-history apps do not keep XENIO's text
    assert str(C.MAX_SAVE_BYTES) in C._JXA and C.MAX_SAVE_BYTES >= 100_000_000  # a copied screenshot fits
    # measured: JXA returns NSData.length as a string, so "total += d.length" concatenated "0", "116", "4254"... and
    # a 4 KB copied link counted as too big to keep - every paste was refused
    assert "total += Number(d.length)" in C._JXA


@pytest.mark.skipif(not C.supported(), reason="macOS only")
def test_jxa_sizes_are_numbers_on_this_mac():
    import subprocess

    script = 'ObjC.import("Foundation"); var d = $("abc").dataUsingEncoding($.NSUTF8StringEncoding); ' \
             'var total = 0; total += Number(d.length); total += Number(d.length); String(total)'
    out = subprocess.run(["/usr/bin/osascript", "-l", "JavaScript", "-e", script], capture_output=True, text=True)
    assert out.stdout.strip() == "6"


# ---- the rules -------------------------------------------------------------------------------------------------------

def gui(args, **info):
    return p.classify_gui_action(args, info)


PRACTICE = {"app": "safari", "title": "Polynomial Multiplication | Practice | GeeksforGeeks", "focused": {"name": "", "type": "textarea"}}


def test_writing_code_into_a_practice_editor_is_an_ordinary_action():
    assert gui({"action": "paste", "text": CODE, "select_all": True}, **PRACTICE).level is RiskLevel.SAFE_WRITE
    assert gui({"action": "type", "code": True, "text": CODE}, **PRACTICE).level is RiskLevel.SAFE_WRITE
    assert gui({"action": "copy", "select_all": True}, **PRACTICE).level is RiskLevel.READ
    for title in ("Top Interview 150 - Two Sum - LeetCode", "DSA Mock Test 3 | GeeksforGeeks", "Placement Test Series - Day 4",
                  "Arrays Quiz | Practice", "Virtual Contest - Weekly Contest 412", "Mock Interview: Graphs",
                  "Practice Exam - Data Structures", "Upsolving Codeforces Round 951"):
        study = dict(PRACTICE, title=title)
        assert not gui({"action": "paste", "text": CODE}, **study).blocked, title
        assert not gui({"action": "copy", "select_all": True}, **study).blocked, title


@pytest.mark.parametrize("title", ["Weekly Contest 412 - LeetCode", "Online Assessment | HackerRank", "Coding Round - Codility",
                                   "Mid-term Exam - DSA", "Proctored Test", "Interview Round 2 - CoderPad"])
def test_no_answers_are_written_or_questions_read_in_tests_contests_or_interviews(title):
    info = dict(PRACTICE, title=title)
    for args in ({"action": "paste", "text": CODE}, {"action": "type", "code": True, "text": CODE},
                 {"action": "type", "text": "int main() {\n}"}, {"action": "copy", "select_all": True}):
        decision = gui(args, **info)
        assert decision.blocked and "outside help" in decision.reason or "answer them" in decision.reason, (title, args)
    assert not gui({"action": "type", "text": "Tanishq Narula"}, **info).blocked  # filling in your name is fine


def test_secrets_passwords_and_terminals():
    assert gui({"action": "paste", "text": "sk-or-v1-abcdefghijklmnopqrstuvwxyz012345"}, **PRACTICE).blocked
    assert gui({"action": "paste", "text": "hunter2"}, app="safari", focused={"name": "Password", "is_password": True}).blocked
    assert gui({"action": "copy"}, app="safari", focused={"name": "Password", "is_password": True}).blocked
    assert gui({"action": "paste", "text": "rm -rf build"}, app="terminal").level is RiskLevel.CONFIRM_REQUIRED


def test_the_model_is_told_and_copying_off_the_screen_needs_the_users_words(monkeypatch, tmp_path):
    module, ctx, _ws = load(monkeypatch, tmp_path)
    tools = ctx.tools["xenio_gui"][1]["parameters"]["properties"]
    assert {"code", "select_all"} <= set(tools)
    web = ctx.sections["xenio-guard.web"]()
    assert "never say you cannot type into a web page" in web and "COMPLETE, working program" in web
    assert all(len(ctx.sections[k]()) <= 4000 for k in ctx.sections)  # Hermes cuts sections above 4000 characters
    svc, backend = service(tmp_path / "svc", [Win(1, "Two Sum - LeetCode", "Safari", True)],
                           FakeUIA(focused={"found": True, "name": "", "type": "textarea"}))
    desktop_tools.set_service(svc)
    try:
        pre = ctx.hooks["pre_tool_call"]
        _turn(ctx, "play some music")
        out = pre(tool_name="xenio_gui", args={"action": "copy", "select_all": True}, session_id="s1")
        assert out is not None and out.get("action") != "block"  # not blocked: XENIO asks the user first
        _turn(ctx, "read this problem on my screen and solve it", turn="t2")
        assert pre(tool_name="xenio_gui", args={"action": "copy", "select_all": True}, session_id="s1") is None
    finally:
        desktop_tools.set_service(None)
