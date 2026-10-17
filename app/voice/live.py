"""Real-time conversation pieces for voice mode.

    Hermes streams text ─► SentenceChunker ─► ReplyStream (hold-back rule) ─► SpeechQueue ─► speaker
                                                                         ▲
    while Hermes works:  "One moment." / "Checking your calendar."  ─────┘  (pre-synthesized, no delay)

* **SentenceChunker** turns streamed text into speakable sentences the moment each one is complete
  (the first may be cut at a comma so the voice starts sooner).
* **ReplyStream** speaks sentences as they arrive, except a sentence that claims an outcome ("Done.",
  "I've created the event", "you declined"): that one, and everything after it, waits until the turn is
  over and XENIO has checked the claim against the tool results (orchestrator ``truth_check``). Streaming
  never says "Done" for something that failed.
* **FillerBank** holds short acknowledgments and per-tool narration, synthesized once and cached on
  disk, so they play instantly.
* **SpeechQueue** plays everything in order on one audio stream, synthesizing the next sentence while
  the current one plays, and stops at once on barge-in.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import re
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import numpy as np

from app.voice.audio import decode_wav, encode_wav
from app.voice.speech_text import to_speakable

log = logging.getLogger("xenio.voice.live")

# ---- sentences --------------------------------------------------------------------------------------

_ABBREVIATIONS = ("e.g.", "i.e.", "etc.", "vs.", "mr.", "mrs.", "ms.", "dr.", "st.", "no.", "approx.", "fig.")
_BOUNDARY = re.compile(r"[.!?…]+[\"')\]]*(?=\s)|\n")
_SOFT_BOUNDARY = re.compile(r"[,;:—–](?=\s)")
FIRST_SOFT_CUT = 90  # the first segment may end at a comma once it is this long


class SentenceChunker:
    def __init__(self) -> None:
        self._buf = ""
        self._emitted = 0

    def feed(self, text: str) -> list[str]:
        self._buf += text
        out = []
        while True:
            # Inside an unclosed code block nothing after the opening ``` is cut (it is not read aloud anyway);
            # complete sentences before it still go out.
            limit = self._buf.rfind("```") if self._buf.count("```") % 2 == 1 else None
            cut = self._next_cut(limit)
            if cut is None:
                break
            piece, self._buf = self._buf[:cut], self._buf[cut:]
            if piece.strip():
                out.append(piece.strip())
                self._emitted += 1
        return out

    def _next_cut(self, limit: int | None = None) -> int | None:
        fences = [m.start() for m in re.finditer("```", self._buf)]
        blocks = [(fences[i], fences[i + 1] + 3) for i in range(0, len(fences) - 1, 2)]  # closed code blocks
        for m in _BOUNDARY.finditer(self._buf):
            end = m.end()
            if limit is not None and end > limit:
                return None
            if any(start < end <= stop for start, stop in blocks):
                continue  # never inside a code block
            before = self._buf[:end].rstrip().lower()
            if before.endswith(_ABBREVIATIONS) or re.search(r"\b\d+\.$", before) and not self._buf[end:end + 1].isspace():
                continue
            if len(self._buf[:end].strip()) < 2:
                continue
            return end
        if self._emitted == 0 and len(self._buf) >= FIRST_SOFT_CUT and limit is None:
            soft = [m.end() for m in _SOFT_BOUNDARY.finditer(self._buf) if m.end() >= 40]
            if soft:
                return soft[0]
        return None

    def flush(self) -> list[str]:
        rest, self._buf = self._buf.strip(), ""
        return [rest] if rest else []


# ---- the hold-back rule -----------------------------------------------------------------------------

class ReplyStream:
    """Decides which streamed sentences can be spoken now and what is said once the turn is over."""

    def __init__(self, speak: Callable[[str], None], claims_outcome: Callable[[str], bool]):
        self._speak = speak
        self._claims = claims_outcome
        self._chunker = SentenceChunker()
        self.raw: list[str] = []  # everything Hermes streamed
        self.spoken: list[str] = []
        self.held: list[str] = []
        self.holding = False

    def feed(self, chunk: str) -> None:
        self.raw.append(chunk)
        for sentence in self._chunker.feed(chunk):
            self._sentence(sentence)

    def hold(self) -> None:
        """A tool failed or something was declined: the rest waits for the final check."""
        self.holding = True

    def _sentence(self, sentence: str) -> None:
        if self.holding or self._claims(sentence):
            self.holding = True
            self.held.append(sentence)
            return
        self.spoken.append(sentence)
        self._speak(sentence)

    def finish(self, final_text: str, ok: bool = True) -> list[str]:
        """What still has to be said, given the reply XENIO finally settled on."""
        for sentence in self._chunker.flush():
            if self.holding or self._claims(sentence):
                self.held.append(sentence)
            else:
                self.spoken.append(sentence)
                self._speak(sentence)
        streamed = "".join(self.raw).strip()
        final = (final_text or "").strip()
        if not ok:
            return [final] if final and not self.spoken else []
        if final == streamed or not final:
            return list(self.held)
        if streamed and final.startswith(streamed):  # a correction note was added
            return [*self.held, final[len(streamed):].strip()]
        return [final]  # the reply was replaced by what really happened


# ---- narration while Hermes works -----------------------------------------------------------------

ACKNOWLEDGMENTS = ("One moment.", "Sure, one second.", "Okay, let me check.", "On it.")
_NARRATION = (
    ("xenio_calendar", "Checking your calendar."),
    ("xenio_email", "Looking at your email."),
    ("xenio_github", "Checking GitHub."),
    ("xenio_tasks", "Checking your tasks."),
    ("xenio_project", "Looking at your project notes."),
    ("xenio_daily_briefing", "Putting your briefing together."),
    ("xenio_schedule", "Setting that up."),
    ("xenio_notifications", "Checking your notifications."),
    ("xenio_open_url", "Opening it for you."),
    ("xenio_look_at_screen", "Taking a look at your screen."),
    ("xenio_download", "Downloading it."),
    ("web_search", "Searching the web."),
    ("web_extract", "Reading up on it."),
    ("browser", "Looking at the page."),
    ("terminal", "Running that now."),
    ("execute_code", "Running that now."),
    ("read_file", "Looking at your files."),
    ("search_files", "Looking at your files."),
    ("xenio_list_directory", "Looking at your files."),
    ("xenio_inspect_path", "Looking at your files."),
)
# Closing replies ("thanks" -> "Anytime.") and the didn't-catch-that reply: answered without Hermes, instantly.
SHORT_REPLIES = ("Okay.", "Anytime.", "Bye for now.", "Sorry, I didn't catch that.")
PHRASES = (*ACKNOWLEDGMENTS, *dict.fromkeys(p for _, p in _NARRATION), *SHORT_REPLIES)


def narration_for(tool_title: str) -> str | None:
    """A short spoken status for a tool Hermes started, or None (internal tools stay silent)."""
    title = (tool_title or "").lower()
    for prefix, phrase in _NARRATION:
        if title.startswith(prefix):
            return phrase
    return None


class FillerBank:
    """Pre-synthesized phrases (cached as WAV on disk per voice), so they play with zero delay."""

    def __init__(self, tts, cache_dir: Path, voice: str = "", phrases: tuple[str, ...] = PHRASES):
        self._tts = tts
        self._dir = cache_dir
        self._voice = voice
        self._phrases = phrases
        self._audio: dict[str, tuple[np.ndarray, int]] = {}
        self._rng = random.Random()
        self._last_ack = ""

    def _path(self, text: str) -> Path:
        return self._dir / (hashlib.sha1(f"{self._voice}|{text}".encode()).hexdigest()[:16] + ".wav")

    async def warm(self) -> int:
        """Load cached phrases and synthesize missing ones (in the background at start-up)."""
        self._dir.mkdir(parents=True, exist_ok=True)
        for text in self._phrases:
            path = self._path(text)
            try:
                if path.is_file():
                    pcm, rate = decode_wav(path.read_bytes())
                else:
                    data = await self._tts.synthesize(text)
                    path.write_bytes(data)
                    pcm, rate = decode_wav(data)
                self._audio[text] = (pcm[:, 0] if pcm.ndim == 2 else pcm, rate)
            except Exception as exc:  # a missing filler only means silence there
                log.debug("Filler not available: %s (%s)", text, exc)
        return len(self._audio)

    def get(self, text: str) -> tuple[np.ndarray, int] | None:
        return self._audio.get(text)

    def acknowledgment(self) -> str | None:
        ready = [a for a in ACKNOWLEDGMENTS if a in self._audio and a != self._last_ack]
        if not ready:
            return None
        self._last_ack = self._rng.choice(ready)
        return self._last_ack


# ---- ordered, gapless speech output ----------------------------------------------------------------

class SpeechQueue:
    """Speak items in order: text (synthesized, streamed) or cached audio. ``say`` never blocks."""

    def __init__(self, tts, speaker, fillers: FillerBank | None = None,
                 on_item: Callable[[str, bool], None] | None = None, on_first_audio: Callable[[], None] | None = None,
                 on_error: Callable[[Exception], None] | None = None):
        self._tts = tts
        self._speaker = speaker
        self._fillers = fillers
        self._on_item = on_item  # (text, started) - e.g. the self-name echo guard
        self._on_first_audio = on_first_audio
        self._on_error = on_error
        self._items: asyncio.Queue = asyncio.Queue()
        self._runner: asyncio.Task | None = None
        self._stopped = False
        self.spoken_any = False
        self.first_audio_at: float | None = None
        self.queued = 0
        self.on_unspeakable: Callable[[str], None] | None = None  # text in a language no installed voice speaks

    def say(self, text: str) -> None:
        speakable = to_speakable(text).strip()
        if self._stopped or not speakable or not any(ch.isalnum() for ch in speakable):
            return
        can_speak = getattr(self._tts, "can_speak", None)
        if can_speak is not None and not can_speak(speakable):  # V5.5: no voice for this language (German)
            if self.on_unspeakable is not None:
                self.on_unspeakable(speakable)
            return
        cached = self._fillers.get(speakable) if self._fillers else None
        task = None if cached else asyncio.ensure_future(self._synthesize(speakable))
        self._enqueue((speakable, cached, task))

    def _enqueue(self, item) -> None:
        self.queued += 1
        self._items.put_nowait(item)
        if self._runner is None or self._runner.done():
            self._runner = asyncio.ensure_future(self._run())

    async def _synthesize(self, text: str) -> list:
        """Start synthesis now (in parallel with what is playing); chunks are collected as they arrive."""
        out: asyncio.Queue = asyncio.Queue()

        async def pump():
            try:
                if hasattr(self._tts, "synthesize_stream"):
                    async for rate, pcm in self._tts.synthesize_stream(text):
                        await out.put((rate, pcm))
                else:  # a provider that only returns whole clips
                    pcm, rate = decode_wav(await self._tts.synthesize(text))
                    await out.put((rate, pcm[:, 0] if pcm.ndim == 2 else pcm))
            except Exception as exc:
                log.warning("Speech synthesis failed", extra={"fields": {"error": str(exc)[:200]}})
                if self._on_error is not None:
                    self._on_error(exc)
            finally:
                await out.put(None)

        return [out, asyncio.ensure_future(pump())]

    async def _chunks(self) -> AsyncIterator[tuple[int, np.ndarray]]:
        """Everything queued, then whatever arrives within a short grace period (one stream, no gaps)."""
        while not self._stopped:
            try:
                text, cached, task = await asyncio.wait_for(self._items.get(), 0.25)
            except TimeoutError:
                return
            if self._on_item:
                self._on_item(text, True)
            try:
                if cached is not None:
                    yield cached[1], cached[0]
                else:
                    out, _pump = await task
                    while (piece := await out.get()) is not None:
                        if self._stopped:
                            return
                        yield piece
            finally:
                if self._on_item:
                    self._on_item(text, False)

    async def _run(self) -> None:
        while not self._stopped and not self._items.empty():
            if hasattr(self._speaker, "play_stream"):
                await self._speaker.play_stream(self._chunks(), on_start=self._started)
            else:  # simple speakers (tests, harnesses): play item by item
                async for rate, pcm in self._chunks():
                    self._started()
                    await self._speaker.play(pcm, rate)

    def _started(self) -> None:
        if not self.spoken_any:
            self.spoken_any = True
            self.first_audio_at = time.monotonic()
            if self._on_first_audio:
                self._on_first_audio()

    async def drain(self) -> None:
        """Wait until everything queued has been played."""
        while self._runner is not None and not self._runner.done():
            await asyncio.shield(self._runner)

    def stop(self) -> None:
        self._stopped = True
        self._speaker.stop()
        while not self._items.empty():
            _text, _cached, task = self._items.get_nowait()
            if task is not None:
                task.cancel()


def silence(seconds: float, rate: int = 24_000) -> np.ndarray:
    return np.zeros(int(rate * seconds), dtype=np.int16)


__all__ = ["SentenceChunker", "ReplyStream", "FillerBank", "SpeechQueue", "narration_for", "ACKNOWLEDGMENTS",
           "encode_wav"]
