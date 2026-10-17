"""Windows UI Automation (V5.5): what is on screen, by name. The same accessibility data screen readers use.

Used to (1) find a button or text box by its name and get its screen position ("click Save" without guessing
pixels), (2) learn the real name of the element under a click point, so the safety policy judges a click by what
it actually hits ("Send", "Place order") and not by the model's description, and (3) check the focused element:
is it a password field (XENIO never types there), what text does it hold now (to verify typing).

UI Automation is a COM API driven here through raw ctypes vtables (no pywin32/comtypes in Hermes' Python). A
mistake in raw COM can crash the process, so this module runs as its own short-lived process:
``python -m app.desktop.uia <command> ...`` prints one JSON object; ``UIAClient`` starts it with a timeout. A crash
or hang costs one failed lookup, never the Hermes process.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes as w

from app.desktop.common import UIAError
from app.desktop.common import score_name as _score

# ---- COM plumbing -------------------------------------------------------------------------------------------

_HRESULT = ctypes.c_long
_P = ctypes.c_void_p


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort), ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, text: str) -> "_GUID":
        import uuid

        u = uuid.UUID(text)
        g = cls()
        g.Data1, g.Data2, g.Data3 = u.fields[0], u.fields[1], u.fields[2]
        g.Data4[:] = list(u.bytes[8:])
        return g


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


CLSID_CUIAutomation = "{ff48dba4-60ef-4201-aa87-54103eef594e}"
IID_IUIAutomation = "{30cbe57d-d9d0-452a-ab13-7ac5ac4825ee}"
IID_IUIAutomationValuePattern = "{a94cd8b1-0844-4cd6-9d2d-640537ab39e9}"
IID_IUIAutomationTextPattern = "{32eba289-3583-42c9-9c59-3b6d9a1e4b6a}"
UIA_ValuePatternId, UIA_TextPatternId = 10002, 10014

CONTROL_TYPES = {
    50000: "button", 50001: "calendar", 50002: "checkbox", 50003: "combobox", 50004: "edit", 50005: "link",
    50006: "image", 50007: "listitem", 50008: "list", 50009: "menu", 50010: "menubar", 50011: "menuitem",
    50012: "progressbar", 50013: "radiobutton", 50014: "scrollbar", 50015: "slider", 50016: "spinner",
    50017: "statusbar", 50018: "tab", 50019: "tabitem", 50020: "text", 50021: "toolbar", 50022: "tooltip",
    50023: "tree", 50024: "treeitem", 50025: "custom", 50026: "group", 50027: "thumb", 50028: "datagrid",
    50029: "dataitem", 50030: "document", 50031: "splitbutton", 50032: "window", 50033: "pane", 50034: "header",
    50035: "headeritem", 50036: "table", 50037: "titlebar", 50038: "separator", 50039: "semanticzoom", 50040: "appbar",
}

# vtable slots (UIAutomationClient.h; checked live on Notepad and Calculator)
_AUT_ELEMENT_FROM_HANDLE, _AUT_ELEMENT_FROM_POINT, _AUT_GET_FOCUSED = 6, 7, 8
_AUT_CONTROL_VIEW_WALKER = 14
_EL_GET_PATTERN_AS = 14
_EL_PROCESS_ID, _EL_CONTROL_TYPE, _EL_NAME = 20, 21, 23
_EL_HAS_FOCUS, _EL_ENABLED, _EL_AUTOMATION_ID, _EL_CLASS = 26, 28, 29, 30
_EL_IS_PASSWORD, _EL_NATIVE_HWND, _EL_OFFSCREEN, _EL_RECT = 35, 36, 38, 43
_WALK_FIRST_CHILD, _WALK_NEXT_SIBLING, _WALK_PARENT = 4, 6, 3
_VALUE_GET = 4
_TEXT_DOCUMENT_RANGE = 7
_RANGE_GET_TEXT = 12


def _method(ptr, index, *argtypes):
    vtable = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(_P))).contents
    return ctypes.WINFUNCTYPE(_HRESULT, _P, *argtypes)(vtable[index])


def _release(ptr) -> None:
    if ptr:
        _method(ptr, 2)(ptr)


class _Com:
    def __init__(self):
        self.ole32 = ctypes.WinDLL("ole32")
        self.oleaut32 = ctypes.WinDLL("oleaut32")
        self.oleaut32.SysFreeString.argtypes = [_P]
        self.ole32.CoInitializeEx(None, 2)  # apartment-threaded
        self.automation = _P()
        hr = self.ole32.CoCreateInstance(ctypes.byref(_GUID.parse(CLSID_CUIAutomation)), None, 1,
                                         ctypes.byref(_GUID.parse(IID_IUIAutomation)), ctypes.byref(self.automation))
        if hr != 0 or not self.automation:
            raise OSError(f"UI Automation is not available (0x{hr & 0xFFFFFFFF:08x})")

    def bstr(self, ptr, index) -> str:
        out = _P()
        if _method(ptr, index, ctypes.POINTER(_P))(ptr, ctypes.byref(out)) != 0 or not out:
            return ""
        try:
            return ctypes.wstring_at(out)
        finally:
            self.oleaut32.SysFreeString(out)

    def integer(self, ptr, index) -> int:
        out = ctypes.c_int()
        _method(ptr, index, ctypes.POINTER(ctypes.c_int))(ptr, ctypes.byref(out))
        return out.value

    def handle(self, ptr, index) -> int:
        out = _P()
        _method(ptr, index, ctypes.POINTER(_P))(ptr, ctypes.byref(out))
        return int(out.value or 0)

    def rect(self, ptr) -> tuple[int, int, int, int]:
        r = _RECT()
        _method(ptr, _EL_RECT, ctypes.POINTER(_RECT))(ptr, ctypes.byref(r))
        return r.left, r.top, r.right, r.bottom

    def element_from_point(self, x: int, y: int):
        out = _P()
        _method(self.automation, _AUT_ELEMENT_FROM_POINT, _POINT, ctypes.POINTER(_P))(
            self.automation, _POINT(x, y), ctypes.byref(out))
        return out

    def focused(self):
        out = _P()
        _method(self.automation, _AUT_GET_FOCUSED, ctypes.POINTER(_P))(self.automation, ctypes.byref(out))
        return out

    def element_from_handle(self, hwnd: int):
        out = _P()
        _method(self.automation, _AUT_ELEMENT_FROM_HANDLE, _P, ctypes.POINTER(_P))(
            self.automation, _P(hwnd), ctypes.byref(out))
        return out

    def walker(self):
        out = _P()
        _method(self.automation, _AUT_CONTROL_VIEW_WALKER, ctypes.POINTER(_P))(self.automation, ctypes.byref(out))
        return out

    def walk(self, walker, index, element):
        out = _P()
        _method(walker, index, _P, ctypes.POINTER(_P))(walker, element, ctypes.byref(out))
        return out

    def describe(self, el) -> dict:
        left, top, right, bottom = self.rect(el)
        control = self.integer(el, _EL_CONTROL_TYPE)
        return {"name": self.bstr(el, _EL_NAME)[:200], "type": CONTROL_TYPES.get(control, str(control)),
                "automation_id": self.bstr(el, _EL_AUTOMATION_ID)[:80], "class": self.bstr(el, _EL_CLASS)[:80],
                "rect": [left, top, right, bottom], "center": [(left + right) // 2, (top + bottom) // 2],
                "enabled": bool(self.integer(el, _EL_ENABLED)), "focused": bool(self.integer(el, _EL_HAS_FOCUS)),
                "is_password": bool(self.integer(el, _EL_IS_PASSWORD)), "pid": self.integer(el, _EL_PROCESS_ID),
                "offscreen": bool(self.integer(el, _EL_OFFSCREEN)), "hwnd": self.handle(el, _EL_NATIVE_HWND)}

    def value(self, el, limit: int = 4000) -> str | None:
        """The element's text: ValuePattern, else the TextPattern document (Notepad's editor). None = unreadable."""
        pattern = _P()
        if _method(el, _EL_GET_PATTERN_AS, ctypes.c_int, ctypes.POINTER(_GUID), ctypes.POINTER(_P))(
                el, UIA_ValuePatternId, ctypes.byref(_GUID.parse(IID_IUIAutomationValuePattern)),
                ctypes.byref(pattern)) == 0 and pattern:
            try:
                return self.bstr(pattern, _VALUE_GET)[:limit]
            finally:
                _release(pattern)
        if _method(el, _EL_GET_PATTERN_AS, ctypes.c_int, ctypes.POINTER(_GUID), ctypes.POINTER(_P))(
                el, UIA_TextPatternId, ctypes.byref(_GUID.parse(IID_IUIAutomationTextPattern)),
                ctypes.byref(pattern)) == 0 and pattern:
            doc = _P()
            try:
                if _method(pattern, _TEXT_DOCUMENT_RANGE, ctypes.POINTER(_P))(pattern, ctypes.byref(doc)) == 0 and doc:
                    out = _P()
                    if _method(doc, _RANGE_GET_TEXT, ctypes.c_int, ctypes.POINTER(_P))(doc, limit, ctypes.byref(out)) == 0:
                        try:
                            return ctypes.wstring_at(out) if out else ""
                        finally:
                            self.oleaut32.SysFreeString(out)
            finally:
                _release(doc)
                _release(pattern)
        return None


def _physical() -> None:
    try:
        ctypes.windll.user32.SetThreadDpiAwarenessContext(_P(-4))
    except (AttributeError, OSError):
        pass


def _ancestors(com: _Com, el, depth: int = 3) -> list[str]:
    """Names of the element's parents (a click on an unnamed icon inside a "Send" button is a click on "Send")."""
    walker, names, current = com.walker(), [], el
    try:
        _method(current, 1)(current)  # AddRef: the loop releases what it walks
        for _ in range(depth):
            parent = com.walk(walker, _WALK_PARENT, current)
            _release(current)
            current = parent
            if not current:
                break
            name = com.bstr(current, _EL_NAME)
            if name:
                names.append(name[:120])
    finally:
        _release(current)
        _release(walker)
    return names


