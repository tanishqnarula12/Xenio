"""Window management (V5.5): list, find, focus, minimize, maximize, restore and close application windows.

Closing is always graceful: the window gets WM_CLOSE (exactly what its X button sends), so the application can ask
to save; no process is ever terminated. Focusing works around Windows' foreground lock (a background process may
not steal focus) and is verified afterwards: the result says whether the window really came to the front.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import asdict, dataclass

from app.desktop import win32


class WindowError(Exception):
    pass


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    process: str  # executable name, e.g. "Discord.exe"
    pid: int
    rect: tuple[int, int, int, int]
    minimized: bool = False
    maximized: bool = False
    foreground: bool = False

    def public(self) -> dict:
        """What the model sees: no handle juggling needed, titles clipped."""
        d = asdict(self)
        d["title"] = self.title[:120]
        return {k: d[k] for k in ("title", "process", "minimized", "maximized", "foreground")}

    @property
    def app(self) -> str:
        """The app's name: the executable, or the title for Store apps hosted by ApplicationFrameHost."""
        if self.process.lower() in _UWP_HOSTS and self.title:
            return self.title.split(" - ")[-1].strip()
        return os.path.splitext(self.process)[0]

    @property
    def is_shell(self) -> bool:
        return self.process.lower() in _SHELL_PROCESSES


_UWP_HOSTS = {"applicationframehost.exe"}  # UWP apps (Calculator, ...) live in a frame window owned by this host
# Windows shell surfaces (Quick settings, Start, Search, the keyboard host...) that can hold the foreground after an
# app launch; measured: UI Automation queries against them can hang, and they are never what the user means.
_SHELL_PROCESSES = {"shellhost.exe", "shellexperiencehost.exe", "searchhost.exe", "startmenuexperiencehost.exe",
                    "textinputhost.exe", "lockapp.exe", "searchapp.exe", "widgets.exe"}


def _info(hwnd, foreground) -> WindowInfo:
    pid = win32.window_pid(hwnd)
    process = os.path.basename(win32.process_path(pid)) or "?"
    return WindowInfo(int(hwnd), win32.window_text(hwnd), process, pid, win32.window_rect(hwnd),
                      bool(win32.user32.IsIconic(hwnd)), bool(win32.user32.IsZoomed(hwnd)), int(hwnd) == foreground)


def list_windows() -> list[WindowInfo]:
    foreground = int(win32.user32.GetForegroundWindow() or 0)
    return [_info(h, foreground) for h in win32.top_level_windows()]


def session_locked() -> bool:
    return win32.session_locked()


def list_hidden_windows() -> list[WindowInfo]:
    """Main windows of apps that are running but hidden in the tray."""
    return [_info(h, 0) for h in win32.hidden_app_windows()]


def show(hwnd: int) -> bool:
    """Bring a tray app's hidden main window back and to the front. True only if it is visible AND in front."""
    u = win32.user32
    if not u.IsWindow(hwnd):
        raise WindowError("that window no longer exists")
    u.ShowWindow(hwnd, win32.SW_SHOW)
    if u.IsIconic(hwnd):
        u.ShowWindow(hwnd, win32.SW_RESTORE)
    return _wait(lambda: bool(u.IsWindowVisible(hwnd))) and focus(hwnd)


def foreground_window() -> WindowInfo | None:
    hwnd = win32.user32.GetForegroundWindow()
    if not hwnd:
        return None
    return _info(hwnd, int(hwnd))


def active_app_window() -> WindowInfo | None:
    """The app window the user is working in: the foreground one, unless that is a shell surface (Quick settings,
    Start, Search) - then the topmost real application window (Windows lists windows top-down)."""
    fg = foreground_window()
    if fg is not None and not fg.is_shell and win32.user32.IsWindowVisible(fg.hwnd):
        return fg
    for window in list_windows():
        if not window.is_shell and not window.minimized:
            return window
    return fg


def _words(text: str) -> list[str]:
    return [t for t in re.split(r"[^\w]+", (text or "").lower()) if t]


