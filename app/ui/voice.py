"""``--voice``'s talk engine inside the UI process.

The page used to record in the browser and play replies there: slow (no hedged speech recognition, the whole reply
synthesized only after it was written), no wake word, and Safari may refuse to play audio the page did not start
inside a click. Here the same engine as ``--voice`` runs on the Mac's own microphone and speaker: "Hey XENIO" (or the
page's microphone button), VAD endpointing, hedged recognition with name hints, the answer spoken sentence by sentence
while Hermes writes it, "Xenio, stop", and listening for a follow-up.

Spoken requests go through the controller's turn, so they appear in the conversation and drive the orb exactly like
typed ones (and are refused the same way while XENIO is paused or busy). The page never receives audio or a key: it
only sees the engine's phase (``AssistantState.voice``) and a level for the orb.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable

import numpy as np

from app.logging_setup import event
from app.safety.permissions import APPROVE_TASK, interpret_answer
from app.voice.assistant import State, VoiceAssistant, create_voice_stt, setup_realtime
from app.voice.audio import LiveSpeaker, Microphone, MicrophoneUnavailable, rms
from app.voice.speech_text import strip_wake_words
from app.voice.stt import STTError
from app.voice.tts import create_tts
from app.voice.wakeword import WakeWordError, create_busy_stop_detector, create_detectors

log = logging.getLogger("xenio.ui.voice")

PHASES = {State.RECORDING: "listening", State.TRANSCRIBING: "transcribing", State.SPEAKING: "speaking"}
# "yes to all", "yes, do everything", "go ahead with the whole task", "don't ask me again"
_WHOLE_TASK = re.compile(r"\b(?:all|everything|every\s+thing|whole\s+(?:task|thing)|entire\s+task|rest\s+of\s+(?:it|the|this)"
                         r"|for\s+this\s+task|don'?t\s+ask(?:\s+me)?\s+again)\b", re.I)


def wants_whole_task(answer: str) -> bool:
    return bool(_WHOLE_TASK.search(answer or ""))


class _ConversationTap:
    """What the engine sees as its orchestrator: the controller's turn (the conversation, the orb, the guard)."""

    def __init__(self, ctl):
        self._ctl = ctl

    @property
    def busy(self) -> bool:
        return self._ctl.orchestrator.busy

    async def process_text(self, text, on_chunk=None, context=None, on_tool=None):
        return await self._ctl.voice_turn(text, on_chunk=on_chunk, on_tool=on_tool, context=context)

    async def interrupt(self):
        return await self._ctl.orchestrator.interrupt()