def cmd_at(com: _Com, x: int, y: int) -> dict:
    el = com.element_from_point(x, y)
    if not el:
        return {"found": False}
    try:
        return {"found": True, **com.describe(el), "parents": _ancestors(com, el)}
    finally:
        _release(el)


def cmd_focused(com: _Com, with_value: bool) -> dict:
    el = com.focused()
    if not el:
        return {"found": False}
    try:
        info = {"found": True, **com.describe(el)}
        if with_value and not info["is_password"]:
            info["value"] = com.value(el)
        return info
    finally:
        _release(el)


def cmd_find(com: _Com, hwnd: int, query: str, limit: int, max_nodes: int, budget: float) -> dict:
    """Breadth-first over the control view of one window; named elements scored against ``query``."""
    root = com.element_from_handle(hwnd)
    if not root:
        return {"found": False, "elements": [], "error": "window not accessible"}
    walker, queue, seen, results = com.walker(), [(root, 0)], 0, []
    deadline = time.monotonic() + budget
    try:
        while queue and seen < max_nodes and time.monotonic() < deadline:
            el, depth = queue.pop(0)
            seen += 1
            info = com.describe(el)
            if info["name"] and not info["offscreen"]:
                score = _score(query, info["name"]) if query else 0.5
                if score > 0:
                    results.append((score, info))
            if depth < 25:
                child = com.walk(walker, _WALK_FIRST_CHILD, el)
                while child and len(queue) < max_nodes:
                    queue.append((child, depth + 1))
                    child = com.walk(walker, _WALK_NEXT_SIBLING, child)
            _release(el)
    finally:
        for el, _depth in queue:
            _release(el)
        _release(walker)
    results.sort(key=lambda si: -si[0])
    return {"found": bool(results), "scanned": seen, "complete": not queue,
            "elements": [dict(info, score=round(score, 2)) for score, info in results[:limit]]}


