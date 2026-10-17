"""Turn-taking: has the user finished talking, and what did they say?

A person listening does not wait for a fixed half second of silence. They notice when the speaker has
finished a thought, and they wait longer when a sentence trails off ("open the..."). XENIO approximates
this with the transcript itself. At the first short pause it starts transcribing what it has heard so far
(``EarlyTranscriber``). If the user stays quiet and the words form a finished request, the turn ends when
that transcript arrives, so no time is lost afterwards on speech recognition. If the words trail off, it
waits longer for the rest. If the user carries on talking, the early transcript is dropped and a new one
starts at the next pause.

``classify_utterance`` sorts what was heard. Recognizer noise ("Thank you." on silence, "you") is ignored.
Closing remarks ("stop", "never mind", "thanks, that's all", "bye") end the conversation without asking
Hermes. Everything else is a request."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable

import numpy as np

from app.voice.speech_text import strip_wake_words

log = logging.getLogger("xenio.voice.turns")

# A request that ends on one of these words is almost certainly not finished yet.
_TRAILING_WORDS = frozenset(
    "and or but so because the a an to of for with in on at from into onto about my your his her their our its "
    "this these those um uh umm uhm er erm hmm if then also plus like is are was can could would should will "
    "some any which than as by let let's i i'm we".split()
)
_TRAILING_PUNCTUATION = (",", "...", "…", "-", "–", "—", ":")

# Whisper "hears" these in silence, music or noise (well-known artefacts of its training data).
_NOISE = frozenset({
    "", "you", "thanks for watching", "thank you for watching", "thanks for watching and see you next time",
    "please subscribe", "subscribe", "like and subscribe", "music", "applause", "laughter", "silence",
    "blank_audio", "blank audio", "inaudible", "foreign", "hmm", "mm", "mhm", "uh", "um", "ah", "oh",
})
_NOISE_PREFIXES = ("subtitles by", "subtitled by", "transcribed by", "captions by", "amara.org")

# Closing remarks: the user is done (or wants XENIO to stop). Matched on the whole utterance only, so
# "stop the music" or "no, open the other one" remain requests.
_THANKS = r"(?:thanks?|thank you)(?: (?:so|very) much| a lot| again)?"
_DONE = r"(?:that'?s (?:all|it)|that is all|i'?m (?:good|done)|all good|we'?re done)"
_STOP = (r"(?:stop(?: it| talking| listening)?|never ?mind|forget it|cancel(?: that)?|shut up|be quiet|quiet|"
         r"enough|go to sleep|sleep|leave it|that'?s enough)")
_BYE = r"(?:bye(?: bye)?|goodbye|good night|see you(?: later)?|later|talk (?:to you )?later)"
# "okay", "sure", "no" also answer a question - they only close the conversation when XENIO asked none
_SOFT = r"(?:ok(?:ay)?|cool|great|perfect|nice|awesome|alright|all right|got it|sure|fine|no(?:pe)?|nothing(?: else)?)"
_CLOSER = re.compile(rf"^(?:(?:{_THANKS}|{_DONE}|{_STOP}|{_BYE}|{_SOFT})(?: |$))+$")
_HAS_HARD = re.compile(rf"(?:^| )(?:{_THANKS}|{_DONE}|{_STOP}|{_BYE})(?: |$)")
_REPLY_FOR = (  # each reply is pre-synthesized (app/voice/live.py SHORT_REPLIES)
    (re.compile(rf"\b(?:{_BYE})\b"), "Bye for now."),
    (re.compile(rf"\b(?:{_THANKS})\b"), "Anytime."),
)


def _words(text: str) -> str:
    """Lower-case words only: punctuation, brackets and music symbols removed, apostrophes kept."""
    text = re.sub(r"[^\w\s']", " ", text.lower().replace("’", "'"))
    return re.sub(r"\s+", " ", text).strip()


def sounds_unfinished(text: str, name: str = "Xenio") -> bool:
    """True when a transcript stops mid-thought: trailing "and"/"the"/"um", a trailing comma or dash, or
    nothing yet except the wake word."""
    stripped = text.strip()
    if not stripped or not _words(strip_wake_words(stripped, name)):
        return True
    if stripped.endswith(("?", "!")):
        return False
    if stripped.endswith(_TRAILING_PUNCTUATION):
        return True
    last = _words(stripped).rsplit(" ", 1)[-1]
    return last in _TRAILING_WORDS


def classify_utterance(text: str, name: str = "Xenio", *, after_question: bool = False) -> str:
    """"noise" (ignore it), "closer" (end the conversation) or "request" (send it to Hermes).
    ``after_question``: XENIO's last reply asked something, so "okay", "sure" or "no" is an answer."""
    words = _words(text)
    if words in _NOISE or words.startswith(_NOISE_PREFIXES) or not any(ch.isalnum() for ch in words):
        return "noise"
    core = _words(strip_wake_words(text, name))
    # the name can also come last ("stop Xenio", "thanks Xenio") or be the only word ("Xenio" = nothing yet)
    core = re.sub(rf"(?:^| )(?:hey )?[xz]{re.escape(name.lower()[1:-1])}\w?$", "", core).strip()
    if not core:
        return "noise"
    if _CLOSER.match(core) and not (after_question and not _HAS_HARD.search(core)):
        return "closer"
    return "request"


