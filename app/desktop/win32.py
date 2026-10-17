"""Windows bindings for XENIO's desktop control (V5.5): windows, processes, focus, mouse and keyboard input.

Standard library only (ctypes): this runs inside Hermes' Python through the xenio-guard plugin. Every handle is
declared as a pointer type so 64-bit handles are never truncated. Nothing here decides whether an action is
allowed - that is ``app/safety/policy.py`` - and nothing here touches files or the network.
"""

from __future__ import annotations

import ctypes
import os
from contextlib import contextmanager

if os.name != "nt":  # pragma: no cover - XENIO's desktop control is Windows-only
    raise ImportError("desktop control is implemented for Windows only")

from ctypes import wintypes as w

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_H = ctypes.c_void_p

# ---- structures ---------------------------------------------------------------------------------------------


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", w.DWORD), ("dwFlags", w.DWORD),
                ("time", w.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", w.WORD), ("wScan", w.WORD), ("dwFlags", w.DWORD), ("time", w.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", w.DWORD), ("wParamL", w.WORD), ("wParamH", w.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", w.DWORD), ("u", _INPUTUNION)]


INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF_MOVE, MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0001, 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP, MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP = 0x0008, 0x0010, 0x0020, 0x0040
MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL, MOUSEEVENTF_ABSOLUTE, MOUSEEVENTF_VIRTUALDESK = 0x0800, 0x1000, 0x8000, 0x4000
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0001, 0x0002, 0x0004
WHEEL_DELTA = 120

# ---- functions --------------------------------------------------------------------------------------------------

WNDENUMPROC = ctypes.WINFUNCTYPE(w.BOOL, _H, w.LPARAM)
user32.EnumWindows.restype, user32.EnumWindows.argtypes = w.BOOL, [WNDENUMPROC, w.LPARAM]
user32.IsWindowVisible.restype, user32.IsWindowVisible.argtypes = w.BOOL, [_H]
user32.IsIconic.restype, user32.IsIconic.argtypes = w.BOOL, [_H]
user32.IsZoomed.restype, user32.IsZoomed.argtypes = w.BOOL, [_H]
user32.IsWindow.restype, user32.IsWindow.argtypes = w.BOOL, [_H]
user32.GetWindowTextLengthW.restype, user32.GetWindowTextLengthW.argtypes = ctypes.c_int, [_H]
user32.GetWindowTextW.restype, user32.GetWindowTextW.argtypes = ctypes.c_int, [_H, w.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype, user32.GetClassNameW.argtypes = ctypes.c_int, [_H, w.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.restype, user32.GetWindowThreadProcessId.argtypes = w.DWORD, [_H, ctypes.POINTER(w.DWORD)]
user32.GetWindow.restype, user32.GetWindow.argtypes = _H, [_H, ctypes.c_uint]
user32.GetWindowLongW.restype, user32.GetWindowLongW.argtypes = ctypes.c_long, [_H, ctypes.c_int]
user32.GetWindowRect.restype, user32.GetWindowRect.argtypes = w.BOOL, [_H, ctypes.POINTER(RECT)]
user32.GetForegroundWindow.restype, user32.GetForegroundWindow.argtypes = _H, []
user32.SetForegroundWindow.restype, user32.SetForegroundWindow.argtypes = w.BOOL, [_H]
user32.BringWindowToTop.restype, user32.BringWindowToTop.argtypes = w.BOOL, [_H]
user32.ShowWindow.restype, user32.ShowWindow.argtypes = w.BOOL, [_H, ctypes.c_int]
user32.PostMessageW.restype, user32.PostMessageW.argtypes = w.BOOL, [_H, ctypes.c_uint, w.WPARAM, w.LPARAM]
user32.AttachThreadInput.restype, user32.AttachThreadInput.argtypes = w.BOOL, [w.DWORD, w.DWORD, w.BOOL]
user32.GetSystemMetrics.restype, user32.GetSystemMetrics.argtypes = ctypes.c_int, [ctypes.c_int]
user32.SetCursorPos.restype, user32.SetCursorPos.argtypes = w.BOOL, [ctypes.c_int, ctypes.c_int]
user32.GetCursorPos.restype, user32.GetCursorPos.argtypes = w.BOOL, [ctypes.POINTER(POINT)]
user32.SendInput.restype, user32.SendInput.argtypes = ctypes.c_uint, [ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int]
user32.GetAsyncKeyState.restype, user32.GetAsyncKeyState.argtypes = ctypes.c_short, [ctypes.c_int]
user32.MapVirtualKeyW.restype, user32.MapVirtualKeyW.argtypes = ctypes.c_uint, [ctypes.c_uint, ctypes.c_uint]
user32.GetDoubleClickTime.restype, user32.GetDoubleClickTime.argtypes = ctypes.c_uint, []
kernel32.GetCurrentThreadId.restype, kernel32.GetCurrentThreadId.argtypes = w.DWORD, []
kernel32.OpenProcess.restype, kernel32.OpenProcess.argtypes = _H, [w.DWORD, w.BOOL, w.DWORD]
kernel32.CloseHandle.restype, kernel32.CloseHandle.argtypes = w.BOOL, [_H]
kernel32.QueryFullProcessImageNameW.restype = w.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [_H, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
try:
    user32.SetThreadDpiAwarenessContext.restype = _H
    user32.SetThreadDpiAwarenessContext.argtypes = [_H]
    _set_dpi = user32.SetThreadDpiAwarenessContext
except AttributeError:  # before Windows 10 1607
    _set_dpi = None
try:
    _advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    _advapi.OpenProcessToken.restype, _advapi.OpenProcessToken.argtypes = w.BOOL, [_H, w.DWORD, ctypes.POINTER(_H)]
    _advapi.GetTokenInformation.restype = w.BOOL
    _advapi.GetTokenInformation.argtypes = [_H, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)]
except (OSError, AttributeError):  # pragma: no cover
    _advapi = None
try:
    _dwm = ctypes.WinDLL("dwmapi")
    _dwm.DwmGetWindowAttribute.restype = ctypes.c_long
    _dwm.DwmGetWindowAttribute.argtypes = [_H, w.DWORD, ctypes.c_void_p, w.DWORD]
except (OSError, AttributeError):  # pragma: no cover
    _dwm = None

SW_MINIMIZE, SW_MAXIMIZE, SW_RESTORE, SW_SHOW = 6, 3, 9, 5
WM_CLOSE = 0x0010
GW_OWNER = 4
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
DWMWA_CLOAKED, DWMWA_EXTENDED_FRAME_BOUNDS = 14, 9
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79


@contextmanager
def physical_pixels():
    """Per-monitor DPI awareness for this thread only: coordinates are real screen pixels (also at 125 %/150 %)."""
    old = _set_dpi(ctypes.c_void_p(-4)) if _set_dpi else None
    try:
        yield
    finally:
        if old:
            _set_dpi(ctypes.c_void_p(old))


def virtual_screen() -> tuple[int, int, int, int]:
    """(x, y, width, height) of the whole desktop across monitors, in physical pixels."""
    with physical_pixels():
        m = user32.GetSystemMetrics
        return m(SM_XVIRTUALSCREEN), m(SM_YVIRTUALSCREEN), m(SM_CXVIRTUALSCREEN), m(SM_CYVIRTUALSCREEN)


def window_text(hwnd) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def class_name(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def window_pid(hwnd) -> int:
    pid = w.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def window_thread(hwnd) -> int:
    return int(user32.GetWindowThreadProcessId(hwnd, None))


def process_path(pid: int) -> str:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = w.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        return buf.value if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)) else ""
    finally:
        kernel32.CloseHandle(handle)


def process_elevated(pid: int) -> bool | None:
    """True when the process runs as administrator: normal-user input cannot reach it (Windows UIPI)."""
    if _advapi is None:
        return None
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None  # typically a protected/elevated process
    token = _H()
    try:
        if not _advapi.OpenProcessToken(handle, 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
            return None
        elevation, size = w.DWORD(), w.DWORD()
        ok = _advapi.GetTokenInformation(token, 20, ctypes.byref(elevation), 4, ctypes.byref(size))  # TokenElevation
        return bool(elevation.value) if ok else None
    finally:
        if token:
            kernel32.CloseHandle(token)
        kernel32.CloseHandle(handle)


def window_rect(hwnd) -> tuple[int, int, int, int]:
    """(left, top, right, bottom) without the invisible resize border, in physical pixels."""
    rect = RECT()
    with physical_pixels():
        if _dwm is None or _dwm.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect),
                                                       ctypes.sizeof(rect)) != 0:
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom


def is_cloaked(hwnd) -> bool:
    """Windows 10/11 keep some windows 'visible' but cloaked (other virtual desktop, suspended UWP frames)."""
    if _dwm is None:
        return False
    value = ctypes.c_int(0)
    _dwm.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(value), ctypes.sizeof(value))
    return bool(value.value)


