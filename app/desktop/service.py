"""The desktop service behind XENIO's desktop tools (V5.5): open apps, manage windows, find UI elements, act, verify.

It runs inside Hermes (xenio-guard plugin). The policy has already decided that an action may run (and the user has
said yes where needed) before any method here is called. This module makes the action precise and honest:

* **targets**: a named element ("the message box", "Send") is found through UI Automation (Windows) or the
  Accessibility API (macOS) in the active window, so a click lands on the element itself, not on a guessed pixel;
* **verification**: opening an app waits for its window; typing reads the field back; clicks and keys report
  whether the screen changed and which window is in front. Unverified is never reported as done;
* **writing code and long text** (V3): ``paste`` puts the text in at once through the clipboard (the user's clipboard
  is put back), ``type`` with ``code`` types it line by line for editors that refuse pastes, undoing what code
  editors add on their own (indentation, closing brackets, autocomplete), and ``copy`` reads a field or page exactly.
  Written code is read back and compared before it is reported as written;
* **emergency stop**: every step checks "the user said stop" (voice) and the physical Esc key;
* **one action at a time** (2026-10-25): a second input action while one runs is refused, a newer request in the same
  session cancels the older one's typing, and every action has a deadline below Hermes' own (Hermes gives up on a
  tool at its deadline or on a stop, but the thread that types would go on);
* **coding tasks** (``app.desktop.task``): the steps of "solve this on my screen" as explicit states, a ledger that
  keeps a program from being written twice, and an ``outcome`` that says exactly what was verified, compiled and passed;
* **full screen kept**: code is typed without Esc (measured: an Esc the page does not use ends Safari's full screen),
  and the window's full screen is checked before and after every action - restored if XENIO's own keys ended it;
* **short-lived context**: which apps were opened or focused recently (names only, nothing from the screen).
"""

from __future__ import annotations

import collections
import difflib
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from app.desktop import apps as apps_mod
from app.desktop import mac_terminal
from app.desktop.clipboard import Clipboard, ClipboardError
from app.desktop.input import MAX_EVENT_UNITS, DesktopController, InputError, Stopped, normalize_combo, parse_keys, split_units
from app.desktop.stop import EscWatcher, stop_requested_since
from app.desktop.task import EditorTask, TaskState, TransitionError, outcome
from app.desktop.uia import UIAClient, UIAError

GUI_ACTIONS = ("move", "click", "double_click", "right_click", "scroll", "drag", "type", "key", "paste", "copy", "read",
               "run")
# the buttons that run code on a problem page or in an online editor, best first - never Submit
RUN_BUTTONS = ("compile & run", "compile and run", "run code", "run", "run tests", "run test", "test run", "execute")
_BUSY = re.compile(r"\b(?:compiling|running|executing|judging|pending|evaluating|processing|in queue|queued|waiting)\b"
                   r"|ready for evaluation", re.IGNORECASE)  # measured on GeeksforGeeks: "Request Queued", "Ready for evaluation"
_RUN_FAILED = re.compile(r"compilation error|compile error|\berror:|runtime error|segmentation fault|time limit exceeded"
                         r"|memory limit exceeded|wrong answer|exception|aborted|core dumped"
                         r"|took more time than expected", re.IGNORECASE)  # GeeksforGeeks' time limit, measured
# what kind of failure a result shows, first match wins
_RUN_KINDS = (("compile", re.compile(r"compilation error|compile error|\berror:", re.I), "compilation error"),
              ("time_limit", re.compile(r"time limit exceeded|took more time than expected", re.I), "time limit exceeded"),
              ("memory_limit", re.compile(r"memory limit exceeded", re.I), "memory limit exceeded"),
              ("runtime", re.compile(r"runtime error|segmentation fault|core dumped|aborted|exception", re.I),
               "runtime error"),
              ("wrong_answer", re.compile(r"wrong answer", re.I), "wrong answer"))
_TESTS_PASSED = re.compile(r"test\s*cases?\s*passed\s*:?\s*(\d+)\s*/\s*(\d+)", re.I)  # "Test Cases Passed: 10010 /10014"
# where a judge's result begins (a problem statement's own "Output:" does not count)
_RESULT_HEAD = re.compile(r"compilation (?:completed|error|results|successful)|compile error|your output|expected output"
                          r"|runtime error|wrong answer|time limit exceeded|memory limit exceeded|segmentation fault"
                          r"|\baccepted\b|test results?\b|\berror:|took more time than expected", re.IGNORECASE)
MAX_PASTE = 50_000  # characters per paste
MAX_CODE = 8_000  # characters typed as code in one call (about 4 minutes of typing)
MAX_COPY = 40_000  # characters of copied text returned
WINDOW_ACTIONS = ("list", "focus", "minimize", "maximize", "restore", "close")
CONTEXT_SECONDS = 1800
TRAY_GRACE = 6.0  # seconds a tray app gets to show its own window again before XENIO shows it
ACTION_DEADLINE = 330.0  # seconds one desktop action may run: below Hermes' 420 s per tool call (then Hermes gives up
                         # on the call while the thread typing it would go on)
BUSY_WAIT = 1.5  # seconds a desktop action waits for the one before it to end
# sites whose code editors refuse pasting / copying (GeeksforGeeks: confirmed by the user; measured 6 of 6 pastes);
# others are learned from evidence: the editor still showing what it held before the paste
PASTE_REFUSED = frozenset({"geeksforgeeks"})
COPY_REFUSED = frozenset({"geeksforgeeks"})
OWN_BUNDLES = ("local.xenio.",)  # XENIO's own windows (the orb, its quick chat, Mission Control): app "XENIO"
_WORDISH = re.compile(r"[\w=]")  # a piece of code typed at once must not end in these (see _type_code)


class DesktopError(Exception):
    def __init__(self, message: str, status: str = "FAILED"):
        super().__init__(message)
        self.status = status  # app.tools.base.STATUSES


def _platform_backends():
    """(windows module, control reader class) for this system: Win32 + UI Automation, or macOS Accessibility."""
    if sys.platform == "darwin":
        from app.desktop import mac_windows
        from app.desktop.ax import AXClient

        return mac_windows, AXClient
    from app.desktop import windows

    return windows, UIAClient


@dataclass
class _Recent:
    name: str
    at: float


def _norm_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\r", "\n")).strip()


