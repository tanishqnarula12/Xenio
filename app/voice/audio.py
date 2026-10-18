"""Audio I/O: microphone capture, interruptible playback, WAV encode/decode, endpointing.

Barge-in is a first-class design constraint: the microphone streams continuously (so a
local detector can hear "Xenio, stop" while XENIO talks) and ``Speaker.stop()`` halts
playback within one audio block.
"""

from __future__ import annotations

import asyncio
import collections
import io
import logging
import os
import struct
import threading
import time
import wave
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

log = logging.getLogger("xenio.voice.audio")

SAMPLE_RATE = 16_000
FRAME_SAMPLES = 1_280  # 80 ms at 16 kHz — the wake-word engine's natural chunk
# Output streams are fed by a Python callback. With PortAudio's variable, tiny buffers (blocksize 0) the callback runs
# ~1700 times a second and every wait for the interpreter lock drops audio: measured on this Mac with other Python
# threads busy in short bursts (as in the UI process), only 7.4% of the audio reached the device - XENIO's voice
# cracked. Fixed 1024-sample blocks with "high" latency delivered 100% under the same load, for ~70 ms of latency.
OUTPUT_BLOCK = 1_024


class AudioError(Exception):
    pass


class MicrophoneUnavailable(AudioError):
    pass


class SpeakerUnavailable(AudioError):
    pass


def _sd():
    try:
        import sounddevice
    except (ImportError, OSError) as exc:  # OSError: PortAudio library missing
        raise AudioError(f"sounddevice/PortAudio unavailable: {exc}") from exc
    return sounddevice


def resolve_input_device(device: int | str | None):
    """(device, extra_settings) for capture.

    On Windows the default PortAudio route is MME, which on some machines (measured here:
    an Intel Smart Sound mic array) delivers near-silence (RMS < 1) while the same mic via
    WASAPI carries the real signal. So by default XENIO captures from the WASAPI default
    input and lets Windows convert to 16 kHz (``auto_convert``)."""
    if os.name != "nt":
        return device, None
    sd = _sd()
    try:
        apis = sd.query_hostapis()
        if device is None:
            for api in apis:
                index = api.get("default_input_device", -1)
                if "WASAPI" in api["name"] and index is not None and index >= 0:
                    return index, sd.WasapiSettings(auto_convert=True)
            return None, None
        info = sd.query_devices(device, "input")
        if "WASAPI" in apis[info["hostapi"]]["name"]:
            return device, sd.WasapiSettings(auto_convert=True)
    except Exception:
        log.debug("Could not resolve a WASAPI input device", exc_info=True)
    return device, None


# ---- WAV ----------------------------------------------------------------------


