"""Working in the user's own browser: one call per step, each returning the page; their tabs left alone."""

import subprocess
import sys

import pytest

from app.desktop.browser import BrowserWork
from app.desktop.service import DesktopError

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")


class W:
    def __init__(self, hwnd, title, process="Safari", foreground=False):
        self.hwnd, self.title, self.process, self.foreground, self.minimized, self.pid = hwnd, title, process, foreground, False, 70
        self.app = process


class Reader:
    def __init__(self):
        self.pages = {}
        self.pressed = []
        self.sets = []
        self.on_press = None

    def form_scan(self, hwnd):
        page = self.pages[hwnd]
        return {"token": 1, "nodes": page["nodes"], "complete": True, "web_area": [0, 0, 800, 900], "url": page.get("url", ""), "count": 5, "pid": 70, "ms": 3}

    def form_press(self, token, index):
        self.pressed.append(index)
        if self.on_press:
            self.on_press(index)
        return {"pressed": True, "can_press": True}

    def form_set(self, token, index, text, blur=False):
        self.sets.append((index, text))
        return {"focused": True, "accepted": True, "value": text}

    def browser_tabs(self, hwnd):
        return {"tabs": ["One", "Two"]}

    def browser_button(self, hwnd, action):
        self.pressed.append(action)
        if self.on_press:
            self.on_press(action)
        return {"pressed": True}


def words(text):
    return {"role": "AXStaticText", "text": text, "rect": [0, 0, 10, 10], "hidden": False, "depth": 3}


def link(i, name):
    return {"i": i, "role": "AXLink", "title": name, "desc": "", "rect": [10, 10 + i, 80, 20], "enabled": True, "hidden": False, "depth": 3}


def button(i, name):
    return {"i": i, "role": "AXButton", "title": name, "desc": "", "rect": [10, 10 + i, 80, 20], "enabled": True, "hidden": False, "depth": 3}


def field(i, name, secure=False, what="text field"):  # (i decides where it is: controls must not sit on each other)
    return {"i": i, "role": "AXTextField", "sub": "AXSecureTextField" if secure else "", "what": what, "title": name, "desc": "", "value": "", "placeholder": "", "dom_id": "",
            "rect": [10, 10 + i, 200, 30], "enabled": True, "required": False, "invalid": False, "focused": False, "secure": secure, "hidden": False, "group": "", "depth": 3}


class Desktop:
    _data_dir = None

    def __init__(self):
        self.uia, self.is_stopped, self.gui_calls, self.awake = Reader(), False, [], 0
        self.list = [W(1, "(9) YouTube", foreground=True)]
        self.closed = []

        class Windows:
            def list_windows(me):
                return list(self.list)

            def focus(me, hwnd):
                return True

            def session_locked(me):
                return False

            def close(me, hwnd):
                self.closed.append(hwnd)
                self.list = [w for w in self.list if w.hwnd != hwnd]
                return "closed"
        self.windows = Windows()

    def is_own(self, w):
        return False

    def stopped(self):
        return self.is_stopped

    def keep_awake(self):
        self.awake += 1

    def gui(self, args, session=""):
        self.gui_calls.append(args)


@pytest.fixture
def desk():
    d = Desktop()
    d.uia.pages[1] = {"nodes": [words("Your video")], "url": "https://youtube.com/x"}
    return d


def opener(desk, title="Example", nodes=None, code=0, err="", new=True):
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        if new and code == 0:
            desk.list = [W(2, title), *desk.list]
            desk.uia.pages[2] = {"nodes": nodes if nodes is not None else [words("Hello from the page")], "url": "https://example.com/"}
        return subprocess.CompletedProcess(argv, code, "", err)
    return run, calls


def work(desk, run=None, **kw):
    return BrowserWork(desk, runner=run or (lambda *a, **k: None), sleep=lambda s: None, default_browser="Safari", **kw)


