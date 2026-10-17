"""Screen capture (V5): the primary display, the active window or a region, captured only when asked.

Privacy by construction: the picture exists only in memory. It is captured with Windows GDI (ctypes),
encoded as PNG with zlib (both standard library, because this runs inside Hermes' Python, which has no
Pillow), handed to the vision provider and dropped. Nothing is written to disk and nothing about its
content is logged. The capture is DPI-aware, so a 150 % scaled display is captured at full resolution.
Large screens are scaled down by Windows itself (HALFTONE) to ``max_edge`` pixels: vision models resize
anything bigger anyway (OpenAI-family models work at most at 768 px on the short side), and a smaller
image uploads faster.

``render_text_image`` draws a test card (text on a dark background) the same way, so the vision path can
be tested without looking at the user's real screen (``python -m app.main --vision-test``).
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import struct
import time
import zlib
from dataclasses import dataclass, field

DEFAULT_MAX_EDGE = 1600
MIN_REGION = 16
SOURCES = ("screen", "window", "region")  # implemented; "browser" is Hermes' browser_vision, "camera" and
# "uploaded_image" are future sources (docs/VISION_ARCHITECTURE.md)


class CaptureError(Exception):
    pass


@dataclass
class CapturedImage:
    """One screenshot, in memory only. ``digest`` identifies identical screens (no repeated uploads)."""

    data: bytes
    width: int
    height: int
    source: str
    source_size: tuple[int, int]
    mime: str = "image/png"
    title: str = ""
    captured_at: float = field(default_factory=time.time)
    digest: str = ""
    capture_ms: int = 0
    encode_ms: int = 0

    def data_url(self) -> str:
        import base64

        return f"data:{self.mime};base64," + base64.b64encode(self.data).decode("ascii")

    def discard(self) -> None:
        """Drop the pixels (the object may live on in a traceback or a cache entry)."""
        self.data = b""


# ---- PNG (standard library) --------------------------------------------------------------------------------

def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)


def encode_png(rgb: bytes | bytearray, width: int, height: int, level: int = 6) -> bytes:
    """8-bit RGB rows (top-down) -> PNG bytes. Filter "none": a screen's flat areas still compress well."""
    stride = width * 3
    if width < 1 or height < 1 or len(rgb) != stride * height:
        raise CaptureError(f"bad image buffer ({len(rgb)} bytes for {width}x{height})")
    raw = bytearray((stride + 1) * height)
    view = memoryview(rgb)
    for y in range(height):
        start = y * (stride + 1) + 1
        raw[start:start + stride] = view[y * stride:(y + 1) * stride]
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header) + _chunk(b"IDAT", zlib.compress(bytes(raw), level)) + \
        _chunk(b"IEND", b"")


def png_size(data: bytes) -> tuple[int, int]:
    """(width, height) of PNG bytes; raises CaptureError for anything that is not a PNG."""
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise CaptureError("not a PNG image")
    return struct.unpack(">II", data[16:24])


def bgra_to_rgb(bgra: bytes) -> bytearray:
    rgb = bytearray(len(bgra) // 4 * 3)
    rgb[0::3], rgb[1::3], rgb[2::3] = bgra[2::4], bgra[1::4], bgra[0::4]
    return rgb


def scaled_size(width: int, height: int, max_edge: int) -> tuple[int, int]:
    longest = max(width, height)
    if max_edge <= 0 or longest <= max_edge:
        return width, height
    factor = max_edge / longest
    return max(1, round(width * factor)), max(1, round(height * factor))


# ---- Windows GDI -------------------------------------------------------------------------------------------

_SM_CXSCREEN, _SM_CYSCREEN = 0, 1
_SM_XVIRTUALSCREEN, _SM_YVIRTUALSCREEN, _SM_CXVIRTUALSCREEN, _SM_CYVIRTUALSCREEN = 76, 77, 78, 79
_SRCCOPY, _CAPTUREBLT, _HALFTONE = 0x00CC0020, 0x40000000, 4
_DPI_PER_MONITOR_V2 = -4
_DWMWA_EXTENDED_FRAME_BOUNDS = 9


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32),
                ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32)]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", ctypes.c_uint32 * 3)]


