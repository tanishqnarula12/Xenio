"""Text-to-speech: provider interface + OpenAI-compatible implementation.

The configured provider is nabh.cloud's kokoro-tts endpoint (OpenAI ``/audio/speech`` JSON
body). XENIO asks for ``response_format: "wav"`` so playback needs no MP3 codec. Verified
behaviour of that endpoint: it honours ``wav`` (a streaming RIFF header with 0xFFFFFFFF
sizes) but labels every response ``audio/mpeg``, so the format is detected from the bytes.
"""

from __future__ import annotations

import logging
import re
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from urllib.parse import urlsplit

import httpx
import numpy as np

from app.config import TTSSettings
from app.logging_setup import event
from app.voice._http import (
    ConnectionWarmer,
    ProviderConnectionError,
    ProviderTimeout,
    error_snippet,
    new_client,
    post_with_retries,
)

log = logging.getLogger("xenio.voice.tts")
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")


class TTSError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class TTSTimeout(TTSError):
    pass


class TTSProvider(ABC):
    @abstractmethod
    async def synthesize(self, text: str) -> bytes:
        """Return WAV audio bytes for ``text``."""

    async def synthesize_stream(self, text: str) -> AsyncIterator[tuple[int, np.ndarray]]:
        """(sample_rate, int16 mono samples) chunks as soon as they arrive. Default: the whole clip at once."""
        from app.voice.audio import decode_wav

        pcm, rate = decode_wav(await self.synthesize(text))
        yield rate, (pcm[:, 0] if pcm.ndim == 2 else pcm)

    def warm(self) -> None:
        """Get ready for a request that is about to come (e.g. open the network connection)."""
        return None

    async def aclose(self) -> None:
        return None


class WavStreamParser:
    """Incremental RIFF/WAVE reader for 16-bit PCM: header first, then samples as bytes arrive.

    nabh's Kokoro streams its WAV with 0xFFFFFFFF sizes, so the data chunk is read until the stream ends."""

    def __init__(self) -> None:
        self._buf = b""
        self.rate: int | None = None
        self.channels = 1
        self._in_data = False
        self.unsupported: str | None = None

    def feed(self, data: bytes) -> np.ndarray | None:
        self._buf += data
        if not self._in_data and not self._read_header():
            return None
        usable = len(self._buf) - len(self._buf) % (2 * self.channels)
        if not usable:
            return None
        samples = np.frombuffer(self._buf[:usable], dtype="<i2")
        self._buf = self._buf[usable:]
        if self.channels > 1:
            samples = samples.reshape(-1, self.channels).mean(axis=1).astype(np.int16)
        return samples

    def _read_header(self) -> bool:
        buf = self._buf
        if len(buf) < 12:
            return False
        if buf[:4] != b"RIFF" or buf[8:12] != b"WAVE":
            self.unsupported = "not a WAV stream"
            return False
        pos = 12
        while len(buf) >= pos + 8:
            chunk_id, size = buf[pos:pos + 4], int.from_bytes(buf[pos + 4:pos + 8], "little")
            if chunk_id == b"data":
                if self.rate is None:
                    self.unsupported = "WAV data before its format"
                    return False
                self._buf, self._in_data = buf[pos + 8:], True
                return True
            if len(buf) < pos + 8 + size:
                return False  # wait for the rest of this chunk
            if chunk_id == b"fmt ":
                fmt, channels, rate = (int.from_bytes(buf[pos + 8:pos + 10], "little"),
                                       int.from_bytes(buf[pos + 10:pos + 12], "little"),
                                       int.from_bytes(buf[pos + 12:pos + 16], "little"))
                bits = int.from_bytes(buf[pos + 22:pos + 24], "little")
                if fmt not in (1, 0xFFFE) or bits != 16:
                    self.unsupported = f"WAV format {fmt}/{bits}-bit"
                    return False
                self.rate, self.channels = rate, max(1, channels)
            pos += 8 + size + (size & 1)
        return False