class TestOpen:
    def test_a_new_window_and_the_page_in_the_same_call(self, desk):
        run, calls = opener(desk)
        out = work(desk, run).open("https://example.com")
        assert out["verified"] is True and out["url"] == "https://example.com/" and "Hello from the page" in out["text"] and out["own_window"] is True

    def test_the_address_is_an_argument_never_part_of_the_script(self, desk):
        run, calls = opener(desk)
        work(desk, run).open('https://example.com/"; do shell script "rm -rf ~"')
        argv = calls[0]
        assert "rm -rf" not in argv[argv.index("-e") + 1] and "rm -rf" in argv[-1]

    def test_it_never_touches_what_is_open(self, desk):
        run, _ = opener(desk)
        w = work(desk, run)
        w.open("https://example.com")
        assert not desk.uia.pressed and not desk.closed and any(x.hwnd == 1 for x in desk.list)

    def test_the_same_page_again_is_the_window_it_already_opened(self, desk):
        run, calls = opener(desk)
        w = work(desk, run)
        w.open("https://example.com")
        again = w.open("https://www.example.com/")
        assert again["reused"] is True and len(calls) == 1 and len(desk.list) == 2

    def test_another_page_gets_its_own_window(self, desk):
        run, calls = opener(desk)
        w = work(desk, run)
        w.open("https://example.com")
        run2, calls2 = opener(desk, title="Other")
        desk.list = [x for x in desk.list]
        w._run = lambda argv, **kw: (calls2.append(argv), desk.list.insert(0, W(3, "Other")), desk.uia.pages.__setitem__(3, {"nodes": [words("Other page")], "url": "https://other.test/"}), subprocess.CompletedProcess(argv, 0, "", ""))[-1]
        out = w.open("https://other.test")
        assert "reused" not in out and len(calls2) == 1

    def test_no_permission_is_said_with_the_way_to_give_it(self, desk):
        run, _ = opener(desk, code=1, err="execution error: Not authorized to send Apple events to Safari. (-1743)")
        with pytest.raises(DesktopError, match="Automation") as refused:
            work(desk, run).open("https://example.com")
        assert refused.value.status == "NEEDS_PERMISSION"

    def test_no_new_window_is_not_success(self, desk, monkeypatch):
        import app.desktop.browser as module

        monkeypatch.setattr(module, "OPEN_WAIT", 0.01)
        run, _ = opener(desk, new=False)
        with pytest.raises(DesktopError, match="no new window"):
            work(desk, run).open("https://example.com")

    def test_a_page_that_shows_nothing_yet_is_said_so(self, desk, monkeypatch):
        import app.desktop.browser as module

        monkeypatch.setattr(module, "OPEN_WAIT", 0.01)
        run, _ = opener(desk, nodes=[])
        assert work(desk, run).open("https://example.com")["verified"] is False

    def test_a_browser_it_cannot_drive_is_said(self, desk):
        with pytest.raises(DesktopError) as refused:
            BrowserWork(desk, runner=None, default_browser="Firefox").open("https://example.com")
        assert refused.value.status == "NOT_SUPPORTED"

    def test_stop_means_stop(self, desk):
        desk.is_stopped = True
        with pytest.raises(DesktopError, match="stopped"):
            work(desk).open("https://example.com")


class TestPage:
    def test_it_returns_the_whole_page_from_one_read(self, desk):
        desk.uia.pages[1] = {"nodes": [words("Welcome"), link(1, "Pricing"), button(2, "Sign up"), field(90, "Email", what="email field")], "url": "https://x.test/"}
        out = work(desk).page({}, "s")
        assert out["url"] == "https://x.test/" and "Welcome" in out["text"] and out["links"] == ["Pricing"] and out["buttons"] == ["Sign up"]
        assert out["fields"] == [{"label": "Email", "kind": "email"}]

    def test_the_same_page_again_is_not_sent_again(self, desk):
        w = work(desk)
        w.page({}, "s")
        out = w.page({}, "s")
        assert out["unchanged"] is True and "text" not in out
        assert "text" in w.page({"fresh": True}, "s")

    def test_a_login_page_says_the_user_signs_in(self, desk):
        desk.uia.pages[1] = {"nodes": [field(1, "Password", secure=True)], "url": ""}
        out = work(desk).page({}, "s")
        assert out["needs_login"] is True and "sign in" in out["login_note"]

    def test_a_human_check_is_handed_over(self, desk):
        desk.uia.pages[1] = {"nodes": [words("Verify you are human")], "url": ""}
        out = work(desk).page({}, "s")
        assert out["human_check"] is True and "Only the user" in out["human_note"]

    def test_a_running_test_is_never_read(self, desk):
        desk.uia.pages[1] = {"nodes": [words("Contest Ends in 00 : 01 : 40 : 49")], "url": ""}
        with pytest.raises(DesktopError, match="test, contest"):
            work(desk).page({}, "s")

    def test_it_works_in_its_own_window_first(self, desk):
        run, _ = opener(desk)
        w = work(desk, run)
        w.open("https://example.com")
        assert "Hello" in w.page({"fresh": True}, "s")["text"]

    def test_another_window_by_its_title(self, desk):
        run, _ = opener(desk)
        w = work(desk, run)
        w.open("https://example.com")
        assert "Your video" in w.page({"window": "YouTube"}, "s")["text"]

    def test_no_such_window_is_said(self, desk):
        with pytest.raises(DesktopError, match="no browser window"):
            work(desk).page({"window": "nothing like this"}, "s")

    def test_no_browser_is_said(self, desk):
        desk.list = []
        with pytest.raises(DesktopError, match="no browser window"):
            work(desk).page({}, "s")