def encode_wav(pcm: np.ndarray, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Mono/multichannel int16 samples -> WAV bytes."""
    samples = np.asarray(pcm, dtype=np.int16)
    channels = 1 if samples.ndim == 1 else samples.shape[1]
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(samples.tobytes())
    return buf.getvalue()


def decode_wav(data: bytes) -> tuple[np.ndarray, int]:
    """WAV bytes -> (int16 array shaped (frames, channels), sample_rate).

    Parses chunks by hand because streaming TTS endpoints send 0xFFFFFFFF placeholder
    sizes, which the stdlib ``wave`` module does not handle."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise AudioError("Not a WAV file")
    fmt: tuple[int, int, int, int] | None = None
    pos = 12
    while pos + 8 <= len(data):
        chunk_id = data[pos : pos + 4]
        size = struct.unpack_from("<I", data, pos + 4)[0]
        body_start = pos + 8
        if chunk_id == b"fmt ":
            audio_format, channels, rate = struct.unpack_from("<HHI", data, body_start)
            bits = struct.unpack_from("<H", data, body_start + 14)[0]
            if audio_format == 0xFFFE and size >= 26:  # WAVE_FORMAT_EXTENSIBLE: real format in the GUID
                audio_format = struct.unpack_from("<H", data, body_start + 24)[0]
            fmt = (audio_format, channels, rate, bits)
        elif chunk_id == b"data":
            if fmt is None:
                raise AudioError("WAV data chunk before fmt chunk")
            end = len(data) if size == 0xFFFFFFFF or body_start + size > len(data) else body_start + size
            return _to_int16(data[body_start:end], *fmt)
        if size == 0xFFFFFFFF:
            break
        pos = body_start + size + (size & 1)  # chunks are word-aligned
    raise AudioError("WAV has no data chunk")


def _to_int16(raw: bytes, audio_format: int, channels: int, rate: int, bits: int) -> tuple[np.ndarray, int]:
    if channels < 1 or rate < 1:
        raise AudioError("Invalid WAV header")
    if audio_format == 1 and bits == 16:
        samples = np.frombuffer(raw[: len(raw) - len(raw) % 2], dtype="<i2").astype(np.int16)
    elif audio_format == 1 and bits == 8:
        samples = ((np.frombuffer(raw, dtype=np.uint8).astype(np.int16) - 128) << 8).astype(np.int16)
    elif audio_format == 1 and bits == 24:
        usable = len(raw) - len(raw) % 3
        b = np.frombuffer(raw[:usable], dtype=np.uint8).reshape(-1, 3)
        as32 = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) | (b[:, 2].astype(np.int32) << 16))
        as32 = np.where(as32 & 0x800000, as32 - 0x1000000, as32)
        samples = (as32 >> 8).astype(np.int16)
    elif audio_format == 3 and bits == 32:
        floats = np.frombuffer(raw[: len(raw) - len(raw) % 4], dtype="<f4")
        samples = (np.clip(floats, -1.0, 1.0) * 32767).astype(np.int16)
    else:
        raise AudioError(f"Unsupported WAV encoding (format={audio_format}, bits={bits})")
    usable = len(samples) - len(samples) % channels
    return samples[:usable].reshape(-1, channels), rate


def tone(frequency: float = 880.0, ms: int = 120, sample_rate: int = 24_000, volume: float = 0.25) -> np.ndarray:
    """A short sine 'earcon' played when the wake word is heard."""
    t = np.arange(int(sample_rate * ms / 1000)) / sample_rate
    wave_ = np.sin(2 * np.pi * frequency * t) * volume
    fade = np.minimum(1.0, np.minimum(t, t[::-1]) * sample_rate / 240)  # 10 ms fades, no clicks
    return (wave_ * fade * 32767).astype(np.int16).reshape(-1, 1)


def wake_chime(sample_rate: int = 24_000, volume: float = 0.22) -> np.ndarray:
    """XENIO's wake sound (~0.26 s): a quick rising synth glide that resolves into a bright, shimmering ping."""
    t = np.arange(int(sample_rate * 0.26)) / sample_rate
    # a phase-continuous exponential sweep 520 -> 1040 Hz over 90 ms, then it fades into the ping
    sweep, f0, ratio = 0.09, 520.0, 2.0
    ts = np.minimum(t, sweep)
    phase = 2 * np.pi * f0 * (sweep / np.log(ratio) * (ratio ** (ts / sweep) - 1) + ratio * np.maximum(t - sweep, 0))
    glide = np.sin(phase) * np.clip(t / 0.008, 0, 1) * np.exp(-np.maximum(t - sweep, 0) / 0.025)
    # the ping: a fifth (G6 + D7), each with a slightly detuned twin for the shimmer, ringing out from 75 ms
    tp = np.maximum(t - 0.075, 0)
    ring = np.where(t >= 0.075, np.clip(tp / 0.004, 0, 1) * np.exp(-tp / 0.06), 0.0)
    ping = sum(a * np.sin(2 * np.pi * f * tp) for f, a in ((1568.0, 0.5), (1571.5, 0.35), (2349.3, 0.3), (2353.4, 0.2)))
    wave_ = 0.55 * glide + ping * ring
    wave_ *= np.minimum(1.0, (t[-1] - t) * sample_rate / 240)  # 10 ms fade-out, no click
    wave_ *= volume / max(float(np.max(np.abs(wave_))), 1e-9)
    return (wave_ * 32767).astype(np.int16).reshape(-1, 1)


