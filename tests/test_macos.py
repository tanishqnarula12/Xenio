"""macOS desktop backend: key/mouse translation, the accessibility helper protocol, AX role mapping, the app catalog,
window listing without the Accessibility permission, and read-only checks against the real window server. No input
is ever posted (fake event sinks)."""

import plistlib
import sys
import time
from pathlib import Path

import pytest

from app.desktop import apps as A
from app.desktop import input as I
from app.desktop import macos as M
from app.desktop.common import UIAError, WindowError, WindowInfo

mac_only = pytest.mark.skipif(sys.platform != "darwin", reason="macOS")


# ---- the input backend ------------------------------------------------------------------------------------------

class FakeMac:
    """Stands in for app.desktop.macos: records what would be posted."""

    EV_LEFT_DOWN, EV_LEFT_UP, EV_RIGHT_DOWN, EV_RIGHT_UP, EV_MOVED = 1, 2, 3, 4, 5
    EV_LEFT_DRAGGED, EV_RIGHT_DRAGGED, EV_OTHER_DOWN, EV_OTHER_UP, EV_OTHER_DRAGGED = 6, 7, 25, 26, 27
    ACCESSIBILITY_STEP = M.ACCESSIBILITY_STEP

    def __init__(self, trusted=True):
        self._trusted, self.posted = trusted, []

    def trusted(self):
        return self._trusted

    def can_post_events(self):
        return self._trusted

    def mouse_location(self):
        return (0.0, 0.0)

    def double_click_interval(self):
        return 0.5

    def desktop_bounds(self):
        return (0, 0, 1470, 956)

    def mouse_event(self, kind, x, y, button=0, clicks=1, flags=0):
        return ("mouse", kind, x, y, button, clicks, flags)

    def key_event(self, code, down, flags=0):
        return ("key", code, down, flags)

    def unicode_event(self, units, down):
        return ("text", "".join(chr(u) for u in units), down)

    def scroll_event(self, y, x=0):
        return ("scroll", y, x)

    def press_media_key(self, code):
        self.posted.append(("media", code))

    def post(self, event):
        self.posted.append(event)


@pytest.fixture
def mac(monkeypatch):
    fake = FakeMac()
    import app.desktop as pkg

    monkeypatch.setattr(pkg, "macos", fake, raising=False)
    monkeypatch.setitem(sys.modules, "app.desktop.macos", fake)
    return fake


def test_without_accessibility_nothing_is_sent(monkeypatch):
    fake = FakeMac(trusted=False)
    monkeypatch.setitem(sys.modules, "app.desktop.macos", fake)
    import app.desktop as pkg

    monkeypatch.setattr(pkg, "macos", fake, raising=False)
    with pytest.raises(I.InputError, match="Accessibility"):
        I.MacBackend()


def test_shortcuts_carry_the_command_flag(mac):
    c = I.DesktopController(I.MacBackend(), sleep=lambda s: None)
    c.hotkey("cmd", "s")
    keys = [e for e in mac.posted if e[0] == "key"]
    assert keys[0] == ("key", 0x37, True, 0x100000)  # Command down
    assert keys[1] == ("key", 0x01, True, 0x100000)  # S down, with the Command flag (apps read flags, not order)
    assert keys[-1] == ("key", 0x37, False, 0)  # Command released last, flag cleared


def test_mac_delete_key_is_backspace_and_media_keys_are_system_events(mac):
    c = I.DesktopController(I.MacBackend(), sleep=lambda s: None)
    c.hotkey("cmd", "delete")
    assert ("key", 0x33, True, 0x100000) in mac.posted  # the key labelled "delete" on a Mac
    mac.posted.clear()
    c.hotkey("playpause")
    assert mac.posted == [("media", 16)]  # one press, through the system media key, for any player
    with pytest.raises(I.InputError, match="Mac keyboard"):
        c.hotkey("printscreen")


def test_typing_is_unicode_and_newlines_are_return(mac):
    c = I.DesktopController(I.MacBackend(), sleep=lambda s: None)
    c.type_text("नमस्ते\nok")
    text = "".join(e[1] for e in mac.posted if e[0] == "text" and e[2])
    assert text == "नमस्तेok" and ("key", 0x24, True, 0) in mac.posted


def test_double_click_is_a_double_click_to_the_app(mac):
    c = I.DesktopController(I.MacBackend(), sleep=lambda s: None)
    c.double_click(100, 200)
    downs = [e for e in mac.posted if e[0] == "mouse" and e[1] == FakeMac.EV_LEFT_DOWN]
    assert [d[5] for d in downs] == [1, 2] and downs[0][2:4] == (100, 200)
    mac.posted.clear()
    c.right_click(10, 10)
    assert [e[1] for e in mac.posted if e[0] == "mouse"] == [FakeMac.EV_MOVED, FakeMac.EV_RIGHT_DOWN, FakeMac.EV_RIGHT_UP]