class UiVoice(VoiceAssistant):
    def __init__(self, ctl, stt, tts, mic, speaker, wake, stop):
        # the page shows the conversation, so nothing is printed (it would put what was said in a terminal log)
        super().__init__(ctl.settings, _ConversationTap(ctl), stt, tts, mic, speaker, wake, stop, out=lambda _line: None)
        self.ctl = ctl
        self.wake_enabled = True  # the "Hey XENIO" setting; the microphone button works either way
        self._mic_level = 0.0
        self._asking = asyncio.Lock()  # one spoken approval question at a time

    @property
    def wake_available(self) -> bool:
        return self._wake is not None

    def set_state(self, state: State) -> None:
        super().set_state(state)
        phase = PHASES.get(state, "")
        if self.ctl.state.voice != phase:
            self.ctl.state.voice = phase
            self.ctl.state.voice_since = time.time() * 1000
            self.ctl.publish_state()

    def level(self) -> float:
        """0..1 for the orb: XENIO's own voice while it speaks, the microphone otherwise."""
        raw = getattr(self.speaker, "output_level", 0.0) if self._state is State.SPEAKING else self._mic_level
        return round(min(1.0, (raw / 2500.0) ** 0.6), 3)

    def feed_frame(self, frame: np.ndarray) -> None:
        self._mic_level = rms(frame)
        if self._state is State.IDLE and self._recorder is None and not self._may_wake():
            return  # paused, "Hey XENIO" switched off, or a typed request is running: nothing can wake XENIO
        super().feed_frame(frame)

    def _may_wake(self) -> bool:
        ctl = self.ctl
        return (self.wake_enabled and not ctl.state.paused and ctl.state.connection == "connected"
                and not ctl.turn_running())

    # ---- what the page can ask (on XENIO's loop) ----------------------------------------------------------------

    def listen_now(self) -> str:
        """The microphone button, as if the wake word had been heard: "listening", or "busy" while XENIO works."""
        if self._state is State.RECORDING:
            return "listening"
        if self._state is State.SPEAKING:
            self.barge_in("the microphone button")  # stop talking and take what the user says next
            return "listening"
        if self._state is not State.IDLE or self.ctl.turn_running() or self.ctl.state.paused:
            return "busy"
        event(log, "Listening from the page's button")
        self._warm_up()
        self.set_state(State.RECORDING)
        recorder = self._begin_recording()
        recorder.seed(list(self._idle_frames)[-2:])
        self._idle_frames.clear()
        self._wake_recorder = recorder
        self._wake_event.set()
        return "listening"

    def finish_listening(self) -> bool:
        """The button again while listening: the user is done talking, use what was heard."""
        if self._recorder is None:
            return False
        self._recorder.finish()
        return True

    def hush(self) -> None:
        """The page's Stop: stop talking now (the controller stops the request itself)."""
        if self._state is not State.SPEAKING:
            return
        self.interrupted = True
        if self._output is not None:
            self._output.stop()
        self.speaker.stop()

    async def speak_reply(self, text: str) -> None:
        """A typed request's answer, spoken like a spoken one - only while nothing else is being heard or said."""
        await self._say_while_idle(text)

    async def announce(self, text: str) -> bool:
        """A reminder or a notification: a chime, then the words - only while XENIO is not busy with the user."""
        if self.ctl.turn_running():
            return False
        return await self._say_while_idle(text, chime=True)

    async def _say_while_idle(self, text: str, chime: bool = False) -> bool:
        if self._state is not State.IDLE or self.tts is None or not text.strip():
            return False
        self.interrupted = False
        self.set_state(State.SPEAKING)
        try:
            if chime:
                await self._earcon()
            await self.speak(text)
            return True
        finally:
            if self._state is State.SPEAKING:
                self.set_state(State.IDLE)
            if self._interrupt_recorder is not None:  # "Xenio" said over it: the conversation loop takes the words
                self._wake_recorder, self._interrupt_recorder = self._interrupt_recorder, None
                self._wake_event.set()

    async def ask_approval(self, request, strict: bool, answer: Callable[[object], bool]) -> None:
        """The PermissionGate's question, asked out loud like in --voice; the page shows it too and the first answer -
        said or clicked - decides. A clear yes / no answers it ("yes to all" also covers the rest of the task in
        XENIO's workspace, when the request qualifies); unclear twice, it says the question is on screen and stops.
        Cancelled when the question is answered on screen: it stops talking and listening at once."""
        async with self._asking:
            before = self._state
            confined = bool(getattr(request, "confined", False)) and not strict
            what = request.description if len(request.description) <= 160 else request.description[:157] + "..."
            if strict:
                prompts = (f"This is a high-risk action. This will {what}. Say confirm to continue, or no to cancel.",
                           "Please say confirm, or no.")
            else:
                prompts = (f"This will {what}. Should I go ahead?" +
                           (" Say yes, or yes to all to finish this task in my workspace without asking." if confined else ""),
                           "Please say yes or no.")
            try:
                await self.drain_output()  # finish the sentence XENIO is saying first
                if self._reply is not None:
                    self._reply.hold()  # what Hermes writes after the question waits for the checks
                for prompt in prompts:
                    self.interrupted = False
                    self.set_state(State.SPEAKING)
                    if not await self.speak(prompt) and self.interrupted:
                        return
                    self.set_state(State.RECORDING)
                    audio = await self.record_utterance()
                    if audio is None:
                        continue
                    self.set_state(State.TRANSCRIBING)
                    try:
                        heard = strip_wake_words(await self.transcribe_utterance(audio), self.settings.name)
                    except STTError:
                        continue
                    verdict = interpret_answer(heard, strict=strict)
                    event(log, "Approval answered aloud", verdict=verdict, whole_task=confined and wants_whole_task(heard))
                    if verdict == "yes":
                        answer(APPROVE_TASK if confined and wants_whole_task(heard) else True)
                        return
                    if verdict == "no":
                        answer(False)
                        return
                self.set_state(State.SPEAKING)
                await self.speak("I'll leave the question on your screen.")
            except asyncio.CancelledError:  # answered on screen
                self._drop_listening()
                raise
            finally:
                if self._state in (State.SPEAKING, State.RECORDING, State.TRANSCRIBING):
                    self.set_state(before if before in (State.IDLE, State.THINKING) else State.IDLE)

    def _drop_listening(self) -> None:
        if self._output is not None:
            self._output.stop()
        self.speaker.stop()
        if self._recorder is not None:
            self._recorder.finish()
            self._recorder = None
        if self._turn is not None:
            self._turn[1].cancel()
            self._turn = None


async def run_ui_voice(ctl) -> None:
    """Build the talk engine and run it for the UI's lifetime. When it cannot run (no speech recognition, no
    microphone) the reason is shown on the page and its microphone button records in the browser instead."""
    settings = ctl.settings
    v = settings.voice
    stt = await create_voice_stt(settings, announce=lambda _line: None)
    if stt is None:
        ctl.voice_unavailable("speech recognition is not configured (STT_API_KEY / STT_API_URL in XENIO's .env)")
        return
    tts = create_tts(settings.tts)
    wake = stop = None
    try:
        wake, stop = await asyncio.to_thread(create_detectors, v.wake_phrase, v.stop_phrases, settings.models_dir,
                                             settings.data_dir, v.wake_sensitivity, v.wake_name_only)
    except WakeWordError as exc:
        event(log, "Wake word unavailable", logging.ERROR, error=str(exc))
    engine = UiVoice(ctl, stt, tts, Microphone(v.input_device), LiveSpeaker(v.output_device), wake, stop)
    try:
        setup_realtime(engine, settings)
        if v.barge_in:
            engine.busy_stop = await asyncio.to_thread(create_busy_stop_detector, settings.models_dir,
                                                       settings.data_dir, v.wake_sensitivity)
    except (WakeWordError, OSError, RuntimeError) as exc:  # each part is optional: the engine still works without
        event(log, "Voice extras unavailable", logging.WARNING, error=str(exc))
    try:
        try:
            await engine.mic.start()
        except MicrophoneUnavailable as exc:
            ctl.voice_unavailable(f"the microphone is not available ({exc}); allow it in System Settings > Privacy & "
                                  "Security > Microphone")
            return
        ctl.attach_voice(engine)
        await engine.converse()
    except MicrophoneUnavailable as exc:
        ctl.voice_unavailable(f"the microphone stopped working ({exc})")
    finally:
        ctl.detach_voice(engine)
        engine.speaker.stop()
        if hasattr(engine.speaker, "close"):
            engine.speaker.close()
        await engine.mic.stop()
        await stt.aclose()
        if tts is not None:
            await tts.aclose()