def resample(pcm: np.ndarray, from_rate: int, to_rate: int) -> np.ndarray:
    """Linear-interpolation resampling of int16 (frames, channels) audio. Good enough for speech."""
    if from_rate == to_rate or len(pcm) == 0:
        return pcm
    data = pcm.reshape(len(pcm), -1).astype(np.float32)
    n_out = max(1, int(round(len(data) * to_rate / from_rate)))
    x_old = np.arange(len(data))
    x_new = np.linspace(0, len(data) - 1, n_out)
    out = np.stack([np.interp(x_new, x_old, data[:, c]) for c in range(data.shape[1])], axis=1)
    return np.clip(out, -32768, 32767).astype(np.int16)


def rms(frame: np.ndarray) -> float:
    if frame.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))


# ---- endpointing --------------------------------------------------------------


MIN_SPEECH_THRESHOLD = 25.0  # RMS; below this is mic self-noise even on noise-suppressing arrays
MAX_SPEECH_THRESHOLD = 400.0


def speech_threshold(wake_peak: float, noise_floor: float) -> float:
    """Speech/silence threshold calibrated from how loudly the user just said the wake phrase.

    Mic gain varies ~20x between devices (this laptop's noise-suppressing Intel array reads
    0-10 in silence and quietly for speech), so a fixed level fails. Voiced speech stays within
    ~-16 dB of the loudest wake-phrase frame, while silence sits near the noise floor."""
    threshold = max(wake_peak * 0.15, noise_floor * 2.5, MIN_SPEECH_THRESHOLD)
    return min(threshold, MAX_SPEECH_THRESHOLD)


