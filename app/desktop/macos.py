"""macOS desktop backend (V5.5 on Mac): CoreGraphics, the Accessibility API and a little AppKit, through ctypes.

Hermes' Python has no pyobjc, so (like the Windows backend) this is the standard library plus ctypes against the
system frameworks. Two halves:

* **in-process primitives** that are plain C calls with fixed signatures: synthetic mouse/keyboard events
  (``post_*``), the physical Esc key state, display bounds, "is the screen locked", the window server's window list,
  in-memory screen capture with ImageIO encoding;
* **accessibility work** (reading a window's controls, focusing/minimizing/closing windows) talks to other apps over
  IPC and can hang on a busy app, so it runs in a persistent helper process (``python -m app.desktop.macos serve``)
  that answers one JSON line per request. A hang or crash costs one failed lookup and a restart, never the Hermes
  process, and the helper stays warm, so a lookup takes milliseconds instead of a new interpreter per call.

Permissions (granted by the user to the app XENIO runs in, e.g. Terminal; macOS asks once):
**Accessibility** (read controls, control windows, send mouse/keyboard) and **Screen & System Audio Recording**
(screenshots, window titles). Without them every call says exactly which permission is missing.

Coordinates are global display points with the origin at the top-left of the main display, the same space the
Accessibility API and CGEvents use; Retina pixels are only used inside a capture.
"""

from __future__ import annotations

import ctypes
import json
import os
import plistlib
import select
import subprocess
import sys
import threading
import time

# ---- frameworks ---------------------------------------------------------------------------------------------------

_P = ctypes.c_void_p
_FW = "/System/Library/Frameworks/{0}.framework/{0}"
_UTF8 = 0x08000100


class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class CGSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class CGRect(ctypes.Structure):
    _fields_ = [("origin", CGPoint), ("size", CGSize)]


class MacError(Exception):
    pass


ACCESSIBILITY_STEP = ("macOS has not given XENIO Accessibility access. Open System Settings > Privacy & Security > "
                      "Accessibility and switch on the app XENIO runs in (Terminal, iTerm or VS Code), then restart XENIO.")
SCREEN_STEP = ("macOS has not given XENIO screen access. Open System Settings > Privacy & Security > Screen & System "
               "Audio Recording and switch on the app XENIO runs in (Terminal, iTerm or VS Code), then restart XENIO.")


