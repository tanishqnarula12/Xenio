"""XENIO's talk engine inside the UI process (app/ui/voice.py): spoken requests go through the controller's turn,
the engine's phase drives the orb, the page's microphone button and Stop reach it, "Hey XENIO" listens only when it
may, and it degrades to the browser path with a reason when it cannot run. Fakes stand in for the microphone, the
speaker and the speech services; the controller, the server and the assistant state are real."""

import asyncio
import time
from types import SimpleNamespace

import numpy as np
import pytest

from app.ui.assistant import AssistantState
from app.ui.voice import UiVoice, run_ui_voice
from app.voice.assistant import State
from app.voice.audio import MicrophoneUnavailable, wake_chime
from tests.test_ui_backend import FakeSTT, FakeTTS, call, stack, wait_for  # noqa: F401 - fixtures
from tests.test_engineering_service import repo, settings  # noqa: F401 - fixtures


class FakeMic:
    def __init__(self, fail=False):
        self.fail, self.frames, self.stopped = fail, asyncio.Queue(), False

    async def start(self):
        if self.fail:
            raise MicrophoneUnavailable("no input device")

    async def read(self, timeout=5.0):
        return await self.frames.get()

    async def stop(self):
        self.stopped = True


class FakeSpeaker:
    playing = False
    output_level = 1200.0

    def __init__(self):
        self.played, self.stops = [], 0

    async def play(self, pcm, rate):
        self.played.append(len(pcm))
        return True

    def stop(self):
        self.stops += 1

    def echo_possible(self):
        return False


class FakeWake:
    def __init__(self):
        self.frames = 0

    def process(self, frame):
        self.frames += 1
        return None

    def reset(self):
        pass


def on_loop(stack, coro, timeout=10):  # noqa: F811
    return asyncio.run_coroutine_threadsafe(coro, stack.loop).result(timeout)


def engine_for(stack, wake=None):  # noqa: F811
    async def build():
        return UiVoice(stack.ctl, FakeSTT(), FakeTTS(), FakeMic(), FakeSpeaker(), wake, None)

    engine = on_loop(stack, build())
    stack.ctl.attach_voice(engine)
    return engine


# ---- the orb follows the engine ------------------------------------------------------------------------------------

def test_engine_phase_drives_the_orb(stack):  # noqa: F811
    engine = engine_for(stack)

    async def walk():
        seen = []
        for state in (State.RECORDING, State.TRANSCRIBING, State.SPEAKING, State.IDLE):
            engine.set_state(state)
            seen.append((stack.ctl.state.voice, stack.ctl.state.derive()))
        return seen

    assert on_loop(stack, walk()) == [("listening", "LISTENING"), ("transcribing", "PROCESSING"),
                                      ("speaking", "SPEAKING"), ("", "IDLE")]
    published = [e["data"]["voice"] for e in stack.hub.since(0)[0] if e["type"] == "assistant.state"]
    assert {"listening", "transcribing", "speaking"} <= set(published)


def test_assistant_state_voice_priority():
    s = AssistantState(connection="connected")
    s.turn_started("t1")
    s.voice = "speaking"
    assert s.derive() == "SPEAKING"  # the reply is spoken while Hermes still writes
    s.approvals = 1
    assert s.derive() == "WAITING_APPROVAL"  # a human decision always shows first
    s.approvals, s.voice = 0, "transcribing"
    assert s.derive() == "PROCESSING"  # the turn's own phase once a turn runs
    s.turn_finished(True)
    assert s.derive() == "PROCESSING"
    s.voice = ""
    assert s.derive() == "COMPLETED"
    assert s.snapshot()["voice"] is None


# ---- spoken requests are ordinary turns --------------------------------------------------------------------------

def test_spoken_request_goes_through_the_conversation(stack):  # noqa: F811
    chunks = []
    response = on_loop(stack, stack.ctl.voice_turn("make a folder called Projects", on_chunk=chunks.append,
                                                   context="spoken"))
    assert response.ok and response.text == "Done. Your folder is ready."
    assert chunks == ["Done. Your folder is ready."]  # the engine speaks while the answer is written
    user = [e for e in stack.hub.since(0)[0] if e["type"] == "chat.user"]
    assert user and user[-1]["data"]["via"] == "voice" and user[-1]["data"]["text"] == "make a folder called Projects"
    assert stack.ctl.conversation[-1]["role"] == "assistant"


def test_spoken_request_refused_like_a_typed_one(stack):  # noqa: F811
    stack.ctl.set_paused(True)
    response = on_loop(stack, stack.ctl.voice_turn("open spotify"))
    assert not response.ok and "paused" in response.text
    assert not any(e["type"] == "chat.user" for e in stack.hub.since(0)[0])


# ---- the page's buttons ----------------------------------------------------------------------------------------------