class UtteranceRecorder:
    """Endpointing: speech starts with the first speech frame and ends after ``silence_seconds``
    without speech (or at ``max_seconds``).

    With a ``vad`` (Silero, ``app/voice/vad.py``) a frame is speech when the VAD says so: robust to
    noise, no calibration, so a short ``silence_seconds`` (~0.55 s) is safe. Without one, a frame is
    speech when it is louder than ``threshold`` (normally calibrated by ``speech_threshold``; else 3x
    the running noise floor, never below 60). Trailing silence is not part of ``audio()``: less to
    upload, a faster transcript."""

    def __init__(
        self,
        *,
        sample_rate: int = SAMPLE_RATE,
        frame_samples: int = FRAME_SAMPLES,
        silence_seconds: float = 1.0,
        max_seconds: float = 15.0,
        start_timeout: float = 6.0,
        min_speech_seconds: float = 0.25,
        noise_floor: float = 30.0,
        threshold: float | None = None,
        pre_roll_seconds: float = 0.5,
        vad=None,
        tail_seconds: float = 0.25,
    ):
        self._vad = vad
        if vad is not None:
            vad.reset()
        self._tail_frames = max(1, round(tail_seconds / (frame_samples / sample_rate)))
        self._last_speech = -1  # index in _frames of the last speech frame
        self._frame_s = frame_samples / sample_rate
        self.start_timeout = start_timeout
        self._silence_frames = max(1, round(silence_seconds / self._frame_s))
        self._max_frames = max(1, round(max_seconds / self._frame_s))
        self._start_frames = max(1, round(start_timeout / self._frame_s))
        self._min_speech_frames = max(1, round(min_speech_seconds / self._frame_s))
        self._pre_roll_frames = max(1, round(pre_roll_seconds / self._frame_s))
        self._noise = noise_floor
        self._fixed_threshold = threshold
        self._frames: list[np.ndarray] = []
        self._pre_roll: list[np.ndarray] = []
        self._speech_frames = 0
        self._quiet_run = 0
        self._waited = 0
        self._peak = 0.0
        self.started = False
        self.done = False

    @property
    def threshold(self) -> float:
        if self._fixed_threshold is not None:
            return self._fixed_threshold
        return max(self._noise * 3.0, 60.0)

    @property
    def quiet_seconds(self) -> float:
        """How long the user has been silent since they last spoke (0 while speaking)."""
        return self._quiet_run * self._frame_s if self.started else 0.0

    @property
    def speech_mark(self) -> int:
        """Changes whenever new speech arrives (the index of the last speech frame)."""
        return self._last_speech

    def finish(self) -> None:
        """End the utterance now (the turn-taking logic decided the user has finished)."""
        self.done = True

    def seed(self, frames: list[np.ndarray]) -> None:
        """Audio heard just before recording started (words said while the wake word was still
        being recognised); kept as pre-roll so the first word is not clipped."""
        self._pre_roll = (list(frames) + self._pre_roll)[-self._pre_roll_frames:]

    def feed(self, frame: np.ndarray, can_start: bool = True) -> bool:
        """Add one frame. Returns True once the utterance is complete (or timed out).
        ``can_start=False`` (e.g. while XENIO's own beep plays) keeps the frame as pre-roll
        but never treats it as the start of speech."""
        if self.done:
            return True
        level = rms(frame)
        self._peak = max(self._peak, level)
        if self._vad is not None:
            loud = self._vad.is_speech(frame) and level >= 2.0  # 2.0: digital silence is never speech
        else:
            loud = level > self.threshold
        self._waited += 1  # total frames seen; the start timeout only applies before real speech
        if not self.started:
            if loud and can_start:
                self.started = True
                self._frames.extend(self._pre_roll)  # keep the word onset
                self._frames.append(frame)
                self._last_speech = len(self._frames) - 1
                self._speech_frames = 1
            else:
                if not loud:
                    self._noise = 0.9 * self._noise + 0.1 * level  # track the room's noise floor
                self._pre_roll = (self._pre_roll + [frame])[-self._pre_roll_frames :]
                if self._waited >= self._start_frames:
                    self.done = True
            return self.done
        self._frames.append(frame)
        if loud:
            self._speech_frames += 1
            self._quiet_run = 0
            self._last_speech = len(self._frames) - 1
        else:
            self._quiet_run += 1
        if self._quiet_run >= self._silence_frames and self._speech_frames < self._min_speech_frames:
            # Only a blip (beep echo, click, cough): not an utterance. Keep waiting for real speech.
            self.started = False
            self._pre_roll = self._frames[-self._pre_roll_frames :]
            self._frames, self._speech_frames, self._quiet_run, self._last_speech = [], 0, 0, -1
            if self._waited >= self._start_frames:
                self.done = True
            return self.done
        if self._quiet_run >= self._silence_frames or len(self._frames) >= self._max_frames:
            self.done = True
        return self.done

    def audio(self) -> np.ndarray | None:
        """The captured utterance, or None if no real speech was heard."""
        if not self.started or self._speech_frames < self._min_speech_frames:
            return None
        end = len(self._frames) if self._last_speech < 0 else min(len(self._frames), self._last_speech + 1 + self._tail_frames)
        return np.concatenate(self._frames[:end]).astype(np.int16)

    def stats(self) -> dict[str, object]:
        return {
            "started": self.started,
            "speech_s": round(self._speech_frames * self._frame_s, 2),
            "peak_rms": int(self._peak),
            "threshold": "vad" if self._vad is not None else int(self.threshold),
            "noise": int(self._noise),
        }


