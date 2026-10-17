import struct

import numpy as np
import pytest

from app.voice.audio import AudioError, UtteranceRecorder, decode_wav, encode_wav, resample, speech_threshold, tone
from app.voice.speech_text import split_for_tts, strip_interrupt_words, strip_wake_words, to_speakable
from app.voice.wakeword import pronunciation_variants, stop_keywords, wake_keywords


def test_wav_roundtrip():
    pcm = (np.sin(np.linspace(0, 50, 4000)) * 10000).astype(np.int16)
    out, rate = decode_wav(encode_wav(pcm, 16000))
    assert rate == 16000 and out.shape == (4000, 1)
    assert np.array_equal(out[:, 0], pcm)


def test_decode_streaming_wav_with_unknown_sizes():
    pcm = np.arange(-500, 500, dtype=np.int16)
    wav = bytearray(encode_wav(pcm, 24000))
    struct.pack_into("<I", wav, 4, 0xFFFFFFFF)   # RIFF size
    struct.pack_into("<I", wav, 40, 0xFFFFFFFF)  # data size
    out, rate = decode_wav(bytes(wav))
    assert rate == 24000 and np.array_equal(out[:, 0], pcm)


def test_decode_float32_and_stereo():
    floats = np.array([0.0, 0.5, -0.5, 1.0], dtype="<f4")
    fmt = struct.pack("<HHIIHH", 3, 2, 8000, 8000 * 8, 8, 32)
    body = b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", floats.nbytes) + floats.tobytes()
    wav = b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WAVE" + body
    out, rate = decode_wav(wav)
    assert rate == 8000 and out.shape == (2, 2)
    assert out[0, 1] == 16383 and out[1, 1] == 32767


def test_decode_rejects_garbage():
    with pytest.raises(AudioError):
        decode_wav(b"ID3 this is an mp3")
    with pytest.raises(AudioError):
        decode_wav(b"RIFF\x00\x00\x00\x00WAVE")


def test_resample_and_tone():
    pcm = np.zeros((24000, 1), dtype=np.int16)
    assert resample(pcm, 24000, 16000).shape == (16000, 1)
    t = tone(ms=100, sample_rate=24000)
    assert t.shape == (2400, 1) and t.dtype == np.int16 and np.abs(t).max() > 0


def _frames(level, count, n=1280):
    return [np.full(n, level, dtype=np.int16) * np.where(np.arange(n) % 2, 1, -1).astype(np.int16) for _ in range(count)]


def test_recorder_captures_speech_until_silence():
    rec = UtteranceRecorder(silence_seconds=0.4, max_seconds=10, start_timeout=3)
    done = False
    for frame in _frames(30, 5) + _frames(3000, 10) + _frames(30, 10):
        done = rec.feed(frame)
        if done:
            break
    assert done and rec.started
    audio = rec.audio()
    assert audio is not None and len(audio) >= 10 * 1280  # speech plus pre-roll


def test_recorder_returns_none_without_speech():
    rec = UtteranceRecorder(start_timeout=0.8)
    for frame in _frames(20, 20):
        if rec.feed(frame):
            break
    assert rec.done and rec.audio() is None


def test_calibrated_threshold_catches_quiet_speech():
    # A noise-suppressing mic: silence ~5, speech ~150 RMS. The old fixed 400 heard nothing.
    threshold = speech_threshold(wake_peak=180, noise_floor=5)
    assert 25 <= threshold < 60
    rec = UtteranceRecorder(threshold=threshold, silence_seconds=0.4, start_timeout=3)
    for frame in _frames(5, 3) + _frames(150, 8) + _frames(5, 8):
        if rec.feed(frame):
            break
    assert rec.audio() is not None
    assert rec.stats()["started"] is True and rec.stats()["peak_rms"] >= 149


def test_speech_threshold_bounds():
    assert speech_threshold(0, 0) == 25  # never below mic self-noise
    assert speech_threshold(100_000, 0) == 400  # loud mics never get stricter than before
    assert speech_threshold(100, 40) == 100  # noisy room: 2.5x the floor wins


