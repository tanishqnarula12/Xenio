"""Voice mode: microphone -> local wake word -> record -> STT -> Hermes -> TTS -> speaker.

A single listener task owns the microphone and routes every 80 ms frame by state:

    IDLE                 -> wake-word detector (local; nothing leaves the machine)
    RECORDING            -> utterance recorder (the only audio ever sent to STT)
    THINKING / SPEAKING  -> stop-phrase detector ("xenio stop" = barge-in)

Barge-in stops playback immediately and cancels the Hermes turn if one is running.
"""

from __future__ import annotations

import asyncio
import collections
import enum
import logging
import re
import time
from collections.abc import Awaitable, Callable

import numpy as np

from app.config import Settings
from app.logging_setup import event
from app.orchestrator import MSG_HERMES_UNAVAILABLE, Orchestrator, claims_outcome, describe_actions
from app.safety.permissions import NO_CLEAR_ANSWER, PermissionGate, PermissionRequest, interpret_answer, is_new_request
from app.sources import describe_sources
from app.voice.audio import (
    SAMPLE_RATE,
    AudioError,
    LiveSpeaker,
    Microphone,
    MicrophoneUnavailable,
    Speaker,
    TalkOverDetector,
    UtteranceRecorder,
    decode_wav,
    encode_wav,
    rms,
    speech_threshold,
    wake_chime,
)
from app.voice.speech_text import (
    normalize_spoken_filenames,
    split_for_tts,
    strip_interrupt_words,
    strip_wake_words,
    to_speakable,
)
from app.voice.live import FillerBank, ReplyStream, SpeechQueue, narration_for
from app.voice.local_stt import HybridSTT, create_local_stt, fix_names
from app.voice.stt import STTError, STTProvider, create_stt
from app.voice.tts import TTSError, TTSProvider, create_tts
from app.voice.turns import EarlyTranscriber, classify_utterance, closing_reply
from app.voice.vad import create_vad
from app.voice.wakeword import WakeWordDetector, WakeWordError, create_detectors

log = logging.getLogger("xenio.voice")

MSG_STT_FAILED = "Sorry, I didn't catch that."
MSG_NO_VOICE_FOR_LANGUAGE = "I can't speak German with my voice, so my answer is on your screen."

# Appended to spoken requests so Hermes answers like a voice assistant, not a document.
VOICE_CONTEXT = (
    "[XENIO voice mode: the user is speaking to you and will hear your reply read aloud as you write it, so "
    "speed matters. Answer general-knowledge questions, arithmetic and chit-chat directly from what you know, "
    "without tools or skills; use a tool only when the answer needs the user's own data, current information or "
    "an action. Start with the answer itself in a short first sentence, then at most two more short, natural "
    "spoken sentences, with no lists, tables or markdown, unless they explicitly ask for detail.]"
)


def _echoes_prompt(text: str, prompt: str) -> bool:
    """Whisper sometimes returns (part of) its vocabulary prompt for audio without words."""
    words = re.findall(r"\w+", text.lower())
    return len(words) >= 3 and " ".join(words) in " ".join(re.findall(r"\w+", prompt.lower()))


class State(enum.Enum):
    IDLE = "idle"
    RECORDING = "recording"
    TRANSCRIBING = "transcribing"
    THINKING = "thinking"
    SPEAKING = "speaking"


