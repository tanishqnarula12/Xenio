"""Speech-to-text: provider interface + OpenAI-compatible implementation.

The configured provider is nabh.cloud's whisper-large-v3-turbo endpoint, which takes the
OpenAI ``/audio/transcriptions`` multipart format and returns ``{"text": ...}``. Any other
OpenAI-compatible endpoint (OpenAI, Groq, a local faster-whisper server) works by changing
``STT_API_URL`` / ``STT_MODEL``. A different wire format = a new ``STTProvider`` subclass.
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from urllib.parse import urlsplit

import httpx

from app.config import STTSettings
from app.logging_setup import event
from app.voice._http import (
    ConnectionWarmer,
    ProviderConnectionError,
    ProviderTimeout,
    error_snippet,
    new_client,
    post_with_retries,
)

log = logging.getLogger("xenio.voice.stt")


class STTError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class STTTimeout(STTError):
    pass


class STTProvider(ABC):
    @abstractmethod
    async def transcribe(self, audio_data: bytes, *, filename: str = "audio.wav", content_type: str = "audio/wav",
                         prompt: str | None = None) -> str:
        """Return the transcript ('' when no speech was recognised). ``prompt`` = vocabulary hints."""

    def warm(self) -> None:
        """Get ready for a request that is about to come (e.g. open the network connection)."""
        return None

    async def aclose(self) -> None:
        return None


_LANGUAGE_NAMES = {"english": "en", "hindi": "hi", "german": "de", "urdu": "ur", "punjabi": "pa", "nepali": "ne",
                   "marathi": "mr", "bengali": "bn", "gujarati": "gu", "sanskrit": "sa", "dutch": "nl", "afrikaans": "af",
                   "luxembourgish": "lb", "danish": "da", "swedish": "sv", "norwegian": "no"}
# A detection outside the expected languages is usually a near neighbour of one of them.
_NEAREST = {"ur": "hi", "pa": "hi", "ne": "hi", "mr": "hi", "bn": "hi", "gu": "hi", "sa": "hi", "nl": "de", "af": "de",
            "lb": "de", "da": "de", "sv": "de", "no": "de", "nn": "de", "yi": "de"}


def _language_of(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return ""
    raw = str(payload.get("language") or "").strip().lower() if isinstance(payload, dict) else ""
    return _LANGUAGE_NAMES.get(raw, raw)


class OpenAICompatibleSTT(STTProvider):
    def __init__(self, settings: STTSettings, transport: httpx.AsyncBaseTransport | None = None,
                 hedge_after: float = 0.0):
        if not settings.configured:
            raise STTError("STT is not configured: set STT_API_KEY and STT_API_URL in .env")
        self._settings = settings
        self.hedge_after = hedge_after  # seconds; 0 = never send a second request
        self.last_language = ""  # language of the last transcript (detected or forced)
        self._client = new_client(settings.timeout, transport)
        self._warmer = ConnectionWarmer(self._client, settings.api_url)

    def warm(self) -> None:
        self._warmer.warm()

    @property
    def host(self) -> str:
        return urlsplit(self._settings.api_url).netloc

    async def transcribe(self, audio_data: bytes, *, filename: str = "audio.wav", content_type: str = "audio/wav",
                         prompt: str | None = None) -> str:
        """Hedged: the hosted Whisper usually answers in ~1.2 s but sometimes takes 5-9 s (measured). If the
        first request is still running after ``hedge_after`` seconds, an identical second one is sent and the
        first answer wins; the other is cancelled."""
        if not audio_data:
            raise STTError("No audio to transcribe")
        once = lambda: self._transcribe_once(audio_data, filename, content_type, prompt)  # noqa: E731
        if self.hedge_after <= 0:
            return await once()
        first = asyncio.ensure_future(once())
        done, _ = await asyncio.wait({first}, timeout=self.hedge_after)
        if done:
            return first.result()
        event(log, "STT slow; sending a second request", after_s=self.hedge_after)
        pending, error = {first, asyncio.ensure_future(once())}, None
        try:
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if task.exception() is None:
                        return task.result()
                    error = task.exception()
        finally:
            for task in pending:
                task.cancel()
        assert error is not None
        raise error

    async def _transcribe_once(self, audio_data: bytes, filename: str, content_type: str, prompt: str | None,
                               language: str | None = None) -> str:
        """V5.5 multilingual: without a forced language Whisper detects it (``verbose_json`` reports which). A
        detection outside the expected languages (measured risk: Hindi heard as Urdu, written in Urdu script) is
        transcribed once more in the nearest expected language."""
        s = self._settings
        forced = language or s.language
        detect = not forced and bool(s.languages)
        data = {"model": s.model, "response_format": "verbose_json" if detect else "json"}
        if forced:
            data["language"] = forced
        if prompt:
            # Whisper uses the prompt as preceding context: it biases spelling of names ("XenioTest", "hello.txt").
            data["prompt"] = prompt[:600]
        event(log, "STT request started", host=self.host, model=s.model, bytes=len(audio_data))
        started = time.monotonic()
        self._warmer.touch()
        try:
            response = await post_with_retries(
                self._client,
                s.api_url,
                what="STT request",
                max_retries=s.max_retries,
                headers={"Authorization": f"Bearer {s.api_key.reveal()}"},
                data=data,
                files={"file": (filename, audio_data, content_type)},
            )
        except ProviderTimeout as exc:
            raise STTTimeout(str(exc)) from exc
        except ProviderConnectionError as exc:
            raise STTError(str(exc)) from exc

        if response.status_code >= 400:
            hint = " (check STT_API_KEY)" if response.status_code in (401, 403) else ""
            raise STTError(f"STT failed with HTTP {response.status_code}{hint}: {error_snippet(response)}", response.status_code)
        text = self._parse(response)
        detected = _language_of(response) if detect else ""
        event(log, "STT request finished", ms=int((time.monotonic() - started) * 1000), chars=len(text),
              **({"language": detected} if detected else {}))
        if detect and detected and detected not in s.languages:
            retry = _NEAREST.get(detected) if _NEAREST.get(detected) in s.languages else s.languages[0]
            event(log, "STT language outside the expected ones; transcribing again", detected=detected, retry=retry)
            return await self._transcribe_once(audio_data, filename, content_type, prompt, language=retry)
        self.last_language = detected or forced or ""
        return text

    @staticmethod
    def _parse(response: httpx.Response) -> str:
        if "json" in response.headers.get("content-type", ""):
            try:
                payload = response.json()
            except ValueError as exc:
                raise STTError("STT returned malformed JSON") from exc
            text = payload.get("text") if isinstance(payload, dict) else None
            if not isinstance(text, str):
                raise STTError("STT response has no 'text' field")
            return text.strip()
        return response.text.strip()

    async def aclose(self) -> None:
        self._warmer.cancel()
        await self._client.aclose()


def create_stt(settings: STTSettings) -> STTProvider | None:
    """The configured provider, or None when STT credentials are missing."""
    return OpenAICompatibleSTT(settings) if settings.configured else None
