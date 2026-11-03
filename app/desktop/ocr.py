"""Reading a window's pixels on this Mac (V4 phase 1): the step between the accessibility tree and a vision model.

Some windows show nothing through accessibility (measured: an IDE and the ChatGPT app, 0 characters on the first read:
Electron builds its tree only when asked), while the cloud vision model costs 22 s (median of 58 real looks). Apple's Vision framework reads the same window in 130-280 ms
on the device: nothing leaves the Mac and the pixels are released at once. Each line comes with its place on screen, so a
control an app does not expose can still be found by its visible words.

Lines are put in reading order per column: a problem statement beside an editor must not come out interleaved.

It runs in XENIO's accessibility helper (``macos.helper()``), never in the caller: measured, a window capture inside
Hermes' Python blocked for 30 s in the system's capture service, while the helper's Python answers in 10 ms. The helper's
calls have a timeout and it restarts after one, so a stalled capture costs a few seconds once - then OCR rests for a while
and the look goes on to the vision model.
"""

from __future__ import annotations

import ctypes
import sys
import time

MIN_CONFIDENCE = 0.3
_GUTTER_BINS = 100
READ_TIMEOUT = 6.0  # seconds one window may take in the helper (measured: 130-280 ms)
REST_SECONDS = 300.0  # after a failure OCR is not tried again for this long: no look waits for it twice
_vision_loaded = False
_resting_until = 0.0


class OcrError(Exception):
    pass


def available() -> bool:
    return sys.platform == "darwin"


def _load(lib) -> None:
    global _vision_loaded
    if not _vision_loaded:
        ctypes.CDLL("/System/Library/Frameworks/Vision.framework/Vision")
        _vision_loaded = True
    if not lib.cls("VNRecognizeTextRequest"):
        raise OcrError("the Vision framework has no text recognizer here")


def recognize(image, lib) -> list[dict]:
    """The text lines in a CGImage: ``{"text", "box": (x, y, w, h)}`` with the box in 0..1 of the image, y from the top."""
    from app.desktop.macos import CGRect

    P = ctypes.c_void_p
    _load(lib)
    pool = lib.objc.objc_autoreleasePoolPush()
    try:
        request = lib.send(lib.send(lib.cls("VNRecognizeTextRequest"), "alloc"), "init")
        handler = lib.send(lib.send(lib.cls("VNImageRequestHandler"), "alloc"), "initWithCGImage:options:", P, image,
                           lib.send(lib.cls("NSDictionary"), "dictionary"), argtypes=(P, P))
        try:
            lib.send(request, "setRecognitionLevel:", None, 0, argtypes=(ctypes.c_long,))  # accurate: 130-280 ms, measured
            lib.send(request, "setUsesLanguageCorrection:", None, False, argtypes=(ctypes.c_bool,))  # code is not prose
            requests = lib.send(lib.cls("NSArray"), "arrayWithObject:", P, request, argtypes=(P,))
            error = P()
            if not lib.send(handler, "performRequests:error:", ctypes.c_bool, requests, ctypes.byref(error),
                            argtypes=(P, ctypes.c_void_p)):
                raise OcrError("the text recognizer failed on this image")
            results = lib.send(request, "results")
            lines = []
            for i in range(int(lib.send(results, "count", ctypes.c_ulong)) if results else 0):
                seen = lib.send(results, "objectAtIndex:", P, i, argtypes=(ctypes.c_ulong,))
                candidates = lib.send(seen, "topCandidates:", P, 1, argtypes=(ctypes.c_ulong,))
                if not candidates or not lib.send(candidates, "count", ctypes.c_ulong):
                    continue
                best = lib.send(candidates, "objectAtIndex:", P, 0, argtypes=(ctypes.c_ulong,))
                if lib.send(best, "confidence", ctypes.c_float) < MIN_CONFIDENCE:
                    continue
                text = lib.send(lib.send(best, "string"), "UTF8String", ctypes.c_char_p)
                box = lib.send(seen, "boundingBox", CGRect)
                lines.append({"text": (text or b"").decode("utf-8", "replace"),
                              "box": (box.origin.x, 1.0 - box.origin.y - box.size.height, box.size.width, box.size.height)})
            return lines
        finally:
            lib.send(handler, "release", None)
            lib.send(request, "release", None)
    finally:
        lib.objc.objc_autoreleasePoolPop(pool)