def top_level_windows() -> list:
    """Handles of visible, uncloaked, titled top-level application windows (what Alt+Tab shows)."""
    found = []

    def callback(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, GW_OWNER) and not is_cloaked(hwnd):
            if not (user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW) and user32.GetWindowTextLengthW(hwnd) > 0:
                found.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return found


user32.OpenInputDesktop.restype, user32.OpenInputDesktop.argtypes = _H, [w.DWORD, w.BOOL, w.DWORD]
user32.CloseDesktop.restype, user32.CloseDesktop.argtypes = w.BOOL, [_H]
DESKTOP_SWITCHDESKTOP = 0x0100


def session_locked() -> bool:
    """True while the PC is locked (or the sign-in / UAC secure desktop is up): then the input desktop cannot be
    opened, or the lock screen app is in front. Input sent now would land on the lock screen."""
    desk = user32.OpenInputDesktop(0, False, DESKTOP_SWITCHDESKTOP)
    if not desk:
        return True
    user32.CloseDesktop(desk)
    fg = user32.GetForegroundWindow()
    return bool(fg) and os.path.basename(process_path(window_pid(fg))).lower() == "lockapp.exe"


GWL_STYLE, WS_CAPTION = -16, 0x00C00000


def hidden_app_windows() -> list:
    """Handles of HIDDEN main windows (title bar, no owner): apps sitting in the tray, like Discord after its X."""
    found = []

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, GW_OWNER):
            style, ex = user32.GetWindowLongW(hwnd, GWL_STYLE), user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if style & WS_CAPTION == WS_CAPTION and not ex & WS_EX_TOOLWINDOW and user32.GetWindowTextLengthW(hwnd) > 0:
                found.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return found


def send_inputs(inputs: list[INPUT]) -> int:
    if not inputs:
        return 0
    array = (INPUT * len(inputs))(*inputs)
    return int(user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT)))
