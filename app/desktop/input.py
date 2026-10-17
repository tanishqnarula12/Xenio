"""Mouse and keyboard (V5.5): XENIO's hands.

``DesktopController`` moves, clicks, scrolls, drags, types and presses keys with Windows' ``SendInput`` (the same
path a real mouse/keyboard takes), in physical screen pixels. It does not decide what is allowed (the policy does,
before the plugin lets a call run) but it enforces the mechanics:

* coordinates must be whole numbers on the desktop (no off-screen or malformed targets);
* text is typed as Unicode characters, so Hindi, German or emoji type exactly as given, whatever the keyboard
  layout; at most 2 000 characters per call;
* key names are checked against a fixed list (``enter``, ``ctrl+shift+p``, ``f5``, ``win+r`` ...);
* every step checks the emergency stop (``stop_check``), so "Xenio stop" or the Esc key ends typing mid-word;
* nothing is logged here: the audit log gets the action type, never the typed text.

The ``backend`` is swappable: unit tests record the input events instead of sending them.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass

MAX_TEXT = 2000
MAX_SCROLL = 50
MAX_HOTKEY_KEYS = 4

_VK = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "return": 0x0D, "shift": 0x10, "ctrl": 0x11, "control": 0x11,
    "alt": 0x12, "menu": 0x12, "pause": 0x13, "capslock": 0x14, "esc": 0x1B, "escape": 0x1B, "space": 0x20,
    "pageup": 0x21, "pgup": 0x21, "pagedown": 0x22, "pgdn": 0x22, "end": 0x23, "home": 0x24, "left": 0x25,
    "up": 0x26, "right": 0x27, "down": 0x28, "printscreen": 0x2C, "insert": 0x2D, "delete": 0x2E, "del": 0x2E,
    "win": 0x5B, "windows": 0x5B, "lwin": 0x5B, "super": 0x5B, "apps": 0x5D, "contextmenu": 0x5D,
    "numlock": 0x90, "scrolllock": 0x91, "volumemute": 0xAD, "volumedown": 0xAE, "volumeup": 0xAF,
    "medianext": 0xB0, "mediaprev": 0xB1, "mediastop": 0xB2, "mediaplaypause": 0xB3, "playpause": 0xB3,
    ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF, "`": 0xC0, "[": 0xDB, "\\": 0xDC, "]": 0xDD,
    "'": 0xDE,
}
_VK.update({f"f{i}": 0x6F + i for i in range(1, 25)})
_VK.update({chr(c): c for c in range(ord("0"), ord("9") + 1)})
_VK.update({chr(c).lower(): c for c in range(ord("A"), ord("Z") + 1)})
MODIFIERS = {"ctrl", "control", "alt", "menu", "shift", "win", "windows", "lwin", "super"}
_EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5D}  # need the extended-key flag
_ALIASES = {"control": "ctrl", "menu": "alt", "windows": "win", "lwin": "win", "super": "win", "return": "enter",
            "escape": "esc", "del": "delete", "pgup": "pageup", "pgdn": "pagedown"}


class InputError(Exception):
    pass


class Stopped(InputError):
    """The emergency stop fired during the action."""


def parse_keys(keys: str) -> list[str]:
    """"Ctrl+Shift+P" -> ["ctrl", "shift", "p"]. Raises InputError for unknown or malformed key names."""
    if not isinstance(keys, str) or not keys.strip():
        raise InputError("no key given")
    parts = [p.strip().lower() for p in keys.replace(" ", "").split("+")]
    if "" in parts or len(parts) > MAX_HOTKEY_KEYS:
        raise InputError(f"write a key or a combination of 1 to {MAX_HOTKEY_KEYS} keys joined by '+', e.g. ctrl+s")
    out = []
    for part in parts:
        if part not in _VK:
            raise InputError(f"unknown key {part!r}")
        out.append(_ALIASES.get(part, part))
    if any(k not in ("ctrl", "alt", "shift", "win") for k in out[:-1]):
        raise InputError("in a combination, every key but the last must be ctrl, alt, shift or win")
    return out


def normalize_combo(keys: str) -> str:
    """Canonical text of a key combination ("Shift+Ctrl+P" -> "ctrl+shift+p"), for policy decisions."""
    parts = parse_keys(keys)
    order = {"ctrl": 0, "alt": 1, "shift": 2, "win": 3}
    mods = sorted((p for p in parts[:-1]), key=lambda p: order.get(p, 9))
    return "+".join(mods + [parts[-1]])


@dataclass
class Bounds:
    x: int
    y: int
    width: int
    height: int

    def contains(self, px: int, py: int) -> bool:
        return self.x <= px < self.x + self.width and self.y <= py < self.y + self.height


def check_point(x, y, bounds: Bounds) -> tuple[int, int]:
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in (x, y)):
        raise InputError("coordinates must be whole numbers of screen pixels")
    if not bounds.contains(x, y):
        raise InputError(f"({x}, {y}) is outside the screen ({bounds.width}x{bounds.height} at {bounds.x},{bounds.y})")
    return x, y


class Win32Backend:
    """The real thing: SendInput / SetCursorPos in physical pixels."""

    def __init__(self):
        from app.desktop import win32

        self._w = win32

    def bounds(self) -> Bounds:
        return Bounds(*self._w.virtual_screen())

    def move(self, x: int, y: int) -> None:
        with self._w.physical_pixels():
            if not self._w.user32.SetCursorPos(x, y):
                raise InputError("the mouse pointer could not be moved")

    def mouse(self, flags: int, data: int = 0) -> None:
        event = self._w.INPUT(type=self._w.INPUT_MOUSE)
        event.u.mi = self._w.MOUSEINPUT(0, 0, data & 0xFFFFFFFF, flags, 0, 0)
        if self._w.send_inputs([event]) != 1:
            raise InputError("Windows refused the mouse input (another program may be blocking input)")

    def key(self, vk: int, up: bool) -> None:
        flags = (self._w.KEYEVENTF_KEYUP if up else 0) | (self._w.KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED else 0)
        event = self._w.INPUT(type=self._w.INPUT_KEYBOARD)
        event.u.ki = self._w.KEYBDINPUT(vk, self._w.user32.MapVirtualKeyW(vk, 0), flags, 0, 0)
        if self._w.send_inputs([event]) != 1:
            raise InputError("Windows refused the keyboard input")

    def unicode_tap(self, code_units: list[int]) -> None:
        """One character (1-2 UTF-16 code units), press and release in a single SendInput call."""
        events = []
        for up in (False, True):
            for unit in code_units:
                event = self._w.INPUT(type=self._w.INPUT_KEYBOARD)
                event.u.ki = self._w.KEYBDINPUT(0, unit, self._w.KEYEVENTF_UNICODE | (self._w.KEYEVENTF_KEYUP if up else 0),
                                                0, 0)
                events.append(event)
        if self._w.send_inputs(events) != len(events):
            raise InputError("Windows refused the keyboard input")

    def double_click_time(self) -> float:
        return self._w.user32.GetDoubleClickTime() / 1000.0


_BUTTONS = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}


class DesktopController:
    """move_mouse / click / double_click / right_click / scroll / drag / type_text / press_key / hotkey.

    Synchronous core (the plugin's tool handlers are synchronous) with ``async`` wrappers for the rest of XENIO."""

    def __init__(self, backend=None, stop_check: Callable[[], bool] | None = None, sleep=time.sleep,
                 key_delay: float = 0.03):  # measured: at 8 ms per character apps drop keystrokes, at 30 ms none
        self.backend = backend or Win32Backend()
        self.stop_check = stop_check or (lambda: False)
        self._sleep = sleep
        self.key_delay = key_delay
        self.typed = 0  # characters typed by the last type_text, also when it was stopped

    def _guard(self) -> None:
        if self.stop_check():
            raise Stopped("stopped by the user")

    def _point(self, x, y) -> tuple[int, int]:
        return check_point(x, y, self.backend.bounds())

    # ---- mouse -----------------------------------------------------------------------------------------------

    def move_mouse(self, x, y) -> None:
        x, y = self._point(x, y)
        self._guard()
        self.backend.move(x, y)

    def click(self, x, y, button: str = "left", count: int = 1) -> None:
        if button not in _BUTTONS:
            raise InputError("button must be left, right or middle")
        down, up = _BUTTONS[button]
        x, y = self._point(x, y)
        self._guard()
        self.backend.move(x, y)
        self._sleep(0.03)
        for i in range(count):
            self._guard()
            self.backend.mouse(down)
            self.backend.mouse(up)
            if i + 1 < count:
                self._sleep(min(0.08, self.backend.double_click_time() / 4))

    def double_click(self, x, y) -> None:
        self.click(x, y, "left", 2)

    def right_click(self, x, y) -> None:
        self.click(x, y, "right", 1)

    def scroll(self, amount: int, x=None, y=None, horizontal: bool = False) -> None:
        """``amount`` wheel notches: positive = up (or right), negative = down (or left)."""
        if not isinstance(amount, int) or isinstance(amount, bool) or amount == 0 or abs(amount) > MAX_SCROLL:
            raise InputError(f"scroll amount must be a whole number between -{MAX_SCROLL} and {MAX_SCROLL}, not 0")
        if x is not None or y is not None:
            self.move_mouse(x, y)
        flag = 0x1000 if horizontal else 0x0800
        step = 1 if amount > 0 else -1
        for _ in range(abs(amount)):
            self._guard()
            self.backend.mouse(flag, step * 120)
            self._sleep(0.02)

    def drag(self, start: tuple[int, int], end: tuple[int, int], steps: int = 12) -> None:
        (sx, sy), (ex, ey) = self._point(*start), self._point(*end)
        self._guard()
        self.backend.move(sx, sy)
        self._sleep(0.05)
        self.backend.mouse(_BUTTONS["left"][0])
        try:
            for i in range(1, steps + 1):
                self._guard()
                self.backend.move(sx + (ex - sx) * i // steps, sy + (ey - sy) * i // steps)
                self._sleep(0.015)
        finally:
            self.backend.mouse(_BUTTONS["left"][1])  # never leave the button held down, even when stopped

    # ---- keyboard --------------------------------------------------------------------------------------------

    def type_text(self, text: str) -> int:
        """Type ``text`` exactly (Unicode). Returns the number of characters typed (fewer if stopped)."""
        if not isinstance(text, str) or not text:
            raise InputError("nothing to type")
        if len(text) > MAX_TEXT:
            raise InputError(f"at most {MAX_TEXT} characters per call")
        typed = self.typed = 0
        for ch in text.replace("\r\n", "\n"):
            self._guard()
            if ch == "\n":
                self.backend.key(0x0D, False)
                self.backend.key(0x0D, True)
            elif ch == "\t":
                self.backend.key(0x09, False)
                self.backend.key(0x09, True)
            else:
                data = ch.encode("utf-16-le")  # characters outside the BMP are two UTF-16 code units
                self.backend.unicode_tap([int.from_bytes(data[i:i + 2], "little") for i in range(0, len(data), 2)])
            typed = self.typed = typed + 1
            self._sleep(self.key_delay)
        return typed

    def press_key(self, key: str) -> None:
        self.hotkey(key)

    def hotkey(self, *keys: str) -> None:
        combo = parse_keys("+".join(keys)) if len(keys) > 1 else parse_keys(keys[0])
        vks = [_VK[k] for k in combo]
        self._guard()
        pressed = []
        try:
            for vk in vks:
                self.backend.key(vk, False)
                pressed.append(vk)
                self._sleep(0.01)
        finally:
            for vk in reversed(pressed):  # always release, in reverse order
                self.backend.key(vk, True)

    # ---- async wrappers (spec: DesktopController.move_mouse(...) etc. as coroutines) ---------------------------

    async def amove_mouse(self, x, y):
        return await asyncio.to_thread(self.move_mouse, x, y)

    async def aclick(self, x, y, button="left"):
        return await asyncio.to_thread(self.click, x, y, button)

    async def atype_text(self, text):
        return await asyncio.to_thread(self.type_text, text)

    async def ahotkey(self, *keys):
        return await asyncio.to_thread(self.hotkey, *keys)
