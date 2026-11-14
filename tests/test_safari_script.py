"""Agents in the CEO's own Safari through Safari's own scripting, which reaches windows on every desktop (Space).

A stand-in for osascript plays Safari here. The page script itself was run on real WebKit pages (read, type, a rich-text box,
Save that really saved, a password box refused and its value never read)."""

import json
import subprocess

import pytest

from app.desktop.own_browser import OwnBrowser
from app.desktop.safari_script import JS_OFF, SafariScript
from app.desktop.service import DesktopError

PAGE = {"title": "Asha | LinkedIn", "url": "https://www.linkedin.com/in/asha/", "ready": "complete", "text": "Asha Sharma\nStudent",
        "buttons": ["Save"], "links": ["Edit intro"], "fields": [{"label": "Headline", "kind": "text", "secure": False}], "needs_login": False, "human_check": False}


class Safari:
    """What osascript would answer, and every call made to it."""

    def __init__(self, js_on=True, windows=None):
        self.js_on = js_on
        self.calls: list[list[str]] = []
        self.windows = windows if windows is not None else {7: ["Inbox (3) - Gmail", "Asha | LinkedIn"]}
        self.closed: list[str] = []
        self.page = dict(PAGE)
        self.next_id = 100
        self.after_click = None

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        script, args = argv[argv.index("-e") + 1], argv[argv.index("--") + 1:]
        if "make new document" in script:
            self.next_id += 1
            self.windows[self.next_id] = ["Asha | LinkedIn"]
            return subprocess.CompletedProcess(argv, 0, f"{self.next_id}\n", "")
        if "do JavaScript" in script:
            if not self.js_on:
                return subprocess.CompletedProcess(argv, 1, "", "execution error: Safari got an error: You must enable 'Allow JavaScript from Apple Events' in the Developer section of Safari Settings to use 'do JavaScript'. (8)")
            if int(args[0]) not in self.windows:
                return subprocess.CompletedProcess(argv, 1, "", "execution error: Safari got an error: Can't get window id 5. (-1728)")
            cmd = json.loads(args[2].split("})(", 1)[1].rsplit("))", 1)[0])
            if cmd["do"] == "click":
                if self.after_click:
                    self.page.update(self.after_click)
                return self._ok(argv, {"pressed": cmd["name"].title()})
            if cmd["do"] == "type":
                if cmd["name"].lower() == "password":
                    return self._ok(argv, {"error": "password", "field": "Password"})
                return self._ok(argv, {"field": cmd["name"], "held": True})
            return self._ok(argv, self.page)
        if "every window" in script and "every tab" in script:
            sep = args[0]
            lines = []
            for n, (wid, tabs) in enumerate(self.windows.items()):
                lines.append(sep.join(["W", str(wid), str(n + 1)]))
                lines += [sep.join(["T", str(i + 1), t, "https://x.test/"]) for i, t in enumerate(tabs)]
            return subprocess.CompletedProcess(argv, 0, "\n".join(lines) + "\n", "")
        if "close w" in script:
            self.closed.append(args[0])
            self.windows.pop(int(args[0]), None)
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(script)

    @staticmethod
    def _ok(argv, answer):
        return subprocess.CompletedProcess(argv, 0, json.dumps(answer) + "\n", "")


def scripted(safari, locked=False):
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    return SafariScript(runner=safari, sleep=sleep, clock=lambda: now[0], locked=lambda: locked)


