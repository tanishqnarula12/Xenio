"""Local wake-word detection. No audio leaves the machine before the wake phrase is heard.

Engine: sherpa-onnx open-vocabulary keyword spotting (the same engine family Hermes uses
for its own "hey hermes"). Any phrase is BPE-tokenised at start-up — "hey xenio" needs no
training. One spotter listens for the wake phrase *and* the stop phrases used for barge-in.
"""

from __future__ import annotations

import logging
import tarfile
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path

import numpy as np

from app.voice.audio import FRAME_SAMPLES, SAMPLE_RATE

log = logging.getLogger("xenio.voice.wakeword")

KWS_MODEL_NAME = "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"
KWS_MODEL_URL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/{KWS_MODEL_NAME}.tar.bz2"


class WakeWordError(Exception):
    pass


class WakeWordDetector(ABC):
    frame_samples: int = FRAME_SAMPLES

    @abstractmethod
    def process(self, frame: np.ndarray) -> str | None:
        """Feed one int16 frame; return the detected phrase (lower-case) or None."""

    def reset(self) -> None:
        """Forget buffered audio (call after a detection or after XENIO spoke)."""

    def close(self) -> None:
        pass


def kws_model_dir(models_dir: Path) -> Path:
    return models_dir / KWS_MODEL_NAME


def kws_model_present(models_dir: Path) -> bool:
    return (kws_model_dir(models_dir) / "tokens.txt").is_file()


def ensure_kws_model(models_dir: Path) -> Path:
    """Download and unpack the keyword-spotting model once (~13 MB)."""
    target = kws_model_dir(models_dir)
    if (target / "tokens.txt").is_file():
        return target
    models_dir.mkdir(parents=True, exist_ok=True)
    archive = models_dir / f"{KWS_MODEL_NAME}.tar.bz2"
    log.info("Downloading wake-word model (one-time, ~13 MB)", extra={"fields": {"url": KWS_MODEL_URL}})
    try:
        urllib.request.urlretrieve(KWS_MODEL_URL, archive)  # noqa: S310 - fixed https URL
        with tarfile.open(archive, "r:bz2") as tf:
            tf.extractall(models_dir, filter="data")
    except Exception as exc:
        raise WakeWordError(f"Could not download the wake-word model: {exc}") from exc
    finally:
        archive.unlink(missing_ok=True)
    if not (target / "tokens.txt").is_file():
        raise WakeWordError(f"Wake-word model unpacked incorrectly: {target}")
    return target


def _bpe_tokens(phrases: list[str], tokens_path: Path, bpe_path: Path) -> list[list[str]]:
    """BPE-tokenise phrases against the model vocabulary. Equivalent to sherpa_onnx.text2token
    for ``tokens_type="bpe"``, without its unconditional pypinyin (Chinese) import."""
    try:
        import sentencepiece
    except ImportError as exc:
        raise WakeWordError(f"sentencepiece is not installed: {exc}") from exc
    vocab = {line.split()[0] for line in tokens_path.read_text(encoding="utf-8").splitlines() if line.strip()}
    processor = sentencepiece.SentencePieceProcessor(model_file=str(bpe_path))
    result = []
    for phrase in phrases:
        tokens = processor.encode(phrase, out_type=str)
        missing = [t for t in tokens if t not in vocab]
        if not tokens or missing:
            raise WakeWordError(f"Wake phrase {phrase.lower()!r} cannot be spotted (unknown tokens: {missing})")
        result.append(tokens)
    return result


def _model_file(model_dir: Path, part: str) -> str:
    # Prefer the fp32 files over the int8 ones (names ending in "int8.onnx").
    hits = sorted(p for p in model_dir.glob(f"{part}-*.onnx") if not p.name.endswith("int8.onnx"))
    if not hits:
        raise WakeWordError(f"Wake-word model file missing: {part}-*.onnx in {model_dir}")
    return str(hits[0])


def pronunciation_variants(phrase: str) -> list[str]:
    """Spellings the acoustic model can match. A word-initial X is spoken as Z in English
    (xylophone, Xena), and the BPE vocabulary only matches the sound when spelled that way:
    measured on TTS speech, "ZENIO" caught 6/6 "Xenio" utterances and "XENIO" 2/6."""
    words = phrase.lower().split()
    z_spelled = " ".join("z" + w[1:] if w.startswith("x") and len(w) > 1 else w for w in words)
    return list(dict.fromkeys([" ".join(words), z_spelled]))


