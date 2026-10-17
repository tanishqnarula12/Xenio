"""Window management (V5.5): list, find, focus, minimize, maximize, restore and close application windows.

Closing is always graceful: the window gets WM_CLOSE (exactly what its X button sends), so the application can ask
to save; no process is ever terminated. Focusing works around Windows' foreground lock (a background process may
not steal focus) and is verified afterwards: the result says whether the window really came to the front.
"""

from __future__ import annotations

import os
import time

from app.desktop import win32
from app.desktop.common import _SHELL_PROCESSES, _UWP_HOSTS, WindowError, WindowInfo, _words, match_score, rank_windows
from app.desktop.common import wait_until as _wait

__all__ = ["WindowError", "WindowInfo", "match_score", "_SHELL_PROCESSES", "_UWP_HOSTS", "_words"]


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


def find_windows(query: str, windows: list[WindowInfo] | None = None) -> list[WindowInfo]:
    """Windows matching ``query``, best first (ties: the foreground one, then the order Windows lists them)."""
    return rank_windows(query, list_windows() if windows is None else windows)


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