def match_score(query: str, window: WindowInfo) -> float:
    """How well a spoken app/window name matches a window: exact app name > all words in title > some words."""
    q = " ".join(_words(query))
    if not q:
        return 0.0
    app, title = " ".join(_words(window.app)), " ".join(_words(window.title))
    if q == app or q == title:
        return 1.0
    if title.endswith(q) or title.startswith(q) or (app and (app.startswith(q) or q.startswith(app))):
        return 0.9
    words = _words(query)
    hay = set(_words(window.title)) | set(_words(window.app))
    hits = sum(1 for word in words if word in hay or any(h.startswith(word) for h in hay if len(word) >= 3))
    return 0.8 * hits / len(words) if words else 0.0


def find_windows(query: str, windows: list[WindowInfo] | None = None) -> list[WindowInfo]:
    """Windows matching ``query``, best first (ties: the foreground one, then the order Windows lists them)."""
    windows = list_windows() if windows is None else windows
    scored = [(match_score(query, w), w) for w in windows]
    scored = [(s, w) for s, w in scored if s >= 0.5]
    best = max((s for s, _ in scored), default=0)
    return [w for s, w in sorted(scored, key=lambda sw: (-sw[0], not sw[1].foreground)) if s >= best - 0.05]


def _press_alt() -> None:
    """A synthetic Alt tap lets this process take the foreground (Windows' documented foreground-lock rule)."""
    down = win32.INPUT(type=win32.INPUT_KEYBOARD)
    down.u.ki = win32.KEYBDINPUT(0x12, 0, 0, 0, 0)
    up = win32.INPUT(type=win32.INPUT_KEYBOARD)
    up.u.ki = win32.KEYBDINPUT(0x12, 0, win32.KEYEVENTF_KEYUP, 0, 0)
    win32.send_inputs([down, up])


def focus(hwnd: int, timeout: float = 2.0) -> bool:
    """Bring a window to the front (restoring it if minimized). True only if it really is the foreground window."""
    u = win32.user32
    if not u.IsWindow(hwnd):
        raise WindowError("that window no longer exists")
    if u.IsIconic(hwnd):
        u.ShowWindow(hwnd, win32.SW_RESTORE)
    if not u.SetForegroundWindow(hwnd):
        _press_alt()
        if not u.SetForegroundWindow(hwnd):
            current = u.GetForegroundWindow()
            mine, theirs = win32.kernel32.GetCurrentThreadId(), win32.window_thread(current) if current else 0
            if theirs and theirs != mine and u.AttachThreadInput(mine, theirs, True):
                try:
                    u.BringWindowToTop(hwnd)
                    u.SetForegroundWindow(hwnd)
                finally:
                    u.AttachThreadInput(mine, theirs, False)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if int(u.GetForegroundWindow() or 0) == int(hwnd):
            return True
        time.sleep(0.05)
    return False


def minimize(hwnd: int) -> bool:
    win32.user32.ShowWindow(hwnd, win32.SW_MINIMIZE)
    return _wait(lambda: bool(win32.user32.IsIconic(hwnd)))


def maximize(hwnd: int) -> bool:
    win32.user32.ShowWindow(hwnd, win32.SW_MAXIMIZE)
    return _wait(lambda: bool(win32.user32.IsZoomed(hwnd)))


def restore(hwnd: int) -> bool:
    win32.user32.ShowWindow(hwnd, win32.SW_RESTORE)
    return _wait(lambda: not win32.user32.IsIconic(hwnd) and not win32.user32.IsZoomed(hwnd))


def close(hwnd: int, timeout: float = 3.0) -> str:
    """Ask the window to close (WM_CLOSE). "closed" | "still open" (it may be asking to save; never forced)."""
    win32.user32.PostMessageW(hwnd, win32.WM_CLOSE, 0, 0)
    gone = _wait(lambda: not win32.user32.IsWindow(hwnd) or not win32.user32.IsWindowVisible(hwnd), timeout)
    return "closed" if gone else "still open"


def _wait(condition, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return bool(condition())
