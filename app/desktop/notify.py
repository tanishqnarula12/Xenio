"""Desktop notifications (macOS Notification Center) through AppleScript's ``display notification``.

The title and text reach osascript as arguments (``on run argv``), never inside the script source, so a reminder, an
email subject or an agent's message cannot change what runs. macOS shows them as coming from "Script Editor"; if they
do not appear, allow notifications for Script Editor in System Settings > Notifications.
"""

from __future__ import annotations

import logging
import subprocess
import sys

from app.logging_setup import event

log = logging.getLogger("xenio.desktop.notify")

_SCRIPT = """on run argv
  if (item 4 of argv) is "1" then
    display notification (item 2 of argv) with title (item 1 of argv) subtitle (item 3 of argv) sound name "Glass"
  else
    display notification (item 2 of argv) with title (item 1 of argv) subtitle (item 3 of argv)
  end if
end run"""


def _clip(text: str, limit: int) -> str:
    text = " ".join("".join(ch if ch.isprintable() else " " for ch in str(text or "")).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def notify(title: str, message: str, subtitle: str = "", sound: bool = True, run=subprocess.run) -> bool:
    """Show one notification. Returns False (and logs why) when it could not be shown; never raises."""
    if sys.platform != "darwin":
        return False
    args = ["osascript", "-e", _SCRIPT, "--", _clip(title, 80) or "XENIO", _clip(message, 400), _clip(subtitle, 80),
            "1" if sound else "0"]
    try:
        done = run(args, capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        event(log, "Desktop notification failed", logging.WARNING, error=str(exc)[:200])
        return False
    if done.returncode != 0:
        event(log, "Desktop notification failed", logging.WARNING, error=(done.stderr or "")[:200])
        return False
    return True