class OpenAICompatibleTTS(TTSProvider):
    def __init__(self, settings: TTSSettings, transport: httpx.AsyncBaseTransport | None = None):
        if not settings.configured:
            raise TTSError("TTS is not configured: set TTS_API_KEY and TTS_API_URL in .env")
        self._settings = settings
        self._client = new_client(settings.timeout, transport)
        self._warmer = ConnectionWarmer(self._client, settings.api_url)

    def warm(self) -> None:
        self._warmer.warm()

    def voice_for(self, text: str) -> str | None:
        """The voice for this text (V5.5): Devanagari -> the Hindi voice; German -> the German voice if one is set
        (none exists on nabh/Kokoro or in this Windows, so None = cannot be spoken properly); else the default."""
        s = self._settings
        if _DEVANAGARI.search(text or ""):
            return s.voice_hi or None
        from app.language.detect import detect_language

        if detect_language(text).code == "de":
            return s.voice_de or None
        return s.voice

    def can_speak(self, text: str) -> bool:
        return self.voice_for(text) is not None

    @property
    def host(self) -> str:
        return urlsplit(self._settings.api_url).netloc

    async def synthesize(self, text: str) -> bytes:
        text = text.strip()
        if not text:
            raise TTSError("Nothing to synthesize")
        s = self._settings
        voice = self.voice_for(text) or s.voice
        body = {"model": s.model, "input": text, "voice": voice, "response_format": s.response_format}
        event(log, "TTS started", host=self.host, model=s.model, voice=voice, chars=len(text))
        started = time.monotonic()
        self._warmer.touch()
        try:
            response = await post_with_retries(
                self._client,
                s.api_url,
                what="TTS request",
                max_retries=s.max_retries,
                headers={"Authorization": f"Bearer {s.api_key.reveal()}"},
                json=body,
            )
        except ProviderTimeout as exc:
            raise TTSTimeout(str(exc)) from exc
        except ProviderConnectionError as exc:
            raise TTSError(str(exc)) from exc

        if response.status_code >= 400:
            hint = " (check TTS_API_KEY)" if response.status_code in (401, 403) else ""
            raise TTSError(f"TTS failed with HTTP {response.status_code}{hint}: {error_snippet(response)}", response.status_code)
        audio = response.content
        if audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
            kind = "MP3" if audio[:3] == b"ID3" or audio[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2") else "unknown"
            raise TTSError(f"TTS returned {kind} audio instead of WAV ({len(audio)} bytes)")
        event(log, "TTS finished", ms=int((time.monotonic() - started) * 1000), bytes=len(audio))
        return audio

    async def synthesize_stream(self, text: str) -> AsyncIterator[tuple[int, np.ndarray]]:
        """Audio while it is being generated: measured on nabh.cloud, the first bytes arrive after ~0.35-0.6 s
        while the whole sentence takes 0.9-1.4 s, so playback can start that much earlier."""
        text = text.strip()
        if not text:
            return
        s = self._settings
        body = {"model": s.model, "input": text, "voice": self.voice_for(text) or s.voice, "response_format": s.response_format}
        started = time.monotonic()
        self._warmer.touch()
        parser, first = WavStreamParser(), None
        try:
            async with self._client.stream("POST", s.api_url, json=body,
                                           headers={"Authorization": f"Bearer {s.api_key.reveal()}"}) as response:
                if response.status_code >= 400:
                    await response.aread()
                    hint = " (check TTS_API_KEY)" if response.status_code in (401, 403) else ""
                    raise TTSError(f"TTS failed with HTTP {response.status_code}{hint}: {error_snippet(response)}",
                                   response.status_code)
                async for data in response.aiter_bytes():
                    samples = parser.feed(data)
                    if parser.unsupported:
                        raise TTSError(f"TTS stream not playable: {parser.unsupported}")
                    if samples is not None and len(samples):
                        if first is None:
                            first = time.monotonic() - started
                        yield parser.rate, samples
        except httpx.TimeoutException as exc:
            raise TTSTimeout("TTS request timed out") from exc
        except httpx.TransportError as exc:
            raise TTSError(f"TTS connection failed: {type(exc).__name__}") from exc
        event(log, "TTS streamed", chars=len(text), first_audio_ms=int((first or 0) * 1000),
              ms=int((time.monotonic() - started) * 1000))

    async def aclose(self) -> None:
        self._warmer.cancel()
        await self._client.aclose()


def create_tts(settings: TTSSettings) -> TTSProvider | None:
    """The configured provider, or None when TTS credentials are missing."""
    return OpenAICompatibleTTS(settings) if settings.configured else None