class VoiceAssistant:
    def __init__(
        self,
        settings: Settings,
        orchestrator: Orchestrator,
        stt: STTProvider,
        tts: TTSProvider | None,
        mic: Microphone,
        speaker: Speaker,
        wake_detector: WakeWordDetector | None,
        stop_detector: WakeWordDetector | None,
        out: Callable[[str], None] = print,
        read_line: Callable[[str], Awaitable[str | None]] | None = None,
    ):
        self.settings = settings
        self.name = settings.name.upper()
        self.orchestrator = orchestrator
        self.stt = stt
        self.tts = tts
        self.mic = mic
        self.speaker = speaker
        self._wake = wake_detector
        self._stop = stop_detector
        self.out = out
        self._read_line = read_line  # push-to-talk: async callable(prompt) -> str | None
        self._state = State.IDLE
        self._recorder: UtteranceRecorder | None = None
        self._record_done = asyncio.Event()
        self._wake_event = asyncio.Event()
        self._wake_recorder: UtteranceRecorder | None = None
        self._fatal: Exception | None = None
        self.interrupted = False
        self._tts_warned = False
        # Level tracking while idle: room noise floor, and the last ~1.6 s of frame levels so the
        # speech threshold can be calibrated from how loudly the wake phrase was just said.
        self._noise_floor = 30.0
        self._recent_levels: collections.deque[float] = collections.deque(maxlen=20)
        self._idle_frames: collections.deque[np.ndarray] = collections.deque(maxlen=6)
        self._vocabulary: collections.deque[str] = collections.deque(maxlen=12)  # recent file/folder names
        self._threshold: float | None = settings.voice.speech_threshold
        # Barge-in: a recorder started the instant the user interrupted, the echo-aware talk-over
        # detector, and a guard so XENIO saying its own name cannot interrupt itself.
        self._interrupt_recorder: UtteranceRecorder | None = None
        self.last_interrupt: tuple[str, float] | None = None
        self._talkover = TalkOverDetector(enabled=settings.voice.talk_over)
        self._name_guard_until = 0.0
        first, rest = settings.name[:1].lower(), re.escape(settings.name[1:].lower())
        self._name_pattern = re.compile(rf"\b{'[xz]' if first in 'xz' else re.escape(first)}{rest}\b", re.IGNORECASE)
        # V4: notifications from scheduled checks, spoken while idle (set by run_voice_mode).
        self.notifications = None
        self.proactive = None
        self._announcing = False
        # Real time (set by run_voice_mode): Silero VAD endpointing, cached acknowledgments/narration, and the
        # speech output of the turn in progress.
        self.vad = None
        self.fillers: FillerBank | None = None
        self._output: SpeechQueue | None = None
        self._reply: ReplyStream | None = None
        self.last_turn_timing: dict[str, float] = {}
        # Turn-taking: the early transcriber of the recording in progress / of the one just finished, the last
        # ~0.5 s heard while busy (the start of an interruption), and whether XENIO's last reply asked something.
        self._turn: tuple[UtteranceRecorder, EarlyTranscriber] | None = None
        self._done_turn: tuple[UtteranceRecorder, EarlyTranscriber] | None = None
        self._busy_frames: collections.deque[np.ndarray] = collections.deque(maxlen=6)
        self._asked = False
        self.busy_stop: WakeWordDetector | None = None  # V5.5: bare "stop"/"abort"/"cancel" while working silently
        self._unspeakable_warned = False
        self.held_request: str | None = None  # said in answer to a yes/no question, handled after this turn

    # ---- state & frame routing ----------------------------------------------

    @property
    def state(self) -> State:
        return self._state

    def set_state(self, state: State) -> None:
        if state is self._state:
            return
        if state is State.IDLE and self._wake is not None:
            self._wake.reset()  # drop audio buffered while XENIO itself was talking
        if state in (State.THINKING, State.SPEAKING) and self._state not in (State.THINKING, State.SPEAKING):
            # All listen for interruptions while busy. Each one still holds the audio of its LAST busy period (measured:
            # the "stop" that ended one request, left in busy_stop, cancelled the next request 1 s after it began).
            for detector in (self._stop, self._wake, self.busy_stop):
                if detector is not None:
                    detector.reset()
        log.debug("Voice state %s -> %s", self._state.value, state.value)
        self._state = state

    async def _listen_loop(self) -> None:
        while True:
            try:
                frame = await self.mic.read()
            except MicrophoneUnavailable as exc:
                self._fatal = exc
                self._wake_event.set()
                self._record_done.set()
                return
            self.feed_frame(frame)

    def feed_frame(self, frame: np.ndarray) -> None:
        recorder = self._recorder
        if recorder is not None:
            # XENIO's own beep (and its echo) must not count as the user starting to speak.
            echo = getattr(self.speaker, "echo_possible", None)
            was_started = recorder.started
            done = recorder.feed(frame, can_start=not (echo() if echo else False))
            if recorder.started and not was_started:
                self._warm_up()  # the user started talking: open the connections the reply will need
            turn = self._turn
            if not done and turn is not None and turn[0] is recorder and turn[1].update(recorder):
                recorder.finish()  # the user has finished a sentence and the transcript is already here
                done = True
            if done:
                self._recorder = None
                self._record_done.set()
            return
        if self._state is State.IDLE:
            self._track_level(rms(frame))
            self._idle_frames.append(frame)
            if self._wake is not None and self._wake.process(frame):
                # Capture from the very next frame: words said right after the wake phrase count.
                if self.settings.voice.speech_threshold is None:
                    self._threshold = speech_threshold(max(self._recent_levels, default=0.0), self._noise_floor)
                event(log, "Wake word detected", wake_peak=int(max(self._recent_levels, default=0)),
                      noise=int(self._noise_floor), threshold=int(self._threshold or 0))
                self._warm_up()
                self.set_state(State.RECORDING)
                self._wake_recorder = self._begin_recording()
                # The detector fires ~0.3 s after the phrase ends; the command may already have begun.
                self._wake_recorder.seed(list(self._idle_frames)[-4:])
                self._idle_frames.clear()
                self._wake_event.set()
        elif self._state in (State.THINKING, State.SPEAKING) and self.settings.voice.barge_in and not self.interrupted:
            self._busy_frames.append(frame)
            self._listen_for_interrupt(frame)

    def _warm_up(self) -> None:
        """Speech recognition is needed in a second, the voice a few seconds later: connect to both now."""
        for provider in (self.stt, self.tts):
            warm = getattr(provider, "warm", None)
            if warm is not None:
                warm()

    def _listen_for_interrupt(self, frame: np.ndarray) -> None:
        """While XENIO thinks or talks: "Xenio, stop", "stop Xenio", or just "Xenio" interrupts.
        Keyword spotting is used because it survives the speaker echo (measured: 12/12 detections
        at the user's level over real echo, 0 false triggers on echo alone)."""
        if self._stop is not None and (phrase := self._stop.process(frame)):
            self.barge_in(phrase)
            return
        if self._wake is not None and (phrase := self._wake.process(frame)):
            if time.monotonic() < self._name_guard_until:
                log.debug("Ignored the name while XENIO itself was saying it")
            else:
                self.barge_in(phrase)
            return
        # Talking over XENIO: only while its voice is actually coming out of the speaker (the echo it learns from
        # must be real playback, not the silence while Hermes thinks), and only for speech, not any loud sound.
        playing = getattr(self.speaker, "playing", True)
        if self.busy_stop is not None and not playing and (phrase := self.busy_stop.process(frame)):
            self.barge_in(phrase)  # V5.5: a bare "stop" while XENIO works silently (opening apps, clicking, typing)
            return
        if self._state is State.SPEAKING and playing:
            speech = self.vad.is_speech(frame) if self.vad is not None and self._talkover.enabled else None
            if self._talkover.update(rms(frame), self._threshold or 100.0, speech):
                self.barge_in("talking over")

    def _track_level(self, level: float) -> None:
        self._recent_levels.append(level)
        # The floor follows quiet frames quickly and loud ones very slowly, so speech never raises it.
        weight = 0.3 if level < self._noise_floor else 0.005
        self._noise_floor += weight * (level - self._noise_floor)

    def barge_in(self, phrase: str = "stop") -> None:
        """Stop talking/thinking now and start capturing whatever the user says next."""
        if self.interrupted:
            return
        event(log, "Barge-in", phrase=phrase, state=self._state.value)
        self.interrupted = True
        self.last_interrupt = (phrase, time.monotonic())
        from app.desktop.stop import request_stop  # V5.5: also stops XENIO's hands (mouse/keyboard) immediately

        request_stop("voice", self.settings.data_dir)
        was_thinking = self._state is State.THINKING or getattr(self.orchestrator, "busy", False)
        if self._output is not None:
            self._output.stop()  # also drops sentences still waiting to be spoken
        self.speaker.stop()
        if was_thinking:  # with streamed replies Hermes may still be writing while XENIO talks
            asyncio.ensure_future(self.orchestrator.interrupt())
        self.out("[interrupted]")
        if self._recorder is None:  # not in the middle of a spoken confirmation
            v = self.settings.voice
            self.set_state(State.RECORDING)
            self._interrupt_recorder = self._begin_recording(start_timeout=max(v.follow_up_seconds, v.start_timeout))
            # what the user said while interrupting ("no, I meant...") must not be lost
            self._interrupt_recorder.seed(list(self._busy_frames))
        self._busy_frames.clear()

    # ---- building blocks ------------------------------------------------------

    def _begin_recording(self, start_timeout: float | None = None) -> UtteranceRecorder:
        """Route microphone frames to a new recorder starting with the next frame.

        With the VAD and early transcription the turn ends when the user has finished a sentence (see
        ``app/voice/turns.py``); the recorder itself only stops after the longer "patience" silence."""
        v = self.settings.voice
        early = v.early_transcribe and self.vad is not None and self.stt is not None
        if early:
            silence = v.turn_patience_seconds
        else:
            silence = v.vad_silence_seconds if self.vad is not None else v.silence_seconds
        recorder = UtteranceRecorder(
            silence_seconds=silence,
            max_seconds=v.max_record_seconds,
            start_timeout=start_timeout or v.start_timeout,
            noise_floor=self._noise_floor,
            threshold=self._threshold,
            vad=self.vad,
        )
        if self._turn is not None:
            self._turn[1].cancel()
        self._turn = (recorder, EarlyTranscriber(self.transcribe, name=self.settings.name,
                                                 end_seconds=v.vad_silence_seconds,
                                                 patience_seconds=v.turn_patience_seconds)) if early else None
        self._record_done = asyncio.Event()
        self._recorder = recorder
        return recorder

    async def _finish_recording(self, recorder: UtteranceRecorder) -> np.ndarray | None:
        v = self.settings.voice
        try:
            await asyncio.wait_for(self._record_done.wait(), v.max_record_seconds + recorder.start_timeout + 5)
        except TimeoutError:
            pass
        finally:
            if self._recorder is recorder:
                self._recorder = None
        if self._fatal is not None:
            raise self._fatal
        turn = self._turn if self._turn is not None and self._turn[0] is recorder else None
        if turn is not None:
            self._turn = None
        audio = recorder.audio()
        if self._done_turn is not None:
            self._done_turn[1].cancel()
        self._done_turn = turn if audio is not None else None
        if turn is not None and audio is None:
            turn[1].cancel()
        event(log, "Recording finished", **recorder.stats(),
              **({"early_requests": turn[1].requests, "ended_early": turn[1].ended_early} if turn else {}))
        return audio

    async def record_utterance(self) -> np.ndarray | None:
        return await self._finish_recording(self._begin_recording())

    async def transcribe(self, audio: np.ndarray) -> str:
        names = ", ".join([self.settings.name, *reversed(self._vocabulary)])
        hints = f"{names}. File names are written like notes.txt or report.pdf."
        text = (await self.stt.transcribe(encode_wav(audio, SAMPLE_RATE), prompt=hints)).strip()
        if _echoes_prompt(text, hints):
            return ""  # Whisper repeats its prompt when it hears no words
        return fix_names(text, self.settings.name, tuple(self._vocabulary))

    async def transcribe_utterance(self, audio: np.ndarray) -> str:
        """The transcript of a just-recorded utterance: usually already there from the early transcript."""
        turn, self._done_turn = self._done_turn, None
        if turn is not None:
            return await turn[1].result(turn[0], audio)
        return await self.transcribe(audio)

    def learn_names(self, tool_calls: list) -> None:
        """Remember file/folder names XENIO just worked with, so speech recognition spells them the
        same way next time ("XenioTest", not "Xenio test"; "hello.txt", not "HelloTXT")."""
        for call in tool_calls:
            title = getattr(call, "title", "") or ""
            for path in re.findall(r"(?:[A-Za-z]:|~)?[\\/][^\"'|;&]+", title):
                for part in re.split(r"[\\/]", path.strip())[-2:]:
                    part = part.strip()
                    if 2 < len(part) <= 40 and not re.fullmatch(r"[A-Za-z]:|Users|OneDrive|Desktop|Documents", part):
                        if part in self._vocabulary:
                            self._vocabulary.remove(part)
                        self._vocabulary.append(part)

    async def speak(self, text: str) -> bool:
        """Speak ``text``. Returns False if interrupted or if voice output failed."""
        if self.tts is None:
            return False
        can_speak = getattr(self.tts, "can_speak", None)
        if can_speak is not None and text.strip() and not can_speak(to_speakable(text)):
            spoken: list[str] = []
            self._cannot_speak(spoken.append)
            text = spoken[0] if spoken else ""
            if not text:
                return True
        segments = split_for_tts(to_speakable(text))
        if not segments:
            return True
        pending: asyncio.Task[bytes] | None = asyncio.ensure_future(self.tts.synthesize(segments[0]))
        self._talkover.begin_playback()
        try:
            for index in range(len(segments)):
                assert pending is not None
                audio = await pending
                # Synthesize the next segment while this one plays.
                pending = asyncio.ensure_future(self.tts.synthesize(segments[index + 1])) if index + 1 < len(segments) else None
                if self.interrupted:
                    return False
                pcm, rate = decode_wav(audio)
                says_name = bool(self._name_pattern.search(segments[index]))
                if says_name:
                    self._name_guard_until = float("inf")
                try:
                    if not await self.speaker.play(pcm, rate):
                        return False
                finally:
                    if says_name:  # its echo can arrive a little after playback ends
                        self._name_guard_until = time.monotonic() + 1.0
            return True
        except (TTSError, AudioError) as exc:
            event(log, "Voice output failed", logging.ERROR, error=str(exc))
            if not self._tts_warned:
                self.out(f"(voice output unavailable: {exc})")
                self._tts_warned = True
            return False
        finally:
            self._talkover.end_playback(interrupted=self.interrupted)
            if pending is not None and not pending.done():
                pending.cancel()

    async def say(self, text: str) -> None:
        self.out(f"{self.name}: {text}")
        await self.speak(text)

    async def announce_notifications(self, _new=None) -> None:
        """Speak undelivered notifications - only while idle, never over a conversation (they wait)."""
        if self.notifications is None or self._announcing or self.state is not State.IDLE:
            return
        self._announcing = True
        try:
            for note in await asyncio.to_thread(self.notifications.pending, 5):
                if self.state is not State.IDLE:
                    break
                self.out(note.line())
                await asyncio.to_thread(self.notifications.mark_delivered, [note.id])
                await self.speak(f"Notification: {note.spoken()}")
        finally:
            self._announcing = False

    async def _earcon(self) -> None:
        try:
            await self.speaker.play(wake_chime(), 24_000)
        except AudioError:
            pass

    # ---- one exchange ---------------------------------------------------------

    async def handle_utterance(self, audio: np.ndarray, *, follow_up: bool = False) -> bool:
        """One exchange. Returns False when the user closed the conversation ("thanks, that's all", "stop").
        ``follow_up``: heard without the wake word, so sounds that are not words are ignored silently."""
        self.interrupted = False
        self.set_state(State.TRANSCRIBING)
        try:
            heard = await self.transcribe_utterance(audio)
        except STTError as exc:
            event(log, "STT failed", logging.ERROR, error=str(exc))
            self.set_state(State.SPEAKING)
            await self.say(MSG_STT_FAILED)
            return True
        return await self.handle_heard(heard, follow_up=follow_up)

    async def handle_heard(self, heard: str, *, follow_up: bool = False) -> bool:
        """One exchange from the words heard (also a request said while XENIO was asking a yes/no question)."""
        if any(ch.isalnum() for ch in heard) and not strip_wake_words(heard, self.settings.name):
            self.out(f"\nYou: {heard}")
            return True  # only the wake word: the request is still to come, keep listening
        kind = classify_utterance(heard, self.settings.name, after_question=self._asked)
        if kind == "noise":
            event(log, "Ignored a sound that was not a request", chars=len(heard), follow_up=follow_up)
            if not follow_up:
                self.set_state(State.SPEAKING)
                await self._say_quick(MSG_STT_FAILED)
            return True
        self.out(f"\nYou: {normalize_spoken_filenames(heard)}")
        if kind == "closer":
            self._asked = False
            self.set_state(State.SPEAKING)
            await self._say_quick(closing_reply(heard))
            return False
        # After an interruption the capture includes the keyword ("Xenio, stop. What about...").
        text = normalize_spoken_filenames(strip_interrupt_words(strip_wake_words(heard, self.settings.name)))
        if not text:
            return True  # only "stop" after an interruption - nothing to send, keep listening

        self.set_state(State.THINKING)
        context = VOICE_CONTEXT if self.settings.voice.brief_replies else None
        if self.settings.voice.stream_replies and self.tts is not None:
            await self._respond_live(text, context)
            return True
        response = await self.orchestrator.process_text(text, context=context)
        if self.interrupted:
            return True  # barge_in already said "[interrupted]" and is capturing the next words
        if response.cancelled:
            self.out(f"{self.name}: [stopped]")
            return True
        self.set_state(State.SPEAKING)
        self.out(f"{self.name}: {response.text}")
        for line in response.action_lines() + describe_sources(response.sources):
            self.out(line)
        self.learn_names(response.tool_calls)
        self._asked = response.text.rstrip().endswith("?")
        await self.speak(response.text)
        return True

    async def _say_quick(self, text: str) -> None:
        """A short phrase, instantly from the cache when it has been pre-synthesized."""
        self.out(f"{self.name}: {text}")
        cached = self.fillers.get(text) if self.fillers is not None else None
        if cached is None:
            await self.speak(text)
            return
        try:
            await self.speaker.play(cached[0], cached[1])
        except AudioError as exc:
            event(log, "Voice output failed", logging.ERROR, error=str(exc))

    async def _respond_live(self, text: str, context: str | None) -> None:
        """Real-time reply: speak each sentence as Hermes writes it, say a short acknowledgment if the answer
        takes a moment, narrate what XENIO is doing ("Checking your calendar."), and hold back any sentence
        that claims an outcome until it has been checked against the tool results."""
        v = self.settings.voice
        sent = time.monotonic()
        out = SpeechQueue(self.tts, self.speaker, self.fillers, on_item=self._guard_name, on_first_audio=self._first_audio,
                          on_error=self._voice_failed)
        out.on_unspeakable = lambda _text: self._cannot_speak(out.say)
        reply = ReplyStream(out.say, claims_outcome)
        self._output, self._reply = out, reply
        said = {"fillers": 0, "last": 0.0, "narrated": set()}

        def filler(phrase: str | None) -> None:
            """A cached phrase, only before the real answer has started, at most two per turn."""
            if (not phrase or self.fillers is None or self.fillers.get(phrase) is None or reply.spoken
                    or said["fillers"] >= 2 or self.interrupted or time.monotonic() - said["last"] < 1.5):
                return
            said["fillers"] += 1
            said["last"] = time.monotonic()
            out.say(phrase)

        def on_tool(activity) -> None:
            if getattr(activity, "status", "") == "failed":
                reply.hold()  # what follows a failure waits for the final check
            phrase = narration_for(getattr(activity, "title", "") or "")
            if v.acknowledge and phrase and phrase not in said["narrated"] and getattr(activity, "status", "") in (
                    "pending", "in_progress", "", None):
                said["narrated"].add(phrase)
                filler(phrase)

        async def acknowledge() -> None:
            await asyncio.sleep(v.ack_delay_seconds)
            if out.queued == 0 and self.fillers is not None:
                filler(self.fillers.acknowledgment())

        ack = asyncio.ensure_future(acknowledge()) if v.acknowledge else None
        try:
            response = await self.orchestrator.process_text(text, on_chunk=reply.feed, context=context, on_tool=on_tool)
        finally:
            if ack is not None:
                ack.cancel()
        try:
            if self.interrupted:
                out.stop()
                return  # barge_in already said "[interrupted]" and is capturing the next words
            if response.cancelled:
                out.stop()
                self.out(f"{self.name}: [stopped]")
                return
            for sentence in reply.finish(response.text, response.ok):
                out.say(sentence)
            self._asked = response.text.rstrip().endswith("?")
            self.out(f"{self.name}: {response.text}")
            for line in response.action_lines() + describe_sources(response.sources):
                self.out(line)
            self.learn_names(response.tool_calls)
            await out.drain()
            first = (out.first_audio_at - sent) if out.first_audio_at else None
            self.last_turn_timing = {"first_audio_s": round(first, 2) if first is not None else -1,
                                     "reply_s": round(time.monotonic() - sent, 2)}
            event(log, "Voice turn timing", **self.last_turn_timing, fillers=said["fillers"],
                  streamed_sentences=len(reply.spoken), held=len(reply.held))
        finally:
            self._talkover.end_playback(interrupted=self.interrupted)
            self._output = self._reply = None

    def _cannot_speak(self, say) -> None:
        """A reply in a language no installed voice can pronounce (German): shown, not garbled; said once why."""
        if self._unspeakable_warned:
            return
        self._unspeakable_warned = True
        self.out("(No German voice is installed, so German answers are shown on screen instead of spoken.)")
        say(MSG_NO_VOICE_FOR_LANGUAGE)

    def _voice_failed(self, exc: Exception) -> None:
        event(log, "Voice output failed", logging.ERROR, error=str(exc))
        if not self._tts_warned:
            self.out(f"(voice output unavailable: {exc})")
            self._tts_warned = True

    def _first_audio(self) -> None:
        self.set_state(State.SPEAKING)
        self._talkover.begin_playback()

    def _guard_name(self, text: str, started: bool) -> None:
        """XENIO saying its own name must not trigger a barge-in through the speakers."""
        if self._name_pattern.search(text):
            self._name_guard_until = float("inf") if started else time.monotonic() + 1.0

    async def drain_output(self) -> None:
        """Let XENIO finish what it is saying (before a spoken confirmation question)."""
        if self._output is not None:
            await self._output.drain()

    # ---- main loop --------------------------------------------------------------

    async def _wait_for_trigger(self) -> UtteranceRecorder | None:
        """Block until the user wants to talk; return the recorder already capturing. None = quit."""
        if self._read_line is not None:
            line = await self._read_line("\n[Press Enter and speak, or type /exit] ")
            if line is None or line.strip().lower() in {"/exit", "/quit", "exit", "quit"}:
                return None
            self.set_state(State.RECORDING)
            return self._begin_recording()
        self._wake_event.clear()
        self._wake_recorder = None
        await self._wake_event.wait()
        if self._fatal is not None:
            raise self._fatal
        assert self._wake_recorder is not None  # started by feed_frame on the wake frame
        return self._wake_recorder

    async def run(self) -> int:
        self.out("Connecting to Hermes (the first start can take ~30 s)...")
        if not await self.orchestrator.start():
            await self.say(MSG_HERMES_UNAVAILABLE)
            self.out("Run `python -m app.main --health` for details.")
            return 1
        try:
            await self.mic.start()
        except MicrophoneUnavailable as exc:
            self.out(f"{self.name}: I can't access the microphone ({exc}).")
            self.out("MANUAL STEP REQUIRED: allow microphone access (Windows Settings > Privacy & security > Microphone).")
            await self.orchestrator.close()
            return 1

        background = (asyncio.ensure_future(self.proactive.run_forever(deliver=self.announce_notifications))
                      if self.proactive is not None else None)
        how = "Press Enter to talk" if self._read_line else f"Say \"{self.settings.voice.wake_phrase}\""
        stop_hint = f" Say \"{self.settings.voice.stop_phrases[0]}\" to interrupt." if self._stop and self.settings.voice.stop_phrases else ""
        self.out(f"{self.name} is listening. {how}.{stop_hint} Ctrl+C quits.")
        try:
            return await self.converse()
        except MicrophoneUnavailable as exc:
            self.out(f"{self.name}: the microphone stopped working ({exc}).")
            return 1
        finally:
            if background is not None:
                background.cancel()
            self.speaker.stop()
            if hasattr(self.speaker, "close"):
                self.speaker.close()
            await self.mic.stop()
            await self.stt.aclose()
            if self.tts is not None:
                await self.tts.aclose()
            await self.orchestrator.close()

    async def converse(self) -> int:
        """The conversation on an already started microphone: wait for the wake word (or Enter), record, answer,
        keep listening for a follow-up. Returns 0 when the user quits; a lost microphone raises."""
        listener = asyncio.ensure_future(self._listen_loop())
        warming = asyncio.ensure_future(self.fillers.warm()) if self.fillers is not None else None
        if hasattr(self.speaker, "warm"):
            self.speaker.warm()  # open the persistent output now: the first reply starts without delay
        self._warm_up()
        follow_up_s = self.settings.voice.follow_up_seconds if self._read_line is None else 0.0
        in_conversation = False
        try:
            while True:
                interrupt_recorder, self._interrupt_recorder = self._interrupt_recorder, None
                follow_up = interrupt_recorder is None and in_conversation
                if interrupt_recorder is not None or in_conversation:
                    # Conversation mode: after a reply (or an interruption) keep listening, no wake word.
                    self.set_state(State.RECORDING)
                    self.out("(listening...)")
                    recorder = interrupt_recorder or self._begin_recording(start_timeout=follow_up_s)
                    audio = await self._finish_recording(recorder)
                    if audio is None:
                        in_conversation = False
                        self.out(f"(Say \"{self.settings.voice.wake_phrase}\" when you need me.)")
                        continue
                else:
                    self.set_state(State.IDLE)
                    await self.announce_notifications()  # what scheduled checks found while we were busy
                    recorder = await self._wait_for_trigger()
                    if recorder is None:
                        break
                    self.out("(listening...)")
                    beep = asyncio.ensure_future(self._earcon())  # plays while the recorder already runs
                    audio = await self._finish_recording(recorder)
                    await beep
                    if audio is None:
                        self.out("(I didn't hear anything.)")
                        continue
                keep_talking = await self.handle_utterance(audio, follow_up=follow_up)
                while keep_talking and self.held_request:
                    held, self.held_request = self.held_request, None
                    self.interrupted = False
                    keep_talking = await self.handle_heard(held, follow_up=True)
                in_conversation = follow_up_s > 0 and keep_talking
                if not keep_talking and follow_up_s > 0:
                    self.out(f"(Say \"{self.settings.voice.wake_phrase}\" when you need me.)")
            return 0
        finally:
            listener.cancel()
            if warming is not None:
                warming.cancel()