def test_microphone_button_listens_on_the_mac(stack):  # noqa: F811
    engine = engine_for(stack)
    status, body, _ = call(stack.server, "POST", "/api/v1/voice/listen", {})
    assert status == 200 and body == {"listening": True}
    assert engine.state is State.RECORDING and stack.ctl.state.voice == "listening"
    assert engine._wake_event.is_set() and engine._wake_recorder is not None  # the conversation loop takes it from here
    status, body, _ = call(stack.server, "POST", "/api/v1/voice/finish", {})
    assert status == 200 and body == {"finished": True} and engine._recorder.done
    loud = (np.sin(np.arange(512) / 3) * 3000).astype(np.int16).reshape(-1, 1)
    stack.loop.call_soon_threadsafe(engine.feed_frame, loud)
    on_loop(stack, asyncio.sleep(0))
    status, body, _ = call(stack.server, "GET", "/api/v1/voice/level")
    assert status == 200 and 0.5 < body["level"] <= 1 and body["phase"] == "listening"


def test_microphone_button_refused_while_xenio_works(stack):  # noqa: F811
    engine = engine_for(stack)
    stack.orch.release = asyncio.Event()
    call(stack.server, "POST", "/api/v1/chat", {"text": "do something long"})
    assert wait_for(lambda: stack.orch.busy)
    status, body, _ = call(stack.server, "POST", "/api/v1/voice/listen", {})
    assert status == 409 and "still working" in body["error"]
    assert engine.state is State.IDLE
    stack.loop.call_soon_threadsafe(stack.orch.release.set)


def test_without_the_engine_the_page_records_itself(stack):  # noqa: F811
    stack.ctl.voice_problem = "the microphone is not available"
    status, body, _ = call(stack.server, "POST", "/api/v1/voice/listen", {})
    assert status == 503 and "microphone is not available" in body["error"]
    status, body, _ = call(stack.server, "GET", "/api/v1/snapshot")
    assert body["voice"]["engine"] is False and body["voice"]["problem"] == "the microphone is not available"
    status, body, _ = call(stack.server, "GET", "/api/v1/voice/level")
    assert body == {"level": 0.0, "phase": None}


def test_stop_silences_xenio(stack):  # noqa: F811
    engine = engine_for(stack)
    on_loop(stack, asyncio.sleep(0))
    stack.loop.call_soon_threadsafe(engine.set_state, State.SPEAKING)
    assert wait_for(lambda: engine.state is State.SPEAKING)
    status, _, _ = call(stack.server, "POST", "/api/v1/chat/cancel", {})
    assert status == 200
    assert wait_for(lambda: engine.speaker.stops > 0) and engine.interrupted


def test_typed_answers_are_spoken_by_the_engine(stack):  # noqa: F811
    engine = engine_for(stack)
    spoken = []

    async def speak_reply(text):
        spoken.append(text)

    engine.speak_reply = speak_reply
    stack.ctl.save_ui_settings({"speak_replies": True})
    call(stack.server, "POST", "/api/v1/chat", {"text": "make a folder"})
    assert wait_for(lambda: spoken == ["Done. Your folder is ready."])
    stack.ctl.save_ui_settings({"speak_replies": False})
    call(stack.server, "POST", "/api/v1/chat", {"text": "again"})
    assert wait_for(lambda: not stack.ctl.turn_running())
    assert spoken == ["Done. Your folder is ready."]


# ---- "Hey XENIO" listens only when it may ------------------------------------------------------------------------

def test_wake_word_listens_only_when_allowed(stack):  # noqa: F811
    wake = FakeWake()
    engine = engine_for(stack, wake)
    frame = np.zeros((512, 1), dtype=np.int16)

    def feed():
        before = wake.frames
        on_loop(stack, asyncio.sleep(0))
        stack.loop.call_soon_threadsafe(engine.feed_frame, frame)
        on_loop(stack, asyncio.sleep(0))
        return wake.frames - before

    assert feed() == 1
    stack.ctl.set_paused(True)
    assert feed() == 0  # paused: nothing can wake XENIO
    stack.ctl.set_paused(False)
    status, body, _ = call(stack.server, "PUT", "/api/v1/settings", {"wake_word": False})
    assert status == 200 and engine.wake_enabled is False and stack.ctl.voice_info()["wake"] is False
    assert feed() == 0  # switched off in Settings
    call(stack.server, "PUT", "/api/v1/settings", {"wake_word": True})
    assert engine.wake_enabled and feed() == 1
    assert any(e["type"] == "voice.changed" for e in stack.hub.since(0)[0])


def test_wake_sound_is_a_short_clean_chime():
    pcm = wake_chime()
    samples = pcm[:, 0].astype(np.float32) / 32767
    assert pcm.dtype == np.int16 and 0.2 < len(samples) / 24_000 < 0.35
    assert 0.2 < np.max(np.abs(samples)) <= 0.23  # the old beep's loudness, never clipping
    assert abs(samples[0]) < 0.01 and abs(samples[-1]) < 0.01  # no clicks
    spectrum = np.abs(np.fft.rfft(samples))
    freqs = np.fft.rfftfreq(len(samples), 1 / 24_000)
    assert spectrum[(freqs > 1400) & (freqs < 2500)].sum() > spectrum[freqs < 400].sum() * 5  # bright, not a thud


