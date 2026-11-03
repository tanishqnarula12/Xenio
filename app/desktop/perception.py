"""Seeing the screen at the lowest cost that can answer (V4 phase 1).

A look at the screen used to mean a screenshot and a vision model: 22 s (median of 58 real looks, up to 68 s), when all
59 questions the model really asked were about text ("read the error", "what does the editor show"). So:

    1. accessibility   the window's own text and controls - also a browser page, through the tree WebKit exposes (0.2-0.6 s)
    2. on-device OCR   windows whose tree shows nothing (yet: an Electron app builds its tree only after the first read):
                       the window's pixels, read on this Mac (~0.2 s)
    3. vision model    what only pixels can answer (pictures, charts, how something looks), or when 1 and 2 find no text

The model gets the words of the window instead of a description of them: exact, and with nothing lost to a screenshot's
resolution. If that does not answer, it asks again with ``need_pixels``. A window that has not changed since the last look
in the same request is reported as unchanged instead of sent again.

Everything the vision path applies applies here too: the look's consent (same tool), masked secrets, the result marked as
untrusted content - and a live test's questions are not read, by any route.
"""

from __future__ import annotations

import hashlib
import re
import time

from app.desktop import metrics
from app.desktop.service import MIN_WINDOW_TEXT
from app.safety.policy import LIVE_TEST_REASON, live_test, live_test_page
from app.vision.service import clean_answer

MAX_TEXT = 12_000  # characters of one look (a problem page is 2-3 000; the tree can hold a whole long page)
SAME_WINDOW_SECONDS = 120.0
# what the words of a window cannot answer. Asked for 59 real questions: none of them matches.
_NEEDS_PIXELS = re.compile(
    r"\b(?:image|images|picture|pictures|photo|photos|icon|icons|logo|logos|colou?rs?|charts?|graphs?|diagrams?|plots?"
    r"|thumbnails?|videos?|maps?|drawing|illustration|screenshot|wallpaper|visually|visual|looks?\s+like|look\s+good"
    r"|design|layout|font|highlighted|blurry)\b", re.IGNORECASE)


class LiveTestRefused(Exception):
    """A window the look would see is a running test or contest: its questions are not read."""


def needs_pixels(question: str) -> bool:
    return bool(_NEEDS_PIXELS.search(question or ""))


class Perception:
    def __init__(self, desktop=None, known_secrets: tuple[str, ...] | list[str] = (), clock=time.monotonic):
        self.desktop = desktop
        self._secrets = tuple(known_secrets)
        self._clock = clock
        self._last: dict[str, tuple[str, float, str]] = {}  # session -> (what was seen, when, the request's task)

    def observe(self, question: str, target: str = "screen", session: str = "", need_pixels: bool = False,
                fresh: bool = False) -> dict | None:
        """What the screen shows, as text - or None when a screenshot must answer. Raises LiveTestRefused."""
        desktop = self.desktop
        if getattr(desktop.uia, "read_text", None) is None:
            return None  # no accessibility text on this system (Windows): the vision path, as before
        started = self._clock()
        desktop.publish("looking", action="look")
        windows = desktop.visible_windows(target, session)
        for seen in desktop.look_would_see(target, session):
            if live_test(seen["title"]) or seen["live_page"]:
                raise LiveTestRefused(LIVE_TEST_REASON)
        if need_pixels or needs_pixels(question):
            return None
        parts, read, levels = [], [], []
        ocr_ms = capture_ms = 0
        for window in windows:
            text, level = self._read(window)
            if len(text) > MAX_TEXT and desktop._ocr is not None:
                # a very long tree (measured: an IDE, 60 000 characters) may not reach what is on screen before it is cut
                try:
                    shown = desktop._ocr(window.hwnd, window.rect)
                    text = f"[what the window shows right now]\n{shown['text']}\n\n[all of the window's text]\n{text}"
                    self._ocr_ms, self._capture_ms = int(shown.get("ocr_ms") or 0), int(shown.get("capture_ms") or 0)
                except Exception:  # noqa: BLE001 - the tree's text still stands
                    pass
            if live_test_page(text, window.title):  # what OCR saw can show a countdown the title and the tree did not
                raise LiveTestRefused(LIVE_TEST_REASON)
            ocr_ms += self._ocr_ms
            capture_ms += self._capture_ms
            if len(text.strip()) < MIN_WINDOW_TEXT:
                continue
            parts.append((window, text, level))
            levels.append(level)
            read.append({"app": window.app, "title": window.title[:120], "read_by": level, "chars": len(text),
                         "fullscreen": bool(desktop._fullscreen(window)), "in_front": bool(window.foreground)})
        ms = (self._clock() - started) * 1000
        folder = desktop._data_dir
        if capture_ms:
            metrics.record("capture", capture_ms, folder, kind="ocr")
        if not parts:  # nothing readable: a picture, a game, a video. Pixels it is.
            metrics.record("perception", ms, folder, level="escalated", windows=len(windows))
            return None
        joined = "\n\n".join((f"=== {w.app}: {w.title[:100]} ===\n" if len(parts) > 1 else "") + t for w, t, _ in parts)
        task = desktop.task(session)
        fingerprint = hashlib.sha256(("|".join(str(w.hwnd) for w, _t, _l in parts) + joined).encode()).hexdigest()
        previous = self._last.get(session)
        same = bool(previous and previous[0] == fingerprint and self._clock() - previous[1] < SAME_WINDOW_SECONDS)
        same_request = same and previous[2] == task.id
        self._last[session] = (fingerprint, self._clock(), task.id)
        level = levels[0] if len(set(levels)) == 1 else "mixed"
        metrics.record("perception", ms, folder, level=level, windows=len(parts), chars=len(joined),
                       unchanged=same, ocr_ms=ocr_ms)
        out: dict = {"source": target, "read_by": level, "windows": read, "changed_since_last_look": not same if previous else None,
                     "captured_at": time.strftime("%H:%M:%S"), "timing_ms": {"perception": int(ms), "ocr": ocr_ms}}
        if same_request and not fresh:
            # already in this request's conversation: nothing new to read, and no reason to send it again
            out.update(unchanged=True, text="", answer_from="the previous look in this request: the windows have not "
                       "changed since. Call again with fresh=true if you no longer have its text.")
            return out
        out["text"] = clean_answer(joined, self._secrets, MAX_TEXT)
        out["truncated"] = len(joined) > MAX_TEXT
        return out

    def _read(self, window) -> tuple[str, str]:
        desktop = self.desktop
        self._ocr_ms = self._capture_ms = 0
        try:
            text = str(desktop.page(window).get("text") or "")
        except Exception:  # noqa: BLE001 - the tree cannot be read: OCR below
            text = ""
        if len(text.strip()) >= MIN_WINDOW_TEXT or desktop._ocr is None:
            return text, "accessibility"
        try:
            seen = desktop._ocr(window.hwnd, window.rect)
        except Exception:  # noqa: BLE001 - no screen permission, or the window went away
            return text, "accessibility"
        self._ocr_ms, self._capture_ms = int(seen.get("ocr_ms") or 0), int(seen.get("capture_ms") or 0)
        return str(seen.get("text") or ""), "ocr"


__all__ = ["Perception", "LiveTestRefused", "needs_pixels", "MAX_TEXT"]
