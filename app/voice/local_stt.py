"""Optional on-device speech recognition (Moonshine base, English), with the cloud Whisper as fallback.

NOT the default (``XENIO_STT_ENGINE=cloud``). With synthetic voices Moonshine looked good (median 128 ms,
5.1 % word error), but with the user's real voice it failed badly. "Close Chrome" came out as "Close words of
enchanted beauty from Mary's hint" and "Stop Xenio" as "Stubbs's Inyo", while nabh.cloud's Whisper
large-v3-turbo transcribed the same requests correctly. Switch it on with ``XENIO_STT_ENGINE=local`` only for
offline use with a clear, US-accented voice.

The model (106 MB, from the official sherpa-onnx release) is downloaded on first use and checked against a
pinned SHA-256.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import re
import tarfile
import time
import urllib.request
from pathlib import Path

import numpy as np

from app.logging_setup import event
from app.voice.audio import decode_wav, resample
from app.voice.stt import STTError, STTProvider

log = logging.getLogger("xenio.voice.local_stt")

MOONSHINE_NAME = "sherpa-onnx-moonshine-base-en-quantized-2026-02-27"
MOONSHINE_URL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/{MOONSHINE_NAME}.tar.bz2"
MOONSHINE_SHA256 = "43232c1d13013d37317163baec3135bd771a186a4356f28c889bab453bb0e891"


def moonshine_present(models_dir: Path) -> bool:
    d = models_dir / MOONSHINE_NAME
    return all((d / f).is_file() for f in ("encoder_model.ort", "decoder_model_merged.ort", "tokens.txt"))


def ensure_moonshine(models_dir: Path) -> Path:
    """The verified model folder, downloading it once (106 MB)."""
    target = models_dir / MOONSHINE_NAME
    if moonshine_present(models_dir):
        return target
    models_dir.mkdir(parents=True, exist_ok=True)
    log.info("Downloading the local speech-recognition model (one-time, 106 MB)", extra={"fields": {"url": MOONSHINE_URL}})
    try:
        data = urllib.request.urlopen(MOONSHINE_URL, timeout=900).read()  # noqa: S310 - fixed https URL
    except Exception as exc:
        raise STTError(f"Could not download the local speech model: {exc}") from exc
    if hashlib.sha256(data).hexdigest() != MOONSHINE_SHA256:
        raise STTError("The downloaded speech model failed its checksum; not using it")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:bz2") as archive:
        archive.extractall(models_dir, filter="data")
    if not moonshine_present(models_dir):
        raise STTError(f"Speech model unpacked incorrectly: {target}")
    return target


class MoonshineSTT(STTProvider):
    """Local transcription; ``transcribe`` takes the same WAV bytes as the cloud provider."""

    def __init__(self, model_dir: Path, threads: int = 4):
        try:
            import sherpa_onnx
        except ImportError as exc:
            raise STTError(f"sherpa-onnx is not installed: {exc}") from exc
        self._recognizer = sherpa_onnx.OfflineRecognizer.from_moonshine_v2(
            encoder=str(model_dir / "encoder_model.ort"), decoder=str(model_dir / "decoder_model_merged.ort"),
            tokens=str(model_dir / "tokens.txt"), num_threads=threads)

    def _decode(self, audio_data: bytes) -> str:
        pcm, rate = decode_wav(audio_data)
        mono = pcm[:, 0] if pcm.ndim == 2 else pcm
        if rate != 16_000:
            mono = resample(mono.reshape(-1, 1), rate, 16_000)[:, 0]
        stream = self._recognizer.create_stream()
        stream.accept_waveform(16_000, (mono.astype(np.float32) / 32768.0).tolist())
        self._recognizer.decode_stream(stream)
        return stream.result.text.strip()

    async def transcribe(self, audio_data: bytes, *, filename: str = "audio.wav", content_type: str = "audio/wav",
                         prompt: str | None = None) -> str:
        if not audio_data:
            raise STTError("No audio to transcribe")
        started = time.monotonic()
        try:
            text = await asyncio.to_thread(self._decode, audio_data)
        except Exception as exc:
            raise STTError(f"Local transcription failed: {exc}") from exc
        event(log, "Local STT finished", ms=int((time.monotonic() - started) * 1000), chars=len(text))
        return text


class HybridSTT(STTProvider):
    """Local first; the cloud provider when the local one is unavailable, fails or hears nothing."""

    def __init__(self, local: STTProvider | None, cloud: STTProvider | None):
        if local is None and cloud is None:
            raise STTError("No speech recognition available")
        self.local, self.cloud = local, cloud

    @property
    def hedge_after(self) -> float:
        return getattr(self.cloud, "hedge_after", 0.0)

    @hedge_after.setter
    def hedge_after(self, value: float) -> None:
        if self.cloud is not None and hasattr(self.cloud, "hedge_after"):
            self.cloud.hedge_after = value

    async def transcribe(self, audio_data: bytes, *, filename: str = "audio.wav", content_type: str = "audio/wav",
                         prompt: str | None = None) -> str:
        if self.local is not None:
            try:
                text = await self.local.transcribe(audio_data, prompt=prompt)
                if any(ch.isalnum() for ch in text) or self.cloud is None:
                    return text
            except STTError as exc:
                if self.cloud is None:
                    raise
                event(log, "Local STT failed; using the cloud", logging.WARNING, error=str(exc)[:200])
        assert self.cloud is not None
        return await self.cloud.transcribe(audio_data, filename=filename, content_type=content_type, prompt=prompt)

    def warm(self) -> None:
        if self.cloud is not None:
            self.cloud.warm()

    async def aclose(self) -> None:
        for provider in (self.local, self.cloud):
            if provider is not None:
                await provider.aclose()


def create_local_stt(models_dir: Path) -> MoonshineSTT | None:
    try:
        return MoonshineSTT(ensure_moonshine(models_dir))
    except Exception as exc:  # no model / no sherpa: the cloud Whisper is used instead
        log.warning("Local speech recognition unavailable; using the cloud", extra={"fields": {"error": str(exc)[:200]}})
        return None


# ---- names the local model cannot know ----------------------------------------------------------------

def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def fix_names(text: str, assistant_name: str, vocabulary: list[str] | tuple[str, ...] = ()) -> str:
    """Repair what an acoustic model writes for names it has never seen: "Zenio"/"Xenia" -> "Xenio", and
    "Zenio Test" -> "XenioTest" when that is a name XENIO recently worked with."""
    name = assistant_name.strip()
    if len(name) > 2:
        core = re.escape(name[1:].lower()).replace("o", "[oa]")
        text = re.sub(rf"\b[xzs]{core}\b", name, text, flags=re.IGNORECASE)
        if name.lower() == "xenio":  # Hindi-accented "Xenio" comes out as "Zinio"/"Xinio"; Devanagari as "ज़ेनियो"
            text = re.sub(r"\b[xz]in[iy]o\b", name, text, flags=re.IGNORECASE)
            text = re.sub(r"(?:ज़|ज|ज़)े?नियो|ज़ीनियो|जीनियो", name, text)
    for word in vocabulary:
        target = _squash(word)
        if len(target) < 5:
            continue
        variants = {target, re.sub(r"^z", "x", target), re.sub(r"^x", "z", target)}
        words = list(re.finditer(r"[A-Za-z0-9._-]+", text))
        for size in (3, 2, 1):
            for i in range(len(words) - size + 1):
                chunk = text[words[i].start():words[i + size - 1].end()]
                squashed = _squash(chunk)
                if chunk != word and {squashed, re.sub(r"^z", "x", squashed)} & variants:
                    text = text[:words[i].start()] + word + text[words[i + size - 1].end():]
                    break
            else:
                continue
            break
    return text