class TestSafariScript:
    def test_it_opens_a_window_of_its_own_and_reads_it_there(self):
        safari = Safari()
        work = scripted(safari)
        view = work.open("https://www.linkedin.com/in/asha/", "job:agent")
        assert view["verified"] and "Student" in view["text"] and view["fields"][0]["label"] == "Headline"
        js = [c for c in safari.calls if "do JavaScript" in c[c.index("-e") + 1]]
        assert js and all(c[-3] == "101" and c[-2] == "1" for c in js)  # its own window, never the CEO's window 7

    def test_what_the_agent_says_is_an_argument_never_part_of_the_script(self):
        safari = Safari()
        work = scripted(safari)
        work.open('https://x.test/"; do shell script "rm -rf ~"', "s")
        work.type_text({"element": "Headline", "text": '"); location="https://evil.test"; ("'}, "s")
        for argv in safari.calls:
            script = argv[argv.index("-e") + 1]
            assert "rm -rf" not in script and "evil.test" not in script
        typed = [c for c in safari.calls if "evil.test" in c[-1]][0][-1]
        assert '\\"); location=\\"https://evil.test\\"' in typed  # inside a JSON string, so it is text and not code

    def test_without_a_window_of_its_own_nothing_runs(self):
        safari = Safari()
        with pytest.raises(DesktopError, match="no window of your own"):
            scripted(safari).page({}, "s")
        assert not [c for c in safari.calls if "do JavaScript" in c[c.index("-e") + 1]]

    def test_a_password_box_is_never_typed_into(self):
        safari = Safari()
        work = scripted(safari)
        work.open("https://x.test/", "s")
        with pytest.raises(DesktopError, match="password box"):
            work.type_text({"element": "Password", "text": "x"}, "s")

    def test_a_click_that_changes_the_page_is_verified(self):
        safari = Safari()
        safari.after_click = {"text": "Asha Sharma\nFounder at Fintness"}
        work = scripted(safari)
        work.open("https://x.test/", "s")
        view = work.click({"element": "save"}, "s")
        assert view["verified"] and view["pressed"] == "Save" and "Founder" in view["text"]

    def test_with_safaris_switch_off_it_says_how_and_leaves_no_window_behind(self):
        safari = Safari(js_on=False)
        work = scripted(safari)
        with pytest.raises(DesktopError) as caught:
            work.open("https://x.test/", "s")
        assert getattr(caught.value, "js_off", False) and "Allow JavaScript from Apple Events" in str(caught.value)
        assert safari.closed == ["101"] and 101 not in safari.windows and 7 in safari.windows

    def test_a_tab_of_the_ceos_is_read_where_it_is_and_named_exactly(self):
        safari = Safari()
        work = scripted(safari)
        with pytest.raises(DesktopError, match="name the tab"):
            work.adopt("s", "a")
        assert work.adopt("s", "linkedin") == (7, 2)  # the second tab of their window: it is not brought to the front
        work.page({}, "s")
        assert not [c for c in safari.calls if "current tab" in c[c.index("-e") + 1] and "set" in c[c.index("-e") + 1]]

    def test_it_closes_only_its_own_windows(self):
        safari = Safari()
        work = scripted(safari)
        work.adopt("theirs", "gmail")
        assert work.close({}, "theirs") == {"closed": False} and safari.closed == []
        work.open("https://x.test/", "mine")
        assert work.close({}, "mine") == {"closed": True} and safari.closed == ["101"]

    def test_on_a_locked_mac_it_opens_presses_and_types_nothing(self):
        safari = Safari()
        work = scripted(safari, locked=True)
        with pytest.raises(DesktopError, match="locked"):
            work.open("https://x.test/", "s")
        work._by_session["s"] = (7, 1)
        for act in (lambda: work.click({"element": "Save"}, "s"), lambda: work.type_text({"element": "Headline", "text": "x"}, "s"), lambda: work.navigate("back", {}, "s")):
            with pytest.raises(DesktopError, match="locked"):
                act()
        assert not [c for c in safari.calls if "make new document" in c[c.index("-e") + 1] or '"do": "click"' in c[-1] or '"do": "type"' in c[-1]]

    def test_a_box_that_appears_a_few_seconds_later_is_waited_for(self):
        """LinkedIn's edit form showed its Headline box 4 to 6 s after the pencil was pressed: the first real try typed too early."""
        safari = Safari()
        work = scripted(safari)
        work.open("https://x.test/", "s")
        tries = []
        real = safari.__call__

        def slow(argv, **kw):
            if '"do": "type"' in argv[-1]:
                tries.append(1)
                if len(tries) < 5:
                    return subprocess.CompletedProcess(argv, 0, json.dumps({"error": 'no box is labelled "Headline"', "fields": ["Search"]}) + "\n", "")
            return real(argv, **kw)

        work._run = slow
        view = work.type_text({"element": "Headline", "text": "Founder"}, "s")
        assert view["verified"] and len(tries) == 5

    def test_a_box_that_never_appears_is_reported_after_a_bounded_wait(self):
        safari = Safari()
        work = scripted(safari)
        work.open("https://x.test/", "s")
        real = safari.__call__
        work._run = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, json.dumps({"error": 'no box is labelled "Bio"', "fields": ["Search"]}) + "\n", "") if '"do": "type"' in argv[-1] else real(argv, **kw)
        with pytest.raises(DesktopError, match="no box is labelled"):
            work.type_text({"element": "Bio", "text": "x"}, "s")

    def test_a_page_still_growing_is_read_once_it_has_stopped(self):
        """LinkedIn's profile went from 962 to 24,000 characters over 6 s after it said it had loaded."""
        safari = Safari()
        grow = iter(["Asha", "Asha Sharma", "Asha Sharma\nStudent\nAbout\nI like coding."])
        real = safari.__call__

        def growing(argv, **kw):
            if '"do": "read"' in argv[-1]:
                safari.page["text"] = next(grow, safari.page["text"])
            return real(argv, **kw)

        work = scripted(safari)
        work._run = growing
        view = work.open("https://x.test/", "s")
        assert "I like coding." in view["text"]

    def test_every_window_on_every_desktop_is_listed_by_title(self):
        tabs = scripted(Safari()).tabs()["windows"]
        assert tabs[0]["tabs"] == ["Inbox (3) - Gmail", "Asha | LinkedIn"] and tabs[0]["browser"] == "Safari"