class TalkOverDetector:
    """Notices the user talking over XENIO without a keyword.

    Only armed when XENIO's own voice barely reaches the microphone (headphones, or an echo
    canceller that removes it). On this laptop's speakers the echo canceller leaves bursts of
    600-1500 RMS — as loud as the user — and neither level nor spectrum separates the two
    (measured), so there it stays disarmed and interrupting works by keyword instead.
    The echo level is learned from each uninterrupted playback (95th percentile mic level)."""

    def __init__(self, frames_needed: int = 4, enabled: bool = True):
        self.enabled = enabled
        self.frames_needed = frames_needed  # 4 x 80 ms of sustained speech
        self.echo_level: float | None = None  # 95th-percentile mic level during XENIO's own playback
        self.echo_peak: float | None = None  # loudest echo frame during that playback
        self._levels: list[float] = []
        self._run = 0

    def armed(self, threshold: float) -> bool:
        # Both the typical AND the worst echo must sit well below speech, or XENIO would cut itself
        # off (measured on this laptop's speakers: p95 63 but bursts up to 375-1500).
        return (
            self.enabled
            and self.echo_level is not None
            and self.echo_peak is not None
            and self.echo_level < threshold * 0.3
            and self.echo_peak < threshold
        )

    def begin_playback(self) -> None:
        self._levels, self._run = [], 0

    def end_playback(self, interrupted: bool) -> None:
        if not interrupted and len(self._levels) >= 12:
            self.echo_level = float(np.percentile(self._levels, 95))
            self.echo_peak = float(max(self._levels))
        self._levels, self._run = [], 0

    def update(self, level: float, threshold: float, speech: bool | None = None) -> bool:
        """Feed one mic frame level while XENIO's audio is actually playing. True = the user is talking over
        it. ``speech`` (from the VAD, when there is one): a cough, a door or music is loud but not speech."""
        self._levels.append(level)
        if not self.armed(threshold):
            self._run = 0
            return False
        assert self.echo_level is not None and self.echo_peak is not None
        loud = level > max(threshold, 3 * self.echo_level, 1.5 * self.echo_peak) and speech is not False
        self._run = self._run + 1 if loud else 0
        return self._run >= self.frames_needed


# ---- devices ------------------------------------------------------------------


@dataclass
class AudioDevices:
    input_name: str
    output_name: str


def describe_devices(input_device: int | str | None = None, output_device: int | str | None = None) -> AudioDevices:
    sd = _sd()
    input_device, _ = resolve_input_device(input_device)
    try:
        inp = sd.query_devices(input_device, "input")
    except Exception as exc:
        raise MicrophoneUnavailable(f"No usable microphone: {exc}") from exc
    try:
        out = sd.query_devices(output_device, "output")
    except Exception as exc:
        raise SpeakerUnavailable(f"No usable speaker: {exc}") from exc
    return AudioDevices(str(inp["name"]), str(out["name"]))


class Microphone:
    """Continuous 16 kHz mono int16 capture delivered as 80 ms frames on an asyncio queue."""

    def __init__(self, device: int | str | None = None, sample_rate: int = SAMPLE_RATE, frame_samples: int = FRAME_SAMPLES):
        self._device = device
        self.sample_rate = sample_rate
        self.frame_samples = frame_samples
        self._stream = None
        self._queue: asyncio.Queue[np.ndarray] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.dropped_frames = 0

    async def start(self) -> None:
        sd = _sd()
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=250)  # ~20 s of audio

        def callback(indata, frames, _time, status) -> None:  # PortAudio thread
            if status:
                self.dropped_frames += 1
            assert self._loop is not None
            self._loop.call_soon_threadsafe(self._put, indata[:, 0].copy())

        device, extra = resolve_input_device(self._device)
        try:
            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="int16",
                blocksize=self.frame_samples,
                device=device,
                callback=callback,
                extra_settings=extra,
            )
            self._stream.start()
        except Exception as exc:
            raise MicrophoneUnavailable(f"Could not open the microphone: {exc}") from exc
        log.info("Microphone started", extra={"fields": {"device": device if device is not None else "default", "wasapi": extra is not None}})

    def _put(self, frame: np.ndarray) -> None:
        assert self._queue is not None
        if self._queue.full():
            self._queue.get_nowait()  # never let a stalled consumer grow memory
            self.dropped_frames += 1
        self._queue.put_nowait(frame)

    async def read(self, timeout: float = 5.0) -> np.ndarray:
        if self._queue is None:
            raise MicrophoneUnavailable("Microphone not started")
        try:
            return await asyncio.wait_for(self._queue.get(), timeout)
        except TimeoutError as exc:
            raise MicrophoneUnavailable("The microphone stopped delivering audio") from exc

    def drain(self) -> None:
        if self._queue is not None:
            while not self._queue.empty():
                self._queue.get_nowait()

    async def stop(self) -> None:
        if self._stream is not None:
            stream, self._stream = self._stream, None
            try:
                stream.stop()
                stream.close()
            except Exception:
                log.debug("Error closing microphone", exc_info=True)


