"""Typed settings loaded from ``.env`` plus the process environment.

The ``.env`` file is read with ``dotenv_values()`` instead of ``load_dotenv()`` on purpose:
STT/TTS keys never enter ``os.environ``, so they are never inherited by the Hermes
subprocess or by any command Hermes runs on the user's behalf.

Precedence: a non-empty process environment variable wins over the ``.env`` value.
"""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"


class ConfigError(ValueError):
    """A configuration value is present but invalid."""


class Secret:
    """A string that never shows its value in repr/str/logs. Use ``reveal()`` at the call site."""

    __slots__ = ("_value",)

    def __init__(self, value: str | None):
        self._value = (value or "").strip()

    def reveal(self) -> str:
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and other._value == self._value

    def __hash__(self) -> int:
        return hash(self._value)

    def __repr__(self) -> str:
        return "Secret('***')" if self._value else "Secret('')"

    __str__ = __repr__


@dataclass(frozen=True)
class HermesSettings:
    command: tuple[str, ...]
    workspace: Path
    startup_timeout: float
    turn_timeout: float
    permission_timeout: float
    resume_session: bool
    allowed_dirs: tuple[Path, ...] = ()  # extra folders treated like your own (project folders, other drives)
    show_browser: bool = False  # show Hermes' own automation browser window (AGENT_BROWSER_HEADED)
    download_max_mb: int = 200
    github_user: str = ""  # the user's GitHub login, for public data before `gh auth login`
    himalaya_account: str = ""  # Himalaya account name for email (empty = Himalaya's default)
    proactive: bool = True  # run due scheduled checks (Hermes cron) while XENIO is running
    vision: "VisionSettings | None" = None  # V5 screen vision (passed to Hermes' process)


@dataclass(frozen=True)
class VisionSettings:
    enabled: bool = True  # xenio_look_at_screen available
    provider: str = "main"  # main = Hermes' main model, strict | auto = Hermes' vision routing | openai-compatible
    model: str = ""  # override the model (main/auto) or name it (openai-compatible)
    max_edge: int = 1600  # screenshots are scaled down to this many pixels on the long side
    api_url: str = ""  # openai-compatible only
    api_key: Secret = Secret("")  # openai-compatible only

    def env(self) -> dict[str, str]:
        """What the xenio-guard plugin reads inside Hermes."""
        out = {"XENIO_VISION": "1" if self.enabled else "0", "XENIO_VISION_PROVIDER": self.provider,
               "XENIO_VISION_MAX_EDGE": str(self.max_edge)}
        if self.model:
            out["XENIO_VISION_MODEL"] = self.model
        if self.api_url:
            out["XENIO_VISION_API_URL"] = self.api_url
        if self.api_key.reveal():
            out["XENIO_VISION_API_KEY"] = self.api_key.reveal()
        return out


@dataclass(frozen=True)
class STTSettings:
    api_key: Secret
    api_url: str
    model: str
    language: str  # forced language (e.g. "en"); empty = detect automatically among ``languages``
    timeout: float
    max_retries: int
    languages: tuple[str, ...] = ("en", "hi", "de")  # V5.5: spoken languages XENIO expects (misdetections retried)

    @property
    def configured(self) -> bool:
        return bool(self.api_key) and bool(self.api_url)


@dataclass(frozen=True)
class TTSSettings:
    api_key: Secret
    api_url: str
    model: str
    voice: str
    response_format: str
    timeout: float
    max_retries: int
    voice_hi: str = "hf_alpha"  # V5.5: replies in Devanagari are spoken with a Hindi voice
    voice_de: str = ""  # no German voice exists on nabh (Kokoro) or in this Windows: German replies are shown, not spoken

    @property
    def configured(self) -> bool:
        return bool(self.api_key) and bool(self.api_url)