class VoiceConfirmer:
    """Asks the user out loud before a dangerous action and listens for the answer."""

    def __init__(self, assistant: VoiceAssistant):
        self._a = assistant

    async def confirm(self, request: PermissionRequest, *, strict: bool) -> bool:
        """Yes -> continue, No -> cancel, anything else -> "Please confirm yes or no." (up to 3 tries).
        Silence, errors and unrelated sentences are never a yes."""
        a = self._a
        await a.drain_output()  # finish "Checking your calendar." etc. before asking
        if a._reply is not None:
            a._reply.hold()  # what Hermes says after this question waits for the final check
        a.out(f"\n!! PERMISSION REQUIRED [{request.risk.name}]\n   This will {request.description}.")
        spoken = request.description if len(request.description) <= 160 else request.description[:157] + "..."
        if strict:
            prompt = f"This is a high-risk action. This will {spoken}. Say confirm to continue, or no to cancel."
            reprompt = "Please say confirm, or no."
        else:
            prompt = f"This will {spoken}. Do you want me to continue?"
            reprompt = "Please confirm yes or no."
        for attempt in range(3):
            if attempt:
                a.out(f"   {prompt}")
            await a.speak(prompt)
            if a.interrupted:
                a.out("   -> cancelled (interrupted)")
                return False
            audio = await a.record_utterance()
            prompt = reprompt
            if audio is None:
                a.out("   (no answer heard)")
                continue
            try:
                answer = await a.transcribe_utterance(audio)
            except STTError as exc:
                a.out(f"   (could not understand the answer: {exc})")
                continue
            words = strip_wake_words(answer, a.settings.name)
            verdict = interpret_answer(words, strict=strict)
            a.out(f"   You: {answer}")
            if verdict != "yes" and is_new_request(words):
                a.held_request = words
                a.out("   -> cancelled; taken as your next request")
                return False if verdict == "no" else NO_CLEAR_ANSWER
            if verdict == "yes":
                a.out("   -> continuing")
                return True
            if verdict == "no":
                a.out("   -> cancelled")
                return False
        a.out("   -> cancelled (no clear answer)")
        await a.speak("I didn't get a clear answer, so I cancelled it.")
        return NO_CLEAR_ANSWER


