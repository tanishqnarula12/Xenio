"""Emergency stop for desktop control (V5.5).

Two ways to stop XENIO's hands immediately:

* **Voice**: "Xenio stop" / "stop Xenio" / "Xenio cancel" / "abort" while XENIO works. The voice process writes a
  stop marker (a small file in XENIO's data folder: XENIO and Hermes are separate processes) and cancels the
  Hermes turn.
* **Keyboard**: pressing **Esc** on the physical keyboard while a mouse/keyboard action runs. XENIO's own key
  presses never count: the watcher pauses while XENIO itself sends Esc.

A stop ends the action in progress between two keystrokes or mouse steps, and every further desktop action in the
same request is refused ("stopped by the user"). The next request starts clean.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

STOP_FILE = "gui_stop"
VK_ESCAPE = 0x1B


def _data_dir() -> Path:
    from app.integrations.base import data_dir

    return data_dir()


def request_stop(reason: str = "voice", folder: Path | None = None) -> None:
    """Called by XENIO's voice/console process when the user says stop."""
    path = (folder or _data_dir()) / STOP_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{time.time():.6f} {reason}\n", encoding="utf-8")
    except OSError:
        pass


def stop_requested_since(since: float, folder: Path | None = None) -> bool:
    """True when the user asked to stop after ``since`` (epoch seconds)."""
    try:
        text = ((folder or _data_dir()) / STOP_FILE).read_text(encoding="utf-8").split()
        return bool(text) and float(text[0]) >= since
    except (OSError, ValueError):
        return False


class EscWatcher:
    """While running, a physical Esc press sets ``pressed``. ``suspend()`` while XENIO itself sends keys."""

    def __init__(self, poll: float = 0.03):
        self.pressed = False
        self._poll = poll
        self._suspended = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self):
        if os.name == "nt":
            from app.desktop import win32

            self._state = win32.user32.GetAsyncKeyState
            self._state(VK_ESCAPE)  # clears the "pressed since last call" bit
        elif sys.platform == "darwin":
            from app.desktop import macos

            # the physical key's state (HID level): XENIO's own events pass the same point, hence suspend() below
            self._state = lambda _vk: 0x8000 if macos.key_down(macos.KVK_ESCAPE) else 0
        else:
            return self
        self._thread = threading.Thread(target=self._run, name="xenio-esc-watch", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.5)
        return False

    def _run(self) -> None:
        while not self._stop.wait(self._poll):
            if self._suspended:
                self._state(VK_ESCAPE)  # swallow XENIO's own Esc
                continue
            if self._state(VK_ESCAPE) & 0x8001:
                self.pressed = True

    def suspend(self):
        watcher = self

        class _Pause:
            def __enter__(self):
                watcher._suspended += 1

            def __exit__(self, *exc):
                time.sleep(0.05)
                watcher._suspended -= 1
                return False

        return _Pause()
