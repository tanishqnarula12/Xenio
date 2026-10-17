"""Mouse and keyboard (V5.5): XENIO's hands.

``DesktopController`` moves, clicks, scrolls, drags, types and presses keys the way a real mouse/keyboard does:
Windows' ``SendInput`` in physical screen pixels, or on macOS CGEvents posted at the hardware event tap, in display
points. It does not decide what is allowed (the policy does,
before the plugin lets a call run) but it enforces the mechanics:

* coordinates must be whole numbers on the desktop (no off-screen or malformed targets);
* text is typed as Unicode characters, so Hindi, German or emoji type exactly as given, whatever the keyboard
  layout; at most 2 000 characters per call;
* key names are checked against a fixed list (``enter``, ``ctrl+shift+p``, ``f5``, ``win+r``, ``cmd+s`` ...);
* every step checks the emergency stop (``stop_check``), so "Xenio stop" or the Esc key ends typing mid-word;
* nothing is logged here: the audit log gets the action type, never the typed text.

The ``backend`` is swappable: unit tests record the input events instead of sending them.
"""

from __future__ import annotations

import asyncio
import sys
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
_VK.update({"cmd": 0x5B, "command": 0x5B, "meta": 0x5B, "option": 0x12, "opt": 0x12, "forwarddelete": 0x2E,
            "fwddelete": 0x2E})
_VK.update({f"f{i}": 0x6F + i for i in range(1, 25)})
_VK.update({chr(c): c for c in range(ord("0"), ord("9") + 1)})
_VK.update({chr(c).lower(): c for c in range(ord("A"), ord("Z") + 1)})
# The key next to the space bar: Windows key on a PC, Command on a Mac (same key code, as Apple keyboards on Windows)
GUI_KEY = "cmd" if sys.platform == "darwin" else "win"
MODIFIERS = {"ctrl", "control", "alt", "menu", "shift", "win", "windows", "lwin", "super", "cmd", "command", "meta",
             "option", "opt"}
_EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5D}  # need the extended-key flag
_ALIASES = {"control": "ctrl", "menu": "alt", "option": "alt", "opt": "alt", "return": "enter", "escape": "esc",
            "del": "delete", "pgup": "pageup", "pgdn": "pagedown",
            **{k: GUI_KEY for k in ("win", "windows", "lwin", "super", "cmd", "command", "meta")}}
# The Mac key labelled "delete" is backspace; forward delete is its own name there (fn+delete)
_ALIASES.update({"delete": "backspace", "del": "backspace", "fwddelete": "forwarddelete"} if GUI_KEY == "cmd"
                else {"forwarddelete": "delete", "fwddelete": "delete"})


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
    if any(k not in ("ctrl", "alt", "shift", GUI_KEY) for k in out[:-1]):
        raise InputError(f"in a combination, every key but the last must be ctrl, alt, shift or {GUI_KEY}")
    return out


def normalize_combo(keys: str) -> str:
    """Canonical text of a key combination ("Shift+Ctrl+P" -> "ctrl+shift+p"), for policy decisions."""
    parts = parse_keys(keys)
    order = {"ctrl": 0, "alt": 1, "shift": 2, GUI_KEY: 3}
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


# Windows virtual-key code -> macOS virtual key code (Carbon kVK_*; the same on every Mac keyboard layout position)
_MAC_KEYS = {
    0x08: 0x33, 0x09: 0x30, 0x0D: 0x24, 0x10: 0x38, 0x11: 0x3B, 0x12: 0x3A, 0x14: 0x39, 0x1B: 0x35, 0x20: 0x31,
    0x21: 0x74, 0x22: 0x79, 0x23: 0x77, 0x24: 0x73, 0x25: 0x7B, 0x26: 0x7E, 0x27: 0x7C, 0x28: 0x7D, 0x2D: 0x72,
    0x2E: 0x75, 0x5B: 0x37,
    0xBA: 0x29, 0xBB: 0x18, 0xBC: 0x2B, 0xBD: 0x1B, 0xBE: 0x2F, 0xBF: 0x2C, 0xC0: 0x32, 0xDB: 0x21, 0xDC: 0x2A,
    0xDD: 0x1E, 0xDE: 0x27,
    **dict(zip(range(ord("0"), ord("9") + 1), (0x1D, 0x12, 0x13, 0x14, 0x15, 0x17, 0x16, 0x1A, 0x1C, 0x19))),
    **dict(zip(range(ord("A"), ord("Z") + 1), (0x00, 0x0B, 0x08, 0x02, 0x0E, 0x03, 0x05, 0x04, 0x22, 0x26, 0x28, 0x25,
                                               0x2E, 0x2D, 0x1F, 0x23, 0x0C, 0x0F, 0x01, 0x11, 0x20, 0x09, 0x0D, 0x07,
                                               0x10, 0x06))),
    **dict(zip(range(0x70, 0x84), (0x7A, 0x78, 0x63, 0x76, 0x60, 0x61, 0x62, 0x64, 0x65, 0x6D, 0x67, 0x6F, 0x69, 0x6B,
                                   0x71, 0x6A, 0x40, 0x4F, 0x50, 0x5A))),
}
_MAC_FLAGS = {0x10: 0x20000, 0x11: 0x40000, 0x12: 0x80000, 0x5B: 0x100000}  # shift, control, option, command
_MAC_MEDIA = {0xB3: 16, 0xB0: 17, 0xB1: 18, 0xAD: 7, 0xAE: 1, 0xAF: 0}  # play/pause, next, previous, mute, vol -/+