@dataclass(frozen=True)
class VoiceSettings:
    wake_phrase: str
    wake_name_only: bool
    stop_phrases: tuple[str, ...]
    wake_sensitivity: float
    barge_in: bool
    input_device: int | str | None
    output_device: int | str | None
    max_record_seconds: float
    silence_seconds: float
    start_timeout: float
    speech_threshold: float | None  # None = calibrate from the wake phrase
    follow_up_seconds: float  # keep listening this long after a reply (0 = always need the wake word)
    talk_over: bool  # allow interrupting by just talking (auto-armed only when there is no echo)
    brief_replies: bool  # ask Hermes for short, speakable answers in voice mode
    # Real-time voice (all on by default; each can be switched off in .env)
    vad: bool = True  # Silero voice-activity detection for endpointing (else loudness)
    vad_silence_seconds: float = 0.55  # silence that ends a finished-sounding turn when the VAD is used
    stream_replies: bool = True  # speak each sentence as soon as Hermes has written it
    acknowledge: bool = True  # short "One moment." / "Checking your calendar." while Hermes works
    ack_delay_seconds: float = 0.3  # say the acknowledgment if no answer has started by then
    stt_hedge_seconds: float = 3.0  # send a second STT request if the first is slower than this (0 = off)
    stt_engine: str = "cloud"  # cloud = nabh Whisper | local = on-device Moonshine (cloud as fallback)
    early_transcribe: bool = True  # transcribe at the first pause; end the turn when the sentence is finished
    turn_patience_seconds: float = 2.0  # silence that ends a turn whose last words sound unfinished


@dataclass(frozen=True)
class FastPathSettings:
    """Simple commands XENIO carries out itself, without the model (app/fastpath.py)."""

    enabled: bool = True
    music_service: str = "auto"  # auto (Spotify when its keys are set, else YouTube) | youtube | spotify
    spotify_client_id: str = ""
    spotify_client_secret: Secret = Secret("")


@dataclass(frozen=True)
class Settings:
    name: str
    data_dir: Path
    log_level: str
    env_file: Path | None
    hermes: HermesSettings
    stt: STTSettings
    tts: TTSSettings
    voice: VoiceSettings
    fast: FastPathSettings = FastPathSettings()

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    def secret_values(self) -> list[str]:
        """Raw secret strings, used only by the log redactor."""
        vision_key = self.hermes.vision.api_key if self.hermes.vision else Secret("")
        return [s.reveal() for s in (self.stt.api_key, self.tts.api_key, vision_key, self.fast.spotify_client_secret) if s]

    def problems(self) -> list[str]:
        """Human-readable configuration gaps (empty list = fully configured)."""
        issues = []
        if not self.stt.api_key:
            issues.append("STT_API_KEY is empty")
        if not self.stt.api_url:
            issues.append("STT_API_URL is empty")
        if not self.tts.api_key:
            issues.append("TTS_API_KEY is empty")
        if not self.tts.api_url:
            issues.append("TTS_API_URL is empty")
        return issues


def default_hermes_home() -> Path:
    """Where Hermes keeps config, memory and plugins (same resolution as Hermes itself)."""
    if os.environ.get("HERMES_HOME"):
        return Path(os.environ["HERMES_HOME"]).expanduser()
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "hermes"
    return Path.home() / ".hermes"


def _default_data_dir() -> Path:
    # Outside OneDrive-synced folders on purpose: models, logs and session state stay local.
    base = os.environ.get("LOCALAPPDATA") if os.name == "nt" else os.environ.get("XDG_DATA_HOME")
    return Path(base) / "xenio" if base else Path.home() / ".local" / "share" / "xenio"


def _split_command(raw: str) -> tuple[str, ...]:
    parts = shlex.split(raw, posix=os.name != "nt")
    # Non-POSIX shlex keeps surrounding quotes ("C:\Program Files\...\hermes.exe").
    return tuple(p[1:-1] if len(p) >= 2 and p[0] == p[-1] == '"' else p for p in parts)


