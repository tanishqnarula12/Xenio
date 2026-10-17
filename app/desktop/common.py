"""Platform-neutral desktop pieces (V5.5): what a window is, how a spoken name matches it, how a control's name
matches a query. Shared by the Windows backend (``windows``/``uia``) and the macOS backend (``mac_windows``/``ax``),
so both judge "Discord" or "the message box" exactly the same way. Standard library only.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import asdict, dataclass


class WindowError(Exception):
    pass


class UIAError(Exception):
    """A window's controls could not be read (UI Automation on Windows, the Accessibility API on macOS)."""


@dataclass
class WindowInfo:
    hwnd: int  # Windows: the window handle; macOS: the CGWindowID
    title: str
    process: str  # Windows: executable name ("Discord.exe"); macOS: the app's name ("Google Chrome")
    pid: int
    rect: tuple[int, int, int, int]
    minimized: bool = False
    maximized: bool = False
    foreground: bool = False
    bundle_id: str = ""  # macOS only ("com.hnc.Discord")

    def public(self) -> dict:
        """What the model sees: no handle juggling needed, titles clipped."""
        d = asdict(self)
        d["title"] = self.title[:120]
        return {k: d[k] for k in ("title", "process", "minimized", "maximized", "foreground")}

    @property
    def app(self) -> str:
        """The app's name: the executable without .exe, the title for Store apps hosted by ApplicationFrameHost, or
        (macOS) the app's own name."""
        if self.process.lower() in _UWP_HOSTS and self.title:
            return self.title.split(" - ")[-1].strip()
        return os.path.splitext(self.process)[0] if self.process.lower().endswith(".exe") else self.process

    @property
    def is_shell(self) -> bool:
        return self.process.lower() in _SHELL_PROCESSES


_UWP_HOSTS = {"applicationframehost.exe"}  # UWP apps (Calculator, ...) live in a frame window owned by this host
# System surfaces that can hold the front after an app launch and are never what the user means. Windows (measured:
# UI Automation queries against them can hang): Quick settings, Start, Search, the keyboard host... macOS: the Dock,
# Spotlight, Control Center, Notification Center, the menu bar, the lock screen (loginwindow).
_SHELL_PROCESSES = {"shellhost.exe", "shellexperiencehost.exe", "searchhost.exe", "startmenuexperiencehost.exe",
                    "textinputhost.exe", "lockapp.exe", "searchapp.exe", "widgets.exe",
                    "dock", "window server", "windowserver", "spotlight", "control center", "controlcenter",
                    "notification center", "notificationcenter", "systemuiserver", "loginwindow", "windowmanager",
                    "screensaverengine", "textinputmenuagent"}


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


def rank_windows(query: str, windows: list[WindowInfo]) -> list[WindowInfo]:
    """Windows matching ``query``, best first (ties: the foreground one, then the order the system lists them)."""
    scored = [(match_score(query, w), w) for w in windows]
    scored = [(s, w) for s, w in scored if s >= 0.5]
    best = max((s for s, _ in scored), default=0)
    return [w for s, w in sorted(scored, key=lambda sw: (-sw[0], not sw[1].foreground)) if s >= best - 0.05]


def score_name(query: str, name: str) -> float:
    """How well a control's accessible name matches what the model asked for ("send" ~ "Send")."""
    q, n = query.lower().strip(), name.lower().strip()
    if not q or not n:
        return 0.0
    if q == n:
        return 1.0
    if n.startswith(q) or q in n.split(" ") or n in q:
        return 0.85
    if q in n:
        return 0.75
    words = [t for t in q.split() if len(t) > 1]
    return 0.6 * sum(1 for t in words if t in n) / len(words) if words else 0.0


def wait_until(condition, timeout: float = 2.0, poll: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(poll)
    return bool(condition())