class _Entry:
    __slots__ = ("samples", "pos", "done", "on_begin")

    def __init__(self, samples: np.ndarray, done: asyncio.Future | None, on_begin: Callable[[], None] | None):
        self.samples, self.pos, self.done, self.on_begin = samples, 0, done, on_begin


class LiveSpeaker:
    """Voice-mode output on ONE output stream that stays open for the whole session.

    Measured on this laptop: opening an output stream costs ~230 ms every time, and the first one after the
    audio device has idled took 5.4 s (the device wakes from power saving). The old ``Speaker`` opened a stream
    per sentence. Here the stream stays open (playing silence when there is nothing to say), so speech starts
    within one buffer (tens of ms), sentences follow each other without gaps, and ``stop()`` silences it at once.
    Same interface as ``Speaker``; if the stream cannot be opened it falls back to ``Speaker``."""

    def __init__(self, device: int | str | None = None, sample_rate: int = 24_000):
        self._device = device
        self._rate = sample_rate
        self._lock = threading.Lock()
        self._queue: collections.deque[_Entry] = collections.deque()
        self._stream = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._fallback: Speaker | None = None
        self._generation = 0  # bumped by stop(): everything queued before it is cancelled
        self.finished_at = 0.0
        self._tail = 0.0  # the device's output latency: audio handed over keeps playing this long
        self.output_level = 0.0  # RMS of the last buffer actually sent to the device (the UI's orb follows it)

    @property
    def playing(self) -> bool:
        with self._lock:
            return bool(self._queue)

    def echo_possible(self, holdoff: float = 0.1) -> bool:
        return self.playing or time.monotonic() - self.finished_at < holdoff + self._tail

    def _callback(self, outdata, frames, _time, _status) -> None:  # PortAudio thread
        written, events = 0, []
        with self._lock:
            while written < frames and self._queue:
                entry = self._queue[0]
                if entry.pos == 0 and entry.on_begin is not None:
                    events.append(entry.on_begin)
                n = min(frames - written, len(entry.samples) - entry.pos)
                outdata[written:written + n, 0] = entry.samples[entry.pos:entry.pos + n]
                entry.pos += n
                written += n
                if entry.pos >= len(entry.samples):
                    self._queue.popleft()
                    if entry.done is not None:
                        events.append(lambda f=entry.done: f.done() or f.set_result(True))
                    if not self._queue:
                        self.finished_at = time.monotonic()
        outdata[written:] = 0
        self.output_level = rms(outdata[:written]) if written else 0.0
        if events and self._loop is not None:
            for callback in events:
                self._loop.call_soon_threadsafe(callback)

    def _on_stream_end(self) -> None:  # the device went away (unplugged, error): reopen on next use
        self._stream = None

    def _ensure(self) -> bool:
        """The persistent stream is open (True), or the fallback speaker must be used (False)."""
        if self._fallback is not None:
            return False
        if self._stream is not None:
            return True
        self._loop = asyncio.get_running_loop()
        try:
            sd = _sd()
            stream = sd.OutputStream(samplerate=self._rate, channels=1, dtype="int16", device=self._device,
                                     callback=self._callback, finished_callback=self._on_stream_end,
                                     blocksize=OUTPUT_BLOCK, latency="high")
            stream.start()
            self._stream = stream
            self._tail = float(getattr(stream, "latency", 0.0) or 0.0)
            return True
        except Exception as exc:
            log.warning("Persistent audio output unavailable; opening a stream per reply",
                        extra={"fields": {"error": str(exc)[:200]}})
            self._fallback = Speaker(self._device)
            return False

    def _samples(self, pcm: np.ndarray, rate: int) -> np.ndarray:
        data = np.asarray(pcm, dtype=np.int16)
        if data.ndim == 2:
            data = data[:, 0]
        if rate != self._rate:
            data = resample(data.reshape(-1, 1), rate, self._rate)[:, 0]
        return np.ascontiguousarray(data, dtype=np.int16)

    def warm(self) -> None:
        """Open the stream now (keeps the audio device awake for the first reply)."""
        try:
            asyncio.get_running_loop()
            self._ensure()
        except RuntimeError:
            pass

    async def play(self, pcm: np.ndarray, sample_rate: int) -> bool:
        if not self._ensure():
            return await self._fallback.play(pcm, sample_rate)
        done = asyncio.get_running_loop().create_future()
        with self._lock:
            self._queue.append(_Entry(self._samples(pcm, sample_rate), done, None))
        return await done

    async def play_stream(self, chunks: AsyncIterator[tuple[int, np.ndarray]],
                          on_start: Callable[[], None] | None = None) -> bool:
        if not self._ensure():
            return await self._fallback.play_stream(chunks, on_start=on_start)
        generation, last, first = self._generation, None, True
        async for rate, pcm in chunks:
            if self._generation != generation:
                break
            last = asyncio.get_running_loop().create_future()
            with self._lock:
                self._queue.append(_Entry(self._samples(pcm, rate), last, on_start if first else None))
            first = False
        if last is not None and self._generation == generation:
            return await last
        return self._generation == generation

    def stop(self) -> None:
        """Barge-in: drop everything queued; the stream itself keeps running (silent)."""
        self._generation += 1
        with self._lock:
            dropped, self._queue = list(self._queue), collections.deque()
            self.finished_at = time.monotonic()
        for entry in dropped:
            if entry.done is not None and not entry.done.done():
                entry.done.set_result(False)
        if self._fallback is not None:
            self._fallback.stop()

    def close(self) -> None:
        self.stop()
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                log.debug("Error closing the audio output", exc_info=True)