class _Reader:
    def __init__(self, file_values: Mapping[str, str | None], environ: Mapping[str, str]):
        self._file = file_values
        self._env = environ

    def str(self, name: str, default: str = "") -> str:
        value = self._env.get(name)
        if value is None or not value.strip():
            value = self._file.get(name)
        if value is None or not str(value).strip():
            return default
        return str(value).strip()

    def float(self, name: str, default: float, minimum: float = 0.0, maximum: float | None = None) -> float:
        raw = self.str(name)
        if not raw:
            return default
        try:
            value = float(raw)
        except ValueError as exc:
            raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
        if value < minimum or (maximum is not None and value > maximum):
            bound = f"between {minimum} and {maximum}" if maximum is not None else f">= {minimum}"
            raise ConfigError(f"{name} must be {bound}, got {value}")
        return value

    def int(self, name: str, default: int, minimum: int = 0) -> int:
        raw = self.str(name)
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError as exc:
            raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
        if value < minimum:
            raise ConfigError(f"{name} must be >= {minimum}, got {value}")
        return value

    def bool(self, name: str, default: bool) -> bool:
        raw = self.str(name).lower()
        if not raw:
            return default
        if raw in {"1", "true", "yes", "on"}:
            return True
        if raw in {"0", "false", "no", "off"}:
            return False
        raise ConfigError(f"{name} must be true/false, got {raw!r}")

    def device(self, name: str) -> int | str | None:
        raw = self.str(name)
        if not raw:
            return None
        return int(raw) if raw.isdigit() else raw


def _choice(value: str, allowed: tuple[str, ...], name: str) -> str:
    if value not in allowed:
        raise ConfigError(f"{name} must be one of {', '.join(allowed)}, got {value!r}")
    return value


