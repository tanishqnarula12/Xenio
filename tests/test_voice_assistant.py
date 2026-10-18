"""Voice loop logic with fake mic/speaker/STT/TTS/Hermes (no audio hardware, no APIs)."""

import asyncio
from pathlib import Path

import numpy as np

from app.hermes.client import HermesPermissionRequest, HermesReply
from app.orchestrator import Orchestrator
from app.safety.permissions import PermissionGate
from app.voice.assistant import MSG_STT_FAILED, VOICE_CONTEXT, State, VoiceAssistant, VoiceConfirmer
from app.voice.audio import FRAME_SAMPLES, encode_wav
from app.voice.stt import STTError
from app.voice.tts import TTSError
from tests.test_orchestrator import FakeHermes

SPEECH = np.full(16000, 3000, dtype=np.int16) * np.where(np.arange(16000) % 2, 1, -1).astype(np.int16)


class FakeSTT:
    def __init__(self, answers):
        self.answers = list(answers)

    async def transcribe(self, audio_data, **_):
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def aclose(self):
        pass


class FakeTTS:
    def __init__(self, fail=False):
        self.fail = fail
        self.texts = []

    async def synthesize(self, text):
        self.texts.append(text)
        if self.fail:
            raise TTSError("tts down")
        return encode_wav(np.zeros(240, dtype=np.int16), 24000)

    async def aclose(self):
        pass


class FakeSpeaker:
    def __init__(self, block=False):
        self.block = block
        self.played = 0
        self.stopped = asyncio.Event()

    async def play(self, pcm, rate):
        self.played += 1
        if self.block:
            await self.stopped.wait()
            return False
        return True

    def stop(self):
        self.stopped.set()


class FakeMic:
    async def start(self):
        pass

    async def stop(self):
        pass

    async def read(self, timeout=5.0):
        await asyncio.sleep(3600)


class FakeDetector:
    def __init__(self, fire_on=None):
        self.fire_on = fire_on
        self.count = 0

    def process(self, frame):
        self.count += 1
        return self.fire_on

    def reset(self):
        pass


def make_assistant(make_settings, stt_answers, tts=None, speaker=None, hermes=None, stop=None):
    settings = make_settings()
    lines = []
    hermes = hermes or FakeHermes(HermesReply("I can search the web and manage files."))
    gate = PermissionGate(None)
    orchestrator = Orchestrator(hermes, gate, Path(settings.hermes.workspace))
    a = VoiceAssistant(
        settings, orchestrator, FakeSTT(stt_answers), tts if tts is not None else FakeTTS(), FakeMic(),
        speaker or FakeSpeaker(), FakeDetector(), stop, out=lines.append,
    )
    gate.confirmer = VoiceConfirmer(a)
    return a, lines, hermes


async def test_full_exchange_prints_and_speaks(make_settings):
    tts = FakeTTS()
    a, lines, hermes = make_assistant(make_settings, ["What can you do?"], tts=tts)
    await a.handle_utterance(SPEECH)
    assert hermes.messages == [f"What can you do?\n\n{VOICE_CONTEXT}"]  # short spoken answers requested
    assert "\nYou: What can you do?" in lines
    assert "XENIO: I can search the web and manage files." in lines
    assert tts.texts == ["I can search the web and manage files."]


async def test_stt_failure_says_it_could_not_understand(make_settings):
    tts = FakeTTS()
    a, lines, hermes = make_assistant(make_settings, [STTError("HTTP 500")], tts=tts)
    await a.handle_utterance(SPEECH)
    assert f"XENIO: {MSG_STT_FAILED}" in lines
    assert hermes.messages == []
    a, lines, _ = make_assistant(make_settings, ["  ...  "])
    await a.handle_utterance(SPEECH)
    assert f"XENIO: {MSG_STT_FAILED}" in lines


async def test_tts_failure_still_prints_reply(make_settings):
    a, lines, _ = make_assistant(make_settings, ["hello"], tts=FakeTTS(fail=True))
    await a.handle_utterance(SPEECH)
    assert "XENIO: I can search the web and manage files." in lines
    assert any("voice output unavailable" in line for line in lines)