class Speaker:
    """Interruptible playback: ``await play(...)`` returns False if ``stop()`` cut it short."""

    def __init__(self, device: int | str | None = None):
        self._device = device
        self._stop = threading.Event()
        self._stream = None
        self.playing = False
        self.finished_at = 0.0  # monotonic time playback last ended (for echo hold-off)

    def echo_possible(self, holdoff: float = 0.1) -> bool:
        """True while playing and shortly after: the mic may still be hearing our own output."""
        return self.playing or time.monotonic() - self.finished_at < holdoff

    async def play(self, pcm: np.ndarray, sample_rate: int) -> bool:
        sd = _sd()
        data = np.asarray(pcm, dtype=np.int16)
        if data.ndim == 1:
            data = data.reshape(-1, 1)
        loop = asyncio.get_running_loop()
        finished = asyncio.Event()
        self._stop.clear()
        position = 0

        def callback(outdata, frames, _time, _status) -> None:  # PortAudio thread
            nonlocal position
            if self._stop.is_set():
                outdata.fill(0)
                raise sd.CallbackStop
            chunk = data[position : position + frames]
            outdata[: len(chunk)] = chunk
            if len(chunk) < frames:
                outdata[len(chunk) :] = 0
                raise sd.CallbackStop
            position += frames

        def on_finished() -> None:  # must return None (cffi void callback)
            loop.call_soon_threadsafe(finished.set)

        def open_stream(rate: int):
            s = sd.OutputStream(
                samplerate=rate,
                channels=data.shape[1],
                dtype="int16",
                device=self._device,
                callback=callback,
                finished_callback=on_finished,
                blocksize=OUTPUT_BLOCK,
                latency="high",
            )
            s.start()
            return s

        try:
            stream = open_stream(sample_rate)
        except Exception as first_error:
            # Some devices reject the TTS rate (e.g. 24 kHz); retry at the device's native rate.
            try:
                native = int(sd.query_devices(self._device, "output")["default_samplerate"])
                if native == sample_rate:
                    raise first_error
                data = resample(data, sample_rate, native)
                stream = open_stream(native)
            except Exception as exc:
                raise SpeakerUnavailable(f"Could not open the speaker: {exc}") from exc
        self._stream, self.playing = stream, True
        try:
            await finished.wait()
        finally:
            self.playing, self._stream = False, None
            self.finished_at = time.monotonic()
            try:
                stream.close()
            except Exception:
                log.debug("Error closing speaker stream", exc_info=True)
        return not self._stop.is_set()

    async def play_stream(self, chunks: AsyncIterator[tuple[int, np.ndarray]],
                          on_start: Callable[[], None] | None = None) -> bool:
        """Play (sample_rate, int16 mono) chunks as they arrive, on ONE output stream: sound starts with the
        first chunk, consecutive sentences follow without gaps, and a slow producer just plays silence until
        more arrives. Returns False if ``stop()`` cut it short."""
        sd = _sd()
        loop = asyncio.get_running_loop()
        finished = asyncio.Event()
        lock = threading.Lock()
        queue: collections.deque[np.ndarray] = collections.deque()
        ended = threading.Event()
        current: dict[str, Any] = {"data": None, "pos": 0}
        self._stop.clear()

        def callback(outdata, frames, _time, _status) -> None:  # PortAudio thread
            if self._stop.is_set():
                outdata.fill(0)
                raise sd.CallbackStop
            written = 0
            while written < frames:
                data = current["data"]
                if data is None or current["pos"] >= len(data):
                    with lock:
                        data = queue.popleft() if queue else None
                    current["data"], current["pos"] = data, 0
                    if data is None:
                        outdata[written:] = 0
                        if ended.is_set():
                            raise sd.CallbackStop
                        return  # producer is behind: a moment of silence, the stream stays open
                n = min(frames - written, len(data) - current["pos"])
                outdata[written:written + n, 0] = data[current["pos"]:current["pos"] + n]
                current["pos"] += n
                written += n

        def on_finished() -> None:  # must return None (cffi void callback)
            loop.call_soon_threadsafe(finished.set)

        stream, stream_rate = None, None
        try:
            async for rate, pcm in chunks:
                if self._stop.is_set():
                    break
                samples = np.asarray(pcm, dtype=np.int16).reshape(-1)
                if stream is None:
                    stream, stream_rate = self._open_stream(sd, rate, callback, on_finished)
                    self._stream, self.playing = stream, True
                    if on_start is not None:
                        on_start()
                if rate != stream_rate:
                    samples = resample(samples, rate, stream_rate).reshape(-1)
                with lock:
                    queue.append(samples)
        finally:
            ended.set()
            if stream is not None:
                try:
                    await finished.wait()
                finally:
                    self.playing, self._stream = False, None
                    self.finished_at = time.monotonic()
                    try:
                        stream.close()
                    except Exception:
                        log.debug("Error closing speaker stream", exc_info=True)
        return not self._stop.is_set()

    def _open_stream(self, sd, rate: int, callback, on_finished):
        """A started mono int16 output stream at ``rate`` (or the device's native rate if it refuses)."""
        def open_at(r: int):
            s = sd.OutputStream(samplerate=r, channels=1, dtype="int16", device=self._device, callback=callback,
                                finished_callback=on_finished, blocksize=OUTPUT_BLOCK, latency="high")
            s.start()
            return s

        try:
            return open_at(rate), rate
        except Exception as first_error:
            try:
                native = int(sd.query_devices(self._device, "output")["default_samplerate"])
                if native == rate:
                    raise first_error
                return open_at(native), native
            except Exception as exc:
                raise SpeakerUnavailable(f"Could not open the speaker: {exc}") from exc

    def stop(self) -> None:
        """Barge-in: silence output immediately."""
        self._stop.set()
        stream = self._stream
        if stream is not None:
            try:
                stream.abort()
            except Exception:
                log.debug("Error aborting playback", exc_info=True)
