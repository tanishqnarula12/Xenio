from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.config import Settings, load_settings
from app.desktop.macos import open_privacy_settings as REAL_OPEN_PRIVACY_SETTINGS  # noqa: F401 - for its own test

FAKE_AGENT = Path(__file__).with_name("fake_acp_agent.py")

BASE_ENV = {
    "XENIO_NAME": "Xenio",
    "STT_API_KEY": "nbh_testkey_stt_0123456789abcdef",
    "STT_API_URL": "https://stt.example.test/v1/audio/transcriptions",
    "TTS_API_KEY": "nbh_testkey_tts_0123456789abcdef",
    "TTS_API_URL": "https://tts.example.test/v1/audio/speech",
}


@pytest.fixture(autouse=True)
def _no_system_settings(monkeypatch):
    """A simulated missing permission must never bring up the real System Settings during tests."""
    from app.desktop import macos

    opened: list[str] = []
    monkeypatch.setattr(macos, "open_privacy_settings", lambda kind, *a, **k: opened.append(kind) or True)
    return opened


@pytest.fixture
def make_settings(tmp_path: Path):
    """Settings built only from the given mapping (never the developer's real .env)."""

    def _make(**overrides: str) -> Settings:
        env = {
            **BASE_ENV,
            "XENIO_DATA_DIR": str(tmp_path / "data"),
            "XENIO_WORKSPACE": str(tmp_path / "workspace"),
            "HERMES_COMMAND": f'"{sys.executable}" "{FAKE_AGENT}"',
            "HERMES_STARTUP_TIMEOUT": "20",
            "HERMES_TURN_TIMEOUT": "20",
            **overrides,
        }
        return load_settings(env_file=tmp_path / "missing.env", environ=env)

    return _make