def test_recorder_ignores_own_beep_but_keeps_it_as_pre_roll():
    rec = UtteranceRecorder(threshold=100, silence_seconds=0.4, start_timeout=3)
    for frame in _frames(3000, 2):
        rec.feed(frame, can_start=False)  # XENIO's beep
    assert not rec.started
    for frame in _frames(500, 5) + _frames(5, 6):
        if rec.feed(frame):
            break
    assert rec.started and rec.audio() is not None


def test_recorder_ignores_a_blip_then_records_real_speech():
    # A beep echo / click starts nothing durable; the user then speaks after a long pause.
    rec = UtteranceRecorder(threshold=100, silence_seconds=0.4, start_timeout=5)
    for frame in _frames(3000, 1) + _frames(5, 10) + _frames(400, 6) + _frames(5, 8):
        if rec.feed(frame):
            break
    audio = rec.audio()
    assert audio is not None and len(audio) >= 6 * 1280


def test_recorder_caps_length():
    rec = UtteranceRecorder(max_seconds=1.0, silence_seconds=5)
    count = 0
    for frame in _frames(3000, 100):
        count += 1
        if rec.feed(frame):
            break
    assert count <= 14


def test_to_speakable_strips_markdown():
    text = to_speakable(
        "# Plan\nHere is **bold** and `code`.\n- item one\n- item two\n```python\nprint(1)\n```\n"
        "See [the docs](https://example.com) or https://x.y/z"
    )
    for junk in ("#", "**", "`", "```", "https://", "]("):
        assert junk not in text
    assert "bold" in text and "item one." in text and "the docs" in text and "code on screen" in text


def test_split_for_tts_limits_segment_size():
    text = " ".join(f"This is sentence number {i}." for i in range(60))
    segments = split_for_tts(text, max_chars=200)
    assert len(segments) > 3
    assert len(segments[0]) <= 160  # short first segment = faster time-to-voice
    assert all(len(s) <= 200 for s in segments)
    assert " ".join(segments) == text
    assert split_for_tts("") == []
    assert all(len(s) <= 50 for s in split_for_tts("word " * 100, max_chars=50))


@pytest.mark.parametrize("heard,expected", [
    ("create HelloTXT inside XenioTest", "create Hello.txt inside XenioTest"),
    ("make hello dot txt please", "make hello.txt please"),
    ("open report. pdf now", "open report.pdf now"),
    ("read notesMD", "read notes.md"),
    ("the HTML page", "the HTML page"),
    ("what is the PDF about", "what is the PDF about"),
    ("show README", "show README"),
])
def test_spoken_filenames_are_normalized(heard, expected):
    from app.voice.speech_text import normalize_spoken_filenames

    assert normalize_spoken_filenames(heard) == expected


def test_wake_keyword_variants():
    assert pronunciation_variants("hey xenio") == ["hey xenio", "hey zenio"]
    assert pronunciation_variants("hey jarvis") == ["hey jarvis"]
    keywords = wake_keywords("hey xenio", allow_name_only=True)
    assert set(keywords) == {"hey xenio", "hey zenio", "xenio", "zenio"}
    assert set(keywords.values()) == {"hey xenio"}
    assert set(wake_keywords("hey xenio", allow_name_only=False)) == {"hey xenio", "hey zenio"}
    assert stop_keywords(("xenio stop",)) == {"xenio stop": "xenio stop", "zenio stop": "xenio stop"}


@pytest.mark.parametrize("heard,left", [
    ("Xenio stops.", ""),            # misheard "Xenio, stop." must not reach Hermes as a request
    ("Xenio, stop.", ""),
    ("Xenio stopped.", ""),
    ("Stop. What about the weather?", "What about the weather?"),
    ("Stop the music", "Stop the music"),
])
def test_stop_words_are_not_sent_as_requests(heard, left):
    assert strip_interrupt_words(strip_wake_words(heard, "Xenio")) == left