class _Lib:
    """All bindings, with explicit argument/return types (64-bit pointers must never be truncated)."""

    def __init__(self):
        if sys.platform != "darwin":
            raise ImportError("the macOS desktop backend runs on macOS only")
        load = ctypes.cdll.LoadLibrary
        self.cf = cf = load(_FW.format("CoreFoundation"))
        self.cg = cg = load(_FW.format("CoreGraphics"))
        self.ax = ax = load(_FW.format("ApplicationServices"))
        self.io = io = load(_FW.format("ImageIO"))
        self.ct = ct = load(_FW.format("CoreText"))
        self.proc = load("/usr/lib/libproc.dylib")
        self.objc = objc = load("/usr/lib/libobjc.A.dylib")
        load(_FW.format("AppKit"))  # registers NSScreen / NSEvent with the runtime

        def sig(lib, name, restype, *argtypes):
            fn = getattr(lib, name)
            fn.restype, fn.argtypes = restype, list(argtypes)
            return fn

        # CoreFoundation
        sig(cf, "CFRelease", None, _P)
        sig(cf, "CFRetain", _P, _P)
        sig(cf, "CFGetTypeID", ctypes.c_ulong, _P)
        for name in ("CFStringGetTypeID", "CFNumberGetTypeID", "CFBooleanGetTypeID", "CFArrayGetTypeID",
                     "CFDictionaryGetTypeID"):
            sig(cf, name, ctypes.c_ulong)
        sig(cf, "CFStringCreateWithCString", _P, _P, ctypes.c_char_p, ctypes.c_uint32)
        sig(cf, "CFStringGetLength", ctypes.c_long, _P)
        sig(cf, "CFStringGetMaximumSizeForEncoding", ctypes.c_long, ctypes.c_long, ctypes.c_uint32)
        sig(cf, "CFStringGetCString", ctypes.c_bool, _P, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32)
        sig(cf, "CFNumberIsFloatType", ctypes.c_bool, _P)
        sig(cf, "CFNumberGetValue", ctypes.c_bool, _P, ctypes.c_int, _P)
        sig(cf, "CFNumberCreate", _P, _P, ctypes.c_int, _P)
        sig(cf, "CFBooleanGetValue", ctypes.c_bool, _P)
        sig(cf, "CFArrayGetCount", ctypes.c_long, _P)
        sig(cf, "CFArrayGetValueAtIndex", _P, _P, ctypes.c_long)
        sig(cf, "CFArrayCreate", _P, _P, ctypes.POINTER(_P), ctypes.c_long, _P)
        sig(cf, "CFDictionaryGetValue", _P, _P, _P)
        sig(cf, "CFDictionaryCreate", _P, _P, ctypes.POINTER(_P), ctypes.POINTER(_P), ctypes.c_long, _P, _P)
        sig(cf, "CFDataCreateMutable", _P, _P, ctypes.c_long)
        sig(cf, "CFDataGetLength", ctypes.c_long, _P)
        sig(cf, "CFDataGetBytePtr", _P, _P)
        sig(cf, "CFAttributedStringCreate", _P, _P, _P, _P)
        self.k_true = _P.in_dll(cf, "kCFBooleanTrue").value
        self.k_false = _P.in_dll(cf, "kCFBooleanFalse").value
        self.array_callbacks = ctypes.addressof(_P.in_dll(cf, "kCFTypeArrayCallBacks"))
        self.dict_key_callbacks = ctypes.addressof(_P.in_dll(cf, "kCFTypeDictionaryKeyCallBacks"))
        self.dict_value_callbacks = ctypes.addressof(_P.in_dll(cf, "kCFTypeDictionaryValueCallBacks"))
        self.t_string, self.t_number = cf.CFStringGetTypeID(), cf.CFNumberGetTypeID()
        self.t_bool, self.t_array, self.t_dict = cf.CFBooleanGetTypeID(), cf.CFArrayGetTypeID(), cf.CFDictionaryGetTypeID()

        # CoreGraphics: displays, window list, session, events, images
        sig(cg, "CGMainDisplayID", ctypes.c_uint32)
        sig(cg, "CGGetActiveDisplayList", ctypes.c_int32, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32))
        sig(cg, "CGDisplayBounds", CGRect, ctypes.c_uint32)
        sig(cg, "CGWindowListCopyWindowInfo", _P, ctypes.c_uint32, ctypes.c_uint32)
        sig(cg, "CGSessionCopyCurrentDictionary", _P)
        sig(cg, "CGEventSourceKeyState", ctypes.c_bool, ctypes.c_int32, ctypes.c_uint16)
        sig(cg, "CGEventCreateMouseEvent", _P, _P, ctypes.c_uint32, CGPoint, ctypes.c_uint32)
        sig(cg, "CGEventCreateKeyboardEvent", _P, _P, ctypes.c_uint16, ctypes.c_bool)
        sig(cg, "CGEventCreateScrollWheelEvent2", _P, _P, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int32,
            ctypes.c_int32, ctypes.c_int32)
        sig(cg, "CGEventKeyboardSetUnicodeString", None, _P, ctypes.c_ulong, ctypes.POINTER(ctypes.c_uint16))
        sig(cg, "CGEventKeyboardGetUnicodeString", None, _P, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_uint16))
        sig(cg, "CGEventSetFlags", None, _P, ctypes.c_uint64)
        sig(cg, "CGEventGetFlags", ctypes.c_uint64, _P)
        sig(cg, "CGEventSetIntegerValueField", None, _P, ctypes.c_uint32, ctypes.c_int64)
        sig(cg, "CGEventGetIntegerValueField", ctypes.c_int64, _P, ctypes.c_uint32)
        sig(cg, "CGEventGetLocation", CGPoint, _P)
        sig(cg, "CGEventGetType", ctypes.c_uint32, _P)
        sig(cg, "CGEventPost", None, ctypes.c_uint32, _P)
        sig(cg, "CGEventCreate", _P, _P)
        for name in ("CGPreflightScreenCaptureAccess", "CGRequestScreenCaptureAccess", "CGPreflightPostEventAccess",
                     "CGRequestPostEventAccess"):
            sig(cg, name, ctypes.c_bool)
        sig(cg, "CGDisplayCreateImageForRect", _P, ctypes.c_uint32, CGRect)
        sig(cg, "CGWindowListCreateImage", _P, CGRect, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32)
        sig(cg, "CGImageGetWidth", ctypes.c_size_t, _P)
        sig(cg, "CGImageGetHeight", ctypes.c_size_t, _P)
        sig(cg, "CGImageRelease", None, _P)
        sig(cg, "CGColorSpaceCreateDeviceRGB", _P)
        sig(cg, "CGColorSpaceRelease", None, _P)
        sig(cg, "CGBitmapContextCreate", _P, _P, ctypes.c_size_t, ctypes.c_size_t, ctypes.c_size_t, ctypes.c_size_t,
            _P, ctypes.c_uint32)
        sig(cg, "CGBitmapContextGetData", _P, _P)
        sig(cg, "CGBitmapContextGetBytesPerRow", ctypes.c_size_t, _P)
        sig(cg, "CGBitmapContextCreateImage", _P, _P)
        sig(cg, "CGContextSetInterpolationQuality", None, _P, ctypes.c_int32)
        sig(cg, "CGContextDrawImage", None, _P, CGRect, _P)
        sig(cg, "CGContextSetRGBFillColor", None, _P, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double)
        sig(cg, "CGContextFillRect", None, _P, CGRect)
        sig(cg, "CGContextSetTextPosition", None, _P, ctypes.c_double, ctypes.c_double)
        sig(cg, "CGContextRelease", None, _P)
        sig(cg, "CGColorCreateGenericRGB", _P, ctypes.c_double, ctypes.c_double, ctypes.c_double, ctypes.c_double)

        # ImageIO / CoreText (capture encoding, the vision test card)
        sig(io, "CGImageDestinationCreateWithData", _P, _P, _P, ctypes.c_size_t, _P)
        sig(io, "CGImageDestinationAddImage", None, _P, _P, _P)
        sig(io, "CGImageDestinationFinalize", ctypes.c_bool, _P)
        sig(ct, "CTFontCreateWithName", _P, _P, ctypes.c_double, _P)
        sig(ct, "CTLineCreateWithAttributedString", _P, _P)
        sig(ct, "CTLineDraw", None, _P, _P)
        self.k_font_attr = _P.in_dll(ct, "kCTFontAttributeName").value
        self.k_color_attr = _P.in_dll(ct, "kCTForegroundColorAttributeName").value
        self.k_lossy = _P.in_dll(io, "kCGImageDestinationLossyCompressionQuality").value

        # Accessibility (HIServices)
        sig(ax, "AXIsProcessTrusted", ctypes.c_bool)
        sig(ax, "AXIsProcessTrustedWithOptions", ctypes.c_bool, _P)
        sig(ax, "AXUIElementCreateSystemWide", _P)
        sig(ax, "AXUIElementCreateApplication", _P, ctypes.c_int32)
        sig(ax, "AXUIElementCopyAttributeValue", ctypes.c_int32, _P, _P, ctypes.POINTER(_P))
        sig(ax, "AXUIElementCopyMultipleAttributeValues", ctypes.c_int32, _P, _P, ctypes.c_uint32, ctypes.POINTER(_P))
        sig(ax, "AXUIElementSetAttributeValue", ctypes.c_int32, _P, _P, _P)
        sig(ax, "AXUIElementPerformAction", ctypes.c_int32, _P, _P)
        sig(ax, "AXUIElementCopyElementAtPosition", ctypes.c_int32, _P, ctypes.c_float, ctypes.c_float, ctypes.POINTER(_P))
        sig(ax, "AXUIElementGetPid", ctypes.c_int32, _P, ctypes.POINTER(ctypes.c_int32))
        sig(ax, "AXUIElementSetMessagingTimeout", ctypes.c_int32, _P, ctypes.c_float)
        sig(ax, "AXUIElementGetTypeID", ctypes.c_ulong)
        sig(ax, "AXValueGetTypeID", ctypes.c_ulong)
        sig(ax, "AXValueGetType", ctypes.c_uint32, _P)
        sig(ax, "AXValueGetValue", ctypes.c_bool, _P, ctypes.c_uint32, _P)
        sig(ax, "AXValueCreate", _P, ctypes.c_uint32, _P)
        try:  # private but stable since 10.x (used by every window manager): AX window -> CGWindowID
            self.ax_window_id = sig(ax, "_AXUIElementGetWindow", ctypes.c_int32, _P, ctypes.POINTER(ctypes.c_uint32))
        except AttributeError:
            self.ax_window_id = None
        self.t_ax_element, self.t_ax_value = ax.AXUIElementGetTypeID(), ax.AXValueGetTypeID()

        sig(self.proc, "proc_pidpath", ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32)
        sig(objc, "objc_getClass", _P, ctypes.c_char_p)
        sig(objc, "sel_registerName", _P, ctypes.c_char_p)
        sig(objc, "objc_autoreleasePoolPush", _P)
        sig(objc, "objc_autoreleasePoolPop", None, _P)
        self._msg_send = ctypes.cast(objc.objc_msgSend, _P).value
        self._protos: dict = {}
        self._strings: dict[str, int] = {}

    # ---- CoreFoundation helpers ---------------------------------------------------------------------------------

    def cfstr(self, text: str) -> int:
        """A CFString constant for attribute/action names (cached for the life of the process, never released)."""
        ref = self._strings.get(text)
        if ref is None:
            ref = self._strings[text] = self.cf.CFStringCreateWithCString(None, text.encode("utf-8"), _UTF8)
        return ref

    def new_cfstr(self, text: str) -> int:
        """A CFString the caller must release."""
        return self.cf.CFStringCreateWithCString(None, text.encode("utf-8"), _UTF8)

    def release(self, ref) -> None:
        if ref:
            self.cf.CFRelease(ref)

    def string(self, ref) -> str:
        n = self.cf.CFStringGetLength(ref)
        size = self.cf.CFStringGetMaximumSizeForEncoding(n, _UTF8) + 1
        buf = ctypes.create_string_buffer(size)
        return buf.value.decode("utf-8", "replace") if self.cf.CFStringGetCString(ref, buf, size, _UTF8) else ""

    def to_py(self, ref, keep_elements: bool = False):
        """CF value -> Python. AX elements come back as retained ints only with ``keep_elements`` (caller releases)."""
        if not ref:
            return None
        tid = self.cf.CFGetTypeID(ref)
        if tid == self.t_string:
            return self.string(ref)
        if tid == self.t_bool:
            return bool(self.cf.CFBooleanGetValue(ref))
        if tid == self.t_number:
            if self.cf.CFNumberIsFloatType(ref):
                out = ctypes.c_double()
                self.cf.CFNumberGetValue(ref, 13, ctypes.byref(out))  # kCFNumberDoubleType
            else:
                out = ctypes.c_int64()
                self.cf.CFNumberGetValue(ref, 4, ctypes.byref(out))  # kCFNumberSInt64Type
            return out.value
        if tid == self.t_array:
            return [self.to_py(self.cf.CFArrayGetValueAtIndex(ref, i), keep_elements)
                    for i in range(self.cf.CFArrayGetCount(ref))]
        if tid == self.t_ax_value:
            kind = self.ax.AXValueGetType(ref)
            if kind == 1:
                p = CGPoint()
                return (p.x, p.y) if self.ax.AXValueGetValue(ref, 1, ctypes.byref(p)) else None
            if kind == 2:
                s = CGSize()
                return (s.width, s.height) if self.ax.AXValueGetValue(ref, 2, ctypes.byref(s)) else None
            return None  # ranges, and kAXValueAXErrorType for attributes an element does not have
        if tid == self.t_ax_element:
            return self.cf.CFRetain(ref) if keep_elements else None
        return None

    def dict_get(self, d, key: str):
        return self.to_py(self.cf.CFDictionaryGetValue(d, self.cfstr(key)))

    def cf_array(self, refs: list[int]) -> int:
        items = (_P * len(refs))(*refs)
        return self.cf.CFArrayCreate(None, items, len(refs), self.array_callbacks)

    def cf_dict(self, pairs: list[tuple[int, int]]) -> int:
        keys = (_P * len(pairs))(*[k for k, _ in pairs])
        values = (_P * len(pairs))(*[v for _, v in pairs])
        return self.cf.CFDictionaryCreate(None, keys, values, len(pairs), self.dict_key_callbacks,
                                          self.dict_value_callbacks)

    # ---- Objective-C (two class methods only: NSScreen frames, the double-click interval) -----------------------

    def send(self, obj, selector: str, restype=_P, *args, argtypes=()):
        key = (restype, tuple(argtypes))
        fn = self._protos.get(key)
        if fn is None:
            fn = self._protos[key] = ctypes.CFUNCTYPE(restype, _P, _P, *argtypes)(self._msg_send)
        return fn(obj, self.objc.sel_registerName(selector.encode()), *args)

    def cls(self, name: str) -> int:
        return self.objc.objc_getClass(name.encode())


