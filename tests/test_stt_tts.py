"""STT/TTS adapters against httpx.MockTransport — no real API calls."""

import json
import logging

import httpx
import numpy as np
import pytest

from app.voice import _http
from app.voice.audio import encode_wav
from app.voice.stt import OpenAICompatibleSTT, STTError, STTTimeout, create_stt
from app.voice.tts import OpenAICompatibleTTS, TTSError, TTSTimeout, create_tts

WAV = encode_wav(np.zeros(1600, dtype=np.int16), 16000)
STREAMING_WAV = WAV[:4] + b"\xff\xff\xff\xff" + WAV[8:40] + b"\xff\xff\xff\xff" + WAV[44:]


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    async def instant(_delay):
        return None

    monkeypatch.setattr(_http.asyncio, "sleep", instant)


def transport(handler):
    calls = []

    def wrapped(request: httpx.Request):
        calls.append(request)
        return handler(request, len(calls))

    return httpx.MockTransport(wrapped), calls


# ---- STT -------------------------------------------------------------------


async def test_stt_success_sends_multipart_and_auth(make_settings):
    s = make_settings()
    t, calls = transport(lambda req, n: httpx.Response(200, json={"text": " What can you do? "}))
    stt = OpenAICompatibleSTT(s.stt, transport=t)
    assert await stt.transcribe(WAV) == "What can you do?"
    req = calls[0]
    assert req.headers["authorization"] == f"Bearer {s.stt.api_key.reveal()}"
    body = req.content.decode("latin-1")
    assert 'name="model"' in body and "whisper-large-v3-turbo" in body
    assert 'name="file"; filename="audio.wav"' in body
    await stt.aclose()


async def test_stt_sends_vocabulary_prompt(make_settings):
    t, calls = transport(lambda req, n: httpx.Response(200, json={"text": "list XenioTest"}))
    stt = OpenAICompatibleSTT(make_settings().stt, transport=t)
    await stt.transcribe(WAV, prompt="Xenio, XenioTest, hello.txt")
    body = calls[0].content.decode("latin-1")
    assert 'name="prompt"' in body and "XenioTest, hello.txt" in body


async def test_stt_retries_then_succeeds(make_settings):
    t, calls = transport(lambda req, n: httpx.Response(503) if n < 3 else httpx.Response(200, json={"text": "ok"}))
    stt = OpenAICompatibleSTT(make_settings().stt, transport=t)
    assert await stt.transcribe(WAV) == "ok"
    assert len(calls) == 3


async def test_stt_auth_error_is_not_retried_and_hints(make_settings):
    t, calls = transport(lambda req, n: httpx.Response(401, json={"error": "bad key"}))
    stt = OpenAICompatibleSTT(make_settings().stt, transport=t)
    with pytest.raises(STTError, match="401.*check STT_API_KEY") as info:
        await stt.transcribe(WAV)
    assert info.value.status == 401
    assert len(calls) == 1


async def test_stt_timeout_after_retries(make_settings):
    def boom(req, n):
        raise httpx.ReadTimeout("slow", request=req)

    t, calls = transport(boom)
    stt = OpenAICompatibleSTT(make_settings(STT_MAX_RETRIES="1").stt, transport=t)
    with pytest.raises(STTTimeout):
        await stt.transcribe(WAV)
    assert len(calls) == 2


async def test_stt_malformed_responses(make_settings):
    for response in (httpx.Response(200, content=b"{not json", headers={"content-type": "application/json"}),
                     httpx.Response(200, json={"nope": 1})):
        t, _ = transport(lambda req, n, r=response: r)
        with pytest.raises(STTError):
            await OpenAICompatibleSTT(make_settings().stt, transport=t).transcribe(WAV)


async def test_stt_rejects_empty_audio_and_missing_config(make_settings):
    t, calls = transport(lambda req, n: httpx.Response(200, json={"text": "x"}))
    with pytest.raises(STTError):
        await OpenAICompatibleSTT(make_settings().stt, transport=t).transcribe(b"")
    assert calls == []
    assert create_stt(make_settings(STT_API_KEY="").stt) is None


async def test_stt_never_logs_the_key(make_settings, caplog):
    s = make_settings()
    t, _ = transport(lambda req, n: httpx.Response(500, text="internal"))
    caplog.set_level(logging.DEBUG, logger="xenio")
    with pytest.raises(STTError):
        await OpenAICompatibleSTT(s.stt, transport=t).transcribe(WAV)
    assert s.stt.api_key.reveal() not in caplog.text


# ---- TTS -------------------------------------------------------------------


async def test_tts_success_requests_wav(make_settings):
    s = make_settings()
    # The real provider labels WAV as audio/mpeg and uses streaming 0xFFFFFFFF sizes.
    t, calls = transport(lambda req, n: httpx.Response(200, content=STREAMING_WAV, headers={"content-type": "audio/mpeg"}))
    tts = OpenAICompatibleTTS(s.tts, transport=t)
    assert await tts.synthesize("Hello there") == STREAMING_WAV
    body = json.loads(calls[0].content)
    assert body == {"model": "kokoro-tts", "input": "Hello there", "voice": "af_bella", "response_format": "wav"}
    assert calls[0].headers["authorization"] == f"Bearer {s.tts.api_key.reveal()}"


async def test_tts_rejects_mp3(make_settings):
    t, _ = transport(lambda req, n: httpx.Response(200, content=b"ID3\x04" + b"\x00" * 100))
    with pytest.raises(TTSError, match="MP3"):
        await OpenAICompatibleTTS(make_settings().tts, transport=t).synthesize("hi")


async def test_tts_errors(make_settings):
    t, calls = transport(lambda req, n: httpx.Response(429))
    with pytest.raises(TTSError, match="429"):
        await OpenAICompatibleTTS(make_settings(TTS_MAX_RETRIES="2").tts, transport=t).synthesize("hi")
    assert len(calls) == 3

    def boom(req, n):
        raise httpx.ConnectTimeout("no route", request=req)

    t, _ = transport(boom)
    with pytest.raises(TTSTimeout):
        await OpenAICompatibleTTS(make_settings(TTS_MAX_RETRIES="0").tts, transport=t).synthesize("hi")

    def refused(req, n):
        raise httpx.ConnectError("refused", request=req)

    t, _ = transport(refused)
    with pytest.raises(TTSError, match="connection failed"):
        await OpenAICompatibleTTS(make_settings(TTS_MAX_RETRIES="0").tts, transport=t).synthesize("hi")


async def test_tts_empty_text_and_missing_config(make_settings):
    t, calls = transport(lambda req, n: httpx.Response(200, content=WAV))
    with pytest.raises(TTSError):
        await OpenAICompatibleTTS(make_settings().tts, transport=t).synthesize("   ")
    assert calls == []
    assert create_tts(make_settings(TTS_API_URL="").tts) is None