def test_drag_moves_as_drag_events_and_scroll_is_lines(mac):
    c = I.DesktopController(I.MacBackend(), sleep=lambda s: None)
    c.drag((10, 10), (50, 50), steps=2)
    kinds = [e[1] for e in mac.posted if e[0] == "mouse"]
    assert kinds == [FakeMac.EV_MOVED, FakeMac.EV_LEFT_DOWN, FakeMac.EV_LEFT_DRAGGED, FakeMac.EV_LEFT_DRAGGED,
                     FakeMac.EV_LEFT_UP]
    mac.posted.clear()
    c.scroll(-2)
    assert mac.posted == [("scroll", -3, 0)] * 2  # one notch = 3 lines, down = negative


# ---- the accessibility helper protocol --------------------------------------------------------------------------

_ECHO = r"""
import json, sys
for line in sys.stdin:
    req = json.loads(line)
    if req["cmd"] == "die":
        sys.exit(1)
    if req["cmd"] == "stale":
        print(json.dumps({"id": -5, "ok": True, "result": "old"}), flush=True)
    if req["cmd"] == "fail":
        print(json.dumps({"id": req["id"], "ok": False, "error": "no such window"}), flush=True)
        continue
    print(json.dumps({"id": req["id"], "ok": True, "result": {"cmd": req["cmd"], "args": req["args"]}}), flush=True)
"""


def test_helper_round_trip_errors_and_restart():
    h = M.Helper(timeout=5, command=[sys.executable, "-c", _ECHO])
    try:
        assert h.call("find", id=7, query="send") == {"cmd": "find", "args": {"id": 7, "query": "send"}}
        assert h.call("stale") == {"cmd": "stale", "args": {}}  # an answer to an older request is skipped
        with pytest.raises(M.MacError, match="no such window"):
            h.call("fail")
        with pytest.raises(M.MacError, match="stopped unexpectedly"):
            h.call("die")
        assert h.call("ping")["cmd"] == "ping"  # restarted on the next call
    finally:
        h.close()


def test_a_hanging_helper_times_out_and_is_replaced():
    h = M.Helper(timeout=0.3, command=[sys.executable, "-c", "import time; time.sleep(30)"])
    started = time.monotonic()
    with pytest.raises(M.MacError, match="timed out"):
        h.call("ping")
    assert time.monotonic() - started < 2 and h._proc is None


def test_ax_client_reports_helper_errors_as_lookup_errors():
    from app.desktop.ax import AXClient

    class Broken:
        def call(self, cmd, timeout=None, **args):
            raise M.MacError(M.ACCESSIBILITY_STEP)

    with pytest.raises(UIAError, match="Accessibility"):
        AXClient(helper=Broken()).find(1, "send")


# ---- accessibility roles -> the policy's control types ----------------------------------------------------------

@pytest.mark.parametrize("facts,expected", [
    ({"AXRole": "AXButton", "AXTitle": "Send"}, ("Send", "button", False)),
    ({"AXRole": "AXTextField", "AXSubrole": "AXSecureTextField", "AXDescription": "Password"}, ("Password", "edit", True)),
    ({"AXRole": "AXTextArea", "AXPlaceholderValue": "Type a message", "AXValue": "draft text"},
     ("Type a message", "edit", False)),  # a field's own text is never its name
    ({"AXRole": "AXStaticText", "AXValue": "Rahul"}, ("Rahul", "text", False)),
    ({"AXRole": "AXRow", "AXSubrole": "AXOutlineRow", "AXTitle": "Inbox"}, ("Inbox", "treeitem", False)),
    ({"AXRole": "AXRadioButton", "AXSubrole": "AXTabButton", "AXTitle": "General"}, ("General", "tabitem", False)),
    ({"AXRole": "AXMenuItem", "AXTitle": "Move to Trash"}, ("Move to Trash", "menuitem", False)),
])
def test_ax_roles_map_to_the_policys_control_types(facts, expected):
    info = M.AX.describe(None, {**facts, "AXPosition": (10, 20), "AXSize": (100, 40)})
    assert (info["name"], info["type"], info["is_password"]) == expected
    assert info["center"] == [60, 40] and info["rect"] == [10, 20, 110, 60] and info["enabled"]


# ---- the app catalog --------------------------------------------------------------------------------------------

def _bundle(folder: Path, name: str, ident: str, **extra) -> None:
    contents = folder / f"{name}.app" / "Contents"
    contents.mkdir(parents=True)
    with open(contents / "Info.plist", "wb") as fh:
        plistlib.dump({"CFBundleIdentifier": ident, "CFBundleName": name, **extra}, fh)


def test_mac_catalog_reads_app_bundles(tmp_path):
    apps = tmp_path / "Applications"
    _bundle(apps, "Spotify", "com.spotify.client")
    _bundle(apps, "Visual Studio Code", "com.microsoft.VSCode")
    _bundle(apps / "Utilities", "Terminal", "com.apple.Terminal")
    _bundle(apps, "Menu Helper", "x.helper", LSUIElement=True)  # a menu-bar agent is not an app you open
    _bundle(tmp_path / "home" / "Applications", "Spotify Old", "com.spotify.client")  # the same app again, elsewhere
    entries = A._mac_apps(dirs=(str(apps), str(apps / "Utilities"), str(tmp_path / "home" / "Applications")), singles=())
    names = sorted(e.name for e in entries)
    assert names == ["Spotify", "Terminal", "Visual Studio Code"]
    vs = A.resolve("vs code", entries).best
    assert vs.app_id == "com.microsoft.VSCode" and vs.source == "applications" and vs.exe_stem == "visual studio code"
    assert A.resolve("command prompt", entries).best.name == "Terminal"  # a Windows name on a Mac