_lib: _Lib | None = None
_lib_lock = threading.Lock()


def lib() -> _Lib:
    global _lib
    with _lib_lock:
        if _lib is None:
            _lib = _Lib()
        return _lib


# ---- displays, session, key state (in-process) ----------------------------------------------------------------

def displays() -> list[tuple[int, int, int, int]]:
    """Every active display as (x, y, width, height) in global points; the main display first."""
    L = lib()
    ids = (ctypes.c_uint32 * 16)()
    count = ctypes.c_uint32()
    if L.cg.CGGetActiveDisplayList(16, ids, ctypes.byref(count)) != 0 or not count.value:
        ids, count = (ctypes.c_uint32 * 1)(L.cg.CGMainDisplayID()), ctypes.c_uint32(1)
    main = L.cg.CGMainDisplayID()
    order = sorted(ids[:count.value], key=lambda d: d != main)
    out = []
    for display in order:
        r = L.cg.CGDisplayBounds(display)
        out.append((int(r.origin.x), int(r.origin.y), int(r.size.width), int(r.size.height)))
    return out


def desktop_bounds() -> tuple[int, int, int, int]:
    """The rectangle around all displays (x, y, width, height), for checking click targets."""
    rects = displays()
    left, top = min(r[0] for r in rects), min(r[1] for r in rects)
    right, bottom = max(r[0] + r[2] for r in rects), max(r[1] + r[3] for r in rects)
    return left, top, right - left, bottom - top


def session_locked() -> bool:
    """True while the lock screen (or another user's session) is in front: input would go to the login window."""
    L = lib()
    d = L.cg.CGSessionCopyCurrentDictionary()
    if not d:
        return True  # no window-server session at all (e.g. over SSH): nothing on screen may be touched
    try:
        if L.dict_get(d, "CGSSessionScreenIsLocked"):
            return True
        return L.dict_get(d, "kCGSSessionOnConsoleKey") is False
    finally:
        L.release(d)


KVK_ESCAPE = 0x35


def key_down(keycode: int) -> bool:
    """Is this physical key held right now (HID state: real keyboards, not apps)?"""
    return bool(lib().cg.CGEventSourceKeyState(1, keycode))  # kCGEventSourceStateHIDSystemState


def trusted(prompt: bool = False) -> bool:
    """Accessibility permission for this process (``prompt`` shows macOS' own request dialog once)."""
    L = lib()
    if not prompt:
        return bool(L.ax.AXIsProcessTrusted())
    options = L.cf_dict([(L.cfstr("AXTrustedCheckOptionPrompt"), L.k_true)])
    try:
        return bool(L.ax.AXIsProcessTrustedWithOptions(options))
    finally:
        L.release(options)


def screen_access(request: bool = False) -> bool:
    L = lib()
    return bool(L.cg.CGRequestScreenCaptureAccess() if request else L.cg.CGPreflightScreenCaptureAccess())


def double_click_interval() -> float:
    try:
        L = lib()
        value = L.send(L.cls("NSEvent"), "doubleClickInterval", ctypes.c_double)
        return float(value) if 0.1 <= value <= 5 else 0.5
    except Exception:  # noqa: BLE001
        return 0.5


# ---- the window server's list (in-process, no permission needed except for titles) -----------------------------

_LIST_ALL, _LIST_ON_SCREEN, _EXCLUDE_DESKTOP = 0, 1, 16


def cg_windows(on_screen_only: bool = False) -> list[dict]:
    """Windows front to back: id, pid, owner (app name), title (needs screen access), bounds, layer, on_screen."""
    L = lib()
    arr = L.cg.CGWindowListCopyWindowInfo((_LIST_ON_SCREEN if on_screen_only else _LIST_ALL) | _EXCLUDE_DESKTOP, 0)
    if not arr:
        return []
    out = []
    try:
        for i in range(L.cf.CFArrayGetCount(arr)):
            d = L.cf.CFArrayGetValueAtIndex(arr, i)
            bounds = L.cf.CFDictionaryGetValue(d, L.cfstr("kCGWindowBounds"))
            b = {k: L.dict_get(bounds, k) or 0 for k in ("X", "Y", "Width", "Height")} if bounds else {}
            out.append({"id": int(L.dict_get(d, "kCGWindowNumber") or 0), "pid": int(L.dict_get(d, "kCGWindowOwnerPID") or 0),
                        "owner": L.dict_get(d, "kCGWindowOwnerName") or "", "title": L.dict_get(d, "kCGWindowName") or "",
                        "layer": int(L.dict_get(d, "kCGWindowLayer") or 0), "on_screen": bool(L.dict_get(d, "kCGWindowIsOnscreen")),
                        "alpha": float(L.dict_get(d, "kCGWindowAlpha") or 0),
                        "rect": (int(b.get("X", 0)), int(b.get("Y", 0)), int(b.get("X", 0) + b.get("Width", 0)),
                                 int(b.get("Y", 0) + b.get("Height", 0)))})
    finally:
        L.release(arr)
    return out