async def test_hermes_unavailable_is_spoken(make_settings):
    from app.hermes.client import HermesUnavailable

    tts = FakeTTS()
    a, lines, _ = make_assistant(make_settings, ["hello"], tts=tts, hermes=FakeHermes(error=HermesUnavailable("x")))
    await a.handle_utterance(SPEECH)
    assert "XENIO: I can't reach my reasoning engine right now." in lines
    assert tts.texts == ["I can't reach my reasoning engine right now."]


async def test_barge_in_stops_playback(make_settings):
    speaker = FakeSpeaker(block=True)
    a, lines, _ = make_assistant(make_settings, ["tell me a story"], speaker=speaker, stop=FakeDetector("xenio stop"))
    task = asyncio.ensure_future(a.handle_utterance(SPEECH))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if a.state is State.SPEAKING and speaker.played:
            break
    a.feed_frame(np.zeros(FRAME_SAMPLES, dtype=np.int16))  # stop detector fires
    await asyncio.wait_for(task, 2)
    assert a.interrupted and speaker.stopped.is_set()


async def test_barge_in_while_thinking_cancels_hermes(make_settings):
    hermes = FakeHermes(HermesReply("late answer", stop_reason="cancelled"), delay=0.3)
    a, lines, _ = make_assistant(make_settings, ["long task"], hermes=hermes, stop=FakeDetector("xenio stop"))
    task = asyncio.ensure_future(a.handle_utterance(SPEECH))
    await asyncio.sleep(0.1)
    assert a.state is State.THINKING
    a.feed_frame(np.zeros(FRAME_SAMPLES, dtype=np.int16))
    await asyncio.wait_for(task, 2)
    assert hermes.cancelled == 1
    assert "[interrupted]" in lines
    assert a.state is State.RECORDING and a._interrupt_recorder is not None  # already capturing


async def test_saying_the_name_interrupts_while_thinking_or_speaking(make_settings):
    for state in (State.THINKING, State.SPEAKING):
        a, lines, _ = make_assistant(make_settings, [])
        a._wake = FakeDetector("hey xenio")
        a.set_state(state)
        a.feed_frame(np.zeros(FRAME_SAMPLES, dtype=np.int16))
        assert a.interrupted and "[interrupted]" in lines


async def test_xenio_saying_its_own_name_does_not_interrupt_it(make_settings):
    speaker = FakeSpeaker(block=True)
    hermes = FakeHermes(HermesReply("I am Xenio, your assistant."))
    a, lines, _ = make_assistant(make_settings, ["who are you"], speaker=speaker, hermes=hermes)
    a._wake = FakeDetector("hey xenio")  # the mic "hears" the name in XENIO's own voice
    task = asyncio.ensure_future(a.handle_utterance(SPEECH))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if a.state is State.SPEAKING and speaker.played:
            break
    a.feed_frame(np.zeros(FRAME_SAMPLES, dtype=np.int16))
    assert not a.interrupted  # guarded: the reply being spoken contains "Xenio"
    speaker.stop()
    await asyncio.wait_for(task, 2)


async def test_words_after_an_interruption_are_captured(make_settings):
    speaker = FakeSpeaker(block=True)
    a, lines, _ = make_assistant(make_settings, ["tell me a story"], speaker=speaker, stop=FakeDetector("xenio stop"))
    a._threshold = 100.0
    task = asyncio.ensure_future(a.handle_utterance(SPEECH))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if a.state is State.SPEAKING and speaker.played:
            break
    a.feed_frame(_level_frame(5))  # "Xenio, stop" -> barge-in
    await asyncio.wait_for(task, 2)
    recorder = a._interrupt_recorder
    for frame in [_level_frame(400)] * 8 + [_level_frame(5)] * 14:  # "what about tomorrow?"
        a.feed_frame(frame)
    audio = await a._finish_recording(recorder)
    assert audio is not None and len(audio) >= 8 * FRAME_SAMPLES


