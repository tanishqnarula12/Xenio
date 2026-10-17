"""Real-time voice: VAD endpointing, hedged STT, streaming TTS parsing, sentence streaming with the hold-back
rule, acknowledgments and narration - with fakes (no audio hardware, no APIs)."""

import asyncio
import io
import struct
import time
import wave

import numpy as np
import pytest

from app.hermes.client import HermesReply, ToolActivity
from app.orchestrator import claims_outcome
from app.voice.audio import FRAME_SAMPLES, UtteranceRecorder, encode_wav
from app.voice.live import FillerBank, ReplyStream, SentenceChunker, SpeechQueue, narration_for
from app.voice.tts import WavStreamParser
from tests.test_voice_assistant import SPEECH, FakeSpeaker, FakeTTS, make_assistant

# ---- sentences -------------------------------------------------------------------------------------


def chunk_all(text, size=7):
    c = SentenceChunker()
    out = []
    for i in range(0, len(text), size):
        out += c.feed(text[i:i + size])
    return out + c.flush()


def test_sentences_come_out_as_soon_as_they_are_complete():
    c = SentenceChunker()
    assert c.feed("Your next meeting is at 3") == []
    assert c.feed(" pm. It's with") == ["Your next meeting is at 3 pm."]
    assert c.feed(" Priya, e.g. the design") == []  # "e.g." is not the end of a sentence
    assert c.feed(" review! ") == ["It's with Priya, e.g. the design review!"]
    assert c.feed("Anything else?") == []  # no space after it yet: could still continue
    assert c.flush() == ["Anything else?"]
    assert chunk_all("Version 3.5 is out. Rain is 80.5 percent likely.\nBring a jacket.") == [
        "Version 3.5 is out.", "Rain is 80.5 percent likely.", "Bring a jacket."]


def test_a_long_first_sentence_is_cut_at_a_comma_for_a_fast_start():
    text = ("Tomorrow you have three meetings in the morning and one in the afternoon, the first one at nine "
            "with the CRM team, so I'd leave early.")
    parts = chunk_all(text, size=200)
    assert parts[0].endswith("afternoon,") and len(parts) == 2


def test_code_blocks_are_not_split():
    c = SentenceChunker()
    assert c.feed("Here it is. ```py\nx = 1. y = 2.\n") == ["Here it is."]
    assert c.feed("``` Done.") == []
    assert c.flush() == ["```py\nx = 1. y = 2.\n``` Done."]


# ---- the hold-back rule (phase 17 while streaming) ---------------------------------------------------

def test_outcome_claims_wait_for_the_check():
    spoken = []
    r = ReplyStream(spoken.append, claims_outcome)
    for chunk in ["Okay, adding it now. ", "Done! The event ", "is in your calendar. ", "Anything else?"]:
        r.feed(chunk)
    assert spoken == ["Okay, adding it now."]  # "Done!" and everything after it waits
    streamed = "".join(r.raw).strip()
    # the tool really succeeded: the held part is spoken as written
    assert r.finish(streamed) == ["Done!", "The event is in your calendar.", "Anything else?"]


def test_a_false_claim_is_never_spoken():
    spoken = []
    r = ReplyStream(spoken.append, claims_outcome)
    r.feed("Sure. I've created the event for 3 pm.")
    final = "The operation failed: Google refused the request (HTTP 403)."
    assert r.finish(final) == [final] and "I've created" not in " ".join(spoken)


def test_a_correction_note_is_added_after_the_held_part():
    r = ReplyStream(lambda s: None, claims_outcome)
    r.feed("You declined the push, so nothing changed.")
    streamed = "".join(r.raw).strip()
    note = "(XENIO correction: you said yes in this turn - nothing was declined. It was allowed to run.)"
    assert r.finish(f"{streamed}\n\n{note}") == ["You declined the push, so nothing changed.", note]


def test_after_a_failed_tool_everything_waits():
    spoken = []
    r = ReplyStream(spoken.append, claims_outcome)
    r.feed("Let me look. ")
    r.hold()
    r.feed("Your calendar looks free. ")
    assert spoken == ["Let me look."] and r.held == ["Your calendar looks free."]


def test_errors_are_spoken_only_if_nothing_was_said():
    assert ReplyStream(lambda s: None, claims_outcome).finish("I can't reach my reasoning engine.", ok=False) == [
        "I can't reach my reasoning engine."]


