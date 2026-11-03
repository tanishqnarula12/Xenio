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


class _NoWindows:
    def session_locked(self):
        return False

    def list_windows(self):
        return []

    list_hidden_windows = find_windows_all = list_windows

    def find_windows(self, query):
        return []

    def foreground_window(self):
        return None

    active_app_window = foreground_window


class _NoControls:
    def find(self, hwnd, query, limit=8, **_):
        return {"found": False, "elements": [], "complete": True}

    def focused(self, with_value=False):
        return {"found": False}

    def at(self, x, y):
        return {"found": False}


@pytest.fixture(autouse=True)
def _no_real_desktop(monkeypatch, tmp_path):
    """Tests never read the real screen or the windows of the Mac running them, and write no real measurements."""
    from app.desktop import apps, metrics
    from app.desktop.service import DesktopService
    from app.tools import desktop as desktop_tools
    from app.tools import vision as vision_tool

    def inert():
        return DesktopService(catalog=apps.AppCatalog(tmp_path, loader=lambda: []), uia=_NoControls(), windows=_NoWindows(),
                              data_dir=tmp_path)

    monkeypatch.setattr(desktop_tools, "_build_service", inert)
    monkeypatch.setattr(desktop_tools, "_service", None)
    monkeypatch.setattr(vision_tool, "_perception", None)
    monkeypatch.setattr(metrics, "_enabled", False)


class MemoryKey:
    """The vault's key for a test: the real Keychain is never touched."""

    def __init__(self):
        self.key = None

    def get(self):
        return self.key

    def create(self):
        self.key = b"k" * 32
        return self.key


@pytest.fixture(autouse=True)
def _no_real_vault(monkeypatch, tmp_path):
    """Tests never read or write the real Keychain, the real profile, or browser windows of the Mac running them."""
    from app.personal import vault
    from app.tools import autofill as autofill_tools

    keys = MemoryKey()
    monkeypatch.setattr(vault, "KeychainKey", lambda *a, **k: keys)
    monkeypatch.setattr(vault, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(autofill_tools, "_vault", None)
    monkeypatch.setattr(autofill_tools, "_engine", None)
    monkeypatch.setattr(autofill_tools, "_browser", None)
    from app.autofill import engine

    engine._filled_sensitive.clear()


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