class MacBackend:
    """macOS: CGEvents posted where hardware events enter (needs the Accessibility permission). The controller speaks
    Windows' virtual-key codes and mouse flags; they are translated here. Held modifiers are put on every key event
    (apps read the flags, not the order of key events), and repeated clicks carry their click count, so a double
    click is a double click to the app."""

    def __init__(self):
        from app.desktop import macos

        self._m = macos
        if not (macos.trusted() or macos.can_post_events()):
            raise InputError(macos.ACCESSIBILITY_STEP)
        self._flags = 0
        self._held: set[int] = set()  # mouse buttons held: 0 left, 1 right, 2 middle
        self._pos = macos.mouse_location()
        self._last_up: tuple[float, int, tuple, int] = (0.0, -1, (0, 0), 0)  # time, button, where, clicks
        self._dct = macos.double_click_interval()

    def bounds(self) -> Bounds:
        return Bounds(*self._m.desktop_bounds())

    def move(self, x: int, y: int) -> None:
        m = self._m
        kind, button = ((m.EV_LEFT_DRAGGED, 0) if 0 in self._held else (m.EV_RIGHT_DRAGGED, 1) if 1 in self._held
                        else (m.EV_OTHER_DRAGGED, 2) if 2 in self._held else (m.EV_MOVED, 0))
        m.post(m.mouse_event(kind, x, y, button, flags=self._flags))
        self._pos = (x, y)

    def mouse(self, flags: int, data: int = 0) -> None:
        m = self._m
        if flags in (0x0800, 0x1000):  # wheel: Windows sends 120 per notch, about 3 lines
            lines = max(1, abs(data) // 40) * (1 if data > 0 else -1)
            m.post(m.scroll_event(lines, 0) if flags == 0x0800 else m.scroll_event(0, lines))
            return
        mapping = {0x0002: (0, True), 0x0004: (0, False), 0x0008: (1, True), 0x0010: (1, False),
                   0x0020: (2, True), 0x0040: (2, False)}
        if flags not in mapping:
            raise InputError(f"unsupported mouse input {flags:#x}")
        button, down = mapping[flags]
        kinds = {0: (m.EV_LEFT_DOWN, m.EV_LEFT_UP), 1: (m.EV_RIGHT_DOWN, m.EV_RIGHT_UP), 2: (m.EV_OTHER_DOWN, m.EV_OTHER_UP)}
        at, then, last_button, last_at, last_clicks = self._pos, *self._last_up
        if down:
            same = last_button == button and last_at == at and time.monotonic() - then <= self._dct
            self._clicks = last_clicks + 1 if same else 1
            self._held.add(button)
        else:
            self._held.discard(button)
        clicks = getattr(self, "_clicks", 1)
        m.post(m.mouse_event(kinds[button][0 if down else 1], at[0], at[1], button, clicks, self._flags))
        if not down:
            self._last_up = (time.monotonic(), button, at, clicks)

    def key(self, vk: int, up: bool) -> None:
        if vk in _MAC_MEDIA:
            if not up:  # one press = down and up of the system media key
                self._m.press_media_key(_MAC_MEDIA[vk])
            return
        code = _MAC_KEYS.get(vk)
        if code is None:
            raise InputError("that key does not exist on a Mac keyboard")
        if vk in _MAC_FLAGS:
            self._flags = (self._flags & ~_MAC_FLAGS[vk]) if up else (self._flags | _MAC_FLAGS[vk])
        self._m.post(self._m.key_event(code, not up, self._flags))

    def unicode_tap(self, code_units: list[int]) -> None:
        self._m.post(self._m.unicode_event(code_units, True))
        self._m.post(self._m.unicode_event(code_units, False))

    def double_click_time(self) -> float:
        return self._dct


def default_backend():
    return MacBackend() if sys.platform == "darwin" else Win32Backend()


_BUTTONS = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}


class DesktopController:
    """move_mouse / click / double_click / right_click / scroll / drag / type_text / press_key / hotkey.

    Synchronous core (the plugin's tool handlers are synchronous) with ``async`` wrappers for the rest of XENIO."""

    def __init__(self, backend=None, stop_check: Callable[[], bool] | None = None, sleep=time.sleep,
                 key_delay: float = 0.03):  # measured: at 8 ms per character apps drop keystrokes, at 30 ms none
        self.backend = backend or default_backend()
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