class _Gdi:
    """ctypes bindings with explicit handle types (64-bit handles must not be truncated to int)."""

    def __init__(self):
        if os.name != "nt":
            raise CaptureError("screen capture is implemented for Windows only")
        from ctypes import wintypes as w

        self.user32 = u = ctypes.WinDLL("user32", use_last_error=True)
        self.gdi32 = g = ctypes.WinDLL("gdi32", use_last_error=True)
        h = ctypes.c_void_p
        u.GetDC.restype, u.GetDC.argtypes = h, [h]
        u.ReleaseDC.restype, u.ReleaseDC.argtypes = ctypes.c_int, [h, h]
        u.GetSystemMetrics.restype, u.GetSystemMetrics.argtypes = ctypes.c_int, [ctypes.c_int]
        u.GetForegroundWindow.restype, u.GetForegroundWindow.argtypes = h, []
        u.GetWindowRect.restype, u.GetWindowRect.argtypes = w.BOOL, [h, ctypes.POINTER(_RECT)]
        u.GetWindowTextW.restype, u.GetWindowTextW.argtypes = ctypes.c_int, [h, w.LPWSTR, ctypes.c_int]
        u.IsIconic.restype, u.IsIconic.argtypes = w.BOOL, [h]
        u.FillRect.restype, u.FillRect.argtypes = ctypes.c_int, [h, ctypes.POINTER(_RECT), h]
        try:
            u.SetThreadDpiAwarenessContext.restype = h
            u.SetThreadDpiAwarenessContext.argtypes = [h]
            self.set_dpi = u.SetThreadDpiAwarenessContext
        except AttributeError:  # before Windows 10 1607
            self.set_dpi = None
        try:
            dwm = ctypes.WinDLL("dwmapi")
            dwm.DwmGetWindowAttribute.restype = ctypes.c_long
            dwm.DwmGetWindowAttribute.argtypes = [h, w.DWORD, ctypes.c_void_p, w.DWORD]
            self.dwm_attr = dwm.DwmGetWindowAttribute
        except (OSError, AttributeError):
            self.dwm_attr = None
        g.CreateCompatibleDC.restype, g.CreateCompatibleDC.argtypes = h, [h]
        g.CreateDIBSection.restype = h
        g.CreateDIBSection.argtypes = [h, ctypes.POINTER(_BITMAPINFO), ctypes.c_uint, ctypes.POINTER(h), h, w.DWORD]
        g.SelectObject.restype, g.SelectObject.argtypes = h, [h, h]
        g.DeleteObject.restype, g.DeleteObject.argtypes = w.BOOL, [h]
        g.DeleteDC.restype, g.DeleteDC.argtypes = w.BOOL, [h]
        g.SetStretchBltMode.restype, g.SetStretchBltMode.argtypes = ctypes.c_int, [h, ctypes.c_int]
        g.SetBrushOrgEx.restype, g.SetBrushOrgEx.argtypes = w.BOOL, [h, ctypes.c_int, ctypes.c_int, h]
        g.StretchBlt.restype = w.BOOL
        g.StretchBlt.argtypes = [h, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                 h, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.DWORD]
        g.CreateSolidBrush.restype, g.CreateSolidBrush.argtypes = h, [w.DWORD]
        g.CreateFontW.restype = h
        g.CreateFontW.argtypes = [ctypes.c_int] * 5 + [w.DWORD] * 8 + [w.LPCWSTR]
        g.SetTextColor.restype, g.SetTextColor.argtypes = w.DWORD, [h, w.DWORD]
        g.SetBkMode.restype, g.SetBkMode.argtypes = ctypes.c_int, [h, ctypes.c_int]
        g.TextOutW.restype, g.TextOutW.argtypes = w.BOOL, [h, ctypes.c_int, ctypes.c_int, w.LPCWSTR, ctypes.c_int]

    def dpi_aware(self):
        """Context manager: physical pixels for this thread only (Hermes' other threads are unaffected)."""
        gdi = self

        class _Ctx:
            def __enter__(self):
                self.old = gdi.set_dpi(ctypes.c_void_p(_DPI_PER_MONITOR_V2)) if gdi.set_dpi else None

            def __exit__(self, *exc):
                if self.old:
                    gdi.set_dpi(ctypes.c_void_p(self.old))

        return _Ctx()

    def dib(self, hdc, width: int, height: int):
        """A top-down 32-bit DIB section: (bitmap handle, pointer to its pixels)."""
        info = _BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        info.bmiHeader.biWidth, info.bmiHeader.biHeight = width, -height
        info.bmiHeader.biPlanes, info.bmiHeader.biBitCount = 1, 32
        bits = ctypes.c_void_p()
        bitmap = self.gdi32.CreateDIBSection(hdc, ctypes.byref(info), 0, ctypes.byref(bits), None, 0)
        if not bitmap or not bits.value:
            raise CaptureError(f"could not allocate a {width}x{height} image (error {ctypes.get_last_error()})")
        return bitmap, bits


