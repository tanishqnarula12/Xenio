"""The desktop service behind XENIO's desktop tools (V5.5): open apps, manage windows, find UI elements, act, verify.

It runs inside Hermes (xenio-guard plugin). The policy has already decided that an action may run (and the user has
said yes where needed) before any method here is called. This module makes the action precise and honest:

* **targets**: a named element ("the message box", "Send") is found through UI Automation in the active window, so a
  click lands on the element itself, not on a guessed pixel;
* **verification**: opening an app waits for its window; typing reads the field back; clicks and keys report
  whether the screen changed and which window is in front. Unverified is never reported as done;
* **emergency stop**: every step checks "the user said stop" (voice) and the physical Esc key;
* **short-lived context**: which apps were opened or focused recently (names only, nothing from the screen).
"""

from __future__ import annotations

import collections
import difflib
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from app.desktop import apps as apps_mod
from app.desktop.input import DesktopController, InputError, Stopped, normalize_combo, parse_keys
from app.desktop.stop import EscWatcher, stop_requested_since
from app.desktop.uia import UIAClient, UIAError

GUI_ACTIONS = ("move", "click", "double_click", "right_click", "scroll", "drag", "type", "key")
WINDOW_ACTIONS = ("list", "focus", "minimize", "maximize", "restore", "close")
CONTEXT_SECONDS = 1800
TRAY_GRACE = 6.0  # seconds a tray app gets to show its own window again before XENIO shows it


class DesktopError(Exception):
    pass


@dataclass
class _Recent:
    name: str
    at: float


def _norm_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\r", "\n")).strip()


