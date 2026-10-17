"""Turn-taking: early transcription, finished/unfinished sentences, closers, noise, talk-over, warm-up."""

import asyncio

import httpx

from app.config import Secret, STTSettings
from app.hermes.client import HermesReply
from app.voice._http import KEEPALIVE_SECONDS, ConnectionWarmer
from app.voice.assistant import MSG_STT_FAILED, State, _echoes_prompt
from app.voice.audio import FRAME_SAMPLES, TalkOverDetector, UtteranceRecorder, rms
from app.voice.live import PHRASES
from app.voice.stt import OpenAICompatibleSTT
from app.voice.turns import EarlyTranscriber, classify_utterance, closing_reply, sounds_unfinished
from tests.test_orchestrator import FakeHermes
from tests.test_voice_assistant import SPEECH, FakeSTT, _level_frame, make_assistant

LOUD, QUIET = _level_frame(400), _level_frame(5)


class LevelVad:
    """Stands in for Silero: a frame is speech when it is loud."""

    def __init__(self):
        self.calls = 0

    def is_speech(self, frame):
        self.calls += 1
        return rms(frame) > 100

    def reset(self):
        pass


# ---- what was said -----------------------------------------------------------------------------------------

def test_unfinished_sentences_are_recognised():
    for text in ("Open the", "Search for, um", "What's the weather and", "Send it to", "Tell me about...",
                 "Hey Xenio", "Remind me to call mom at", ""):
        assert sounds_unfinished(text), text
    for text in ("Open the downloads folder.", "What time is it?", "Open Chrome please.", "Play Starboy on YouTube.",
                 "Close all the tabs and close Chrome on my laptop."):
        assert not sounds_unfinished(text), text


def test_requests_closers_and_noise():
    # the user's real session: these were sent to Hermes as requests before
    assert classify_utterance("Stop Xenio.") == "closer"
    assert classify_utterance("Xenio, stop.") == "closer"
    assert classify_utterance("Never mind.") == "closer"
    assert classify_utterance("Thanks Xenio!") == "closer"
    assert classify_utterance("That's all, thanks.") == "closer"
    assert classify_utterance("Bye!") == "closer"
    # requests stay requests, even when they start like a closer
    assert classify_utterance("Stop the music.") == "request"
    assert classify_utterance("No, open the other one.") == "request"
    assert classify_utterance("Close all the tabs and close Chrome on my laptop.") == "request"
    # Whisper's well-known phantoms on silence or music
    for phantom in ("you", "Thanks for watching!", "[Music]", "  ...  ", "Subtitles by the Amara.org community", "Hey Xenio."):
        assert classify_utterance(phantom) == "noise", phantom


def test_okay_and_no_answer_a_question_instead_of_closing():
    for answer in ("No.", "Okay.", "Sure.", "Nope"):
        assert classify_utterance(answer) == "closer"
        assert classify_utterance(answer, after_question=True) == "request", answer
    assert classify_utterance("No thanks.", after_question=True) == "closer"  # declines AND closes
    assert classify_utterance("Never mind.", after_question=True) == "closer"


def test_closing_replies_are_cached_phrases():
    assert closing_reply("Thank you so much.") == "Anytime."
    assert closing_reply("Okay bye.") == "Bye for now."
    assert closing_reply("Stop Xenio.") == "Okay."
    assert closing_reply("No thanks.") == "Okay."
    for reply in ("Okay.", "Anytime.", "Bye for now.", MSG_STT_FAILED):
        assert reply in PHRASES  # pre-synthesized: spoken without any delay


def test_whisper_echoing_its_prompt_is_not_speech():
    prompt = "Xenio, hello.txt. File names are written like notes.txt or report.pdf."
    assert _echoes_prompt("File names are written like notes.txt.", prompt)
    assert not _echoes_prompt("Open notes.txt", prompt)
    assert not _echoes_prompt("Xenio.", prompt)  # too short to judge


# ---- early transcription -----------------------------------------------------------------------------------

def _recorder(patience=1.6):
    return UtteranceRecorder(silence_seconds=patience, max_seconds=15, start_timeout=6, vad=LevelVad())


async def _feed(recorder, early, frames):
    """Feed frames like the listen loop (one every 80 ms of audio, the event loop running in between).
    Returns the number of frames fed when the turn ended (None if it did not end)."""
    for i, frame in enumerate(frames, 1):
        done = recorder.feed(frame)
        if not done and early.update(recorder):
            recorder.finish()
            done = True
        for _ in range(3):
            await asyncio.sleep(0)
        if done:
            return i
    return None