# ---- when it cannot run ------------------------------------------------------------------------------------------------

def test_engine_reports_why_it_cannot_run(stack, monkeypatch):  # noqa: F811
    import app.ui.voice as V

    async def no_stt(*_a, **_k):
        return None

    monkeypatch.setattr(V, "create_voice_stt", no_stt)
    on_loop(stack, run_ui_voice(stack.ctl))
    assert "speech recognition is not configured" in stack.ctl.voice_problem and stack.ctl.voice is None

    async def fake_stt(*_a, **_k):
        return SimpleNamespace(aclose=_noop)

    mic = FakeMic(fail=True)
    monkeypatch.setattr(V, "create_voice_stt", fake_stt)
    monkeypatch.setattr(V, "create_tts", lambda _cfg: None)
    monkeypatch.setattr(V, "create_detectors", _no_detectors)
    monkeypatch.setattr(V, "setup_realtime", lambda *_a: None)
    monkeypatch.setattr(V, "create_busy_stop_detector", lambda *_a: None)
    monkeypatch.setattr(V, "Microphone", lambda _device: mic)
    monkeypatch.setattr(V, "LiveSpeaker", lambda _device: FakeSpeaker())
    on_loop(stack, run_ui_voice(stack.ctl))
    assert "microphone is not available" in stack.ctl.voice_problem and stack.ctl.voice is None and mic.stopped
    health = {c["name"]: c for c in stack.ctl.health(force=True)["checks"]}
    assert health["Talk engine"]["status"] == "WARN" and "microphone" in health["Talk engine"]["detail"]


async def _noop():
    return None


def _no_detectors(*_a):
    from app.voice.wakeword import WakeWordError

    raise WakeWordError("model missing")


@pytest.fixture(autouse=True)
def _quiet_stop_file(monkeypatch):
    """barge_in() asks XENIO's desktop hands to stop through a file; tests must not touch the real one."""
    import app.desktop.stop as stop

    monkeypatch.setattr(stop, "request_stop", lambda *_a, **_k: None)


def test_speaker_uses_fixed_blocks_and_waits_out_its_own_tail(monkeypatch):
    """Tiny variable buffers made XENIO's voice crack whenever other Python threads were busy (7.4% of the audio
    reached the device in the UI process); fixed blocks delivered all of it. The latency they add is part of the
    echo window, so the end of a reply is not taken for the user speaking."""
    import time

    import app.voice.audio as A

    opened = {}

    class Stream:
        latency = 0.07

        def __init__(self, **kw):
            opened.update(kw)

        def start(self):
            pass

    monkeypatch.setattr(A, "_sd", lambda: SimpleNamespace(OutputStream=Stream))

    async def open_it():
        speaker = A.LiveSpeaker()
        assert speaker._ensure()
        return speaker

    speaker = asyncio.run(open_it())
    assert opened["blocksize"] == A.OUTPUT_BLOCK >= 512 and opened["latency"] == "high"
    speaker.finished_at = time.monotonic() - 0.12
    assert speaker.echo_possible()  # 0.12 s after the last block was handed over, 0.07 s of it was still playing
    speaker.finished_at = time.monotonic() - 0.3
    assert not speaker.echo_possible()


def test_quiet_requests_are_answered_on_screen_only(stack):  # noqa: F811
    """The API's ``quiet``: never spoken, even with "speak every reply" on - and the page is told, so it does not speak
    it either. Saved settings from an older XENIO that no longer exist fall back to the defaults."""
    engine = engine_for(stack)
    spoken = []

    async def speak_reply(text):
        spoken.append(text)

    engine.speak_reply = speak_reply
    stack.ctl.save_ui_settings({"speak_replies": True})
    status, body, _ = call(stack.server, "POST", "/api/v1/chat", {"text": "what's 2+2", "via": "text", "quiet": True})
    assert status == 202
    assert wait_for(lambda: not stack.ctl.turn_running() and stack.ctl.conversation[-1]["role"] == "assistant")
    time.sleep(0.05)
    assert spoken == []
    user = [e["data"] for e in stack.hub.since(0)[0] if e["type"] == "chat.user"][-1]
    assert user["quiet"] is True
    assert call(stack.server, "PUT", "/api/v1/settings", {"orb_summon_mode": "chat"})[0] == 200
    assert call(stack.server, "PUT", "/api/v1/settings", {"orb_summon_mode": "type"})[0] == 422  # the removed mode
    stack.ctl._settings_file.write_text('{"orb_summon_mode": "type", "speak_replies": "loud"}')  # saved by an older XENIO
    assert stack.ctl.ui_settings()["orb_summon_mode"] == "choose" and stack.ctl.ui_settings()["speak_replies"] is True