def columns(lines: list[dict]) -> list[list[dict]]:
    """Lines per column, left to right: a column boundary is a vertical gap no line crosses, in the middle of the image."""
    covered = [False] * _GUTTER_BINS
    for line in lines:
        x, _y, w, _h = line["box"]
        for b in range(int(x * _GUTTER_BINS), min(_GUTTER_BINS, int((x + w) * _GUTTER_BINS) + 1)):
            covered[b] = True
    edges, b = [], 15
    while b < 85:
        if not covered[b]:
            start = b
            while b < 85 and not covered[b]:
                b += 1
            if b - start >= 2:  # at least 2% of the width: not just the gap between two words
                edges.append((start + b) / 2 / _GUTTER_BINS)
        b += 1
    out: list[list[dict]] = [[] for _ in range(len(edges) + 1)]
    for line in lines:
        out[sum(1 for edge in edges if line["box"][0] >= edge)].append(line)
    return [sorted(col, key=lambda ln: (round(ln["box"][1], 3), ln["box"][0])) for col in out if col]


def in_reading_order(lines: list[dict]) -> str:
    return "\n\n".join("\n".join(line["text"] for line in col) for col in columns(lines))


def read_window(hwnd: int, rect: tuple[int, int, int, int], clock=time.monotonic) -> dict:
    """One window's visible text, through the helper: ``{"text", "lines": [{"text", "center": [x, y]}], "capture_ms",
    "ocr_ms"}``, with the centers in screen points (they can be clicked). Raises OcrError when it cannot be read."""
    global _resting_until
    if not available():
        raise OcrError("reading pixels on the device works on macOS only")
    if clock() < _resting_until:
        raise OcrError("on-device OCR is resting after a failure")
    from app.desktop import macos

    try:
        out = macos.helper().call("ocr", timeout=READ_TIMEOUT, id=int(hwnd), rect=[int(v) for v in rect])
    except macos.MacError as exc:
        _resting_until = clock() + REST_SECONDS
        raise OcrError(str(exc)) from None
    if not isinstance(out, dict):
        raise OcrError("the helper gave no answer")
    return out


def read_here(hwnd: int, rect: tuple[int, int, int, int]) -> dict:
    """``read_window``'s work, in the helper process."""
    if not available():
        raise OcrError("reading pixels on the device works on macOS only")
    from app.desktop import macos

    if not macos.screen_access():
        raise OcrError(macos.SCREEN_STEP)
    lib = macos.lib()
    x1, y1, x2, y2 = rect
    started = time.monotonic()
    image = macos.capture_rect(x1, y1, x2 - x1, y2 - y1, hwnd)
    if not image:
        raise OcrError("the window could not be captured (the Mac is locked, or the window is gone)")
    captured = time.monotonic()
    try:
        lines = recognize(image, lib)
    finally:
        lib.cg.CGImageRelease(image)  # the pixels never outlive the reading
    width, height = x2 - x1, y2 - y1
    placed = [{"text": ln["text"], "center": [int(x1 + (ln["box"][0] + ln["box"][2] / 2) * width),
                                              int(y1 + (ln["box"][1] + ln["box"][3] / 2) * height)]} for ln in lines]
    return {"text": in_reading_order(lines), "lines": placed, "capture_ms": int((captured - started) * 1000),
            "ocr_ms": int((time.monotonic() - captured) * 1000)}


__all__ = ["OcrError", "available", "read_window", "read_here", "recognize", "columns", "in_reading_order"]