async def test_a_finished_sentence_ends_the_turn_when_its_transcript_arrives():
    calls = []

    async def transcribe(audio):
        calls.append(len(audio))
        return "What's on my calendar tomorrow?"

    rec = _recorder()
    early = EarlyTranscriber(transcribe, end_seconds=0.55, patience_seconds=1.6)
    ended = await _feed(rec, early, [LOUD] * 10 + [QUIET] * 30)
    # speech 10 frames, then the early transcript starts after 0.25 s of silence and ends the turn at 0.55 s
    assert ended is not None and ended - 10 <= round(0.55 / 0.08) + 1
    assert early.ended_early and early.requests == 1
    assert await early.result(rec, rec.audio()) == "What's on my calendar tomorrow?"
    assert len(calls) == 1  # the early transcript was reused: no second request


async def test_a_sentence_that_trails_off_gets_more_time():
    async def transcribe(audio):
        return "Open the"

    rec = _recorder(patience=1.6)
    early = EarlyTranscriber(transcribe, end_seconds=0.55, patience_seconds=1.6)
    ended = await _feed(rec, early, [LOUD] * 10 + [QUIET] * 40)
    assert ended is not None and (ended - 10) * 0.08 >= 1.6 - 0.01  # waited the full patience
    assert not early.ended_early


async def test_speaking_again_discards_the_early_transcript():
    texts = iter(["Open the", "Open the downloads folder."])
    calls = []

    async def transcribe(audio):
        calls.append(len(audio))
        await asyncio.sleep(0)
        return next(texts)

    rec = _recorder()
    early = EarlyTranscriber(transcribe, end_seconds=0.55, patience_seconds=1.6)
    # "open the" ... (0.4 s pause) ... "downloads folder" ... silence
    ended = await _feed(rec, early, [LOUD] * 6 + [QUIET] * 5 + [LOUD] * 8 + [QUIET] * 30)
    assert ended is not None and early.requests == 2
    final = await early.result(rec, rec.audio())
    assert final == "Open the downloads folder." and calls[1] > calls[0]  # the second covers the whole request


async def test_a_failed_early_request_falls_back_to_the_whole_turn():
    attempts = []

    async def transcribe(audio):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("network")
        return "hello"

    rec = _recorder(patience=0.8)
    early = EarlyTranscriber(transcribe, end_seconds=0.55, patience_seconds=0.8)
    assert await _feed(rec, early, [LOUD] * 6 + [QUIET] * 20) is not None
    assert await early.result(rec, rec.audio()) == "hello" and len(attempts) == 2


async def test_the_assistant_uses_the_early_transcript(make_settings):
    stt = FakeSTT(["What's the time?"])  # only ONE answer: a second request would fail the test
    a, lines, hermes = make_assistant(make_settings, [])
    a.stt, a.vad = stt, LevelVad()
    recorder = a._begin_recording()
    for frame in [LOUD] * 8 + [QUIET] * 25:
        a.feed_frame(frame)
        for _ in range(3):
            await asyncio.sleep(0)
        if a._recorder is None:
            break
    audio = await a._finish_recording(recorder)
    assert audio is not None
    assert await a.handle_utterance(audio) is True
    assert hermes.messages[0].startswith("What's the time?")


# ---- conversation control --------------------------------------------------------------------------------

async def test_a_closer_ends_the_conversation_without_hermes(make_settings):
    for said, reply in (("Thanks, that's all.", "Anytime."), ("Stop Xenio.", "Okay."), ("Bye!", "Bye for now.")):
        a, lines, hermes = make_assistant(make_settings, [said])
        assert await a.handle_utterance(SPEECH, follow_up=True) is False
        assert hermes.messages == [] and f"XENIO: {reply}" in lines


async def test_noise_during_a_follow_up_is_ignored_silently(make_settings):
    a, lines, hermes = make_assistant(make_settings, ["Thanks for watching!"])
    assert await a.handle_utterance(SPEECH, follow_up=True) is True  # keep listening
    assert hermes.messages == [] and not any(line.startswith("XENIO:") for line in lines)
    a, lines, hermes = make_assistant(make_settings, ["you"])
    await a.handle_utterance(SPEECH)  # after the wake word the user expects an answer
    assert f"XENIO: {MSG_STT_FAILED}" in lines


async def test_no_is_an_answer_after_a_question(make_settings):
    a, lines, hermes = make_assistant(make_settings, ["Open my downloads", "No."],
                                      hermes=FakeHermes(HermesReply("Do you mean the Downloads folder?")))
    await a.handle_utterance(SPEECH)
    assert await a.handle_utterance(SPEECH, follow_up=True) is True
    assert [m.split("\n")[0] for m in hermes.messages] == ["Open my downloads", "No."]