_CALIBRATION_TEXT = "Calibrating. Please stay quiet while I say this sentence, so I can measure my own echo."
_STORY_TEXT = (
    "Here is a short story. A lighthouse keeper lived alone on a small rocky island far out at sea. "
    "Every evening he climbed one hundred and twelve steps to light the great lamp. "
    "One stormy night a small fishing boat lost its way in the fog, and only the light guided it home. "
    "The fisherman came back the next morning with fresh bread and warm soup to say thank you. "
    "From that day on, the two of them shared dinner every Sunday, and the keeper was never lonely again."
)


async def run_barge_in_test(settings: Settings) -> int:
    """Interactive check that interrupting XENIO works on this machine with the user's real voice."""
    tts = create_tts(settings.tts)
    if tts is None:
        print("TTS is not configured (TTS_API_KEY / TTS_API_URL) - the test needs XENIO to speak.")
        return 2
    v = settings.voice
    print("Preparing...")
    try:
        wake, stop = await asyncio.to_thread(
            create_detectors, v.wake_phrase, v.stop_phrases, settings.models_dir, settings.data_dir,
            v.wake_sensitivity, v.wake_name_only,
        )
    except WakeWordError as exc:
        print(f"Wake-word engine unavailable: {exc}")
        return 1
    mic, speaker = Microphone(v.input_device), Speaker(v.output_device)
    a = VoiceAssistant(settings, None, None, tts, mic, speaker, wake, stop, out=lambda _t: None)  # type: ignore[arg-type]
    a._threshold = 100.0
    captured: list[tuple[str, np.ndarray]] = []

    async def listen() -> None:
        while True:
            frame = await mic.read()
            captured.append((a.state.value, frame))
            a.feed_frame(frame)

    try:
        await mic.start()
    except MicrophoneUnavailable as exc:
        print(f"Microphone error: {exc}")
        return 1
    listener = asyncio.ensure_future(listen())
    try:
        print("\nPART 1 - stay QUIET while XENIO speaks (measuring its own echo)...")
        a.set_state(State.SPEAKING)
        await a.speak(_CALIBRATION_TEXT)
        echo_peak = int(a._talkover.echo_peak or 0)
        mode = "ARMED (no echo - you can interrupt by just talking)" if a._talkover.armed(100.0) else \
            "off (XENIO's own voice reaches the mic - interrupt with the keyword)"
        print(f"   echo: typical {int(a._talkover.echo_level or 0)}, loudest {echo_peak} -> talk-over {mode}")

        a.set_state(State.IDLE)
        a.set_state(State.SPEAKING)
        a.interrupted = False
        print("\nPART 2 - while XENIO tells a story, say  \"Xenio, stop\"  (or just \"Xenio\")  once it has started.")
        started = time.monotonic()
        mark = len(captured)
        await a.speak(_STORY_TEXT)
        story_levels = [rms(f) for state, f in captured[mark:] if state == State.SPEAKING.value]
    finally:
        listener.cancel()
        speaker.stop()
        await mic.stop()
        await tts.aclose()

    diag = settings.data_dir / "diagnostics"
    diag.mkdir(parents=True, exist_ok=True)
    wav_path = diag / "barge_in_test.wav"
    if captured:
        wav_path.write_bytes(encode_wav(np.concatenate([f for _s, f in captured]), SAMPLE_RATE))
    peak = int(max(story_levels, default=0))
    print()
    if a.interrupted and a.last_interrupt:
        phrase, at = a.last_interrupt
        print(f"PASS - XENIO stopped {at - started:.1f} s into the story after hearing \"{phrase}\".")
        return 0
    print("FAIL - XENIO talked through the whole story without hearing an interruption.")
    print(f"   Loudest sound during the story: {peak}; XENIO's own echo alone peaked at {echo_peak}.")
    if peak <= max(150, echo_peak * 1.3):
        print("   Nothing louder than XENIO's own echo reached the mic, so your voice was not picked up")
        print("   while it talked - the laptop's echo cancellation can mute you during playback.")
        print("   Fix: use headphones (then you can even interrupt by just talking), or lower the volume.")
    else:
        print("   Your voice reached the mic but the keyword was not recognised: say \"Xenio, stop\"")
        print("   clearly, or just \"Xenio\", and try again.")
    print(f"   Mic recording saved locally for diagnosis: {wav_path}")
    return 1