class DesktopService:
    def __init__(self, catalog: apps_mod.AppCatalog | None = None, uia: UIAClient | None = None, controller=None,
                 windows=None, data_dir: Path | None = None, clock=time.time, screen_digest=None, sleep=time.sleep,
                 terminal=None, clipboard=None):
        self.catalog = catalog or apps_mod.AppCatalog(data_dir)
        self._terminal = terminal or mac_terminal.run_in_terminal
        if uia is None or windows is None:
            platform_windows, platform_uia = _platform_backends()
            uia, windows = uia or platform_uia(), windows or platform_windows
        self.uia = uia
        self.windows = windows
        self._controller = controller
        self.clipboard = clipboard or Clipboard()
        self._data_dir = data_dir
        self._clock = clock
        self._sleep = sleep
        self._digest = screen_digest or _screen_digest
        self.turn_started = 0.0  # set per user request by the plugin: stops before it do not count
        self._recent: collections.deque[_Recent] = collections.deque(maxlen=8)
        self.last_typed: tuple[str, str] | None = None  # (app, text) - for "send it?" prompts, cleared on a new turn
        self._code_line = 0  # the line code typing has reached (for an honest "interrupted at line N")
        self._lock = threading.Lock()
        self._input = threading.Lock()  # one input action at a time: two would interleave their keystrokes
        self._running: tuple[str, float] | None = None  # (action, monotonic start) of the input action holding it
        self._generation: dict[str, int] = {}  # per session: bumped by each new user request
        self._tasks: dict[str, EditorTask] = {}  # per session: the current request's coding task
        self._paste_refused, self._copy_refused = set(PASTE_REFUSED), set(COPY_REFUSED)
        self._copy_was_refused = False  # the last read-back's copy was refused by the page

    # ---- context ------------------------------------------------------------------------------------------------

    def new_turn(self, session: str = "", turn: str = "") -> None:
        """A new user request: a stop said before it no longer counts, it gets a new coding task, and an action still
        running from the session's previous request is cancelled at its next keystroke (Hermes abandons a tool call
        when its turn is stopped, but the thread typing it would go on - and then two would type)."""
        self.turn_started = self._clock()
        with self._lock:
            self._generation[session] = self._generation.get(session, 0) + 1
            self._tasks[session] = EditorTask(session=session, turn=turn, clock=self._clock)

    def task(self, session: str = "") -> EditorTask:
        with self._lock:
            return self._tasks.setdefault(session, EditorTask(session=session, clock=self._clock))

    def stopped(self) -> bool:
        return stop_requested_since(self.turn_started, self._data_dir) if self.turn_started else False

    def _check_unlocked(self) -> None:
        locked = getattr(self.windows, "session_locked", None)
        if locked is not None and locked():
            raise DesktopError(f"The {'Mac' if sys.platform == 'darwin' else 'PC'} is locked, so nothing was opened, "
                               "clicked or typed (input would go to the lock screen). Tell the user to unlock it and ask again.")

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

    def open_app(self, name: str, wait_seconds: float = 25.0, command: str = "") -> dict:
        if self.stopped():
            raise DesktopError("stopped by the user")
        if command.strip():
            return self.run_in_terminal(name, command, wait_seconds)
        res = self.resolve_app(name)
        if not res.matches or res.confidence < 0.6:
            raise DesktopError(f"No installed app called {name!r} was found on this "
                               f"{'Mac' if sys.platform == 'darwin' else 'PC'}, so nothing was opened. Tell the user it "
                               "is not installed, or ask for its exact name (xenio_list_apps searches).", "NOT_SUPPORTED")
        if res.ambiguous:
            raise DesktopError(f"Several installed apps match {name!r}: {', '.join(res.candidates())}. Ask the user "
                               "which one they mean.", "NEEDS_CLARIFICATION")
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
            raise DesktopError(f"{entry.name} is open, but the system did not let it come to the front. Ask the user to "
                               f"click it in the {'Dock' if sys.platform == 'darwin' else 'taskbar'}; nothing was typed or clicked.")
        if window is None:
            raise DesktopError(f"{entry.name} was started, but no window of it appeared within {elapsed} s, so it is "
                               "NOT verified as open. It may still be loading or may have opened in the tray.")
        focused = self.windows.focus(window.hwnd) if not window.foreground else True
        self.remember(entry.name)
        return {"app": entry.name, "verified": True, "how": how, "window": window.public(), "in_front": bool(focused),
                "launch_s": elapsed, "resolution": res.describe()}

    def run_in_terminal(self, name: str, command: str, wait_seconds: float = 10.0) -> dict:
        """"Open Terminal and run X": a new, visible window of the user's Terminal runs X (macOS Terminal only)."""
        res = self.resolve_app(name)
        entry = res.best if res.matches and res.confidence >= 0.6 and not res.ambiguous else None
        if sys.platform != "darwin" or entry is None or entry.app_id != mac_terminal.TERMINAL_BUNDLE:
            raise DesktopError(f"XENIO runs a command in a visible window only in the macOS Terminal app (name "
                               f"'Terminal'), not in {name!r}. Nothing was run.", "NOT_SUPPORTED")
        self._check_unlocked()
        try:
            out = self._terminal(command, max(2.0, min(float(wait_seconds or 10), 60.0)))
        except mac_terminal.TerminalError as exc:
            raise DesktopError(str(exc)) from None
        self.remember(entry.name)
        return out

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
                raise DesktopError(f"{target.title!r} could not be brought to the front (the system refused the focus "
                                   "change); NOT verified.")
            self.remember(target.app)
            return {"focused": target.public() | {"foreground": True}, "verified": True}
        if action == "close":
            outcome = self.windows.close(target.hwnd)
            return {"window": target.title[:120], "result": outcome, "verified": outcome == "closed",
                    **({"note": "the app is still open - it may be asking whether to save; nothing was forced"}
                       if outcome != "closed" else {})}
        if action == "maximize" and self._fullscreen(target):
            return {"window": target.title[:120], "result": "already full screen", "verified": True,
                    "note": "the window is in full screen, the largest it gets: left as it is (resizing it would end "
                            "full screen)"}
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

    @staticmethod
    def is_own(window) -> bool:
        """One of XENIO's own windows: the orb, its quick chat, Mission Control."""
        return str(getattr(window, "process", "") or "").lower() == "xenio" or \
            str(getattr(window, "bundle_id", "") or "").startswith(OWN_BUNDLES)

    @staticmethod
    def _coding(args: dict) -> bool:
        action = str(args.get("action") or "").lower()
        return action in ("read", "run") or (args.get("editor") is True and action in ("type", "paste", "copy"))

    def pick_window(self, args: dict, session: str = ""):
        """The window an action works in, changing nothing. Reading a page, running code and writing into an editor
        never work in XENIO's own windows: the orb's quick chat holds the keyboard while it is open (measured: a
        program meant for the GeeksforGeeks editor was pasted into the chat box and reported verified). They go to
        the window the task works in, else the app window right behind XENIO's. The policy probes this same window."""
        name = str(args.get("window") or "")
        if name:
            return self._window(name)
        fg = self.windows.active_app_window()
        if fg is not None and (not self._coding(args) or not self.is_own(fg)):
            return fg
        if fg is None and not self._coding(args):
            raise DesktopError("no active window")
        task = self._tasks.get(session)
        candidates = [w for w in self.windows.list_windows() if not w.is_shell and not w.minimized and not self.is_own(w)]
        chosen = next((w for w in candidates if task is not None and w.hwnd == task.window), None) or \
            (candidates[0] if candidates else None)
        if chosen is None:
            raise DesktopError("only XENIO's own window is open: there is no page or editor to work in", "NOT_SUPPORTED")
        return chosen

    def _fullscreen(self, window) -> bool | None:
        """Whether ``window`` is in full screen (None: cannot be told here)."""
        getter = getattr(self.windows, "fullscreen", None)
        if getter is None:
            return None
        try:
            return getter(window.hwnd)
        except Exception:  # noqa: BLE001 - an unknown state is reported as unknown, never as kept
            return None

    def _site(self, window) -> str:
        """The site a browser window shows, from its title's last part ("Two Sum | Practice | GeeksforGeeks")."""
        title = str(getattr(window, "title", "") or "")
        last = re.split(r"\s+[|\-–—]\s+", title)[-1] if title else ""
        return " ".join(last.lower().split())[:60]

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

    def probe(self, args: dict, session: str = "") -> dict:
        """Facts the policy needs: the window the action will work in (the same one ``gui`` picks, so a test's title
        is always seen), the element a click would hit, the focused field."""
        info: dict = {}
        try:
            locked = getattr(self.windows, "session_locked", None)
            if locked is not None and locked():
                return {"locked": True}
        except Exception:  # noqa: BLE001
            pass
        try:
            try:
                fg = self.pick_window(args, session)
            except DesktopError:
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
            if action in ("type", "key", "paste", "copy"):
                focused = self.uia.focused()
                if focused.get("found"):
                    info["focused"] = {k: focused.get(k) for k in ("name", "type", "is_password", "class")}
        except (UIAError, DesktopError) as exc:
            info["probe_error"] = str(exc)[:160]
        if self.last_typed and info.get("app") and self.last_typed[0] == info["app"]:
            info["typed"] = self.last_typed[1]
        return info

    # ---- reading a window's text ----------------------------------------------------------------------------------

    def read_window(self, window: str = "", session: str = "") -> dict:
        """xenio_gui action read: the window's exact text through accessibility (a web page: also the parts scrolled
        out of view), and whether it has a code editor - no clipboard, no screenshot, nothing pressed. Never XENIO's
        own window (see ``pick_window``); the window read becomes the coding task's window."""
        reader = getattr(self.uia, "read_text", None)
        if reader is None:
            raise DesktopError("reading a window's text works on macOS only here: use action copy with select_all=true, "
                               "or xenio_look_at_screen", "NOT_SUPPORTED")
        target = self._target_window(window) if window else self.pick_window({"action": "read"}, session)
        task = self.task(session)
        inspecting = task.can(TaskState.INSPECTING)
        if inspecting:
            task.to(TaskState.INSPECTING)
        started = time.monotonic()
        try:
            out = reader(target.hwnd)
        except UIAError as exc:
            if inspecting:
                task.end(TaskState.FAILED)
            raise DesktopError(f"the window's text could not be read ({exc}); use xenio_look_at_screen") from None
        except Exception:
            if inspecting:
                task.end(TaskState.FAILED)
            raise
        if inspecting:
            task.window = target.hwnd
            task.to(TaskState.SOLVING)  # the model works out the program next
        from app.integrations.base import UNTRUSTED_NOTE

        result = {"action": "read", "app": target.app, "window": target.public(), "read": True,
                  "text": out.get("text") or "", "chars": out.get("chars", 0), "truncated": bool(out.get("truncated"))}
        if not out.get("complete", True):
            result["incomplete"] = "the window is very large: only its first part was read"
        editors = out.get("editors") or []
        if editors:
            e = editors[0]
            site = self._site(target)
            refused = site in self._paste_refused
            result["editor"] = {"kind": e.get("kind"), "focused": e.get("focused"), "lines_in_view": e.get("lines"),
                                "code_now": str(e.get("text") or "")[:4000],
                                "how_to_write": "xenio_gui action type with code=true, editor=true, select_all=true "
                                                f"({site} does not accept pasting)" if refused else
                                                "xenio_gui action paste with editor=true and select_all=true (if the "
                                                "page refuses the paste, XENIO types it instead)"}
        result.update(untrusted_content=True, note=UNTRUSTED_NOTE, task=task.public(),
                      timings={"read_s": round(time.monotonic() - started, 2)})
        self.remember(target.app)
        return result

    # ---- acting -------------------------------------------------------------------------------------------------

    def _controller_for(self, watcher: EscWatcher, in_front=None, guard=None) -> DesktopController:
        """``guard``: True when the action must end (a newer request, the deadline) - checked before every key."""
        check = lambda: watcher.pressed or self.stopped() or (guard is not None and guard()) \
            or (in_front is not None and not in_front())  # noqa: E731
        if self._controller is not None:
            self._controller.stop_check = check
            return self._controller
        return DesktopController(stop_check=check)

    def gui(self, args: dict, session: str = "") -> dict:
        action = str(args.get("action") or "").lower()
        if action not in GUI_ACTIONS:
            raise DesktopError(f"action must be one of {', '.join(GUI_ACTIONS)}")
        if self.stopped():
            raise DesktopError("stopped by the user: no further desktop actions in this request")
        self._check_unlocked()
        if action == "read":
            return self.read_window(str(args.get("window") or ""), session)
        if not self._input.acquire(timeout=BUSY_WAIT):
            what, since = self._running or ("an action", time.monotonic())
            raise DesktopError(f"BUSY: another desktop action ({what}, started {int(time.monotonic() - since)} s ago) is "
                               "still running, so nothing was done (two would mix their keystrokes). Wait for its "
                               "result; do not repeat it")
        try:
            self._running = (action, time.monotonic())
            return self._gui(action, args, session)
        finally:
            self._running = None
            self._input.release()

    def _gui(self, action: str, args: dict, session: str) -> dict:
        if args.get("editor") is True and action in ("type", "paste"):
            # the editor's whole content, always: measured, a model that wrote a program in parts (select_all false)
            # left it scrambled - pieces went wherever the caret was
            args = {**args, "select_all": True}
        target = self.pick_window(args, session)
        if not target.foreground and not self.windows.focus(target.hwnd):  # input goes to the window in front
            raise DesktopError(f"{target.title!r} could not be brought to the front, so nothing was typed or clicked")
        result: dict = {"action": action, "app": target.app}
        before = self._digest() if action in ("click", "double_click", "right_click", "key", "drag") else None
        full_before = self._fullscreen(target) if action != "move" else None
        lost: dict = {}
        task = self.task(session)
        generation, deadline = self._generation.get(session, 0), time.monotonic() + ACTION_DEADLINE
        why: dict = {}

        def ended() -> bool:
            """A newer request in this session, or the deadline: this action ends at its next key."""
            if self._generation.get(session, 0) != generation:
                why["reason"] = "superseded"
            elif time.monotonic() > deadline:
                why["reason"] = "deadline"
            return bool(why)

        def still_in_front() -> bool:
            """Checked before every keystroke: another app taking the front (measured: Spotify after it loads) must
            never receive the rest of the text."""
            fg = self.windows.foreground_window()
            if fg is not None and (fg.hwnd == target.hwnd or getattr(fg, "pid", None) == getattr(target, "pid", -1)):
                return True
            lost["by"] = fg.title[:80] if fg is not None else "another window"
            return False

        with EscWatcher() as watcher:
            c = self._controller_for(watcher, still_in_front if action in ("type", "paste") else None, ended)
            visible = lambda: self._visible_editor_text(target)  # noqa: E731
            writes_code = args.get("editor") is True and action in ("type", "paste") or \
                (action == "type" and args.get("code") is True) or (action == "paste" and args.get("run") is True)
            try:
                if args.get("editor") is True and action in ("type", "paste", "copy"):
                    result["editor"] = self._focus_editor(c, target)
                if writes_code:
                    result.update(self._write_code(c, watcher, target, args, task, still_in_front, visible, ended))
                    self.last_typed = (target.app.lower(), str(args.get("text") or ""))
                elif action in ("click", "double_click", "right_click", "move", "drag"):
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
                elif action == "paste":
                    result.update(self._paste(c, str(args.get("text") or ""), args.get("select_all") is True, still_in_front,
                                              visible, self._site(target)))
                    self.last_typed = (target.app.lower(), str(args.get("text") or ""))
                    if args.get("run") is True and result.get("verified") is not False:
                        result["run"] = self._run_page(c, target, str(args.get("text") or ""))
                elif action == "copy":
                    result.update(self._copy(c, args.get("select_all") is True))
                elif action == "run":
                    run = self._run_task(c, target, task, "")
                    result.update(run, outcome=outcome(None, None, run), task=task.public())
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
                if why:
                    task.end(TaskState.CANCELLED if why["reason"] == "superseded" else TaskState.TIMED_OUT)
                    where = f" at line {self._code_line}" if writes_code and self._code_line else ""
                    if why["reason"] == "superseded":
                        raise DesktopError(f"CANCELLED{where}: a newer request started, so this {action} stopped (nothing "
                                           "more was typed or clicked).") from None
                    raise DesktopError(f"TIMED_OUT{where}: this {action} took longer than {int(ACTION_DEADLINE)} s, so it "
                                       "stopped. Nothing more was typed; the editor may hold part of it - check before "
                                       "saying anything about it.") from None
                if writes_code and task.in_flight:
                    task.end(TaskState.CANCELLED if not lost else TaskState.FAILED)
                if lost and writes_code:
                    raise DesktopError(
                        f"INTERRUPTED: {lost['by']!r} came to the front while typing code into {target.title[:80]!r}, so "
                        f"typing stopped at line {self._code_line} (nothing was typed anywhere else). Bring "
                        f"{target.app} back to the front and run the same type call again with select_all=true: it "
                        "rewrites the whole editor") from None
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
                if writes_code:
                    task.end(TaskState.FAILED)
                raise DesktopError(str(exc)) from None
            except ClipboardError as exc:
                if writes_code:
                    task.end(TaskState.FAILED)
                raise DesktopError(f"{exc}. Nothing was pasted or copied.") from None
            except Exception:  # DesktopError, or anything unforeseen: the task never stays "in flight"
                if writes_code or action == "run":
                    task.end(TaskState.FAILED)
                raise
        if watcher.pressed:
            raise DesktopError("STOPPED: Esc was pressed during the action.")
        self._sleep(0.35)
        after_fg = self.windows.foreground_window()
        result["in_front_now"] = after_fg.public() if after_fg else None
        if full_before is not None:
            result["fullscreen"] = self._keep_fullscreen(target, full_before, action, args)
        if before is not None:
            after = self._digest()
            result["screen_changed"] = (before != after) if before and after else None  # None = could not check
            if result["screen_changed"] is False:
                result["note"] = ("nothing visibly changed on screen after the action: it may have had no effect. Check "
                                  "with xenio_look_at_screen before saying it worked.")
        self.remember(target.app)
        return result

    # ---- writing and reading text through the clipboard, and typing code ------------------------------------------

    def _wait_for_copy(self, before: int, timeout: float = 1.5) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._sleep(0.08)
            if self.clipboard.change_count() != before:
                return True
        return False

    def _read_back(self, c: DesktopController) -> str | None:
        """Everything in the focused field, exactly (select all + copy; the user's clipboard is put back and the
        selection collapsed). None when the field or page does not let text be copied."""
        cb = self.clipboard
        self._copy_was_refused = False
        saved = cb.save()
        if saved.items is None:
            return None  # the user's clipboard could not be put back afterwards: not read
        c.hotkey("cmd+a")
        self._sleep(0.08)
        try:
            c.hotkey("cmd+c")
            if not self._wait_for_copy(saved.change):
                self._copy_was_refused = True
                return None  # the page refused the copy
            try:
                return cb.get_text()
            finally:
                cb.restore(saved, cb.change_count())
        finally:
            # never leave the whole editor selected - the user's next key would replace everything (measured: GFG
            # refused the copy and the selection stayed)
            c.hotkey("right")

    @staticmethod
    def _compare(expected: str, actual: str | None, whole: bool = True) -> dict:
        """``whole``: the field should hold exactly ``expected``; otherwise it should contain it."""
        if actual is None:
            return {"verified": None, "verification": "the editor's text could not be read back (this page does not "
                                                      "allow copying): confirm with xenio_look_at_screen before saying "
                                                      "it is written"}
        squash = lambda t: re.sub(r"\s+", "", t)  # noqa: E731 - editors re-indent and trim; the code must match
        if squash(expected) == squash(actual) or (not whole and squash(expected) in squash(actual)):
            return {"verified": True, "verification": "read back from the editor: the text is there (spacing aside)"}
        if not whole:
            return {"verified": False, "verification": "read back from the editor: the text is NOT there as written"}
        exp, got = expected.splitlines(), actual.splitlines()
        for i, (a, b) in enumerate(zip(exp, got)):
            if a.strip() != b.strip():
                return {"verified": False, "verification": f"read back from the editor: line {i + 1} came out as "
                                                           f"«{b.strip()[:120]}» instead of «{a.strip()[:120]}»",
                        "corruption": DesktopService._corruption(expected, b)}
        return {"verified": False, "verification": f"read back from the editor: {len(got)} lines instead of {len(exp)}",
                "corruption": {"kind": "missing lines" if len(got) < len(exp) else "extra lines",
                               "lines": len(got), "expected_lines": len(exp)}}

    @staticmethod
    def _corruption(expected: str, wrong: str) -> dict:
        """What a wrong line in the editor is: which line of the program it was meant to be, and how it differs."""
        lines = [line.strip() for line in expected.splitlines()]
        got = wrong.strip()
        near = difflib.get_close_matches(got, [line for line in lines if line], n=1, cutoff=0.3)
        want = near[0] if near else ""
        kind = ("missing characters" if want and len(got) < len(want) else "extra characters" if want and
                len(got) > len(want) else "changed characters" if want else "a line that is not in the program")
        out = {"kind": kind, "got": got[:160]}
        if want:
            out.update(line=lines.index(want) + 1, expected=want[:160])
        return out

    def _shown(self, visible) -> str | None:
        try:
            shown = visible()
        except Exception:  # noqa: BLE001 - verification is best effort; the honest "could not check" remains
            return None
        return shown if shown and shown.strip() else None

    def _editor_pages(self, c: DesktopController, visible, expected: str) -> list[str] | None:
        """What the editor shows, page by page from the top (editors draw only the lines in view; measured: a wrong
        line 9 of 57 passed a check of the lines in view). The caret ends at the end of the text, where writing left
        it."""
        pages: list[str] = []
        squash = lambda t: re.sub(r"\s+", "", t)  # noqa: E731
        c.hotkey("cmd+up")
        try:
            for _ in range(expected.count("\n") // 8 + 3):
                self._sleep(0.15)
                shown = self._shown(visible)
                if shown is None:
                    return pages or None
                if pages and shown == pages[-1]:
                    break  # the editor did not scroll any further
                pages.append(shown)
                if squash(expected).endswith(squash(shown)):
                    break
                c.hotkey("pagedown")
        finally:
            c.hotkey("cmd+down")
        return pages

    # ---- running the code on a problem page --------------------------------------------------------------------------

    def _run_button(self, target) -> dict | None:
        try:
            found = self.uia.find(target.hwnd, "run", limit=25)
        except UIAError:
            return None
        named = [e for e in found.get("elements", []) if e.get("type") == "button" and e.get("enabled", True)
                 and " ".join(str(e.get("name") or "").lower().split()) in RUN_BUTTONS]
        named.sort(key=lambda e: RUN_BUTTONS.index(" ".join(str(e["name"]).lower().split())))
        return named[0] if named else None

    @staticmethod
    def _section(lines: list[str], header: str) -> str | None:
        """The lines under a heading like "Your Output:" (up to the next heading or the gap after the block)."""
        for i, line in enumerate(lines):
            if re.fullmatch(rf"\s*(?:{header})\s*:?\s*", line, re.IGNORECASE):
                body: list[str] = []
                for nxt in lines[i + 1:]:
                    if re.match(r"\s*[A-Z][\w ]{2,30}:\s*$", nxt) or (body and not nxt.strip()):
                        break
                    body.append(nxt.strip())
                return "\n".join(body).strip()
        return None

    @staticmethod
    def _result_area(text: str, program: str) -> list[str]:
        """The page's result area: from its first result heading on, without the program's own lines (the editor is
        part of the page). Found by its headings, not by what changed: a run with the same result changes nothing."""
        squash = lambda t: re.sub(r"\s+", "", t)  # noqa: E731
        code = squash(program)
        own = {squash(line) for line in program.splitlines() if squash(line)}
        lines = text.splitlines()
        start = next((k for k, line in enumerate(lines) if _RESULT_HEAD.search(line)
                      and not (code and len(squash(line)) >= 8 and squash(line) in code)), None)
        if start is None:
            return []
        area = []
        for line in lines[start:start + 40]:
            if squash(line) in own:
                break  # the editor's lines: the result area ended
            area.append(line)
        return area

    def _run_page(self, c: DesktopController, target, program: str = "") -> dict:
        """Click the page's Run / Compile & Run button (never Submit) and return what the run showed: the compiler's
        errors, or the program's output next to the expected one - so the model can fix the program itself."""
        reader = getattr(self.uia, "read_text", None)
        if reader is None:
            raise DesktopError("running code on a page works on macOS only here: click its Run button and look at the "
                               "result", "NOT_SUPPORTED")
        button = self._run_button(target)
        if button is None:
            return {"ran": False, "note": "no Run or Compile & Run button was found in this window: the program was not "
                                          "run. Do not say it works; ask the user or run it another way"}

        def page() -> tuple[str, str]:
            out = reader(target.hwnd)
            return out.get("text") or "", ((out.get("editors") or [{}])[0].get("text") or "")  # the code editor only

        before, shown = page()
        program = program or shown
        clicked_by = self._press_run(c, target, button)
        text, last, changed = before, None, False
        for i in range(30):  # about 30 s: online judges take a few seconds
            self._sleep(1.0)
            text = page()[0]
            changed = changed or text != before
            busy = _BUSY.search("\n".join(self._result_area(text, program)))
            if (changed and text == last and not busy) or (not changed and i >= 7):
                break  # settled - or nothing changed at all (the same result as the last run)
            last = text
        area = self._result_area(text, program)
        output = "\n".join(area).strip()[:3000]
        yours = self._section(area, "your output|output|stdout")
        expected = self._section(area, "expected output|expected")
        facts: dict = {}
        if not area:
            status = "no_output"
        elif _RUN_FAILED.search(output):
            status = "error"
            kind, label = next(((k, lab) for k, rx, lab in _RUN_KINDS if rx.search(output)), ("error", "error"))
            facts.update(kind=kind, kind_text=label)
            if kind == "compile":
                first = next((line.strip() for line in area if re.search(r"error", line, re.I)
                              and not re.fullmatch(r"\s*compil\w* error\s*:?\s*", line, re.I)), "")
                facts["first_error"] = first[:200]
        elif yours is not None and expected is not None:
            squash = lambda t: re.sub(r"\s+", "", t)  # noqa: E731
            status = "matches_expected" if squash(yours) == squash(expected) else "wrong_output"
        else:
            status = "ran"
        counts = _TESTS_PASSED.search(output)
        if counts:
            facts["tests"] = {"passed": int(counts.group(1)), "total": int(counts.group(2))}
        from app.integrations.base import UNTRUSTED_NOTE

        nexts = {
            "error": "Not working yet: it did not compile or run correctly. Read the error above, fix the program and "
                     "write the whole program again with run=true.",
            "wrong_output": "Not working yet: its output differs from the expected output. Find the bug, fix the program "
                            "and write it again with run=true.",
            "matches_expected": "It compiled and its output matches the expected output for this input.",
            "ran": "It ran: compare its output with the problem's expected output before telling the user it works.",
            "no_output": "No result appeared on the page: look (xenio_gui read) before saying anything about it.",
        }
        return {"ran": True, "button": button.get("name"), "clicked_by": clicked_by, "status": status, **facts,
                "output": output or "(no result appeared on the page)",
                **({"same_as_before": True} if not changed else {}), "next": nexts[status],
                "untrusted_content": True, "note": UNTRUSTED_NOTE}

    def _press_run(self, c: DesktopController, target, button: dict) -> str:
        """Click the Run button where it is - if that point really is the button. Otherwise press it through
        accessibility (measured: out of full screen the Dock covered Compile & Run at y=933, and the click went to the
        Dock while the run was reported). Raises when it can be neither clicked nor pressed."""
        x, y = int(button["center"][0]), int(button["center"][1])
        try:
            hit = self.uia.at(x, y)
        except UIAError:
            hit = {"found": False}
        want = " ".join(str(button.get("name") or "").lower().split())
        names = [hit.get("name"), *(hit.get("parents") or [])] if hit.get("found") else []
        ours = hit.get("pid") in (None, getattr(target, "pid", None))
        if not hit.get("found") or (ours and want in {" ".join(str(n or "").lower().split()) for n in names}):
            c.click(x, y)  # the button is there (or nothing can tell: as before)
            return "mouse"
        presser = getattr(self.uia, "press", None)
        if presser is not None:
            try:
                if presser(target.hwnd, str(button.get("name") or "")).get("pressed"):
                    return "accessibility"
            except UIAError:
                pass
        raise DesktopError(f"the {button.get('name')!r} button is covered (at its place on screen is "
                           f"{str(hit.get('name') or 'something else')[:60]!r}) and could not be pressed through "
                           "accessibility: nothing was run")

    def _run_task(self, c: DesktopController, target, task: EditorTask, program: str = "") -> dict:
        """The task's RUNNING step: the run's own result decides RESULT_CONFIRMED / RESULT_FAILED / INCONCLUSIVE."""
        try:
            task.to(TaskState.RUNNING)
        except TransitionError as exc:
            raise DesktopError(f"NOT RUN: {exc}. Wait for that step's result first.") from None
        started = time.monotonic()
        try:
            run = self._run_page(c, target, program)
        except Exception:
            task.end(TaskState.FAILED)
            raise
        status, tests = run.get("status"), run.get("tests")
        failed = status in ("error", "wrong_output") or bool(tests and tests["passed"] < tests["total"])
        confirmed = not failed and (status == "matches_expected" or bool(tests and status == "ran"))
        task.to(TaskState.RESULT_CONFIRMED if confirmed else TaskState.RESULT_FAILED if failed else TaskState.INCONCLUSIVE)
        task.run = {k: run.get(k) for k in ("ran", "status", "kind", "tests")}
        run["seconds"] = round(time.monotonic() - started, 1)
        return run

    def _check_written(self, expected: str, actual: str | None, whole: bool, visible=None, c=None) -> dict:
        """The editor's text read back through the clipboard; where the page refuses copying, the lines the editor
        shows, read through accessibility - all of them, page by page, when the editor's whole text was replaced."""
        if actual is not None or visible is None:
            return self._compare(expected, actual, whole)
        squash = lambda t: re.sub(r"\s+", "", t)  # noqa: E731
        want = squash(expected)
        if not whole or c is None:
            shown = self._shown(visible)
            if shown is None:
                return self._compare(expected, None, whole)
            if want in squash(shown) or squash(shown) in want:
                return {"verified": True, "verification": "the editor shows the new text (the lines in view, read "
                                                          "through accessibility: this page does not allow copying)"}
            return {"verified": False, "verification": "the editor does not show the text as written; the lines in "
                                                       f"view are: «{shown.strip()[:400]}»"}
        pages = self._editor_pages(c, visible, expected)
        if not pages:
            return self._compare(expected, None, whole)
        for page in pages:
            if squash(page) not in want:
                wrong = next((line for line in page.splitlines() if squash(line) and squash(line) not in want), "")
                if not wrong:  # each line is part of the program (a cut-off line is a prefix): align the lines
                    exp, got = [x.strip() for x in expected.splitlines()], [x.strip() for x in page.splitlines()]
                    wrong = next((got[j1] for tag, _i1, _i2, j1, j2 in
                                  difflib.SequenceMatcher(None, exp, got, autojunk=False).get_opcodes()
                                  if tag in ("replace", "insert") and j1 < j2), "")
                return {"verified": False, "verification": "read through accessibility, the editor does not hold the "
                        "text as written" + (f": it shows the line «{wrong.strip()[:160]}»" if wrong else "")
                        + f"; the lines in view: «{page.strip()[:300]}»",
                        **({"corruption": self._corruption(expected, wrong)} if wrong else {})}
        if not want.startswith(squash(pages[0])) or not want.endswith(squash(pages[-1])):
            return {"verified": None, "verification": "only part of the editor could be checked through accessibility "
                                                      "(this page does not allow copying): confirm with "
                                                      "xenio_look_at_screen or run it before saying it is written"}
        return {"verified": True, "verification": f"read through accessibility, page by page ({len(pages)} "
                                                  f"page{'s' if len(pages) > 1 else ''}): the editor holds the text as "
                                                  "written (this page does not allow copying)"}

    def _visible_editor_text(self, target) -> str | None:
        finder = getattr(self.uia, "editor", None)
        if finder is None:
            return None
        try:
            found = finder(target.hwnd, focus=False)
        except UIAError:
            return None
        return found.get("text") if found.get("found") else None

    def _focus_editor(self, c: DesktopController, target) -> dict:
        """editor=true: put the keyboard focus into the window's code editor - through accessibility, else by clicking
        into its text - so the model needs no coordinates."""
        finder = getattr(self.uia, "editor", None)
        if finder is None:
            raise DesktopError("finding the code editor by itself works on macOS only: click inside the editor first, "
                               "then call again without editor=true", "NOT_SUPPORTED")
        try:
            found = finder(target.hwnd, focus=True)
        except UIAError as exc:
            raise DesktopError(f"the code editor could not be found ({exc}): click inside it first, then call again "
                               "without editor=true") from None
        if not found.get("found"):
            raise DesktopError(f"no code editor was found in {target.title[:80]!r}: click inside the editor first "
                               "(xenio_find_ui shows where things are), then call again without editor=true")
        how = "accessibility"
        if not found.get("focused"):
            x, y = found.get("click") or (None, None)
            if not isinstance(x, int) or not isinstance(y, int):
                raise DesktopError("the code editor could not be focused: click inside it first, then call again "
                                   "without editor=true")
            c.click(x, y)
            self._sleep(0.25)
            how = "a click into it"
        return {"kind": found.get("kind"), "focused_by": how}

    def _paste(self, c: DesktopController, text: str, select_all: bool, in_front, visible=None, site: str = "",
               verify: bool = True) -> dict:
        if not text:
            raise DesktopError("nothing to paste")
        if len(text) > MAX_PASTE:
            raise DesktopError(f"at most {MAX_PASTE} characters per paste")
        text = text.replace("\r\n", "\n")
        cb = self.clipboard
        saved = cb.save()
        if saved.items is None:  # the user's clipboard could not be kept, so it is not touched
            raise DesktopError("your clipboard holds something very large that XENIO could not keep and put back, so it "
                               "did not paste (your clipboard is untouched). Type it instead: action type with code=true")
        expect = cb.set_text(text)
        try:
            if not in_front():
                raise DesktopError("another window came to the front, so nothing was pasted")
            if select_all:
                c.hotkey("cmd+a")
                self._sleep(0.08)
            c.hotkey("cmd+v")
            self._sleep(0.5)  # a web page reads the pasted text a moment after the key
        finally:
            restored = cb.restore(saved, expect)
        out = {"pasted_chars": len(text), "replaced_everything": select_all,
               "clipboard": "put back as it was" if restored else "left as it was: something else was copied meanwhile"}
        if not verify:
            return out
        out.update(self._verify_write(c, text, select_all, visible, site))
        if out.get("verified") is False:
            out["hint"] = ("the page may block pasting: type it instead with action type, code=true (and select_all=true "
                           "to replace the editor's content)")
        return out

    def _verify_write(self, c: DesktopController, text: str, whole: bool, visible, site: str = "") -> dict:
        """The VERIFYING step: the editor read back (clipboard), or where the page refuses copying, its lines through
        accessibility. A site known to refuse copying is not asked again (measured on GeeksforGeeks: 1.5 s of waiting
        for a copy that never comes, every time)."""
        skip = bool(site) and site in self._copy_refused and visible is not None \
            and getattr(self.uia, "editor", None) is not None  # only where the editor's lines can be read instead
        actual = None if skip else self._read_back(c)
        if not skip and actual is None and site and getattr(self, "_copy_was_refused", False):
            self._copy_refused.add(site)
        return self._check_written(text, actual, whole, visible, c)

    def _editor_differs(self, text: str, visible) -> bool:
        """Evidence that the editor no longer holds ``text``: the lines it shows are not part of it. Nothing readable
        is no evidence (then a write that was made stands)."""
        shown = self._shown(visible)
        squash = lambda t: re.sub(r"\s+", "", t)  # noqa: E731
        return shown is not None and squash(shown) not in squash(text)

    def _write_code(self, c: DesktopController, watcher: EscWatcher, target, args: dict, task: EditorTask, in_front,
                    visible, ended) -> dict:
        """A program into an editor: the task's WRITING -> VERIFYING (-> RUNNING) steps.

        * the same program is not written again while the editor holds it (it was written earlier in this request):
          a later step that could not check something is no reason to write it twice;
        * a page that refuses pasting gets the program typed in this same call (GeeksforGeeks always: the user
          confirmed it; elsewhere when the editor still shows what it held before the paste);
        * a write that came out wrong is written once more only on evidence - the editor shows other text - and what
          went wrong is named; never on a mere "could not be checked";
        * only the run's own result says whether it compiled and passed."""
        text, how, whole = str(args.get("text") or ""), str(args.get("action") or "").lower(), args.get("select_all") is True
        site, started, timings = self._site(target), time.monotonic(), {}
        can_type = bool(getattr(c.backend, "inserts_strings", False))  # macOS: typing code is fast and needs no Esc
        previous = task.last_write(text, target.hwnd) if whole else None  # the editor's whole program, not an insert
        if previous is not None and previous.verified is not False and not self._editor_differs(text, visible):
            out = {"already_written": True, "verified": previous.verified, "replaced_everything": whole,
                   "verification": f"this exact program was already {previous.how} earlier in this request and the "
                                   "editor still shows it, so it was not written again"}
            entered, done = True, previous.how
        else:
            try:
                task.admit_write(text, target.hwnd, whole)
                task.to(TaskState.WRITING)
            except TransitionError as exc:
                raise DesktopError(f"NOT WRITTEN: {exc}, so it was not written again. Tell the user what the earlier "
                                   "attempt's result said instead") from None
            shown_before = self._shown(visible) if how == "paste" else None
            if how == "paste" and site in self._paste_refused and can_type:
                out = self._type_code(c, watcher, text, whole, visible, verify=False)
                out["typed_instead"] = f"{site} does not accept pasting, so XENIO typed the program instead"
                done = "typed"
            elif how == "paste":
                out, done = self._paste(c, text, whole, in_front, visible, site, verify=False), "pasted"
            else:
                out, done = self._type_code(c, watcher, text, whole, visible, verify=False), "typed"
            timings["write_s"] = round(time.monotonic() - started, 1)
            task.to(TaskState.VERIFYING)
            check = self._verify_write(c, text, whole, visible, site)
            refused = done == "pasted" and check.get("verified") is False and shown_before is not None \
                and self._shown(visible) == shown_before  # the editor holds exactly what it held before the paste
            if refused and can_type:
                self._paste_refused.add(site)
                task.to(TaskState.WRITING)
                out.update(self._type_code(c, watcher, text, whole, visible, verify=False))
                out["typed_instead"] = ("the page refused the paste (the editor still showed what it held before), so "
                                        "XENIO typed the program instead")
                done, refused = "typed", False
                task.to(TaskState.VERIFYING)
                check = self._verify_write(c, text, whole, visible, site)
            elif check.get("verified") is False and done == "typed" and whole and not ended():
                # evidence: the editor holds other text (a stray suggestion or key) - once more, by itself
                out["retyped"] = f"typed a second time because the first time {check.get('verification')}"
                if check.get("corruption"):
                    out["corruption"] = check["corruption"]
                task.to(TaskState.WRITING)
                out.update(self._type_code(c, watcher, text, whole, visible, verify=False))
                task.to(TaskState.VERIFYING)
                check = self._verify_write(c, text, whole, visible, site)
            out.update(check)
            if refused:
                out["hint"] = ("the page refused the paste: type it instead with action type, code=true (and "
                               "select_all=true to replace the editor's content)")
            entered = not refused
            verified = check.get("verified")
            task.record_write(text, target.hwnd, done, verified, whole)
            task.to(TaskState.VERIFIED if verified is True else TaskState.INCONCLUSIVE if verified is None
                    else TaskState.FAILED)
            timings["verify_s"] = round(time.monotonic() - started - timings["write_s"], 1)
        run = None
        if args.get("run") is True and entered and (done == "typed" or out.get("verified") is not False):
            # typed: even when unverified - the compiler shows what is wrong
            run_started = time.monotonic()
            out["run"] = run = self._run_task(c, target, task, text)
            timings["run_s"] = round(time.monotonic() - run_started, 1)
        out["outcome"] = outcome(entered, out.get("verified"), run)
        out["task"] = task.public()
        out["timings"] = {**timings, "total_s": round(time.monotonic() - started, 1)}
        return out

    def _keep_fullscreen(self, target, before: bool, action: str, args: dict) -> dict:
        """The window's full screen after the action, measured. When XENIO's own keys ended it (typing, pasting,
        reading back, running, a key the model chose) it is put back - the user did not ask to leave it. A click, or
        the full-screen shortcut itself, may be what the user wanted: then the change is reported, not undone."""
        after = self._fullscreen(target)
        out: dict = {"before": bool(before), "after": None if after is None else bool(after),
                     "kept": after is not None and bool(after) == bool(before)}
        if before and not after and after is not None:
            deliberate = action in ("click", "double_click", "right_click", "drag") or (
                action == "key" and describe_keys(str(args.get("keys") or "")) == "ctrl+cmd+f")
            if before == "page":  # a page's own full screen (its editor's full-screen button): only the page can
                out["note"] = ("this action ended the page's own full screen (Esc always does); XENIO cannot put a "
                               "page's full screen back - it is NOT in full screen now, tell the user")
                return out
            if not deliberate:
                try:
                    restored = bool(self.windows.fullscreen(target.hwnd, True))
                except Exception:  # noqa: BLE001
                    restored = False
                out.update(after=restored, restored=restored)
            out["note"] = ("this action ended the window's full screen; " + (
                "XENIO put it back" if out.get("restored") else "it is NOT in full screen now - tell the user"
                if not deliberate else "it was a click or the full-screen key, so it was left like that"))
        return out

    def _copy(self, c: DesktopController, select_all: bool) -> dict:
        cb = self.clipboard
        saved = cb.save()
        if saved.items is None:
            raise DesktopError("your clipboard holds something very large that XENIO could not keep and put back, so it "
                               "copied nothing (your clipboard is untouched); use xenio_look_at_screen instead")
        if select_all:
            c.hotkey("cmd+a")
            self._sleep(0.08)
        c.hotkey("cmd+c")
        if not self._wait_for_copy(saved.change):
            return {"copied": False, "text": "", "note": "nothing was copied: select the text first (or use "
                    "select_all), or this page does not allow copying - then use xenio_look_at_screen"}
        try:
            text = cb.get_text()
        finally:
            cb.restore(saved, cb.change_count())
        from app.integrations.base import UNTRUSTED_NOTE

        return {"copied": True, "chars": len(text), "text": text[:MAX_COPY], "truncated": len(text) > MAX_COPY,
                "untrusted_content": True, "note": UNTRUSTED_NOTE}

    def _type_code(self, c: DesktopController, watcher: EscWatcher, text: str, select_all: bool, visible=None,
                   verify: bool = True) -> dict:
        """Code typed into an editor that auto-indents, auto-closes brackets and pops up completions, so that exactly
        ``text`` ends up there: before each line, whatever the editor added after the caret (closing brackets pushed
        down by Enter) and its automatic indentation are removed, then the line is typed with its own indentation.
        The document is written top to bottom, so anything after the caret was put there by the editor.

        macOS (``inserts_strings``): each line goes in as a few insertions of up to 20 characters. An editor takes an
        insertion as one piece, so it closes no bracket and no completion list can take a character inside it
        (measured in Ace). A piece never ends inside a word or after "=": there the editor would open its completion
        list (and Enter or a space would take a suggestion: measured "return x" + Enter became "return xor") or macOS
        its unit conversion. A line that ends in a word gets a space in its last piece, then Backspace (no list opens
        on Backspace, measured). No Esc is ever pressed: an Esc the page does not use ends Safari's full screen
        (measured), and the keys typed during that animation are lost (measured: "#include <bits/stdc++.h>" came out
        "#includedc++.h>" and the program was typed twice). About five times faster than key by key.

        Elsewhere (Windows) it is typed key by key, and a completion list a word opened is closed with Esc before
        punctuation or Enter so it cannot take them."""
        if not text.strip():
            raise DesktopError("nothing to type")
        if len(text) > MAX_CODE:
            raise DesktopError(f"at most {MAX_CODE} characters of code per call: type the rest in a second call")
        lines = [line.rstrip() for line in text.replace("\r\n", "\n").expandtabs(4).split("\n")]
        while lines and not lines[-1]:
            lines.pop()
        self._code_line = 0
        pause = lambda: self._sleep(0.04)  # noqa: E731 - let the editor finish each step

        def close_completions():
            with watcher.suspend():  # XENIO's own Esc must not count as the user's stop
                c.hotkey("esc")
            pause()

        def clear_after_caret():
            c.hotkey("shift+cmd+down")
            pause()
            c.hotkey("forwarddelete")
            pause()

        if select_all:
            c.hotkey("cmd+a")
            pause()
            c.hotkey("forwarddelete")
            pause()
        strings = bool(getattr(c.backend, "inserts_strings", False))
        for i, line in enumerate(lines):
            self._code_line = i + 1
            clear_after_caret()
            c.hotkey("shift+cmd+left")  # the editor's own indentation on this new line
            pause()
            c.hotkey("forwarddelete")
            pause()
            if strings:
                ends_word = bool(line) and bool(_WORDISH.fullmatch(line[-1]))
                for piece in code_pieces(line + (" " if ends_word else "")):
                    c.insert_text(piece)
                if ends_word and not line.endswith("="):  # after "=" the space stays: Backspace could bring the offer back
                    c.hotkey("backspace")
                    pause()
                if i + 1 < len(lines):
                    c.hotkey("enter")
                    pause()
                continue
            # a word is typed, then any completion list it opened is closed before the next character: editors
            # accept a suggestion on punctuation (measured: Ace turned "j)" into "just)"), on Enter, and on GeeksforGeeks
            # even on a space (measured: "pos == len" became "possible == len"). And after "=" macOS offers a unit
            # conversion that the next key accepts (measured in Safari: "double m2 = lo" became "double m2 =1.196 yd²
            # lo" - m2 read as square metres): "=" is typed alone and Esc dismisses that before anything else.
            for piece in re.findall(r"\w+|=|[^\w=]+", line):
                c.type_text(piece)
                if (re.match(r"\w", piece) and not piece.isdigit()) or piece == "=":
                    close_completions()
            if i + 1 < len(lines):
                c.hotkey("enter")
                pause()
        clear_after_caret()
        out = {"typed_lines": len(lines), "typed_chars": sum(len(line) for line in lines), "replaced_everything": select_all,
               "typed_by": "line pieces" if strings else "keys"}
        if verify:
            out.update(self._verify_write(c, "\n".join(lines), select_all, visible))
        return out

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


def code_pieces(line: str, limit: int = MAX_EVENT_UNITS) -> list[str]:
    """One line of code in pieces typed at once (see ``DesktopService._type_code``): at most ``limit`` UTF-16 units,
    each ending in a character that is neither part of a word nor "=" - except a single word longer than ``limit``."""
    pieces, rest = [], line
    while rest:
        head = split_units(rest, limit)[0]
        if head == rest:
            pieces.append(rest)
            break
        cut = max((i for i, ch in enumerate(head) if not _WORDISH.fullmatch(ch)), default=-1)
        piece = head[:cut + 1] if cut >= 0 else head
        pieces.append(piece)
        rest = rest[len(piece):]
    return pieces


def describe_keys(keys: str) -> str:
    try:
        return normalize_combo(keys)
    except InputError:
        return keys


__all__ = ["DesktopService", "DesktopError", "GUI_ACTIONS", "WINDOW_ACTIONS", "parse_keys", "describe_keys"]