def closing_reply(text: str) -> str:
    """What XENIO says when the user closes the conversation."""
    words = _words(text)
    if words.startswith(("no ", "nope ")):  # "no thanks"
        return "Okay."
    for pattern, reply in _REPLY_FOR:
        if pattern.search(words):
            return reply
    return "Okay."


class EarlyTranscriber:
    """Transcribes at the first pause instead of after the turn, and ends the turn as soon as the user has
    clearly finished (see the module docstring).

    ``update(recorder)`` is called after every microphone frame the recorder takes and returns True when
    the turn should end now. ``result(recorder, audio)`` returns the transcript of the finished turn, reusing
    the early transcript when nothing was said after it."""

    def __init__(self, transcribe: Callable[[np.ndarray], Awaitable[str]], *, name: str = "Xenio",
                 pause_seconds: float = 0.25, end_seconds: float = 0.55, patience_seconds: float = 2.0,
                 max_requests: int = 4):
        self._transcribe = transcribe
        self._name = name
        self.pause_seconds = pause_seconds  # silence that starts an early transcript
        self.end_seconds = end_seconds  # minimum silence before a finished-sounding turn ends
        self.patience_seconds = patience_seconds  # silence that ends any turn (set on the recorder)
        self.max_requests = max_requests
        self.requests = 0
        self.ended_early = False
        self._task: asyncio.Task[str] | None = None
        self._mark = -1  # the recorder's last-speech index when the early transcript was started

    def update(self, recorder) -> bool:
        if not recorder.started:
            return False
        mark, quiet = recorder.speech_mark, recorder.quiet_seconds
        if self._task is not None and mark != self._mark:  # the user carried on talking: that text is stale
            self._task.cancel()
            self._task = None
        if self._task is None and quiet >= self.pause_seconds and self.requests < self.max_requests:
            audio = recorder.audio()
            if audio is not None:
                self._mark = mark
                self.requests += 1
                self._task = asyncio.ensure_future(self._transcribe(audio))
        task = self._task
        if task is not None and task.done() and not task.cancelled() and task.exception() is None:
            text = task.result()
            if quiet >= (self.patience_seconds if sounds_unfinished(text, self._name) else self.end_seconds):
                self.ended_early = quiet < self.patience_seconds
                return True
        return False

    async def result(self, recorder, audio: np.ndarray) -> str:
        task, self._task = self._task, None
        if task is not None and self._mark == recorder.speech_mark:
            try:
                return await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - the early request failed; transcribe the final audio instead
                log.debug("Early transcript failed; transcribing the whole turn", exc_info=True)
        elif task is not None:
            task.cancel()
        return await self._transcribe(audio)

    def cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
