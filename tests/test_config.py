from pathlib import Path

import pytest

from app.config import ConfigError, Secret, load_settings


def test_defaults_and_values(make_settings):
    s = make_settings()
    assert s.name == "Xenio"
    assert s.stt.api_key.reveal() == "nbh_testkey_stt_0123456789abcdef"
    assert s.stt.model == "whisper-large-v3-turbo"
    assert s.tts.voice == "af_bella"
    assert s.tts.response_format == "wav"
    assert s.voice.wake_phrase == "hey xenio"
    assert s.voice.stop_phrases == ("xenio stop", "stop xenio", "xenio cancel", "xenio abort")  # V5.5 emergency stop
    assert s.stt.languages == ("en", "hi", "de") and s.tts.voice_hi == "hf_alpha" and s.tts.voice_de == ""
    assert s.hermes.command[-1].endswith("fake_acp_agent.py")
    assert s.problems() == []


def test_secrets_never_render(make_settings):
    s = make_settings()
    text = repr(s) + str(s)
    assert "nbh_testkey" not in text
    assert "Secret('***')" in text
    assert sorted(s.secret_values()) == sorted([s.stt.api_key.reveal(), s.tts.api_key.reveal()])


def test_missing_keys_reported(make_settings):
    s = make_settings(STT_API_KEY="", TTS_API_URL="")
    assert not s.stt.configured
    assert not s.tts.configured
    assert "STT_API_KEY is empty" in s.problems()
    assert "TTS_API_URL is empty" in s.problems()


def test_env_file_is_read_and_process_env_wins(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("XENIO_NAME=Friday\nSTT_MODEL=from-file\nTTS_VOICE=from-file\n", encoding="utf-8")
    s = load_settings(env_file=env_file, environ={"TTS_VOICE": "from-env", "XENIO_DATA_DIR": str(tmp_path)})
    assert s.name == "Friday"
    assert s.stt.model == "from-file"
    assert s.tts.voice == "from-env"
    assert s.voice.wake_phrase == "hey friday"
    assert s.env_file == env_file


def test_env_file_values_do_not_leak_into_os_environ(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("STT_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("STT_API_KEY=nbh_should_not_leak_123456\n", encoding="utf-8")
    import os

    load_settings(env_file=env_file, environ={})
    assert "STT_API_KEY" not in os.environ


@pytest.mark.parametrize(
    "name,value",
    [("STT_TIMEOUT", "abc"), ("XENIO_WAKE_SENSITIVITY", "1.5"), ("XENIO_BARGE_IN", "maybe"), ("TTS_FORMAT", "mp3")],
)
def test_invalid_values_raise(make_settings, name, value):
    with pytest.raises(ConfigError):
        make_settings(**{name: value})


def test_device_parsing(make_settings):
    s = make_settings(XENIO_INPUT_DEVICE="9", XENIO_OUTPUT_DEVICE="Speakers")
    assert s.voice.input_device == 9
    assert s.voice.output_device == "Speakers"


def test_secret_bool_and_equality():
    assert not Secret("")
    assert Secret("abc") == Secret("abc")
    assert str(Secret("abc")) == "Secret('***')"