async def test_closer_ends_conversation_mode_in_the_loop(make_settings):
    from tests.test_voice_assistant import FakeSpeaker, FakeTTS, FireOnce, ScriptedMic
    from app.orchestrator import Orchestrator
    from app.safety.permissions import PermissionGate
    from app.voice.assistant import VoiceAssistant
    from pathlib import Path

    settings = make_settings(XENIO_FOLLOW_UP_SECONDS="5", XENIO_SILENCE_SECONDS="0.4")
    hermes = FakeHermes(HermesReply("It's ten o'clock."))
    lines = []
    script = ([QUIET] * 5 + [_level_frame(300)] * 6 + [_level_frame(300)] * 6 + [QUIET] * 8
              + [_level_frame(300)] * 6 + [QUIET] * 8)
    mic = ScriptedMic(script)
    a = VoiceAssistant(settings, Orchestrator(hermes, PermissionGate(None), Path(settings.hermes.workspace)),
                       FakeSTT(["what's the time", "thanks"]), FakeTTS(), mic, FakeSpeaker(), FireOnce(at=11), None,
                       out=lines.append)
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
    assert [m.split("\n")[0] for m in hermes.messages] == ["what's the time"]  # "thanks" never reached Hermes
    assert "XENIO: Anytime." in lines and any("when you need me" in line for line in lines)


# ---- interrupting ------------------------------------------------------------------------------------------

class PlayingSpeaker:
    def __init__(self):
        self.playing = False
        self.stopped = 0

    async def play(self, pcm, rate):
        return True

    def stop(self):
        self.stopped += 1


def test_talk_over_needs_speech_not_just_a_loud_sound():
    t = TalkOverDetector()
    t.begin_playback()
    for _ in range(30):
        t.update(2, 100)  # no echo: armed
    t.end_playback(interrupted=False)
    assert [t.update(400, 100, speech=False) for _ in range(6)] == [False] * 6  # a door, a cough, music
    assert [t.update(400, 100, speech=True) for _ in range(4)][-1] is True


async def test_talk_over_only_learns_from_real_playback(make_settings):
    a, lines, _ = make_assistant(make_settings, [], speaker=PlayingSpeaker())
    a.vad = LevelVad()
    a.set_state(State.SPEAKING)
    for _ in range(20):  # Hermes still thinking after "One moment.": nothing is playing
        a.feed_frame(QUIET)
    assert a._talkover._levels == [] and a.vad.calls == 0
    a.speaker.playing = True
    a.feed_frame(QUIET)
    assert len(a._talkover._levels) == 1 and a.vad.calls == 1


async def test_words_said_while_interrupting_are_kept(make_settings):
    a, lines, _ = make_assistant(make_settings, [], speaker=PlayingSpeaker())
    a._threshold = 100.0
    a._stop = None
    a.set_state(State.SPEAKING)
    a.speaker.playing = True
    a._talkover.echo_level, a._talkover.echo_peak = 2.0, 5.0  # headphones: talk-over armed
    for _ in range(4):  # "No, I meant..." over XENIO's voice
        a.feed_frame(_level_frame(400))
    assert a.interrupted and a._interrupt_recorder is not None
    recorder = a._interrupt_recorder
    for frame in [_level_frame(400)] * 5 + [QUIET] * 14:  # "...the other one"
        a.feed_frame(frame)
    audio = await a._finish_recording(recorder)
    assert audio is not None and len(audio) >= (4 + 5) * FRAME_SAMPLES  # the first words are in the recording


# ---- connections ------------------------------------------------------------------------------------------

class _Warmable:
    def __init__(self):
        self.warmed = 0

    def warm(self):
        self.warmed += 1


async def test_connections_are_warmed_when_the_user_starts_talking(make_settings):
    a, _, _ = make_assistant(make_settings, [])
    a.stt, a.tts = _Warmable(), _Warmable()
    a._threshold = 100.0
    recorder = a._begin_recording()
    a.feed_frame(QUIET)
    assert a.stt.warmed == 0
    a.feed_frame(LOUD)
    assert a.stt.warmed == 1 and a.tts.warmed == 1
    a.feed_frame(LOUD)
    assert a.stt.warmed == 1  # once per utterance
    recorder.finish()


async def test_warm_up_sends_no_credentials_and_keeps_the_connection():
    seen = []

    def handler(request: httpx.Request):
        seen.append((request.method, str(request.url), request.headers.get("authorization")))
        return httpx.Response(200, json={"text": "hi"})

    settings = STTSettings(api_key=Secret("sk-test-not-real"), api_url="https://api.example.test/v1/audio/transcriptions",
                           model="whisper", language="", timeout=10, max_retries=0)
    stt = OpenAICompatibleSTT(settings, transport=httpx.MockTransport(handler))
    stt.warm()
    stt.warm()  # rate-limited: one warm-up
    await asyncio.sleep(0.05)
    assert seen == [("HEAD", "https://api.example.test/", None)]
    await stt.transcribe(b"RIFF....WAVE")
    stt.warm()  # just used: nothing to warm
    await asyncio.sleep(0.05)
    assert [s[0] for s in seen] == ["HEAD", "POST"] and seen[1][2] == "Bearer sk-test-not-real"
    assert KEEPALIVE_SECONDS >= 60
    await stt.aclose()


async def test_warmer_without_a_loop_or_url_does_nothing():
    warmer = ConnectionWarmer(httpx.AsyncClient(), "")
    warmer.warm()
    assert warmer._task is None