_gdi: _Gdi | None = None


def _bindings() -> _Gdi:
    global _gdi
    if _gdi is None:
        _gdi = _Gdi()
    return _gdi


class ScreenCapture:
    """Capture on request: ``capture_screen()``, ``capture_window()`` (the active window) or
    ``capture_region(x, y, width, height)`` in physical pixels of the virtual desktop."""

    def __init__(self, max_edge: int = DEFAULT_MAX_EDGE):
        self.max_edge = max_edge

    def screens(self) -> dict[str, tuple[int, int, int, int]]:
        gdi = _bindings()
        with gdi.dpi_aware():
            m = gdi.user32.GetSystemMetrics
            return {"primary": (0, 0, m(_SM_CXSCREEN), m(_SM_CYSCREEN)),
                    "virtual": (m(_SM_XVIRTUALSCREEN), m(_SM_YVIRTUALSCREEN), m(_SM_CXVIRTUALSCREEN),
                                m(_SM_CYVIRTUALSCREEN))}

    def capture_screen(self) -> CapturedImage:
        x, y, w, h = self.screens()["primary"]
        return self._grab(x, y, w, h, "screen")

    def capture_window(self) -> CapturedImage:
        gdi = _bindings()
        with gdi.dpi_aware():
            hwnd = gdi.user32.GetForegroundWindow()
            if not hwnd:
                raise CaptureError("no active window")
            if gdi.user32.IsIconic(hwnd):
                raise CaptureError("the active window is minimized")
            rect = _RECT()
            ok = gdi.dwm_attr is not None and gdi.dwm_attr(hwnd, _DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect),
                                                          ctypes.sizeof(rect)) == 0
            if not ok and not gdi.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                raise CaptureError("could not find the active window's position")
            buffer = ctypes.create_unicode_buffer(256)
            gdi.user32.GetWindowTextW(hwnd, buffer, 256)
        vx, vy, vw, vh = self.screens()["virtual"]
        left, top = max(rect.left, vx), max(rect.top, vy)
        right, bottom = min(rect.right, vx + vw), min(rect.bottom, vy + vh)
        if right - left < MIN_REGION or bottom - top < MIN_REGION:
            raise CaptureError("the active window is not visible on screen")
        image = self._grab(left, top, right - left, bottom - top, "window")
        image.title = buffer.value[:120]
        return image

    def capture_region(self, x: int, y: int, width: int, height: int) -> CapturedImage:
        validate_region([x, y, width, height], self.screens()["virtual"])
        return self._grab(x, y, width, height, "region")

    def _grab(self, x: int, y: int, width: int, height: int, source: str) -> CapturedImage:
        gdi = _bindings()
        out_w, out_h = scaled_size(width, height, self.max_edge)
        started = time.monotonic()
        with gdi.dpi_aware():
            screen = gdi.user32.GetDC(None)
            if not screen:
                raise CaptureError("the screen cannot be read (locked or no desktop session)")
            memory = bitmap = old = None
            try:
                memory = gdi.gdi32.CreateCompatibleDC(screen)
                bitmap, bits = gdi.dib(screen, out_w, out_h)
                old = gdi.gdi32.SelectObject(memory, bitmap)
                gdi.gdi32.SetStretchBltMode(memory, _HALFTONE)
                gdi.gdi32.SetBrushOrgEx(memory, 0, 0, None)
                if not gdi.gdi32.StretchBlt(memory, 0, 0, out_w, out_h, screen, x, y, width, height,
                                            _SRCCOPY | _CAPTUREBLT):
                    raise CaptureError(f"the screen could not be copied (error {ctypes.get_last_error()})")
                bgra = ctypes.string_at(bits, out_w * out_h * 4)
            finally:
                if old:
                    gdi.gdi32.SelectObject(memory, old)
                if bitmap:
                    gdi.gdi32.DeleteObject(bitmap)
                if memory:
                    gdi.gdi32.DeleteDC(memory)
                gdi.user32.ReleaseDC(None, screen)
        rgb = bgra_to_rgb(bgra)
        del bgra
        captured = time.monotonic()
        if not rgb.strip(b"\x00"):
            raise CaptureError("the capture came back black (a locked screen, or a window that blocks screenshots)")
        digest = hashlib.sha256(rgb).hexdigest()
        data = encode_png(rgb, out_w, out_h)
        del rgb
        return CapturedImage(data, out_w, out_h, source, (width, height), digest=digest,
                             capture_ms=int((captured - started) * 1000),
                             encode_ms=int((time.monotonic() - captured) * 1000))


