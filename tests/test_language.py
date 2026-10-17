"""V5.5 multilingual: language detection, language-independent intents, the reply-language note, multilingual STT,
the Hindi voice, the German-voice limitation, name repair and the voice emergency stop."""

import asyncio
import json
import time

import httpx
import numpy as np
import pytest

from app.config import Secret, STTSettings, TTSSettings
from app.language.detect import detect_language, language_note, parse_intents
from app.language.translit import sound_key, transliterate
from app.voice.local_stt import fix_names
from app.voice.stt import OpenAICompatibleSTT
from app.voice.tts import OpenAICompatibleTTS


def intents(text):
    return [(i.kind, i.target) for i in parse_intents(text)]


# ---- the spec's examples: one normalized intent whatever the language ---------------------------------------

@pytest.mark.parametrize("text,lang", [
    ("Open Chrome.", "en"), ("Chrome kholo.", "hi"), ("Chrome open kar do.", "hi"), ("Öffne Chrome.", "de"),
    ("Chrome kholo and search GitHub.", "hi"), ("Öffne Chrome und search GitHub.", "de"), ("Chrome खोलो।", "hi"),
    ("Hey Xenio, Discord kholo.", "hi"), ("Xenio Discord open kar do.", "hi"), ("Xenio, öffne Discord.", "de"),
    ("Discord öffnen.", "de"), ("Discord खोल दो", "hi"),
])
def test_open_chrome_or_discord_in_every_language(text, lang):
    info = detect_language(text)
    assert info.code == lang, text
    first = parse_intents(text)[0]
    assert first.kind == "OPEN_APPLICATION" and first.target in ("Chrome", "Discord"), text


@pytest.mark.parametrize("text,expected", [
    ("Xenio Chrome kholo and search for my GitHub.", [("OPEN_APPLICATION", "Chrome"), ("SEARCH", "GitHub")]),
    ("Chrome kholo and search GitHub.", [("OPEN_APPLICATION", "Chrome"), ("SEARCH", "GitHub")]),
    ("Öffne Chrome und search GitHub.", [("OPEN_APPLICATION", "Chrome"), ("SEARCH", "GitHub")]),
    ("Chrome kholo und suche nach GitHub.", [("OPEN_APPLICATION", "Chrome"), ("SEARCH", "GitHub")]),
    ("Chrome खोलो और GitHub सर्च करो.", [("OPEN_APPLICATION", "Chrome"), ("SEARCH", "GitHub")]),
    ("Xenio, open Discord und dann meinen GitHub öffnen.", [("OPEN_APPLICATION", "Discord"), ("OPEN_APPLICATION", "GitHub")]),
    ("Open OBS and फिर recording start करो.", [("OPEN_APPLICATION", "OBS"), ("START", "recording")]),
    ("Open Notepad and write Hello Xenio.", [("OPEN_APPLICATION", "Notepad"), ("TYPE_TEXT", "Hello Xenio")]),
    ("Switch back to Chrome.", [("SWITCH_TO", "Chrome")]), ("Minimize Discord.", [("MINIMIZE", "Discord")]),
    ("Close Notepad.", [("CLOSE_APPLICATION", "Notepad")]), ("Notepad band karo", [("CLOSE_APPLICATION", "Notepad")]),
    ("Stop.", [("STOP", "")]), ("What's my Python version?", []),
])
def test_code_switching_keeps_the_whole_instruction(text, expected):
    assert intents(text) == expected


def test_messages_keep_who_and_what():
    i = parse_intents("Discord kholo aur Rahul ko 'I'm coming' type karo.")
    assert (i[0].kind, i[0].target) == ("OPEN_APPLICATION", "Discord")
    assert (i[1].kind, i[1].target, i[1].extra["to"]) == ("TYPE_TEXT", "I'm coming", "Rahul")
    s = parse_intents("Open Discord and send Rahul hello.")[1]
    assert (s.kind, s.target, s.extra["to"]) == ("SEND_MESSAGE", "hello", "Rahul")


def test_mixed_detection_and_english_stays_english():
    assert detect_language("Chrome खोलो और GitHub सर्च करो.").mixed
    assert detect_language("Xenio Chrome kholo and search for my GitHub.").mixed
    for english in ("Open Chrome and search for GitHub.", "What's on my calendar tomorrow?", "Do it to the main file",
                    "Look at my screen."):
        assert detect_language(english).code == "en", english