_bundles: dict[int, tuple[str, str]] = {}


def app_bundle(pid: int) -> tuple[str, str]:
    """(bundle path, bundle id) of a running process ("" when it is not an app bundle)."""
    if pid in _bundles:
        return _bundles[pid]
    buf = ctypes.create_string_buffer(4096)
    path = buf.value.decode("utf-8", "replace") if lib().proc.proc_pidpath(pid, buf, 4096) > 0 else ""
    bundle, ident = "", ""
    marker = path.rfind(".app/Contents/")
    if marker > 0:
        bundle = path[:marker + 4]
        try:
            with open(os.path.join(bundle, "Contents", "Info.plist"), "rb") as fh:
                ident = str(plistlib.load(fh).get("CFBundleIdentifier") or "")
        except (OSError, ValueError, plistlib.InvalidFileException):
            pass
    if len(_bundles) > 512:
        _bundles.clear()
    _bundles[pid] = (bundle, ident)
    return bundle, ident


# ---- synthetic input (in-process) ---------------------------------------------------------------------------------

EV_LEFT_DOWN, EV_LEFT_UP, EV_RIGHT_DOWN, EV_RIGHT_UP, EV_MOVED = 1, 2, 3, 4, 5
EV_LEFT_DRAGGED, EV_RIGHT_DRAGGED, EV_OTHER_DOWN, EV_OTHER_UP, EV_OTHER_DRAGGED = 6, 7, 25, 26, 27
FIELD_CLICK_STATE = 1
FLAG_SHIFT, FLAG_CONTROL, FLAG_OPTION, FLAG_COMMAND = 0x20000, 0x40000, 0x80000, 0x100000
_HID_TAP = 0  # kCGHIDEventTap: the events enter where real hardware events do


def _post(event) -> None:
    L = lib()
    if not event:
        raise MacError("macOS could not create the input event")
    try:
        L.cg.CGEventPost(_HID_TAP, event)
    finally:
        L.release(event)


def mouse_event(kind: int, x: float, y: float, button: int = 0, clicks: int = 1, flags: int = 0) -> int:
    """A mouse event (owned by the caller: post it or release it)."""
    L = lib()
    event = L.cg.CGEventCreateMouseEvent(None, kind, CGPoint(x, y), button)
    if event and clicks > 1:
        L.cg.CGEventSetIntegerValueField(event, FIELD_CLICK_STATE, clicks)
    if event and flags:
        L.cg.CGEventSetFlags(event, flags)
    return event


def key_event(keycode: int, down: bool, flags: int = 0) -> int:
    L = lib()
    event = L.cg.CGEventCreateKeyboardEvent(None, keycode, down)
    if event:
        L.cg.CGEventSetFlags(event, flags)
    return event


def unicode_event(units: list[int], down: bool) -> int:
    """A key event that carries text instead of a key: the app receives exactly these characters, whatever the
    keyboard layout (Hindi, German, emoji). Modifier flags are cleared so a held key cannot turn it into a shortcut."""
    L = lib()
    event = L.cg.CGEventCreateKeyboardEvent(None, 0, down)
    if event:
        buf = (ctypes.c_uint16 * len(units))(*units)
        L.cg.CGEventKeyboardSetUnicodeString(event, len(units), buf)
        L.cg.CGEventSetFlags(event, 0)
    return event


def scroll_event(lines_y: int, lines_x: int = 0) -> int:
    return lib().cg.CGEventCreateScrollWheelEvent2(None, 1, 2, lines_y, lines_x, 0)  # kCGScrollEventUnitLine


def post(event) -> None:
    _post(event)


# Media and volume keys are "system defined" events on a Mac (what the F7-F12 keys send), not key codes; every player
# that reacts to the keyboard's play/next keys reacts to these (Spotify, Music, YouTube in a browser...).
NX_SOUND_UP, NX_SOUND_DOWN, NX_MUTE, NX_PLAY, NX_NEXT, NX_PREVIOUS = 0, 1, 7, 16, 17, 18
_NS_SYSTEM_DEFINED, _NX_SUBTYPE_AUX_CONTROL = 14, 8


def media_event(code: int, down: bool) -> int:
    """A CGEvent for a media key press or release (owned by the caller: post it or release it)."""
    L = lib()
    state = 0xA if down else 0xB
    pool = L.objc.objc_autoreleasePoolPush()
    try:
        event = L.send(L.cls("NSEvent"), "otherEventWithType:location:modifierFlags:timestamp:windowNumber:context:"
                       "subtype:data1:data2:", _P, _NS_SYSTEM_DEFINED, CGPoint(0, 0), state << 8, 0.0, 0, None,
                       _NX_SUBTYPE_AUX_CONTROL, (code << 16) | (state << 8), -1,
                       argtypes=(ctypes.c_ulong, CGPoint, ctypes.c_ulong, ctypes.c_double, ctypes.c_long, _P,
                                 ctypes.c_short, ctypes.c_long, ctypes.c_long))
        cg = L.send(event, "CGEvent") if event else None
        return L.cf.CFRetain(cg) if cg else None  # outlives the autorelease pool
    finally:
        L.objc.objc_autoreleasePoolPop(pool)


def press_media_key(code: int) -> None:
    post(media_event(code, True))
    post(media_event(code, False))


def quit_app(pid: int, timeout: float = 4.0) -> str:
    """Quit an app the way Cmd+Q does (it may ask to save; nothing is forced). "closed" | "still open" | "not running".
    NSRunningApplication.terminate needs no permission."""
    L = lib()
    pool = L.objc.objc_autoreleasePoolPush()
    try:
        app = L.send(L.cls("NSRunningApplication"), "runningApplicationWithProcessIdentifier:", _P, pid,
                     argtypes=(ctypes.c_int32,))
        if not app:
            return "not running"
        L.send(app, "terminate", ctypes.c_bool)
    finally:
        L.objc.objc_autoreleasePoolPop(pool)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return "closed"
        except PermissionError:
            pass
        time.sleep(0.05)
    return "still open"


def can_post_events() -> bool:
    return bool(lib().cg.CGPreflightPostEventAccess())


def mouse_location() -> tuple[float, float]:
    L = lib()
    event = L.cg.CGEventCreate(None)
    try:
        p = L.cg.CGEventGetLocation(event)
        return p.x, p.y
    finally:
        L.release(event)


# ---- capture (in-process, in memory) ------------------------------------------------------------------------------

_IMAGE_ON_SCREEN_ONLY, _IMAGE_INCLUDING_WINDOW = 1, 8
_IMAGE_BOUNDS_IGNORE_FRAMING, _IMAGE_NOMINAL_RESOLUTION = 1, 16
_BGRX = 6 | (2 << 12)  # kCGImageAlphaNoneSkipFirst | kCGBitmapByteOrder32Little: bytes B, G, R, X


