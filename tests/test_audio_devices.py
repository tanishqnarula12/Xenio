"""On a Mac, Bluetooth earbuds stay in music quality: XENIO listens with the Mac's own microphone."""

import json
import subprocess

from app.voice.audio import mac_microphone_instead_of_bluetooth


class FakeSd:
    def query_devices(self):
        return [{"name": "Boult Audio Airbass", "max_input_channels": 1}, {"name": "Boult Audio Airbass", "max_input_channels": 0},
                {"name": "MacBook Air Microphone", "max_input_channels": 1}, {"name": "MacBook Air Speakers", "max_input_channels": 0}]


def profiler(default_transport: str):
    items = [{"_name": "Boult Audio Airbass", "coreaudio_default_audio_input_device": "spaudio_yes", "coreaudio_device_input": 1,
              "coreaudio_device_transport": default_transport},
             {"_name": "MacBook Air Microphone", "coreaudio_device_input": 1, "coreaudio_device_transport": "coreaudio_device_type_builtin"}]
    out = json.dumps({"SPAudioDataType": [{"_items": items}]})
    return lambda argv, **k: subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")


def test_bluetooth_earbuds_as_the_microphone_give_way_to_the_macs_own():
    # measured: Boult Airbass as the default microphone; recording from them turned Spotify flat
    assert mac_microphone_instead_of_bluetooth(FakeSd(), profiler("coreaudio_device_type_bluetooth")) == 2


def test_any_other_default_microphone_is_left_alone():
    assert mac_microphone_instead_of_bluetooth(FakeSd(), profiler("coreaudio_device_type_usb")) is None


def test_no_answer_from_macos_means_the_default_microphone():
    def broken(argv, **k):
        raise subprocess.TimeoutExpired(argv, 5)
    assert mac_microphone_instead_of_bluetooth(FakeSd(), broken) is None
