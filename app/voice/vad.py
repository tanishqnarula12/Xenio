"""Voice activity detection: is the user speaking right now? (Silero VAD, local, ~0.3 ms per 80 ms frame.)

Used for endpointing: XENIO knows the user has finished about half a second after they stop, instead
of waiting for a fixed energy-based silence. Silero tells speech from noise (fans, typing, a TV in
the next room), so it needs no loudness calibration. nabh.cloud has no streaming VAD (its Whisper
endpoint accepts ``vad_filter``, which only trims uploaded audio), so this runs on the device, through
sherpa-onnx, which XENIO already uses for the wake word. No new heavy dependency.

The model (silero_vad.onnx, 0.6 MB) comes from the official sherpa-onnx release and is checked against
a pinned SHA-256 before it is used.
"""

from __future__ import annotations

import hashlib
import logging
import urllib.request
from pathlib import Path

import numpy as np

from app.voice.audio import SAMPLE_RATE

log = logging.getLogger("xenio.voice.vad")

VAD_MODEL_FILE = "silero_vad.onnx"
VAD_MODEL_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx"
VAD_MODEL_SHA256 = "9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6"


class VadError(Exception):
    pass


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ensure_vad_model(models_dir: Path) -> Path:
    """The verified model file, downloading it once (0.6 MB)."""
    target = models_dir / VAD_MODEL_FILE
    if target.is_file() and _sha256(target) == VAD_MODEL_SHA256:
        return target
    models_dir.mkdir(parents=True, exist_ok=True)
    log.info("Downloading the voice-activity model (one-time, 0.6 MB)", extra={"fields": {"url": VAD_MODEL_URL}})
    try:
        data = urllib.request.urlopen(VAD_MODEL_URL, timeout=60).read()  # noqa: S310 - fixed https URL
    except Exception as exc:
        raise VadError(f"Could not download the voice-activity model: {exc}") from exc
    if hashlib.sha256(data).hexdigest() != VAD_MODEL_SHA256:
        raise VadError("The downloaded voice-activity model failed its checksum; not using it")
    tmp = target.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(target)
    return target


class SileroVad:
    """Frame-by-frame speech flag. ``is_speech(frame)`` for each 80 ms int16 frame."""

    def __init__(self, model_path: Path, threshold: float = 0.5):
        try:
            import sherpa_onnx
        except ImportError as exc:
            raise VadError(f"sherpa-onnx is not installed: {exc}") from exc
        # Short internal hang-over: XENIO's recorder decides how much silence ends a turn.
        config = sherpa_onnx.VadModelConfig(
            silero_vad=sherpa_onnx.SileroVadModelConfig(model=str(model_path), threshold=threshold,
                                                        min_silence_duration=0.1, min_speech_duration=0.1,
                                                        window_size=512),
            sample_rate=SAMPLE_RATE, num_threads=1)
        self._config = config
        self._vad = sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=30)

    def is_speech(self, frame: np.ndarray) -> bool:
        self._vad.accept_waveform((np.asarray(frame, dtype=np.float32) / 32768.0).tolist())
        speaking = bool(self._vad.is_speech_detected())
        while not self._vad.empty():  # finished segments are not needed; keep the buffer empty
            self._vad.pop()
        return speaking

    def reset(self) -> None:
        self._vad.reset()


def create_vad(models_dir: Path, threshold: float = 0.5) -> SileroVad | None:
    """A ready VAD, or None (XENIO then falls back to loudness-based endpointing)."""
    try:
        return SileroVad(ensure_vad_model(models_dir), threshold)
    except VadError as exc:
        log.warning("Voice-activity detection unavailable; using loudness endpointing",
                    extra={"fields": {"error": str(exc)}})
        return None