@pytest.mark.parametrize("title,phrase", [
    ("xenio_calendar_events", "Checking your calendar."), ("xenio_email_search", "Looking at your email."),
    ("web_search: crm docs", "Searching the web."), ("terminal: git status", "Running that now."),
    ("xenio_github", "Checking GitHub."), ("skill_view: github", None), ("memory", None),
])
def test_narration(title, phrase):
    assert narration_for(title) == phrase


# ---- endpointing -------------------------------------------------------------------------------------

class ScriptedVad:
    """Speech for the frames listed in ``speech``."""

    def __init__(self, speech):
        self.speech, self.i, self.resets = set(speech), 0, 0

    def is_speech(self, frame):
        self.i += 1
        return self.i in self.speech

    def reset(self):
        self.resets += 1


def test_vad_ends_the_turn_after_a_short_pause_and_trims_the_silence():
    frame = np.full(FRAME_SAMPLES, 500, dtype=np.int16)
    vad = ScriptedVad(range(3, 13))  # speech in frames 3..12 (0.8 s), then silence
    rec = UtteranceRecorder(silence_seconds=0.55, vad=vad, pre_roll_seconds=0.16, tail_seconds=0.16)
    fed = 0
    while not rec.feed(frame):
        fed += 1
    assert fed + 1 == 12 + 7  # done 7 frames (0.56 s) after the last speech frame, not 1 s
    audio = rec.audio()
    assert len(audio) == (2 + 10 + 2) * FRAME_SAMPLES  # pre-roll + speech + short tail; the rest is not sent
    assert vad.resets == 1


def test_vad_ignores_loud_noise_and_energy_mode_still_works():
    loud_noise = np.full(FRAME_SAMPLES, 3000, dtype=np.int16)
    rec = UtteranceRecorder(vad=ScriptedVad([]), start_timeout=0.4)
    while not rec.feed(loud_noise):
        pass
    assert rec.audio() is None  # a fan or TV is not speech
    energy = UtteranceRecorder(threshold=100, silence_seconds=0.3)
    for f in [loud_noise] * 5 + [np.zeros(FRAME_SAMPLES, dtype=np.int16)] * 4:
        if energy.feed(f):
            break
    assert energy.audio() is not None


# ---- hedged STT ----------------------------------------------------------------------------------------

async def test_a_slow_transcription_is_hedged(make_settings):
    from app.voice.stt import OpenAICompatibleSTT

    stt = OpenAICompatibleSTT(make_settings(STT_API_KEY="k", STT_API_URL="https://stt.example").stt, hedge_after=0.05)
    calls = []

    async def once(audio, filename, content_type, prompt):
        calls.append(time.monotonic())
        if len(calls) == 1:
            await asyncio.sleep(5)  # the slow first request
            return "late"
        return "fast"

    stt._transcribe_once = once
    started = time.monotonic()
    assert await stt.transcribe(b"x") == "fast" and len(calls) == 2
    assert time.monotonic() - started < 1
    stt.hedge_after = 0
    calls.clear()

    async def quick(*a):
        calls.append(1)
        return "one"

    stt._transcribe_once = quick
    assert await stt.transcribe(b"x") == "one" and calls == [1]  # no hedge when switched off
    await stt.aclose()


# ---- on-device speech recognition --------------------------------------------------------------------------

@pytest.mark.parametrize("heard,vocab,expected", [
    ("Create a folder called Zenio Test on my desktop.", ["XenioTest"], "Create a folder called XenioTest on my desktop."),
    ("Hey Zenio, what time is it?", [], "Hey Xenio, what time is it?"),
    ("Ask Xenia about it", [], "Ask Xenio about it"),
    ("open xeniotest", ["XenioTest"], "open XenioTest"),
    ("Read notes.txt please", ["notes.txt"], "Read notes.txt please"),  # already right: untouched
    ("The scenic route", [], "The scenic route"),  # not a name
])
def test_fix_names(heard, vocab, expected):
    from app.voice.local_stt import fix_names

    assert fix_names(heard, "Xenio", vocab) == expected


class _STT:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, 0

    async def transcribe(self, audio, **kw):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result

    async def aclose(self):
        pass