class DesktopService:
    def __init__(self, catalog: apps_mod.AppCatalog | None = None, uia: UIAClient | None = None, controller=None,
                 windows=None, data_dir: Path | None = None, clock=time.time, screen_digest=None, sleep=time.sleep):
        from app.desktop import windows as windows_mod

        self.catalog = catalog or apps_mod.AppCatalog(data_dir)
        self.uia = uia or UIAClient()
        self.windows = windows or windows_mod
        self._controller = controller
        self._data_dir = data_dir
        self._clock = clock
        self._sleep = sleep
        self._digest = screen_digest or _screen_digest
        self.turn_started = 0.0  # set per user request by the plugin: stops before it do not count
        self._recent: collections.deque[_Recent] = collections.deque(maxlen=8)
        self.last_typed: tuple[str, str] | None = None  # (app, text) - for "send it?" prompts, cleared on a new turn
        self._lock = threading.Lock()

    # ---- context ------------------------------------------------------------------------------------------------

    def new_turn(self) -> None:
        self.turn_started = self._clock()

    def stopped(self) -> bool:
        return stop_requested_since(self.turn_started, self._data_dir) if self.turn_started else False

    def _check_unlocked(self) -> None:
        locked = getattr(self.windows, "session_locked", None)
        if locked is not None and locked():
            raise DesktopError("The PC is locked, so nothing was opened, clicked or typed (input would go to the lock "
                               "screen). Tell the user to unlock it and ask again.")

    def remember(self, app: str) -> None:
        with self._lock:
            self._recent = collections.deque((r for r in self._recent if r.name.lower() != app.lower()), maxlen=8)
            self._recent.append(_Recent(app, self._clock()))

    def context(self) -> dict:
        now = self._clock()
        recent = [r.name for r in reversed(self._recent) if now - r.at < CONTEXT_SECONDS]
        active = None
        try:
            fg = self.windows.active_app_window()
            active = fg.app if fg else None
        except Exception:  # noqa: BLE001 - context is a convenience
            pass
        return {"active_app": active, "recent_apps": recent[:5]}

    # ---- apps ---------------------------------------------------------------------------------------------------

    def list_apps(self, query: str = "", limit: int = 15) -> list[dict]:
        entries = self.catalog.search(query, limit) if query else sorted(self.catalog.entries(), key=lambda e: e.name)[:limit]
        return [{"name": e.name, "kind": e.kind, "source": e.source} for e in entries]

    def resolve_app(self, name: str) -> apps_mod.Resolution:
        return self.catalog.resolve(name)

    def open_app(self, name: str, wait_seconds: float = 25.0) -> dict:
        if self.stopped():
            raise DesktopError("stopped by the user")
        res = self.resolve_app(name)
        if not res.matches or res.confidence < 0.6:
            raise DesktopError(f"No installed app called {name!r} was found. Ask the user for its exact name, or use "
                               f"xenio_list_apps to search.")
        if res.ambiguous:
            raise DesktopError(f"Several installed apps match {name!r}: {', '.join(res.candidates())}. Ask the user "
                               "which one they mean.")
        entry = res.best
        self._check_unlocked()
        started = time.monotonic()
        windows = self.windows.list_windows()
        open_now = [w for w in windows if apps_mod.window_matches(entry, w)]
        if open_now:
            # Already open, maybe minimized or behind other windows: bring it to the front. Launching it again does not
            # reliably do that (measured: a minimized Discord stayed minimized for 25 s).
            window = max(open_now, key=lambda w: (w.foreground, not w.minimized))  # ties: topmost (Windows' order)
            if window.foreground or self.windows.focus(window.hwnd):
                self.remember(entry.name)
                fresh = next((w for w in self.windows.list_windows() if w.hwnd == window.hwnd), window)
                return {"app": entry.name, "verified": True, "how": "already_open", "window": fresh.public(),
                        "in_front": True, "launch_s": round(time.monotonic() - started, 1), "resolution": res.describe()}
            # Windows refused the focus change: start it as the Start menu does, which usually brings it forward
        before = {w.hwnd for w in windows}
        in_tray = [w for w in self.windows.list_hidden_windows() if apps_mod.window_matches(entry, w)]
        timeout = max(3.0, min(float(wait_seconds or 25), 60.0))
        apps_mod.launch(entry)
        window, how = apps_mod.wait_for_window(entry, before, self.windows.list_windows,
                                                timeout=min(timeout, TRAY_GRACE) if in_tray else timeout)
        if window is None and in_tray:
            # Already running in the tray, and launching it again did not bring its window back (measured: Discord):
            # show that hidden main window ourselves; only a window that was already there before the launch.
            main = max(in_tray, key=lambda w: (w.rect[2] - w.rect[0]) * (w.rect[3] - w.rect[1]))
            if self.windows.show(main.hwnd):
                window = next((w for w in self.windows.list_windows() if w.hwnd == main.hwnd), None)
                how = "shown_from_tray" if window is not None else how
        elapsed = round(time.monotonic() - started, 1)
        if window is None and open_now:
            raise DesktopError(f"{entry.name} is open, but Windows did not let it come to the front. Ask the user to "
                               "click it in the taskbar; nothing was typed or clicked.")
        if window is None:
            raise DesktopError(f"{entry.name} was started, but no window of it appeared within {elapsed} s, so it is "
                               "NOT verified as open. It may still be loading or may have opened in the tray.")
        focused = self.windows.focus(window.hwnd) if not window.foreground else True
        self.remember(entry.name)
        return {"app": entry.name, "verified": True, "how": how, "window": window.public(), "in_front": bool(focused),
                "launch_s": elapsed, "resolution": res.describe()}

    # ---- windows ------------------------------------------------------------------------------------------------

    def _window(self, name: str):
        matches = self.windows.find_windows(name)
        if not matches:
            raise DesktopError(f"No open window matches {name!r}.")
        names = {m.title for m in matches}
        if len(matches) > 1 and len({m.app for m in matches}) > 1:
            raise DesktopError(f"Several windows match {name!r}: {', '.join(sorted(names))[:300]}. Ask which one.")
        return matches[0]

    def window(self, action: str, name: str = "") -> dict:
        if action not in WINDOW_ACTIONS:
            raise DesktopError(f"action must be one of {', '.join(WINDOW_ACTIONS)}")
        if action == "list":
            return {"windows": [w.public() for w in self.windows.list_windows()][:30]}
        if self.stopped():
            raise DesktopError("stopped by the user")
        if not name:
            raise DesktopError("name the window or app")
        self._check_unlocked()
        target = self._window(name)
        if action == "focus":
            ok = self.windows.focus(target.hwnd)
            if not ok:
                raise DesktopError(f"{target.title!r} could not be brought to the front (Windows refused the focus "
                                   "change); NOT verified.")
            self.remember(target.app)
            return {"focused": target.public() | {"foreground": True}, "verified": True}
        if action == "close":
            outcome = self.windows.close(target.hwnd)
            return {"window": target.title[:120], "result": outcome, "verified": outcome == "closed",
                    **({"note": "the app is still open - it may be asking whether to save; nothing was forced"}
                       if outcome != "closed" else {})}
        ok = getattr(self.windows, action)(target.hwnd)
        return {"window": target.title[:120], "result": action + ("d" if action.endswith("e") else "ed"), "verified": ok}

    # ---- UI elements -------------------------------------------------------------------------------------------

    def _target_window(self, name: str = ""):
        if name:
            target = self._window(name)
            if not target.foreground and not self.windows.focus(target.hwnd):
                raise DesktopError(f"{target.title!r} could not be brought to the front")
            return self.windows.foreground_window() or target
        fg = self.windows.active_app_window()
        if fg is None:
            raise DesktopError("no active window")
        return fg

    def find_ui(self, query: str, window: str = "", limit: int = 8) -> dict:
        target = self._target_window(window)
        try:
            found = self.uia.find(target.hwnd, query or "", limit=limit)
        except UIAError as exc:
            raise DesktopError(f"the window's controls could not be read ({exc})") from None
        elements = [{k: e.get(k) for k in ("name", "type", "center", "rect", "enabled", "score")}
                    for e in found.get("elements", [])]
        return {"window": target.public(), "elements": elements, "complete": found.get("complete", True)}

    def _resolve_element(self, name: str, window: str = "") -> dict:
        found = self.find_ui(name, window, limit=5)
        elements = [e for e in found["elements"] if (e.get("score") or 0) >= 0.75 and e.get("enabled", True)]
        if not elements:
            raise DesktopError(f"No control named {name!r} was found in {found['window']['title']!r}. Use xenio_find_ui "
                               "to see what is there, or xenio_look_at_screen.")
        top = elements[0]["score"]
        same = [e for e in elements if e["score"] >= top - 0.01 and e["center"] != elements[0]["center"]]
        if same:
            names = ", ".join(f"{e['name']} ({e['type']})" for e in [elements[0], *same][:4])
            raise DesktopError(f"Several controls match {name!r}: {names}. Be more specific.")
        return elements[0]

    # ---- probing for the policy (what an action would really touch) -------------------------------------------

    def probe(self, args: dict) -> dict:
        """Facts the policy needs: the app in front, the element a click would hit, the focused field."""
        info: dict = {}
        try:
            locked = getattr(self.windows, "session_locked", None)
            if locked is not None and locked():
                return {"locked": True}
        except Exception:  # noqa: BLE001
            pass
        try:
            fg = self.windows.active_app_window()
            if args.get("window"):
                matches = self.windows.find_windows(str(args["window"]))
                fg = matches[0] if matches else fg
            if fg:
                info.update(app=fg.app.lower(), process=fg.process.lower(), title=fg.title[:200])
        except Exception:  # noqa: BLE001
            pass
        action = str(args.get("action") or "").lower()
        try:
            if action in ("click", "double_click", "right_click", "drag", "move"):
                if args.get("element"):
                    el = self._resolve_element(str(args["element"]), str(args.get("window") or ""))
                    info["element"] = {"name": el["name"], "type": el["type"], "parents": []}
                    info["point"] = el["center"]
                elif isinstance(args.get("x"), int) and isinstance(args.get("y"), int):
                    at = self.uia.at(args["x"], args["y"])
                    if at.get("found"):
                        info["element"] = {k: at.get(k) for k in ("name", "type", "is_password", "parents")}
            if action in ("type", "key"):
                focused = self.uia.focused()
                if focused.get("found"):
                    info["focused"] = {k: focused.get(k) for k in ("name", "type", "is_password", "class")}
        except (UIAError, DesktopError) as exc:
            info["probe_error"] = str(exc)[:160]
        if self.last_typed and info.get("app") and self.last_typed[0] == info["app"]:
            info["typed"] = self.last_typed[1]
        return info

    # ---- acting -------------------------------------------------------------------------------------------------

    def _controller_for(self, watcher: EscWatcher, in_front=None) -> DesktopController:
        check = lambda: watcher.pressed or self.stopped() or (in_front is not None and not in_front())  # noqa: E731
        if self._controller is not None:
            self._controller.stop_check = check
            return self._controller
        return DesktopController(stop_check=check)

    def gui(self, args: dict) -> dict:
        action = str(args.get("action") or "").lower()
        if action not in GUI_ACTIONS:
            raise DesktopError(f"action must be one of {', '.join(GUI_ACTIONS)}")
        if self.stopped():
            raise DesktopError("stopped by the user: no further desktop actions in this request")
        self._check_unlocked()
        target = self._target_window(str(args.get("window") or ""))
        if not target.foreground and not self.windows.focus(target.hwnd):  # input goes to the window in front
            raise DesktopError(f"{target.title!r} could not be brought to the front, so nothing was typed or clicked")
        result: dict = {"action": action, "app": target.app}
        before = self._digest() if action in ("click", "double_click", "right_click", "key", "drag") else None
        lost: dict = {}

        def still_in_front() -> bool:
            """Checked before every keystroke: another app taking the front (measured: Spotify after it loads) must
            never receive the rest of the text."""
            fg = self.windows.foreground_window()
            if fg is not None and (fg.hwnd == target.hwnd or getattr(fg, "pid", None) == getattr(target, "pid", -1)):
                return True
            lost["by"] = fg.title[:80] if fg is not None else "another window"
            return False

        with EscWatcher() as watcher:
            c = self._controller_for(watcher, still_in_front if action == "type" else None)
            try:
                if action in ("click", "double_click", "right_click", "move", "drag"):
                    x, y = self._point(args, "element", "x", "y")
                    if action == "move":
                        c.move_mouse(x, y)
                    elif action == "drag":
                        tx, ty = self._point(args, "to_element", "to_x", "to_y")
                        c.drag((x, y), (tx, ty))
                    else:
                        {"click": lambda: c.click(x, y, str(args.get("button") or "left")),
                         "double_click": lambda: c.double_click(x, y), "right_click": lambda: c.right_click(x, y)}[action]()
                    result["at"] = [x, y]
                elif action == "scroll":
                    amount = args.get("amount")
                    c.scroll(amount if isinstance(amount, int) else -3, args.get("x"), args.get("y"))
                elif action == "type":
                    text = args.get("text")
                    before_value = self._field_value()
                    typed = c.type_text(text)
                    result["typed_chars"] = typed
                    self.last_typed = (target.app.lower(), text)
                    check = self._verify_typing(text)
                    if check.get("verified") is False and before_value is not None and isinstance(text, str):
                        check = self._repair_typing(c, text, before_value) or check
                    result.update(check)
                else:  # key
                    with watcher.suspend():
                        c.hotkey(str(args.get("keys") or ""))
                    result["keys"] = normalize_combo(str(args.get("keys") or ""))
                    if result["keys"] in ("enter", "ctrl+enter"):
                        self.last_typed = None
            except Stopped:
                if lost:
                    text, done = str(args.get("text") or ""), c.typed
                    raise DesktopError(
                        f"INTERRUPTED: {lost['by']!r} came to the front while typing into {target.title[:80]!r}, so "
                        f"typing stopped after {done} of {len(text)} characters (the rest was NOT typed anywhere). "
                        f"Bring {target.app} back to the front (xenio_window focus) and type only the rest: "
                        f"«{text[done:]}»") from None
                done = f" after typing {c.typed} of {len(str(args.get('text') or ''))} characters" if action == "type" else ""
                raise DesktopError(f"STOPPED{done}: the user stopped XENIO (voice or Esc). Nothing further was done.") from None
            except InputError as exc:
                raise DesktopError(str(exc)) from None
        if watcher.pressed:
            raise DesktopError("STOPPED: Esc was pressed during the action.")
        self._sleep(0.35)
        after_fg = self.windows.foreground_window()
        result["in_front_now"] = after_fg.public() if after_fg else None
        if before is not None:
            after = self._digest()
            result["screen_changed"] = (before != after) if before and after else None  # None = could not check
            if result["screen_changed"] is False:
                result["note"] = ("nothing visibly changed on screen after the action: it may have had no effect. Check "
                                  "with xenio_look_at_screen before saying it worked.")
        self.remember(target.app)
        return result

    def _point(self, args: dict, element_key: str, x_key: str, y_key: str) -> tuple[int, int]:
        if args.get(element_key):
            center = self._resolve_element(str(args[element_key]), str(args.get("window") or ""))["center"]
            return int(center[0]), int(center[1])
        x, y = args.get(x_key), args.get(y_key)
        if not isinstance(x, int) or not isinstance(y, int) or isinstance(x, bool) or isinstance(y, bool):
            raise DesktopError(f"give either '{element_key}' (a control's name) or whole-number '{x_key}'/'{y_key}' "
                               "screen coordinates (from xenio_find_ui)")
        return x, y

    def _field_value(self) -> str | None:
        """The focused field's text (None: unreadable, or a password field, which is never read)."""
        try:
            focused = self.uia.focused(with_value=True)
        except UIAError:
            return None
        return None if focused.get("is_password") else focused.get("value")

    def _repair_typing(self, c: DesktopController, text: str, before: str) -> dict | None:
        """The field changed, but not into the text (measured: Notepad once dropped a keystroke: 'hello rom Xenio').
        Only when exactly what this call inserted can be told apart: delete just that, type it once more, slower,
        and verify again. Anything else (the field changed in other ways, multi-line text) stays reported as unverified."""
        after = self._field_value()
        if after is None or "\n" in text or "\t" in text:
            return None
        p = 0
        while p < min(len(before), len(after)) and before[p] == after[p]:
            p += 1
        s = 0
        while s < min(len(before), len(after)) - p and before[-1 - s] == after[-1 - s]:
            s += 1
        inserted = after[p:len(after) - s]
        if before[p:len(before) - s] or not inserted or len(inserted) > len(text) + 5:
            return None  # text was removed or replaced too: not a plain insertion by this call
        if difflib.SequenceMatcher(None, inserted, text).ratio() < 0.6:
            return None  # not recognisably our text (an autocomplete, another program)
        for _ in range(len(inserted)):  # the caret sits right after what was inserted
            c.hotkey("backspace")
        self._sleep(0.15)
        if self._field_value() != before:
            return {"verified": False, "verification": "the typed text was garbled and could not be cleanly removed; "
                                                       "look at the field before doing anything else"}
        normal = c.key_delay
        c.key_delay = max(0.06, normal * 2)
        try:
            c.type_text(text)
        finally:
            c.key_delay = normal
        out = self._verify_typing(text)
        out["repaired"] = f"the first attempt came out as «{inserted[:80]}»; it was deleted and typed again"
        return out

    def _verify_typing(self, text: str) -> dict:
        self._sleep(0.25)
        try:
            focused = self.uia.focused(with_value=True)
        except UIAError:
            return {"verified": None, "verification": "the field could not be read back"}
        value = focused.get("value")
        if value is None:
            return {"verified": None, "verification": "this field cannot be read back; confirm with xenio_look_at_screen "
                                                      "if it matters"}
        ok = _norm_text(text) in _norm_text(value)
        out = {"verified": ok, "field": (focused.get("name") or focused.get("type") or "")[:80]}
        if not ok:
            out["verification"] = "the typed text was NOT found in the field afterwards; do not report it as typed"
        return out


def _screen_digest() -> str:
    """A tiny capture (no file, no upload) to tell whether anything on screen changed."""
    try:
        from app.vision.capture import ScreenCapture

        image = ScreenCapture(max_edge=320).capture_screen()
        digest = image.digest
        image.discard()
        return digest
    except Exception:  # noqa: BLE001 - a missing check is reported as "unknown", never as success
        return ""


def describe_keys(keys: str) -> str:
    try:
        return normalize_combo(keys)
    except InputError:
        return keys


__all__ = ["DesktopService", "DesktopError", "GUI_ACTIONS", "WINDOW_ACTIONS", "parse_keys", "describe_keys"]