def test_talk_over_only_arms_without_echo():
    from app.voice.audio import TalkOverDetector

    t = TalkOverDetector()
    assert not t.armed(100)  # nothing learned yet
    t.begin_playback()
    for _ in range(30):
        t.update(900, 100)  # loud residual echo (laptop speakers)
    t.end_playback(interrupted=False)
    assert not t.armed(100)
    t.begin_playback()
    for level in [0] * 28 + [375, 375]:  # quiet on average but with echo bursts (measured here)
        t.update(level, 130)
    t.end_playback(interrupted=False)
    assert not t.armed(130)
    t.begin_playback()
    for _ in range(30):
        t.update(2, 100)  # headphones: XENIO is inaudible to the mic
    t.end_playback(interrupted=False)
    assert t.armed(100)
    t.begin_playback()
    fired = [t.update(level, 100) for level in [3, 400, 400, 400, 400]]
    assert fired == [False, False, False, False, True]
    assert not TalkOverDetector(enabled=False).armed(100)


class FireOnce:
    """Wake detector that fires on its Nth frame."""

    def __init__(self, at):
        self.at, self.count = at, 0

    def process(self, frame):
        self.count += 1
        return "hey xenio" if self.count == self.at else None

    def reset(self):
        pass


def _level_frame(level):
    return (np.full(FRAME_SAMPLES, level, dtype=np.int16) * np.where(np.arange(FRAME_SAMPLES) % 2, 1, -1)).astype(np.int16)


async def test_quiet_speech_right_after_wake_is_captured(make_settings):
    a, lines, _ = make_assistant(make_settings, [])
    a._wake = FireOnce(at=20)
    # 12 quiet frames, the wake phrase at ~200 RMS (detector fires on its last frame)...
    for frame in [_level_frame(5)] * 12 + [_level_frame(200)] * 8:
        a.feed_frame(frame)
    assert a.state is State.RECORDING and a._wake_recorder is not None
    recorder = a._wake_recorder
    # ...then the command, immediately and quietly (~120 RMS), then a pause.
    for frame in [_level_frame(120)] * 10 + [_level_frame(5)] * 14:
        a.feed_frame(frame)
    audio = await a._finish_recording(recorder)
    assert audio is not None and len(audio) >= 10 * FRAME_SAMPLES
    assert recorder.threshold < 120  # calibrated from the wake phrase, not the old fixed 400


class ScriptedMic:
    """Plays scripted frames only while XENIO is listening (IDLE/RECORDING); room silence otherwise."""

    def __init__(self, frames):
        self.frames = list(frames)
        self.assistant = None

    async def start(self):
        pass

    async def stop(self):
        pass

    async def read(self, timeout=5.0):
        await asyncio.sleep(0.001)
        listening = self.assistant is not None and self.assistant.state in (State.IDLE, State.RECORDING)
        if self.frames and listening:
            return self.frames.pop(0)
        return _level_frame(5)


async def test_conversation_mode_follow_up_without_wake_word(make_settings):
    settings = make_settings(XENIO_FOLLOW_UP_SECONDS="1", XENIO_SILENCE_SECONDS="0.4")
    hermes = FakeHermes(HermesReply("Sure."))
    gate = PermissionGate(None)
    lines = []
    script = (
        [_level_frame(5)] * 5 + [_level_frame(300)] * 6  # "Hey Xenio" (wake fires on its last frame)
        + [_level_frame(300)] * 6 + [_level_frame(5)] * 8  # "what's the time" + pause
        + [_level_frame(300)] * 6 + [_level_frame(5)] * 8  # follow-up, NO wake word
    )
    mic = ScriptedMic(script)
    a = VoiceAssistant(
        settings, Orchestrator(hermes, gate, Path(settings.hermes.workspace)), FakeSTT(["what's the time", "and the date?"]),
        FakeTTS(), mic, FakeSpeaker(), FireOnce(at=11), None, out=lines.append,
    )
    mic.assistant = a
    run = asyncio.ensure_future(a.run())
    for _ in range(500):
        await asyncio.sleep(0.01)
        if any("when you need me" in line for line in lines):
            break
    run.cancel()
    try:
        await run
    except asyncio.CancelledError:
        pass
    assert [m.split("\n")[0] for m in hermes.messages] == ["what's the time", "and the date?"]
    assert any("when you need me" in line for line in lines)  # silence ends conversation mode
    assert a.state is State.IDLE