async def create_voice_stt(settings: Settings, announce: Callable[[str], None] = print) -> STTProvider | None:
    """The speech recognition voice conversations use: on-device first when configured, the hosted one as its backup."""
    cloud = create_stt(settings.stt)
    local = None
    if settings.voice.stt_engine == "local":
        announce("Preparing on-device speech recognition...")
        local = await asyncio.to_thread(create_local_stt, settings.models_dir)
    return HybridSTT(local, cloud) if local is not None else cloud


async def run_voice_mode(settings: Settings, orchestrator: Orchestrator, gate: PermissionGate, push_to_talk: bool,
                         notifications=None, proactive=None) -> int:
    name = settings.name.upper()
    stt = await create_voice_stt(settings, announce=print)
    if stt is None:
        print(f"{name}: speech recognition is not configured.")
        print("MANUAL STEP REQUIRED: set STT_API_KEY and STT_API_URL in .env (see docs/SETUP.md).")
        return 2
    tts = create_tts(settings.tts)
    if tts is None:
        print("TTS is not configured (TTS_API_KEY / TTS_API_URL); replies will be printed only.")

    v = settings.voice
    wake = stop = None
    print("Preparing the local wake-word engine...")
    try:
        wake, stop = await asyncio.to_thread(
            create_detectors, v.wake_phrase, v.stop_phrases, settings.models_dir, settings.data_dir,
            v.wake_sensitivity, v.wake_name_only,
        )
    except WakeWordError as exc:
        event(log, "Wake word unavailable", logging.ERROR, error=str(exc))
        if not push_to_talk:
            print(f"Wake word unavailable ({exc}); falling back to push-to-talk.")
            push_to_talk = True

    read_line = None
    if push_to_talk:
        from app.interface.console import ConsoleIO

        read_line = ConsoleIO().readline
        wake = None

    assistant = VoiceAssistant(
        settings, orchestrator, stt, tts, Microphone(v.input_device), LiveSpeaker(v.output_device), wake, stop,
        read_line=read_line,
    )
    gate.confirmer = VoiceConfirmer(assistant)
    assistant.notifications, assistant.proactive = notifications, proactive
    setup_realtime(assistant, settings)
    if v.barge_in:
        try:
            from app.voice.wakeword import create_busy_stop_detector

            assistant.busy_stop = await asyncio.to_thread(create_busy_stop_detector, settings.models_dir,
                                                          settings.data_dir, v.wake_sensitivity)
        except WakeWordError as exc:
            event(log, "Busy-stop words unavailable", logging.WARNING, error=str(exc))
    return await assistant.run()


def setup_realtime(assistant: VoiceAssistant, settings: Settings) -> None:
    """Silero VAD endpointing, hedged STT and cached acknowledgments (each can be switched off in .env)."""
    v = settings.voice
    if v.vad:
        assistant.vad = create_vad(settings.models_dir)
    if hasattr(assistant.stt, "hedge_after"):
        assistant.stt.hedge_after = v.stt_hedge_seconds
    if v.acknowledge and assistant.tts is not None:
        assistant.fillers = FillerBank(assistant.tts, settings.data_dir / "voice_cache", settings.tts.voice)