def validate_region(region, virtual: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """[x, y, width, height] inside the virtual desktop, whole numbers, at least 16 px each way."""
    if not isinstance(region, (list, tuple)) or len(region) != 4:
        raise CaptureError("region must be [x, y, width, height]")
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in region):
        raise CaptureError("region values must be whole numbers")
    x, y, w, h = region
    vx, vy, vw, vh = virtual
    if w < MIN_REGION or h < MIN_REGION:
        raise CaptureError(f"region must be at least {MIN_REGION}x{MIN_REGION} pixels")
    if x < vx or y < vy or x + w > vx + vw or y + h > vy + vh:
        raise CaptureError(f"region is outside the screen ({vw}x{vh} at {vx},{vy})")
    return x, y, w, h


def render_text_image(lines: list[str], width: int = 1280, height: int = 720, font_px: int = 28) -> CapturedImage:
    """A test card drawn with GDI (light monospace text on a dark background), as a PNG in memory."""
    gdi = _bindings()
    memory = gdi.gdi32.CreateCompatibleDC(None)
    bitmap = old = font = brush = None
    try:
        bitmap, bits = gdi.dib(memory, width, height)
        old = gdi.gdi32.SelectObject(memory, bitmap)
        brush = gdi.gdi32.CreateSolidBrush(0x00202020)
        rect = _RECT(0, 0, width, height)
        gdi.user32.FillRect(memory, ctypes.byref(rect), brush)
        font = gdi.gdi32.CreateFontW(-font_px, 0, 0, 0, 400, 0, 0, 0, 0, 0, 0, 5, 0, "Consolas")
        gdi.gdi32.SelectObject(memory, font)
        gdi.gdi32.SetTextColor(memory, 0x00E6E6E6)
        gdi.gdi32.SetBkMode(memory, 1)
        for i, line in enumerate(lines):
            gdi.gdi32.TextOutW(memory, 40, 40 + i * int(font_px * 1.5), line, len(line))
        bgra = ctypes.string_at(bits, width * height * 4)
    finally:
        if old:
            gdi.gdi32.SelectObject(memory, old)
        for handle in (bitmap, font, brush):
            if handle:
                gdi.gdi32.DeleteObject(handle)
        gdi.gdi32.DeleteDC(memory)
    rgb = bgra_to_rgb(bgra)
    return CapturedImage(encode_png(rgb, width, height), width, height, "test_card", (width, height),
                         digest=hashlib.sha256(rgb).hexdigest())