class ScreenReader:
    """The screen-reader route, as on a Mac where Safari is on another desktop: it opens, and sees nothing."""

    def __init__(self, reaches=False):
        self.reaches = reaches
        self.opened = []
        self._by_session = {}

    def _which_browser(self):
        return "Safari"

    def open(self, url, session=""):
        self.opened.append(url)
        if not self.reaches:
            raise DesktopError("Safari opened it on another desktop (Space), where macOS lets no app read a window, so it was closed again", "NEEDS_PERMISSION")
        self._by_session[session] = 1
        return {"window": "page", "url": url, "text": "hello", "verified": True}

    def page(self, args, session=""):
        return {"text": "from the screen reader"}


class TestOwnBrowser:
    def test_safaris_own_scripting_comes_first(self):
        safari, reader = Safari(), ScreenReader()
        own = OwnBrowser(None, script=scripted(safari), ax=reader)
        own.open("https://x.test/", "s")
        assert own.scripted("s") and reader.opened == [] and "Student" in own.page({}, "s")["text"]

    def test_with_the_switch_off_it_uses_the_screen_reader_when_that_reaches(self):
        safari, reader = Safari(js_on=False), ScreenReader(reaches=True)
        own = OwnBrowser(None, script=scripted(safari), ax=reader)
        own.open("https://x.test/", "s")
        assert not own.scripted("s") and own.page({}, "s")["text"] == "from the screen reader" and safari.closed == ["101"]

    def test_with_the_switch_off_and_safari_on_another_desktop_it_says_exactly_what_to_turn_on(self):
        own = OwnBrowser(None, script=scripted(Safari(js_on=False)), ax=ScreenReader(reaches=False))
        with pytest.raises(DesktopError) as caught:
            own.open("https://x.test/", "s")
        assert "another desktop" in str(caught.value) and JS_OFF in str(caught.value) and caught.value.status == "NEEDS_PERMISSION"

    def test_once_the_ceo_turns_it_on_the_next_try_uses_it(self):
        now = [0.0]
        safari = Safari(js_on=False)
        own = OwnBrowser(None, script=scripted(safari), ax=ScreenReader(reaches=True), clock=lambda: now[0])
        own.open("https://x.test/", "a")
        safari.js_on = True
        now[0] = 120.0
        own.open("https://x.test/", "b")
        assert own.scripted("b")

    def test_nothing_happens_before_a_window_is_its_own(self):
        own = OwnBrowser(None, script=scripted(Safari()), ax=ScreenReader())
        with pytest.raises(DesktopError, match="no window of your own"):
            own.click({"element": "Save"}, "s")


@pytest.mark.skipif(not __import__("shutil").which("osacompile"), reason="needs macOS")
def test_every_script_safari_is_sent_really_compiles():
    """A stand-in for Safari cannot catch AppleScript's own grammar: "before" and "after" are its words, and an open script using them failed on the real Mac."""
    from app.desktop import browser, safari_script

    for script in (safari_script._OPEN, safari_script._RUN, safari_script._LIST, safari_script._CLOSE, browser._SAFARI_OPEN, browser._SAFARI_CLOSE):  # Chrome's words compile only where Chrome is installed
        done = subprocess.run(["osacompile", "-o", "/dev/null", "-e", script], capture_output=True, text=True)
        assert done.returncode == 0, (script[:80], done.stderr)