def test_mac_windows_match_by_bundle_id():
    code = A.AppEntry("Visual Studio Code", "com.microsoft.VSCode", "/Applications/Visual Studio Code.app", "applications")
    assert A.window_matches(code, WindowInfo(5, "main.py - xenio", "Code", 9, (0, 0, 9, 9), bundle_id="com.microsoft.VSCode"))
    assert not A.window_matches(code, WindowInfo(6, "Visual Studio Code tips - Safari", "Safari", 3, (0, 0, 9, 9),
                                                 bundle_id="com.apple.Safari"))


def test_downloaded_and_disk_image_apps_need_confirmation():
    assert A.launch_risk(A.AppEntry("Tool", "x", "/Users/me/Downloads/Tool.app", "applications")) == "untrusted_location"
    assert A.launch_risk(A.AppEntry("Tool", "x", "/Volumes/Tool/Tool.app", "applications")) == "untrusted_location"
    assert A.launch_risk(A.AppEntry("Install macOS Tahoe", "x", "/Applications/Install macOS Tahoe.app", "applications")) == "installer"
    assert A.launch_risk(A.AppEntry("Spotify", "x", "/Applications/Spotify.app", "applications")) == "normal"


# ---- windows without the Accessibility permission ---------------------------------------------------------------

def test_window_actions_without_access_say_which_permission(monkeypatch):
    from app.desktop import mac_windows

    monkeypatch.setattr(mac_windows, "_trusted", lambda: False)
    with pytest.raises(WindowError, match="Accessibility"):
        mac_windows.minimize(5)
    with pytest.raises(WindowError, match="Accessibility"):
        mac_windows.close(5)


def test_without_access_the_window_servers_list_is_used(monkeypatch):
    from app.desktop import mac_windows

    monkeypatch.setattr(mac_windows, "_trusted", lambda: False)
    monkeypatch.setattr(M, "cg_windows", lambda on_screen_only=False: [
        {"id": 1, "pid": 10, "owner": "Dock", "title": "", "layer": 0, "alpha": 1, "rect": (0, 0, 900, 900), "on_screen": True},
        {"id": 2, "pid": 11, "owner": "Spotify", "title": "Spotify", "layer": 0, "alpha": 1, "rect": (0, 0, 800, 600), "on_screen": True},
        {"id": 3, "pid": 12, "owner": "Safari", "title": "", "layer": 0, "alpha": 1, "rect": (0, 0, 800, 600), "on_screen": True},
        {"id": 4, "pid": 13, "owner": "Control Center", "title": "", "layer": 25, "alpha": 1, "rect": (0, 0, 30, 30), "on_screen": True},
    ])
    monkeypatch.setattr(M, "app_bundle", lambda pid: ("", f"app.{pid}"))
    windows = mac_windows.list_windows()
    assert [w.process for w in windows] == ["Spotify", "Safari"]  # the Dock and menu-bar items are never targets
    assert windows[0].foreground and not windows[1].foreground and windows[0].bundle_id == "app.11"


# ---- the real window server (read-only) -------------------------------------------------------------------------

@mac_only
def test_real_displays_session_and_window_list():
    assert M.displays() and all(r[2] > 0 and r[3] > 0 for r in M.displays())
    assert isinstance(M.session_locked(), bool)
    windows = M.cg_windows()
    assert windows and all({"id", "pid", "owner", "rect", "layer"} <= set(w) for w in windows)
    assert isinstance(M.key_down(M.KVK_ESCAPE), bool)
    assert 0.1 <= M.double_click_interval() <= 5


@mac_only
def test_real_events_are_built_but_not_posted():
    L = M.lib()
    ev = M.mouse_event(M.EV_LEFT_DOWN, 12, 34, clicks=2)
    try:
        loc = L.cg.CGEventGetLocation(ev)
        assert (loc.x, loc.y) == (12, 34) and L.cg.CGEventGetIntegerValueField(ev, M.FIELD_CLICK_STATE) == 2
    finally:
        L.release(ev)
    ev = M.media_event(M.NX_PLAY, True)
    try:
        assert L.cg.CGEventGetType(ev) == 14  # a system-defined (media key) event
    finally:
        L.release(ev)


@mac_only
def test_real_capture_is_jpeg_in_memory():
    from app.vision.capture import CaptureError, ScreenCapture, render_text_image

    card = render_text_image(["XENIO"], 200, 100)
    assert card.data[:4] == b"\x89PNG" and card.width == 200
    if not M.screen_access():
        with pytest.raises(CaptureError, match="Screen"):
            ScreenCapture(320).capture_screen()
        return
    image = ScreenCapture(320).capture_screen()
    assert image.mime == "image/jpeg" and image.data[:3] == b"\xff\xd8\xff" and max(image.width, image.height) == 320