async def test_hybrid_uses_local_first_and_the_cloud_as_fallback():
    from app.voice.local_stt import HybridSTT
    from app.voice.stt import STTError

    local, cloud = _STT("what's on my calendar"), _STT("cloud text")
    assert await HybridSTT(local, cloud).transcribe(b"wav") == "what's on my calendar" and cloud.calls == 0
    broken = _STT(error=STTError("model missing"))
    assert await HybridSTT(broken, cloud).transcribe(b"wav") == "cloud text"
    silent = _STT("  ")  # the local model heard nothing: ask the cloud before giving up
    assert await HybridSTT(silent, cloud).transcribe(b"wav") == "cloud text"
    assert await HybridSTT(None, cloud).transcribe(b"wav") == "cloud text"
    with pytest.raises(STTError):
        await HybridSTT(_STT(error=STTError("x")), None).transcribe(b"wav")


def test_stt_engine_setting(make_settings):
    from app.config import ConfigError

    # cloud by default: the on-device model failed with the user's real voice (see app/voice/local_stt.py)
    assert make_settings().voice.stt_engine == "cloud"
    assert make_settings(XENIO_STT_ENGINE="local").voice.stt_engine == "local"
    with pytest.raises(ConfigError):
        make_settings(XENIO_STT_ENGINE="telepathy")


# ---- streamed TTS ---------------------------------------------------------------------------------------

def test_wav_stream_parser_handles_any_split():
    pcm = (np.arange(1000) - 500).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(pcm.tobytes())
    data = bytearray(buf.getvalue())
    data[4:8] = struct.pack("<I", 0xFFFFFFFF)  # nabh's streaming header: unknown sizes
    data[40:44] = struct.pack("<I", 0xFFFFFFFF)
    for step in (1, 7, 45, 4096):
        parser, got = WavStreamParser(), []
        for i in range(0, len(data), step):
            out = parser.feed(bytes(data[i:i + step]))
            if out is not None:
                got.append(out)
        assert parser.rate == 24000 and np.array_equal(np.concatenate(got), pcm), step
    bad = WavStreamParser()
    bad.feed(b"ID3\x03" + b"\x00" * 20)
    assert bad.unsupported == "not a WAV stream"


async def test_speech_queue_plays_in_order_and_uses_cached_phrases(tmp_path):
    tts = FakeTTS()
    fillers = FillerBank(tts, tmp_path, "af_bella", phrases=("One moment.",))
    assert await fillers.warm() == 1 and tts.texts == ["One moment."]
    assert await FillerBank(tts, tmp_path, "af_bella", phrases=("One moment.",)).warm() == 1
    assert tts.texts == ["One moment."]  # cached on disk: not synthesized again
    speaker, items = FakeSpeaker(), []
    q = SpeechQueue(tts, speaker, fillers, on_item=lambda text, started: started and items.append(text))
    q.say("One moment.")
    q.say("**Your** next meeting is at 3.")
    q.say("--- **")  # nothing speakable after markdown is removed -> skipped
    await q.drain()
    assert items == ["One moment.", "Your next meeting is at 3."] and speaker.played == 2
    assert tts.texts == ["One moment.", "Your next meeting is at 3."]  # the filler came from the cache
    q.stop()
    q.say("not after stop")
    await q.drain()
    assert speaker.played == 2


# ---- the whole turn ---------------------------------------------------------------------------------------

class StreamingHermes:
    """Streams a reply in pieces, with a tool call first, like Hermes over ACP."""

    def __init__(self, chunks, tool=None, delay=0.05, tool_status="completed", final=None):
        self.chunks, self.tool, self.delay, self.tool_status = chunks, tool, delay, tool_status
        self.final = final
        self.handler = None
        self.agent_info = {}
        self.messages = []
        self.timeline = []

    def set_permission_handler(self, handler):
        self.handler = handler

    async def start(self):
        pass

    async def send_message(self, message, on_chunk=None, on_tool=None):
        self.messages.append(message)
        tools = []
        if self.tool:
            activity = ToolActivity("t1", self.tool, "other", "in_progress")
            on_tool and on_tool(activity)
            await asyncio.sleep(self.delay * 20)  # the tool takes a while: XENIO should not stay silent
            activity.status = self.tool_status
            activity.detail = "Google refused the request (HTTP 403)" if self.tool_status == "failed" else ""
            on_tool and on_tool(activity)
            tools.append(activity)
        for chunk in self.chunks:
            self.timeline.append(("chunk", time.monotonic(), chunk))
            on_chunk and on_chunk(chunk)
            await asyncio.sleep(self.delay)
        return HermesReply(self.final or "".join(self.chunks).strip(), tool_calls=tools)

    async def cancel(self):
        pass