def test_language_note():
    assert language_note("Open Chrome.") == ""  # English: nothing added
    hi = language_note("Chrome kholo and search GitHub.")
    assert "Hindi/Hinglish" in hi and "Devanagari" in hi and "ONE request" in hi and "open 'Chrome'; then search for 'GitHub'" in hi
    de = language_note("Öffne Discord.")
    assert "German" in de and "Reply in German" in de


def test_devanagari_names_by_sound():
    assert transliterate("डिस्कॉर्ड") == "diskord"
    assert sound_key("डिस्कॉर्ड") == sound_key("Discord") and sound_key("क्रोम") == sound_key("Chrome")[:3]


def test_name_repair_for_hindi_accents():
    assert fix_names("Open Notepad and write Hello Zinio.", "Xenio") == "Open Notepad and write Hello Xenio."
    assert fix_names("ज़ेनियो, क्रोम खोलो", "Xenio").startswith("Xenio")


# ---- multilingual speech recognition --------------------------------------------------------------------------

def stt_settings(**kw):
    base = dict(api_key=Secret("k" * 12), api_url="https://stt.example/v1/audio/transcriptions", model="whisper",
                language="", timeout=10, max_retries=0)
    return STTSettings(**{**base, **kw})


def mock_stt(answers):
    seen = []

    def handler(request: httpx.Request):
        body = request.content.decode("latin-1")
        seen.append({"verbose": "verbose_json" in body,
                     "language": next((l for l in ("hi", "de", "en") if f'name="language"\r\n\r\n{l}' in body), None)})
        text, lang = answers.pop(0)
        return httpx.Response(200, json={"text": text, "language": lang})

    return httpx.MockTransport(handler), seen


async def test_language_is_detected_not_forced():
    transport, seen = mock_stt([("Chrome खोलो और GitHub सर्च करो.", "hi")])
    stt = OpenAICompatibleSTT(stt_settings(), transport=transport)
    assert await stt.transcribe(b"RIFF") == "Chrome खोलो और GitHub सर्च करो."
    assert seen == [{"verbose": True, "language": None}] and stt.last_language == "hi"


async def test_a_misdetected_language_is_transcribed_again():
    transport, seen = mock_stt([("کروم کھولو", "urdu"), ("क्रोम खोलो", "hi")])  # Hindi heard as Urdu
    stt = OpenAICompatibleSTT(stt_settings(), transport=transport)
    assert await stt.transcribe(b"RIFF") == "क्रोम खोलो"
    assert seen[1]["language"] == "hi" and stt.last_language == "hi"
    transport, seen = mock_stt([("Hallo", "nl"), ("Hallo", "de")])
    assert await OpenAICompatibleSTT(stt_settings(), transport=transport).transcribe(b"RIFF") == "Hallo"
    assert seen[1]["language"] == "de"
    transport, seen = mock_stt([("Bonjour", "fr"), ("Hello", "en")])  # nothing close: the first expected language
    assert await OpenAICompatibleSTT(stt_settings(), transport=transport).transcribe(b"RIFF") == "Hello"
    assert seen[1]["language"] == "en"


async def test_a_forced_language_is_still_honoured():
    transport, seen = mock_stt([("Open Chrome", "en")])
    await OpenAICompatibleSTT(stt_settings(language="en"), transport=transport).transcribe(b"RIFF")
    assert seen == [{"verbose": False, "language": "en"}]


# ---- multilingual speech output --------------------------------------------------------------------------------

def tts(**kw):
    base = dict(api_key=Secret("k" * 12), api_url="https://tts.example/v1/audio/speech", model="kokoro", voice="af_bella",
                response_format="wav", timeout=10, max_retries=0)
    return OpenAICompatibleTTS(TTSSettings(**{**base, **kw}))


def test_voice_follows_the_language():
    t = tts()
    assert t.voice_for("Chrome is open.") == "af_bella"
    assert t.voice_for("Discord खोल दिया।") == "hf_alpha"  # the Hindi voice pronounces Devanagari
    assert t.voice_for("VS Code ist geöffnet.") is None and not t.can_speak("Discord ist jetzt geöffnet.")
    assert tts(voice_de="de_voice").voice_for("VS Code ist geöffnet.") == "de_voice"