def main(argv: list[str]) -> int:
    _physical()
    try:
        com = _Com()
        if argv[0] == "at":
            out = cmd_at(com, int(argv[1]), int(argv[2]))
        elif argv[0] == "focused":
            out = cmd_focused(com, "--value" in argv)
        elif argv[0] == "find":
            out = cmd_find(com, int(argv[1]), argv[2], int(argv[3]), int(argv[4]), float(argv[5]))
        else:
            out = {"error": f"unknown command {argv[0]!r}"}
    except Exception as exc:  # noqa: BLE001 - reported to the caller as data
        out = {"error": f"{type(exc).__name__}: {exc}"}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


# ---- client (used inside Hermes) ---------------------------------------------------------------------------------

def _python() -> str:
    """XENIO's own interpreter when available (a plain CPython), else this one."""
    root = os.environ.get("XENIO_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    venv = os.path.join(root, ".venv", "Scripts", "python.exe")
    return venv if os.path.isfile(venv) else sys.executable


class UIAClient:
    def __init__(self, timeout: float = 8.0, runner=subprocess.run):
        self.timeout = timeout
        self._run = runner

    def _call(self, *args: str, timeout: float | None = None) -> dict:
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        env = {**os.environ, "PYTHONPATH": root, "PYTHONIOENCODING": "utf-8"}
        for name in ("PYTHONHOME", "PYTHONSTARTUP", "VIRTUAL_ENV", "PYTHONEXECUTABLE"):  # Hermes' interpreter settings
            env.pop(name, None)
        try:
            done = self._run([_python(), "-m", "app.desktop.uia", *args], capture_output=True, timeout=timeout or self.timeout,
                             cwd=root, env=env, stdin=subprocess.DEVNULL,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as exc:
            tail = (exc.stderr or b"")[-200:].decode("utf-8", "replace").strip() if isinstance(exc.stderr, bytes) else ""
            raise UIAError("the accessibility lookup timed out" + (f" ({tail})" if tail else "")) from None
        except OSError as exc:
            raise UIAError(f"the accessibility helper could not start ({exc})") from None
        try:
            data = json.loads((done.stdout or b"").decode("utf-8", "replace") or "{}")
        except ValueError:
            err = (done.stderr or b"")[-200:].decode("utf-8", "replace").strip()
            raise UIAError("the accessibility helper failed" + (f": {err}" if err else "")) from None
        if data.get("error"):
            raise UIAError(str(data["error"])[:200])
        return data

    def at(self, x: int, y: int) -> dict:
        return self._call("at", str(int(x)), str(int(y)))

    def focused(self, with_value: bool = False) -> dict:
        return self._call("focused", *(["--value"] if with_value else []))

    def find(self, hwnd: int, query: str, limit: int = 8, max_nodes: int = 2500, budget: float = 4.0) -> dict:
        return self._call("find", str(int(hwnd)), query or "", str(limit), str(max_nodes), str(budget), timeout=budget + 6)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