def capture_rect(x: float, y: float, width: float, height: float, window_id: int = 0) -> int:
    """A CGImage of a screen area (global points) or of one window, at native (Retina) resolution. Caller releases."""
    L = lib()
    rect = CGRect(CGPoint(x, y), CGSize(width, height))
    if window_id:
        return L.cg.CGWindowListCreateImage(CGRect(CGPoint(0, 0), CGSize(0, 0)), _IMAGE_INCLUDING_WINDOW, window_id,
                                            _IMAGE_BOUNDS_IGNORE_FRAMING)
    return L.cg.CGWindowListCreateImage(rect, _IMAGE_ON_SCREEN_ONLY, 0, 0)


def image_size(image) -> tuple[int, int]:
    L = lib()
    return int(L.cg.CGImageGetWidth(image)), int(L.cg.CGImageGetHeight(image))


def scale_image(image, width: int, height: int) -> tuple[int, bytes]:
    """Draw ``image`` into a width x height bitmap (high-quality scaling). Returns (CGImage of it, its BGRX bytes)."""
    L = lib()
    space = L.cg.CGColorSpaceCreateDeviceRGB()
    ctx = L.cg.CGBitmapContextCreate(None, width, height, 8, width * 4, space, _BGRX)
    L.cg.CGColorSpaceRelease(space)
    if not ctx:
        raise MacError(f"could not allocate a {width}x{height} image")
    try:
        L.cg.CGContextSetInterpolationQuality(ctx, 3)  # kCGInterpolationHigh
        L.cg.CGContextDrawImage(ctx, CGRect(CGPoint(0, 0), CGSize(width, height)), image)
        stride = L.cg.CGBitmapContextGetBytesPerRow(ctx)
        raw = ctypes.string_at(L.cg.CGBitmapContextGetData(ctx), stride * height)
        if stride != width * 4:
            raw = b"".join(raw[r * stride:r * stride + width * 4] for r in range(height))
        return L.cg.CGBitmapContextCreateImage(ctx), raw
    finally:
        L.cg.CGContextRelease(ctx)


def encode_image(image, kind: str = "public.jpeg", quality: float = 0.82) -> bytes:
    """CGImage -> JPEG/PNG bytes with ImageIO, entirely in memory."""
    L = lib()
    data = L.cf.CFDataCreateMutable(None, 0)
    kind_ref = L.new_cfstr(kind)
    options = number = dest = None
    try:
        dest = L.io.CGImageDestinationCreateWithData(data, kind_ref, 1, None)
        if not dest:
            raise MacError("ImageIO could not create an encoder")
        if kind == "public.jpeg":
            q = ctypes.c_double(quality)
            number = L.cf.CFNumberCreate(None, 13, ctypes.byref(q))
            options = L.cf_dict([(L.k_lossy, number)])
        L.io.CGImageDestinationAddImage(dest, image, options)
        if not L.io.CGImageDestinationFinalize(dest):
            raise MacError("ImageIO could not encode the image")
        return ctypes.string_at(L.cf.CFDataGetBytePtr(data), L.cf.CFDataGetLength(data))
    finally:
        for ref in (dest, options, number, kind_ref, data):
            L.release(ref)


def render_text(lines: list[str], width: int, height: int, font_px: int) -> tuple[int, bytes]:
    """A test card (light text on a dark background) drawn with CoreText. Returns (CGImage, BGRX bytes)."""
    L = lib()
    space = L.cg.CGColorSpaceCreateDeviceRGB()
    ctx = L.cg.CGBitmapContextCreate(None, width, height, 8, width * 4, space, _BGRX)
    L.cg.CGColorSpaceRelease(space)
    font_name = L.new_cfstr("Menlo")
    font = L.ct.CTFontCreateWithName(font_name, float(font_px), None)
    color = L.cg.CGColorCreateGenericRGB(0.9, 0.9, 0.9, 1.0)
    attrs = L.cf_dict([(L.k_font_attr, font), (L.k_color_attr, color)])
    try:
        L.cg.CGContextSetRGBFillColor(ctx, 0.125, 0.125, 0.125, 1.0)
        L.cg.CGContextFillRect(ctx, CGRect(CGPoint(0, 0), CGSize(width, height)))
        for i, line in enumerate(lines):
            text = L.new_cfstr(line)
            attributed = L.cf.CFAttributedStringCreate(None, text, attrs)
            ct_line = L.ct.CTLineCreateWithAttributedString(attributed)
            L.cg.CGContextSetTextPosition(ctx, 40.0, float(height - 40 - font_px - i * int(font_px * 1.5)))
            L.ct.CTLineDraw(ct_line, ctx)
            for ref in (ct_line, attributed, text):
                L.release(ref)
        raw = ctypes.string_at(L.cg.CGBitmapContextGetData(ctx), width * 4 * height)
        return L.cg.CGBitmapContextCreateImage(ctx), raw
    finally:
        for ref in (attrs, color, font, font_name):
            L.release(ref)
        L.cg.CGContextRelease(ctx)


# =================================================================================================================
# Accessibility (runs in the helper process)
# =================================================================================================================

AX_OK = 0
_AX_API_DISABLED = -25211
AX_TIMEOUT = 0.6  # seconds an app may take to answer one accessibility question

# AX role -> the control type names XENIO's policy already knows from Windows (button, edit, listitem, ...)
ROLES = {
    "AXButton": "button", "AXMenuButton": "button", "AXDisclosureTriangle": "button", "AXColorWell": "button",
    "AXPopUpButton": "combobox", "AXComboBox": "combobox", "AXTextField": "edit", "AXTextArea": "edit",
    "AXSearchField": "edit", "AXStaticText": "text", "AXHeading": "text", "AXLink": "link", "AXImage": "image",
    "AXCheckBox": "checkbox", "AXRadioButton": "radiobutton", "AXTabGroup": "tab", "AXRow": "listitem",
    "AXCell": "dataitem", "AXMenuItem": "menuitem", "AXMenuBarItem": "menuitem", "AXMenu": "menu",
    "AXMenuBar": "menubar", "AXList": "list", "AXTable": "table", "AXOutline": "tree", "AXWebArea": "document",
    "AXGroup": "group", "AXScrollArea": "pane", "AXSplitGroup": "pane", "AXToolbar": "toolbar", "AXWindow": "window",
    "AXSheet": "window", "AXDialog": "window", "AXSlider": "slider", "AXProgressIndicator": "progressbar",
    "AXIncrementor": "spinner", "AXScrollBar": "scrollbar", "AXTabButton": "tabitem", "AXLayoutArea": "pane",
}
_SUBROLES = {"AXTabButton": "tabitem", "AXOutlineRow": "treeitem", "AXSearchField": "edit",
             "AXSecureTextField": "edit", "AXTableRow": "listitem", "AXContentList": "list"}
_TEXT_ROLES = {"AXStaticText", "AXHeading", "AXLink", "AXCell", "AXMenuItem"}
_FIELD_ROLES = {"AXTextField", "AXTextArea", "AXComboBox", "AXSearchField"}
_NODE_ATTRS = ("AXRole", "AXSubrole", "AXTitle", "AXDescription", "AXValue", "AXPlaceholderValue", "AXPosition",
               "AXSize", "AXEnabled", "AXFocused", "AXChildren", "AXIdentifier")