class RecordingTTS(FakeTTS):
    def __init__(self):
        super().__init__()
        self.at = []

    async def synthesize(self, text):
        self.at.append((time.monotonic(), text))
        return await super().synthesize(text)


async def live_assistant(make_settings, hermes, tmp_path, ack=True, ack_delay=0.1):
    tts = RecordingTTS()
    a, lines, _ = make_assistant(make_settings, ["what's on my calendar tomorrow"], tts=tts, hermes=hermes)
    a.settings = a.settings.__class__(**{**a.settings.__dict__, "voice": a.settings.voice.__class__(
        **{**a.settings.voice.__dict__, "acknowledge": ack, "ack_delay_seconds": ack_delay})})
    if ack:
        a.fillers = FillerBank(tts, tmp_path, "v")
        await a.fillers.warm()
        tts.texts.clear()
        tts.at.clear()
    return a, lines, tts


async def test_first_sentence_is_spoken_before_the_reply_is_finished(make_settings, tmp_path):
    hermes = StreamingHermes(["Tomorrow you have two meetings. ", "The first is at nine. ", "The second at four."],
                             delay=0.2)
    a, lines, tts = await live_assistant(make_settings, hermes, tmp_path, ack=False)
    await a.handle_utterance(SPEECH)
    first_spoken = tts.at[0][0]
    last_chunk = hermes.timeline[-1][1]
    assert tts.at[0][1] == "Tomorrow you have two meetings." and first_spoken < last_chunk  # streamed, not waited
    assert [t for _, t in tts.at] == ["Tomorrow you have two meetings.", "The first is at nine.", "The second at four."]
    assert "XENIO: Tomorrow you have two meetings. The first is at nine. The second at four." in lines
    assert a.last_turn_timing["first_audio_s"] >= 0


async def test_acknowledges_and_narrates_while_hermes_works(make_settings, tmp_path):
    hermes = StreamingHermes(["You have one meeting tomorrow at ten."], tool="xenio_calendar_events", delay=0.1)
    a, lines, tts = await live_assistant(make_settings, hermes, tmp_path, ack=True, ack_delay=0.05)
    await a.handle_utterance(SPEECH)
    spoken = [t for _, t in tts.at]  # cached phrases are not in tts.at; check the speaker order instead
    assert spoken == ["You have one meeting tomorrow at ten."]
    assert a.last_turn_timing["first_audio_s"] < 1.0  # something was said within a second


async def test_no_filler_when_the_answer_is_quick(make_settings, tmp_path):
    hermes = StreamingHermes(["Hello!"], delay=0.0)
    a, _lines, tts = await live_assistant(make_settings, hermes, tmp_path, ack=True, ack_delay=0.5)
    items = []
    orig = a._guard_name
    a._guard_name = lambda text, started: (started and items.append(text), orig(text, started))
    await a.handle_utterance(SPEECH)
    assert items == ["Hello!"]


async def test_a_false_done_after_a_failed_tool_is_replaced_before_it_is_spoken(make_settings, tmp_path):
    hermes = StreamingHermes(["Done! ", "Lunch is on your calendar."], tool="xenio_calendar_create_event",
                             tool_status="failed", delay=0.02)
    a, lines, tts = await live_assistant(make_settings, hermes, tmp_path, ack=False)
    await a.handle_utterance(SPEECH)
    spoken = [t for _, t in tts.at]
    assert spoken == ["The operation failed: Google refused the request (HTTP 403)."]
    assert "XENIO: The operation failed: Google refused the request (HTTP 403)." in lines


async def test_barge_in_stops_streamed_speech_and_hermes(make_settings, tmp_path):
    hermes = StreamingHermes([f"Sentence number {i}. " for i in range(20)], delay=0.05)
    cancelled = []

    async def cancel():
        cancelled.append(True)

    hermes.cancel = cancel
    a, lines, tts = await live_assistant(make_settings, hermes, tmp_path, ack=False)
    turn = asyncio.ensure_future(a.handle_utterance(SPEECH))
    await asyncio.sleep(0.3)
    a.barge_in("xenio")
    await asyncio.wait_for(turn, 5)
    assert "[interrupted]" in lines and len(tts.at) < 20