async def test_unspeakable_text_is_shown_and_explained_once():
    from app.voice.live import SpeechQueue

    class Speaker:
        played = 0

        async def play(self, pcm, rate):
            Speaker.played += 1
            return True

        def stop(self):
            pass

    class FakeTTS:
        def can_speak(self, text):
            return "geöffnet" not in text

        async def synthesize(self, text):
            from app.voice.audio import encode_wav

            return encode_wav(np.zeros(240, dtype=np.int16), 24000)

    q = SpeechQueue(FakeTTS(), Speaker())
    skipped = []
    q.on_unspeakable = skipped.append
    q.say("Discord ist geöffnet.")
    q.say("Done.")
    await q.drain()
    assert skipped == ["Discord ist geöffnet."] and Speaker.played == 1


async def test_assistant_says_once_why_german_is_not_spoken(make_settings):
    from tests.test_voice_assistant import make_assistant
    from app.hermes.client import HermesReply
    from tests.test_orchestrator import FakeHermes

    a, lines, _ = make_assistant(make_settings, ["Öffne Discord."],
                                 hermes=FakeHermes(HermesReply("Discord ist jetzt geöffnet.")))
    a.tts.can_speak = lambda text: "geöffnet" not in text
    a.settings = a.settings.__class__(**{**a.settings.__dict__, "voice": a.settings.voice.__class__(
        **{**a.settings.voice.__dict__, "stream_replies": False})})
    await a.handle_utterance(np.zeros(16000, dtype=np.int16))
    assert "XENIO: Discord ist jetzt geöffnet." in lines and any("No German voice" in line for line in lines)
    assert a.tts.texts == ["I can't speak German with my voice, so my answer is on your screen."]


async def test_orchestrator_adds_the_language_note(make_settings):
    from pathlib import Path

    from app.hermes.client import HermesReply
    from app.orchestrator import Orchestrator
    from app.safety.permissions import PermissionGate
    from tests.test_orchestrator import FakeHermes

    hermes = FakeHermes(HermesReply("ठीक है"))
    orch = Orchestrator(hermes, PermissionGate(None), Path(make_settings().hermes.workspace))
    await orch.process_text("Discord kholo")
    await orch.process_text("Open Discord")
    assert "[XENIO language: the user spoke Hindi/Hinglish" in hermes.messages[0]
    assert hermes.messages[1] == "Open Discord"  # English: unchanged


# ---- the voice emergency stop -----------------------------------------------------------------------------------

async def test_saying_stop_also_stops_the_hands(make_settings):
    from app.desktop.stop import stop_requested_since
    from tests.test_voice_assistant import FakeDetector, make_assistant
    from app.voice.assistant import State

    a, lines, _ = make_assistant(make_settings, [])
    started = time.time()
    a.busy_stop = FakeDetector("stop")
    a.speaker.playing = False  # XENIO is working silently (opening an app, typing)
    a.set_state(State.THINKING)
    a.feed_frame(np.zeros(1280, dtype=np.int16))
    assert a.interrupted and stop_requested_since(started, a.settings.data_dir)


async def test_a_stop_from_the_last_request_cannot_cancel_the_next(make_settings):
    """Measured live: busy_stop kept the audio of the "stop" that ended one request and fired 1 s into the next."""
    from tests.test_voice_assistant import FakeDetector, make_assistant
    from app.voice.assistant import State

    class Remembers(FakeDetector):
        def reset(self):
            self.fire_on = None  # the buffered "stop" audio is gone

    a, lines, _ = make_assistant(make_settings, [])
    a.busy_stop = Remembers("stop")
    a.speaker.playing = False
    a.set_state(State.IDLE)
    a.set_state(State.THINKING)  # the next request starts
    a.feed_frame(np.zeros(1280, dtype=np.int16))
    assert not a.interrupted


async def test_bare_stop_is_ignored_while_xenio_talks(make_settings):
    from tests.test_voice_assistant import FakeDetector, make_assistant
    from app.voice.assistant import State

    a, lines, _ = make_assistant(make_settings, [])
    a.busy_stop = FakeDetector("stop")
    a.speaker.playing = True  # its own reply might contain the word "stop"
    a.set_state(State.SPEAKING)
    a.feed_frame(np.zeros(1280, dtype=np.int16))
    assert not a.interrupted