_WINDOW_ATTRS = ("AXTitle", "AXPosition", "AXSize", "AXMinimized", "AXMain", "AXSubrole", "AXFullScreen")
# Chromium/Electron apps (Chrome, Discord, Slack, VS Code, WhatsApp...) build their full accessibility tree only
# when an assistive app asks for it, like VoiceOver does.
_CHROMIUM = ("com.google.chrome", "com.microsoft.edgemac", "com.brave.browser", "company.thebrowser.browser",
             "com.vivaldi.vivaldi", "com.operasoftware.opera", "org.chromium.chromium")


class AX:
    """One accessibility session (inside the helper)."""

    def __init__(self):
        self.L = lib()
        self.system = self.L.ax.AXUIElementCreateSystemWide()
        self.L.ax.AXUIElementSetMessagingTimeout(self.system, AX_TIMEOUT)
        self._apps: dict[int, int] = {}
        self._enhanced: set[int] = set()
        self._frames: dict[int, tuple] = {}  # window id -> frame before XENIO maximized it (for "restore")
        self._names = self.L.cf_array([self.L.cfstr(a) for a in _NODE_ATTRS])
        self._window_names = self.L.cf_array([self.L.cfstr(a) for a in _WINDOW_ATTRS])

    def require(self) -> None:
        if not self.L.ax.AXIsProcessTrusted():
            raise MacError(ACCESSIBILITY_STEP)

    # ---- raw attribute access --------------------------------------------------------------------------------

    def copy(self, el, name: str):
        out = _P()
        err = self.L.ax.AXUIElementCopyAttributeValue(el, self.L.cfstr(name), ctypes.byref(out))
        if err == _AX_API_DISABLED:
            raise MacError(ACCESSIBILITY_STEP)
        return out.value if err == AX_OK else None

    def get(self, el, name: str, keep_elements: bool = False):
        ref = self.copy(el, name)
        try:
            return self.L.to_py(ref, keep_elements)
        finally:
            self.L.release(ref)

    def get_many(self, el, names_array, names: tuple[str, ...], keep_elements: bool = False) -> dict:
        out = _P()
        if self.L.ax.AXUIElementCopyMultipleAttributeValues(el, names_array, 0, ctypes.byref(out)) != AX_OK or not out:
            return {}
        try:
            values = self.L.to_py(out.value, keep_elements) or []
        finally:
            self.L.release(out.value)
        return dict(zip(names, values))

    def set(self, el, name: str, value) -> bool:
        L = self.L
        if isinstance(value, bool):
            ref, owned = (L.k_true if value else L.k_false), False
        elif isinstance(value, tuple) and len(value) == 2 and name == "AXPosition":
            p = CGPoint(*value)
            ref, owned = L.ax.AXValueCreate(1, ctypes.byref(p)), True
        elif isinstance(value, tuple) and len(value) == 2 and name == "AXSize":
            s = CGSize(*value)
            ref, owned = L.ax.AXValueCreate(2, ctypes.byref(s)), True
        else:
            raise MacError(f"unsupported value for {name}")
        try:
            return L.ax.AXUIElementSetAttributeValue(el, L.cfstr(name), ref) == AX_OK
        finally:
            if owned:
                L.release(ref)

    def act(self, el, action: str) -> bool:
        return self.L.ax.AXUIElementPerformAction(el, self.L.cfstr(action)) == AX_OK

    def app(self, pid: int) -> int:
        el = self._apps.get(pid)
        if el is None:
            el = self._apps[pid] = self.L.ax.AXUIElementCreateApplication(pid)
            self.L.ax.AXUIElementSetMessagingTimeout(el, AX_TIMEOUT)
        return el

    def window_id(self, el) -> int:
        if self.L.ax_window_id is None:
            return 0
        out = ctypes.c_uint32()
        return int(out.value) if self.L.ax_window_id(el, ctypes.byref(out)) == AX_OK else 0

    def pid_of(self, el) -> int:
        out = ctypes.c_int32()
        return int(out.value) if self.L.ax.AXUIElementGetPid(el, ctypes.byref(out)) == AX_OK else 0

    # ---- windows ---------------------------------------------------------------------------------------------

    def frontmost(self) -> tuple[int, int]:
        """(pid, window id) of the app and window the user is working in."""
        app = self.copy(self.system, "AXFocusedApplication")
        if not app:
            return 0, 0
        try:
            pid = self.pid_of(app)
            window = self.copy(app, "AXFocusedWindow") or self.copy(app, "AXMainWindow")
            try:
                return pid, (self.window_id(window) if window else 0)
            finally:
                self.L.release(window)
        finally:
            self.L.release(app)

    def app_windows(self, pid: int) -> list[tuple[int, dict]]:
        """[(retained AX window, facts)] for one app's standard windows and dialogs."""
        windows = self.get(self.app(pid), "AXWindows", keep_elements=True) or []
        out = []
        for el in windows:
            if not el:
                continue
            facts = self.get_many(el, self._window_names, _WINDOW_ATTRS)
            if facts.get("AXSubrole") not in (None, "AXStandardWindow", "AXDialog", "AXSystemDialog", "AXFloatingWindow"):
                self.L.release(el)
                continue
            facts["id"] = self.window_id(el)
            out.append((el, facts))
        return out

    def list_windows(self, include_hidden: bool = False) -> list[dict]:
        cg = [w for w in cg_windows() if w["layer"] == 0 and w["pid"] > 0]
        order = {w["id"]: i for i, w in enumerate(cg)}
        owners = {w["pid"]: w["owner"] for w in cg}
        front_pid, front_id = self.frontmost()
        out = []
        for pid, owner in owners.items():
            if pid == os.getpid():
                continue
            hidden = bool(self.get(self.app(pid), "AXHidden"))
            if hidden != include_hidden:
                continue
            bundle, ident = app_bundle(pid)
            for el, f in self.app_windows(pid):
                self.L.release(el)
                pos, size = f.get("AXPosition") or (0, 0), f.get("AXSize") or (0, 0)
                rect = (int(pos[0]), int(pos[1]), int(pos[0] + size[0]), int(pos[1] + size[1]))
                out.append({"id": f["id"], "title": f.get("AXTitle") or "", "app": owner, "pid": pid, "rect": rect,
                            "minimized": bool(f.get("AXMinimized")),
                            "maximized": bool(f.get("AXFullScreen")) or self._fills_screen(rect),
                            "foreground": pid == front_pid and f["id"] == front_id and not hidden,
                            "bundle_id": ident, "bundle": bundle})
        out.sort(key=lambda w: (w["minimized"], order.get(w["id"], 10 ** 6)))
        return out

    def _find_window(self, wid: int, pid: int = 0) -> tuple[int, int]:
        """(pid, retained AX window) for a CGWindowID."""
        pids = [pid] if pid else sorted({w["pid"] for w in cg_windows() if w["id"] == wid}) or \
            sorted({w["pid"] for w in cg_windows() if w["layer"] == 0})
        for p in pids:
            found = 0
            for el, f in self.app_windows(p):
                if f["id"] == wid and not found:
                    found = el
                else:
                    self.L.release(el)
            if found:
                return p, found
        raise MacError("that window no longer exists")

    def focus(self, wid: int, pid: int = 0, timeout: float = 2.0) -> bool:
        pid, el = self._find_window(wid, pid)
        try:
            app = self.app(pid)
            if self.get(app, "AXHidden"):
                self.set(app, "AXHidden", False)
            if self.get(el, "AXMinimized"):
                self.set(el, "AXMinimized", False)
            self.set(app, "AXFrontmost", True)
            self.act(el, "AXRaise")
            self.set(el, "AXMain", True)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self.frontmost() == (pid, wid):
                    return True
                time.sleep(0.03)
            return self.frontmost() == (pid, wid)
        finally:
            self.L.release(el)

    def minimize(self, wid: int, pid: int = 0) -> bool:
        pid, el = self._find_window(wid, pid)
        try:
            self.set(el, "AXMinimized", True)
            return _poll(lambda: bool(self.get(el, "AXMinimized")))
        finally:
            self.L.release(el)

    def maximize(self, wid: int, pid: int = 0) -> bool:
        """Fill the screen the window is on (minus menu bar and Dock), like a double-click on its title bar."""
        pid, el = self._find_window(wid, pid)
        try:
            pos, size = self.get(el, "AXPosition"), self.get(el, "AXSize")
            if not pos or not size:
                return False
            frame = visible_frame((pos[0] + size[0] / 2, pos[1] + size[1] / 2))
            self._frames.setdefault(wid, (pos, size))
            self.set(el, "AXPosition", (frame[0], frame[1]))
            self.set(el, "AXSize", (frame[2], frame[3]))
            return _poll(lambda: self._fills_screen(self._rect(el)))
        finally:
            self.L.release(el)

    def restore(self, wid: int, pid: int = 0) -> bool:
        pid, el = self._find_window(wid, pid)
        try:
            if self.get(el, "AXMinimized"):
                self.set(el, "AXMinimized", False)
            if self.get(el, "AXFullScreen"):
                self.set(el, "AXFullScreen", False)
            previous = self._frames.pop(wid, None)
            if previous:
                self.set(el, "AXPosition", tuple(previous[0]))
                self.set(el, "AXSize", tuple(previous[1]))
            return _poll(lambda: not self.get(el, "AXMinimized") and not self.get(el, "AXFullScreen"))
        finally:
            self.L.release(el)

    def close(self, wid: int, pid: int = 0, timeout: float = 3.0) -> str:
        """Press the window's close button (exactly what a click on the red button does): the app may ask to save."""
        pid, el = self._find_window(wid, pid)
        try:
            button = self.copy(el, "AXCloseButton")
            if not button:
                return "still open"
            try:
                self.act(button, "AXPress")
            finally:
                self.L.release(button)
        finally:
            self.L.release(el)
        gone = _poll(lambda: wid not in self._window_ids(pid), timeout)
        return "closed" if gone else "still open"

    def unhide(self, pid: int) -> bool:
        app = self.app(pid)
        self.set(app, "AXHidden", False)
        return _poll(lambda: not self.get(app, "AXHidden"))

    def _window_ids(self, pid: int) -> set[int]:
        ids = set()
        for el, f in self.app_windows(pid):
            self.L.release(el)
            ids.add(f["id"])
        return ids

    def _rect(self, el) -> tuple[int, int, int, int]:
        pos, size = self.get(el, "AXPosition") or (0, 0), self.get(el, "AXSize") or (0, 0)
        return int(pos[0]), int(pos[1]), int(pos[0] + size[0]), int(pos[1] + size[1])

    @staticmethod
    def _fills_screen(rect) -> bool:
        cx, cy = (rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2
        try:
            fx, fy, fw, fh = visible_frame((cx, cy))
        except Exception:  # noqa: BLE001
            return False
        return abs(rect[0] - fx) <= 8 and abs(rect[1] - fy) <= 8 and rect[2] - rect[0] >= fw - 16 and rect[3] - rect[1] >= fh - 16

    # ---- controls --------------------------------------------------------------------------------------------

    def _enhance(self, pid: int) -> None:
        """Ask Chromium/Electron apps for their full tree once (what VoiceOver does). Harmless for other apps."""
        if pid in self._enhanced:
            return
        self._enhanced.add(pid)
        app = self.app(pid)
        self.set(app, "AXManualAccessibility", True)  # Electron
        if app_bundle(pid)[1].lower() in _CHROMIUM:
            self.set(app, "AXEnhancedUserInterface", True)
        time.sleep(0.15)  # the app builds the tree asynchronously

    def describe(self, f: dict) -> dict:
        role, sub = str(f.get("AXRole") or ""), str(f.get("AXSubrole") or "")
        kind = _SUBROLES.get(sub) or ROLES.get(role) or role.removeprefix("AX").lower() or "unknown"
        is_password = sub == "AXSecureTextField" or role == "AXSecureTextField"
        name = f.get("AXTitle") or f.get("AXDescription") or ""
        value = f.get("AXValue")
        if not name and role in _TEXT_ROLES and isinstance(value, str):
            name = value
        if not name and role in _FIELD_ROLES:
            name = f.get("AXPlaceholderValue") or ""
        name = " ".join(str(name).split())[:200]
        pos, size = f.get("AXPosition") or (0, 0), f.get("AXSize") or (0, 0)
        rect = [int(pos[0]), int(pos[1]), int(pos[0] + size[0]), int(pos[1] + size[1])]
        return {"name": name, "type": kind, "class": f"{role}/{sub}" if sub else role,
                "automation_id": str(f.get("AXIdentifier") or "")[:80], "rect": rect,
                "center": [(rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2],
                "enabled": f.get("AXEnabled") is not False, "focused": bool(f.get("AXFocused")),
                "is_password": is_password, "offscreen": size[0] <= 0 or size[1] <= 0}

    def find(self, wid: int, query: str, limit: int = 8, max_nodes: int = 2500, budget: float = 2.5) -> dict:
        """Breadth-first over one window's controls; named, visible controls scored against ``query``."""
        from app.desktop.common import score_name

        pid, root = self._find_window(wid)
        self._enhance(pid)
        queue, seen, results = [root], 0, []
        deadline = time.monotonic() + budget
        try:
            while queue and seen < max_nodes and time.monotonic() < deadline:
                el = queue.pop(0)
                seen += 1
                f = self.get_many(el, self._names, _NODE_ATTRS, keep_elements=True)
                children = f.pop("AXChildren", None) or []
                self.L.release(el)
                info = self.describe(f)
                if info["name"] and not info["offscreen"]:
                    score = score_name(query, info["name"]) if query else 0.5
                    if score > 0:
                        results.append((score, info))
                for child in children:
                    if child and len(queue) < max_nodes:
                        queue.append(child)
                    else:
                        self.L.release(child)
        finally:
            for el in queue:
                self.L.release(el)
        results.sort(key=lambda si: -si[0])
        return {"found": bool(results), "scanned": seen, "complete": not queue,
                "elements": [dict(info, score=round(score, 2)) for score, info in results[:limit]]}

    def _parents(self, el, depth: int = 3) -> list[str]:
        names, current = [], self.L.cf.CFRetain(el)
        try:
            for _ in range(depth):
                parent = self.copy(current, "AXParent")
                self.L.release(current)
                current = parent
                if not current:
                    break
                name = self.get(current, "AXTitle") or self.get(current, "AXDescription")
                if name:
                    names.append(str(name)[:120])
        finally:
            self.L.release(current)
        return names

    def at(self, x: float, y: float) -> dict:
        out = _P()
        if self.L.ax.AXUIElementCopyElementAtPosition(self.system, float(x), float(y), ctypes.byref(out)) != AX_OK or not out:
            return {"found": False}
        try:
            info = self.describe(self.get_many(out.value, self._names, _NODE_ATTRS))
            return {"found": True, **info, "pid": self.pid_of(out.value), "parents": self._parents(out.value)}
        finally:
            self.L.release(out.value)

    def focused(self, with_value: bool = False) -> dict:
        el = self.copy(self.system, "AXFocusedUIElement")
        if not el:
            return {"found": False}
        try:
            f = self.get_many(el, self._names, _NODE_ATTRS)
            f.pop("AXChildren", None)
            info = {"found": True, **self.describe(f), "pid": self.pid_of(el)}
            if with_value and not info["is_password"]:
                value = f.get("AXValue")
                info["value"] = value[:4000] if isinstance(value, str) else None
            return info
        finally:
            self.L.release(el)


def _poll(condition, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return bool(condition())


def visible_frame(point: tuple[float, float]) -> tuple[float, float, float, float]:
    """The usable area (without menu bar and Dock) of the screen containing ``point``, in global top-left points."""
    L = lib()
    pool = L.objc.objc_autoreleasePoolPush()
    try:
        screens = L.send(L.cls("NSScreen"), "screens")
        count = L.send(screens, "count", ctypes.c_ulong)
        frames = []
        for i in range(count):
            screen = L.send(screens, "objectAtIndex:", _P, i, argtypes=(ctypes.c_ulong,))
            frame = L.send(screen, "frame", CGRect)
            visible = L.send(screen, "visibleFrame", CGRect)
            frames.append((frame, visible))
        if not frames:
            raise MacError("no screen")
        main_height = frames[0][0].size.height  # Cocoa: y grows upwards from the bottom of the main screen
        best = frames[0]
        for frame, visible in frames:
            top = main_height - (frame.origin.y + frame.size.height)
            if frame.origin.x <= point[0] < frame.origin.x + frame.size.width and top <= point[1] < top + frame.size.height:
                best = (frame, visible)
                break
        v = best[1]
        return v.origin.x, main_height - (v.origin.y + v.size.height), v.size.width, v.size.height
    finally:
        L.objc.objc_autoreleasePoolPop(pool)


# ---- the helper process ----------------------------------------------------------------------------------------

def _handle(ax: AX, cmd: str, a: dict):
    if cmd == "ping":
        return {"trusted": bool(ax.L.ax.AXIsProcessTrusted()), "pid": os.getpid()}
    ax.require()
    if cmd == "windows":
        return ax.list_windows(bool(a.get("hidden")))
    if cmd == "frontmost":
        pid, wid = ax.frontmost()
        return {"pid": pid, "id": wid}
    if cmd in ("focus", "minimize", "maximize", "restore", "close"):
        return getattr(ax, cmd)(int(a["id"]), int(a.get("pid") or 0))
    if cmd == "unhide":
        return ax.unhide(int(a["pid"]))
    if cmd == "find":
        return ax.find(int(a["id"]), str(a.get("query") or ""), int(a.get("limit") or 8), int(a.get("max_nodes") or 2500),
                       float(a.get("budget") or 2.5))
    if cmd == "at":
        return ax.at(float(a["x"]), float(a["y"]))
    if cmd == "focused":
        return ax.focused(bool(a.get("with_value")))
    raise MacError(f"unknown command {cmd!r}")


def serve() -> int:
    """One JSON request per line on stdin, one JSON answer per line on stdout, until stdin closes."""
    ax = AX()
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except ValueError:
            continue
        pool = ax.L.objc.objc_autoreleasePoolPush()
        try:
            out = {"id": req.get("id"), "ok": True, "result": _handle(ax, str(req.get("cmd")), req.get("args") or {})}
        except Exception as exc:  # noqa: BLE001 - reported to the caller as data
            out = {"id": req.get("id"), "ok": False, "error": str(exc)[:300] if isinstance(exc, MacError)
                   else f"{type(exc).__name__}: {exc}"[:300]}
        finally:
            ax.L.objc.objc_autoreleasePoolPop(pool)
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


def _python() -> str:
    """XENIO's own interpreter when available (a plain CPython), else this one."""
    root = os.environ.get("XENIO_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    venv = os.path.join(root, ".venv", "bin", "python")
    return venv if os.path.isfile(venv) else sys.executable


class Helper:
    """Client for the helper process: started on first use, kept warm, restarted after a timeout or crash."""

    def __init__(self, timeout: float = 6.0, command: list[str] | None = None):
        self.timeout = timeout
        self._command = command
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._next = 0
        self._buf = b""  # bytes read from the helper but not yet a whole line

    def _start(self) -> subprocess.Popen:
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        env = {**os.environ, "PYTHONPATH": root, "PYTHONIOENCODING": "utf-8"}
        for name in ("PYTHONHOME", "PYTHONSTARTUP", "VIRTUAL_ENV", "PYTHONEXECUTABLE"):  # Hermes' interpreter settings
            env.pop(name, None)
        self._buf = b""
        return subprocess.Popen(self._command or [_python(), "-m", "app.desktop.macos", "serve"], cwd=root, env=env,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)

    def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def close(self) -> None:
        with self._lock:
            self._kill()

    def call(self, cmd: str, timeout: float | None = None, **args):
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                try:
                    self._proc = self._start()
                except OSError as exc:
                    raise MacError(f"the accessibility helper could not start ({exc})") from None
            self._next += 1
            req_id = self._next
            proc = self._proc
            try:
                proc.stdin.write((json.dumps({"id": req_id, "cmd": cmd, "args": args}) + "\n").encode("utf-8"))
                proc.stdin.flush()
            except OSError:
                self._kill()
                raise MacError("the accessibility helper stopped unexpectedly") from None
            deadline = time.monotonic() + (timeout or self.timeout)
            fd = proc.stdout.fileno()
            while True:
                # raw reads into our own buffer: a buffered reader could hold a whole answer that select() never sees
                while b"\n" not in self._buf:
                    left = deadline - time.monotonic()
                    if left <= 0 or not select.select([fd], [], [], left)[0]:
                        self._kill()  # an app that hangs the helper must not hang XENIO; the next call starts fresh
                        raise MacError("the accessibility lookup timed out (an app did not answer)")
                    chunk = os.read(fd, 65536)
                    if not chunk:
                        self._kill()
                        raise MacError("the accessibility helper stopped unexpectedly")
                    self._buf += chunk
                line, self._buf = self._buf.split(b"\n", 1)
                try:
                    out = json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if out.get("id") != req_id:
                    continue  # an answer to an earlier request that timed out
                if not out.get("ok"):
                    raise MacError(str(out.get("error") or "the accessibility lookup failed"))
                return out.get("result")


_helper: Helper | None = None


def helper() -> Helper:
    global _helper
    if _helper is None:
        _helper = Helper()
    return _helper


def set_helper(value: Helper | None) -> None:
    """Tests: install a fake helper (None = the real one on next use)."""
    global _helper
    _helper = value


if __name__ == "__main__":
    raise SystemExit(serve() if sys.argv[1:2] == ["serve"] else 2)