class SherpaWakeWord(WakeWordDetector):
    def __init__(self, keywords: dict[str, str], model_dir: Path, keywords_file: Path, sensitivity: float = 0.5):
        """``keywords`` maps each spoken spelling to the phrase reported when it is heard."""
        try:
            import sherpa_onnx
        except ImportError as exc:
            raise WakeWordError(f"sherpa-onnx is not installed: {exc}") from exc
        if not keywords:
            raise WakeWordError("No wake phrases configured")

        tokens_path, bpe_path = model_dir / "tokens.txt", model_dir / "bpe.model"
        spellings = list(keywords)
        token_lists = _bpe_tokens([s.upper() for s in spellings], tokens_path, bpe_path)

        self._display_to_phrase: dict[str, str] = {}
        lines = []
        for spelling, tokens in zip(spellings, token_lists):
            display = spelling.upper().replace(" ", "_")  # sherpa rejects spaces in the @label
            self._display_to_phrase[display] = keywords[spelling]
            lines.append(" ".join(tokens) + f" @{display}")
        keywords_file.parent.mkdir(parents=True, exist_ok=True)
        keywords_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        upper = [s.upper() for s in spellings]

        # Same 0..1 -> threshold mapping Hermes validated: 0.5 -> 0.25 (sherpa's recommended value).
        threshold = 0.05 + 0.4 * sensitivity
        self._spotter = sherpa_onnx.KeywordSpotter(
            tokens=str(tokens_path),
            encoder=_model_file(model_dir, "encoder"),
            decoder=_model_file(model_dir, "decoder"),
            joiner=_model_file(model_dir, "joiner"),
            keywords_file=str(keywords_file),
            keywords_threshold=threshold,
            num_threads=1,
        )
        self._stream = self._spotter.create_stream()
        log.info("Wake word ready", extra={"fields": {"phrases": "|".join(p.lower() for p in upper)}})

    def process(self, frame: np.ndarray) -> str | None:
        samples = np.asarray(frame, dtype=np.float32) / 32768.0
        self._stream.accept_waveform(SAMPLE_RATE, samples)
        detected = None
        while self._spotter.is_ready(self._stream):
            self._spotter.decode_stream(self._stream)
            result = self._spotter.get_result(self._stream)
            if result:
                detected = self._display_to_phrase.get(str(result), str(result).replace("_", " ").lower())
                self._spotter.reset_stream(self._stream)  # one utterance fires once
        return detected

    def reset(self) -> None:
        self._stream = self._spotter.create_stream()


_WAKE_PREFIXES = ("hey ", "ok ", "okay ", "hi ", "hello ")


def wake_keywords(wake_phrase: str, allow_name_only: bool) -> dict[str, str]:
    """Spoken spelling -> wake phrase, optionally accepting the bare name after "hey ..."."""
    keywords = {spelling: wake_phrase for spelling in pronunciation_variants(wake_phrase)}
    if allow_name_only:
        name = next((wake_phrase[len(p):] for p in _WAKE_PREFIXES if wake_phrase.startswith(p)), "")
        for spelling in pronunciation_variants(name) if name.strip() else []:
            keywords.setdefault(spelling, wake_phrase)
    return keywords


def stop_keywords(stop_phrases: tuple[str, ...]) -> dict[str, str]:
    keywords: dict[str, str] = {}
    for stop in stop_phrases:
        for spelling in pronunciation_variants(stop):
            keywords.setdefault(spelling, stop)
    return keywords


BUSY_STOP_WORDS = ("stop", "abort", "cancel")


def create_busy_stop_detector(models_dir: Path, data_dir: Path, sensitivity: float) -> SherpaWakeWord:
    """Bare "stop" / "abort" / "cancel" (V5.5 emergency stop). Listened for only while XENIO works without talking
    (thinking, opening apps, clicking, typing), never while its own voice plays, so a reply that contains the word
    "stop" cannot interrupt itself."""
    return SherpaWakeWord({word: word for word in BUSY_STOP_WORDS}, ensure_kws_model(models_dir),
                          data_dir / "kws_busy_stop.txt", sensitivity)


def create_detectors(
    wake_phrase: str,
    stop_phrases: tuple[str, ...],
    models_dir: Path,
    data_dir: Path,
    sensitivity: float,
    allow_name_only: bool = True,
) -> tuple[SherpaWakeWord, SherpaWakeWord | None]:
    """(wake detector, stop detector). They are separate spotters on purpose: a bare-name wake
    keyword ("xenio") would otherwise fire on the prefix of "xenio stop" and swallow it."""
    model_dir = ensure_kws_model(models_dir)
    wake = SherpaWakeWord(wake_keywords(wake_phrase, allow_name_only), model_dir, data_dir / "kws_wake.txt", sensitivity)
    stops = stop_keywords(stop_phrases)
    stop = SherpaWakeWord(stops, model_dir, data_dir / "kws_stop.txt", sensitivity) if stops else None
    return wake, stop