def load_settings(env_file: Path | None = None, environ: Mapping[str, str] | None = None) -> Settings:
    path = env_file if env_file is not None else DEFAULT_ENV_FILE
    file_values = dotenv_values(path) if path.is_file() else {}
    r = _Reader(file_values, os.environ if environ is None else environ)

    name = r.str("XENIO_NAME", "Xenio")
    data_dir = Path(r.str("XENIO_DATA_DIR") or _default_data_dir()).expanduser()

    command = _split_command(r.str("HERMES_COMMAND", "hermes acp"))
    if not command:
        raise ConfigError("HERMES_COMMAND is empty")

    hermes = HermesSettings(
        command=command,
        workspace=Path(r.str("XENIO_WORKSPACE") or Path.home() / "Xenio").expanduser(),
        startup_timeout=r.float("HERMES_STARTUP_TIMEOUT", 120.0, minimum=5),
        turn_timeout=r.float("HERMES_TURN_TIMEOUT", 600.0, minimum=10),
        permission_timeout=r.float("XENIO_PERMISSION_TIMEOUT", 120.0, minimum=5),
        resume_session=r.bool("XENIO_RESUME_SESSION", True),
        allowed_dirs=tuple(
            Path(p.strip()).expanduser() for p in re.split(r"[;,]", r.str("XENIO_ALLOWED_DIRS")) if p.strip()
        ),
        show_browser=r.bool("XENIO_SHOW_BROWSER", False),
        download_max_mb=int(r.float("XENIO_DOWNLOAD_MAX_MB", 200, minimum=1)),
        github_user=r.str("XENIO_GITHUB_USER"),
        himalaya_account=r.str("XENIO_HIMALAYA_ACCOUNT"),
        proactive=r.bool("XENIO_PROACTIVE", True),
        vision=VisionSettings(
            enabled=r.bool("XENIO_VISION", True),
            provider=_choice(r.str("XENIO_VISION_PROVIDER", "main").lower(), ("main", "auto", "openai-compatible"),
                             "XENIO_VISION_PROVIDER"),
            model=r.str("XENIO_VISION_MODEL"),
            max_edge=int(r.float("XENIO_VISION_MAX_EDGE", 1600, minimum=320, maximum=4096)),
            api_url=r.str("XENIO_VISION_API_URL"),
            api_key=Secret(r.str("XENIO_VISION_API_KEY")),
        ),
    )
    stt = STTSettings(
        api_key=Secret(r.str("STT_API_KEY")),
        api_url=r.str("STT_API_URL"),
        model=r.str("STT_MODEL", "whisper-large-v3-turbo"),
        language=r.str("STT_LANGUAGE"),
        timeout=r.float("STT_TIMEOUT", 30.0, minimum=1),
        max_retries=r.int("STT_MAX_RETRIES", 2),
        languages=tuple(x.strip().lower() for x in r.str("XENIO_LANGUAGES", "en,hi,de").split(",") if x.strip()),
    )
    tts = TTSSettings(
        api_key=Secret(r.str("TTS_API_KEY")),
        api_url=r.str("TTS_API_URL"),
        model=r.str("TTS_MODEL", "kokoro-tts"),
        voice=r.str("TTS_VOICE", "af_bella"),
        response_format=r.str("TTS_FORMAT", "wav").lower(),
        timeout=r.float("TTS_TIMEOUT", 30.0, minimum=1),
        max_retries=r.int("TTS_MAX_RETRIES", 2),
        voice_hi=r.str("TTS_VOICE_HI", "hf_alpha"),
        voice_de=r.str("TTS_VOICE_DE"),
    )
    if tts.response_format != "wav":
        raise ConfigError("TTS_FORMAT must be 'wav' (XENIO plays WAV/PCM without extra codecs)")

    wake_phrase = r.str("XENIO_WAKE_PHRASE", f"hey {name}").lower()
    stop_raw = r.str("XENIO_STOP_PHRASES", f"{name} stop,stop {name},{name} cancel,{name} abort")
    voice = VoiceSettings(
        wake_phrase=wake_phrase,
        wake_name_only=r.bool("XENIO_WAKE_NAME_ONLY", True),
        stop_phrases=tuple(p.strip().lower() for p in stop_raw.split(",") if p.strip()),
        wake_sensitivity=r.float("XENIO_WAKE_SENSITIVITY", 0.5, minimum=0.0, maximum=1.0),
        barge_in=r.bool("XENIO_BARGE_IN", True),
        input_device=r.device("XENIO_INPUT_DEVICE"),
        output_device=r.device("XENIO_OUTPUT_DEVICE"),
        max_record_seconds=r.float("XENIO_MAX_RECORD_SECONDS", 15.0, minimum=2),
        silence_seconds=r.float("XENIO_SILENCE_SECONDS", 1.0, minimum=0.3),
        start_timeout=r.float("XENIO_LISTEN_TIMEOUT", 6.0, minimum=1),
        speech_threshold=r.float("XENIO_SPEECH_THRESHOLD", 0.0, minimum=0.0) or None,
        follow_up_seconds=r.float("XENIO_FOLLOW_UP_SECONDS", 8.0, minimum=0.0),
        talk_over=r.bool("XENIO_TALK_OVER", True),
        brief_replies=r.bool("XENIO_VOICE_BRIEF", True),
        vad=r.bool("XENIO_VAD", True),
        vad_silence_seconds=r.float("XENIO_VAD_SILENCE_SECONDS", 0.55, minimum=0.25, maximum=3.0),
        stream_replies=r.bool("XENIO_STREAM_REPLIES", True),
        acknowledge=r.bool("XENIO_ACKNOWLEDGE", True),
        ack_delay_seconds=r.float("XENIO_ACK_DELAY_SECONDS", 0.3, minimum=0.0, maximum=10.0),
        stt_hedge_seconds=r.float("XENIO_STT_HEDGE_SECONDS", 3.0, minimum=0.0, maximum=30.0),
        stt_engine=_choice(r.str("XENIO_STT_ENGINE", "cloud").lower(), ("local", "cloud"), "XENIO_STT_ENGINE"),
        early_transcribe=r.bool("XENIO_EARLY_TRANSCRIBE", True),
        turn_patience_seconds=r.float("XENIO_TURN_PATIENCE_SECONDS", 2.0, minimum=0.5, maximum=5.0),
    )
    fast = FastPathSettings(
        enabled=r.bool("XENIO_FAST_PATH", True),
        music_service=_choice(r.str("XENIO_MUSIC_SERVICE", "auto").lower(), ("auto", "youtube", "spotify"),
                              "XENIO_MUSIC_SERVICE"),
        spotify_client_id=r.str("SPOTIFY_CLIENT_ID"),
        spotify_client_secret=Secret(r.str("SPOTIFY_CLIENT_SECRET")),
    )
    return Settings(
        name=name,
        data_dir=data_dir,
        log_level=r.str("XENIO_LOG_LEVEL", "INFO").upper(),
        env_file=path if path.is_file() else None,
        hermes=hermes,
        stt=stt,
        tts=tts,
        voice=voice,
        fast=fast,
    )
