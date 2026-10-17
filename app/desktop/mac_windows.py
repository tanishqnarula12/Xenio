"""Window management on macOS (V5.5): the same functions as ``windows`` (list, find, focus, minimize, maximize,
restore, close, session_locked), so ``DesktopService`` works unchanged.

With the Accessibility permission everything goes through XENIO's accessibility helper (``macos.helper()``): exact
windows, minimized state, focus verified per window. Without it, XENIO still sees on-screen windows from the window
server and brings apps forward through LaunchServices (like a Dock click), so opening and switching apps works before
the user has granted anything; window actions that need it say which permission is missing.

Closing presses the window's red close button, so the app can ask to save; no process is ever terminated.
"""

from __future__ import annotations

import subprocess
import time

from app.desktop import macos
from app.desktop.common import WindowError, WindowInfo, rank_windows

__all__ = ["WindowError", "WindowInfo", "list_windows", "list_hidden_windows", "foreground_window", "active_app_window",
           "find_windows", "focus", "show", "minimize", "maximize", "restore", "close", "session_locked"]


def _call(cmd: str, **args):
    try:
        return macos.helper().call(cmd, **args)
    except macos.MacError as exc:
        raise WindowError(str(exc)) from None


def _info(d: dict) -> WindowInfo:
    rect = d.get("rect") or (0, 0, 0, 0)
    return WindowInfo(int(d["id"]), d.get("title") or "", d.get("app") or "?", int(d.get("pid") or 0),
                      (int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3])), bool(d.get("minimized")),
                      bool(d.get("maximized")), bool(d.get("foreground")), d.get("bundle_id") or "")


def _trusted() -> bool:
    try:
        return macos.trusted()
    except (ImportError, OSError):
        return False


def _cg_list() -> list[WindowInfo]:
    """Without Accessibility: the window server's on-screen app windows, front to back (the first is in front)."""
    out, front_done = [], False
    for w in macos.cg_windows(on_screen_only=True):
        if w["layer"] != 0 or w["alpha"] <= 0 or w["rect"][2] - w["rect"][0] < 40 or w["rect"][3] - w["rect"][1] < 40:
            continue
        _bundle, ident = macos.app_bundle(w["pid"])
        info = WindowInfo(w["id"], w["title"], w["owner"] or "?", w["pid"], w["rect"], foreground=not front_done,
                          bundle_id=ident)
        if info.is_shell:
            continue
        front_done = True
        out.append(info)
    return out


def list_windows() -> list[WindowInfo]:
    if not _trusted():
        return _cg_list()
    return [_info(d) for d in _call("windows")]


def list_hidden_windows() -> list[WindowInfo]:
    """Windows of apps that are running but hidden (Cmd+H), the macOS counterpart of a tray app."""
    if not _trusted():
        return []
    return [_info(d) for d in _call("windows", hidden=True)]


def session_locked() -> bool:
    return macos.session_locked()


def foreground_window() -> WindowInfo | None:
    """The window in front. Cheap (checked before every keystroke while typing): one helper round trip."""
    if not _trusted():
        windows = _cg_list()
        return windows[0] if windows else None
    front = _call("frontmost")
    if not front or not front.get("pid"):
        return None
    for w in macos.cg_windows(on_screen_only=True):
        if w["id"] == front.get("id") or (not front.get("id") and w["pid"] == front["pid"] and w["layer"] == 0):
            return WindowInfo(w["id"], w["title"], w["owner"] or "?", w["pid"], w["rect"], foreground=True,
                              bundle_id=macos.app_bundle(w["pid"])[1])
    return WindowInfo(int(front.get("id") or 0), "", "?", int(front["pid"]), (0, 0, 0, 0), foreground=True,
                      bundle_id=macos.app_bundle(int(front["pid"]))[1])


def active_app_window() -> WindowInfo | None:
    """The app window the user is working in: the one in front unless that is a system surface (Spotlight, Control
    Center...), then the topmost real app window."""
    fg = foreground_window()
    if fg is not None and not fg.is_shell:
        return fg
    for window in list_windows():
        if not window.is_shell and not window.minimized:
            return window
    return fg


def find_windows(query: str, windows: list[WindowInfo] | None = None) -> list[WindowInfo]:
    return rank_windows(query, list_windows() if windows is None else windows)


def _pid_of(hwnd: int) -> int:
    return next((w["pid"] for w in macos.cg_windows() if w["id"] == hwnd), 0)


def _activate_app(pid: int) -> None:
    """Bring an app forward the way a Dock click does (LaunchServices; needs no permission)."""
    bundle, _ident = macos.app_bundle(pid)
    if not bundle:
        raise WindowError("that app cannot be brought forward without the Accessibility permission. "
                          + macos.ACCESSIBILITY_STEP)
    subprocess.run(["/usr/bin/open", "-a", bundle], capture_output=True, timeout=10)


def focus(hwnd: int, timeout: float = 2.0) -> bool:
    """Bring a window to the front (un-minimizing / un-hiding it). True only if it really is in front afterwards."""
    if _trusted():
        return bool(_call("focus", id=int(hwnd), timeout=timeout + 2))
    pid = _pid_of(hwnd)
    if not pid:
        raise WindowError("that window no longer exists")
    _activate_app(pid)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        fg = foreground_window()
        if fg is not None and fg.pid == pid:
            return True
        time.sleep(0.05)
    return False


def show(hwnd: int) -> bool:
    """Un-hide a hidden app's window and bring it to the front. True only if it is visible AND in front."""
    pid = _pid_of(hwnd)
    if not pid:
        raise WindowError("that window no longer exists")
    _call("unhide", pid=pid)
    return focus(hwnd)


def _need_access(action: str) -> None:
    if not _trusted():
        raise WindowError(f"XENIO cannot {action} windows yet. " + macos.ACCESSIBILITY_STEP)


def minimize(hwnd: int) -> bool:
    _need_access("minimize")
    return bool(_call("minimize", id=int(hwnd)))


def maximize(hwnd: int) -> bool:
    _need_access("maximize")
    return bool(_call("maximize", id=int(hwnd)))


def restore(hwnd: int) -> bool:
    _need_access("restore")
    return bool(_call("restore", id=int(hwnd)))


def close(hwnd: int, timeout: float = 3.0) -> str:
    """Press the window's close button. "closed" | "still open" (it may be asking to save; never forced)."""
    _need_access("close")
    return str(_call("close", id=int(hwnd), timeout=timeout + 3))