class TestClick:
    def page(self, desk):
        desk.uia.pages[1] = {"nodes": [words("Page one"), link(1, "Pricing"), button(2, "Sign up"), link(3, "Learn more"), link(4, "Learn more about us")], "url": "https://x.test/"}

    def test_by_name_with_no_mouse_returning_the_next_page(self, desk):
        self.page(desk)

        def goes(index):
            desk.uia.pages[1] = {"nodes": [words("Plans and prices")], "url": "https://x.test/pricing"}
        desk.uia.on_press = goes
        out = work(desk).click({"element": "Pricing"}, "s")
        assert out["pressed"] == "Pricing" and out["verified"] is True and "Plans and prices" in out["text"] and out["url"].endswith("/pricing")
        assert desk.uia.pressed == [1]

    def test_a_click_that_changes_nothing_is_said(self, desk, monkeypatch):
        import app.desktop.browser as module

        monkeypatch.setattr(module, "CHANGE_WAIT", 0.01)
        self.page(desk)
        out = work(desk).click({"element": "Pricing"}, "s")
        assert out["verified"] is False and "did not change" in out["say"]

    def test_what_should_be_there_afterwards_is_checked(self, desk, monkeypatch):
        import app.desktop.browser as module

        monkeypatch.setattr(module, "CHANGE_WAIT", 0.01)
        self.page(desk)

        def goes(index):
            desk.uia.pages[1] = {"nodes": [words("Something else")], "url": "https://x.test/2"}
        desk.uia.on_press = goes
        out = work(desk).click({"element": "Pricing", "expect": "Plans"}, "s")
        assert out["verified"] is False and "does not say" in out["say"]

    def test_nothing_by_that_name_lists_what_there_is(self, desk):
        self.page(desk)
        with pytest.raises(DesktopError, match="Pricing"):
            work(desk).click({"element": "Checkout"}, "s")
        assert not desk.uia.pressed

    def test_two_things_alike_are_a_question_never_a_guess(self, desk):
        desk.uia.pages[1] = {"nodes": [link(1, "Delete draft one"), link(2, "Delete draft two")], "url": ""}
        with pytest.raises(DesktopError, match="more than one"):
            work(desk).click({"element": "Delete draft"}, "s")
        assert not desk.uia.pressed


class TestType:
    def test_into_a_field_by_name_read_back(self, desk):
        desk.uia.pages[1] = {"nodes": [field(1, "Destination")], "url": ""}
        out = work(desk).type_text({"element": "Destination", "text": "Pune"}, "s")
        assert out["verified"] is True and desk.uia.sets == [(1, "Pune")]

    def test_a_password_field_is_refused(self, desk):
        desk.uia.pages[1] = {"nodes": [field(1, "Password", secure=True)], "url": ""}
        with pytest.raises(DesktopError, match="signs in themselves"):
            work(desk).type_text({"element": "Password", "text": "x"}, "s")
        assert not desk.uia.sets

    def test_a_field_out_of_sight_is_refused(self, desk):
        hidden = dict(field(1, "City"), rect=[-9999, 0, 200, 30])
        desk.uia.pages[1] = {"nodes": [hidden], "url": ""}
        with pytest.raises(DesktopError, match="not visible"):
            work(desk).type_text({"element": "City", "text": "x"}, "s")

    def test_enter_returns_the_results_page(self, desk):
        desk.uia.pages[1] = {"nodes": [field(1, "Search")], "url": "https://x.test/"}

        real = desk.gui

        def enter(args, session=""):
            real(args, session)
            desk.uia.pages[1] = {"nodes": [words("3 results")], "url": "https://x.test/?q=a"}
        desk.gui = enter
        out = work(desk).type_text({"element": "Search", "text": "a", "press_enter": True}, "s")
        assert "3 results" in out["text"] and desk.gui_calls[0]["keys"] == "enter"


class TestMoving:
    def test_back_returns_the_page(self, desk):
        def goes(what):
            desk.uia.pages[1] = {"nodes": [words("Earlier page")], "url": "https://x.test/old"}
        desk.uia.on_press = goes
        out = work(desk).navigate("back", {}, "s")
        assert "Earlier page" in out["text"] and desk.uia.pressed == ["back"]

    def test_tabs_are_listed(self, desk):
        out = work(desk).tabs()
        assert out["windows"][0]["tabs"] == ["One", "Two"] and out["windows"][0]["opened_by_xenio"] is False

    def test_it_closes_its_own_window(self, desk):
        run, _ = opener(desk)
        w = work(desk, run)
        w.open("https://example.com")
        assert w.close({}, "s")["verified"] is True and desk.closed == [2] and any(x.hwnd == 1 for x in desk.list)

    def test_it_keeps_the_display_awake_while_working(self, desk):
        work(desk).page({}, "s")
        assert desk.awake == 1