async def test_wake_word_only_transcript_is_not_sent(make_settings):
    a, lines, hermes = make_assistant(make_settings, ["Hey Xenio."])
    await a.handle_utterance(SPEECH)
    assert hermes.messages == []
    a, lines, hermes = make_assistant(make_settings, ["Hey, Zenio! What time is it?"])
    await a.handle_utterance(SPEECH)
    assert hermes.messages[0].startswith("What time is it?")


async def test_voice_confirmation_flow(make_settings):
    a, lines, hermes = make_assistant(make_settings, [])

    async def fake_record():
        return SPEECH

    a.record_utterance = fake_record

    def cmd(command):
        return HermesPermissionRequest(command, "execute", raw_input={"command": command})

    a.stt = FakeSTT(["Yes, go ahead."])
    assert await hermes.handler(cmd("git push origin main")) is True
    a.stt = FakeSTT(["No."])
    assert await hermes.handler(cmd("git push origin dev")) is False
    # a short unclear answer is asked again, then the clear answer counts
    a.tts = FakeTTS()
    a.stt = FakeSTT(["Hmm?", "yes"])
    assert await hermes.handler(cmd("pip install requests")) is True
    assert "Please confirm yes or no." in a.tts.texts
    # a new request is never an answer: the action is cancelled and the request kept for after this turn
    a.stt = FakeSTT(["What's the weather like today?", "yes"])
    assert await hermes.handler(cmd("pip install flask")) is False
    assert a.held_request == "What's the weather like today?"
    # HIGH_RISK needs the word "confirm"; "yes" alone is unclear
    a.stt = FakeSTT(["yes", "confirm"])
    assert await hermes.handler(cmd("format D:")) is True
    a.stt = FakeSTT(["yes", "yes", "yes"])
    assert await hermes.handler(cmd("format E:")) is False
    assert any("PERMISSION REQUIRED [HIGH_RISK]" in line for line in lines)
    # blocked actions are refused without asking at all
    a.stt = FakeSTT([])
    assert await hermes.handler(cmd("cat ~/.ssh/id_rsa")) is False


async def test_first_word_after_wake_is_kept(make_settings):
    a, _, _ = make_assistant(make_settings, [])
    a._wake = FireOnce(at=10)
    frames = [_level_frame(5)] * 6 + [_level_frame(300)] * 4  # "...Xenio, cre" - detector fires late
    for f in frames:
        a.feed_frame(f)
    recorder = a._wake_recorder
    for f in [_level_frame(300)] * 6 + [_level_frame(5)] * 14:  # "...ate a folder" + pause
        a.feed_frame(f)
    audio = await a._finish_recording(recorder)
    assert audio is not None and len(audio) >= (6 + 4) * FRAME_SAMPLES  # includes the frames heard before detection


def test_learns_names_for_speech_recognition(make_settings):
    from app.hermes.client import ToolActivity

    a, _, _ = make_assistant(make_settings, [])
    a.learn_names([ToolActivity("1", title='terminal: mkdir -p "C:/Users/aniln/OneDrive/Desktop/XenioTest"'),
                   ToolActivity("2", title="write_file: C:/Users/aniln/OneDrive/Desktop/XenioTest/hello.txt")])
    assert "XenioTest" in a._vocabulary and "hello.txt" in a._vocabulary
    assert "Desktop" not in a._vocabulary


async def test_silence_is_never_a_yes(make_settings):
    a, lines, hermes = make_assistant(make_settings, [])

    async def silence():
        return None

    a.record_utterance = silence
    request = HermesPermissionRequest("x", "execute", raw_input={"command": "git push origin main"})
    assert await hermes.handler(request) is False
    assert lines.count("   (no answer heard)") == 3 and "   -> cancelled (no clear answer)" in lines
