"""``python -m app.main --health`` — verify every layer XENIO depends on.

Default checks make no paid API calls: Hermes is started and handshaken over ACP (no LLM
turn), STT/TTS are checked for configuration, audio devices are opened briefly.
``--live`` additionally runs one Hermes turn, one TTS synthesis and one STT transcription.
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from urllib.parse import urlsplit

import numpy as np

from app.config import Settings, default_hermes_home
from app.hermes.adapter import HermesAcpClient
from app.hermes.client import HermesError


@dataclass
class Check:
    name: str
    status: str  # OK | WARN | FAIL
    detail: str


def _check_python() -> Check:
    venv = sys.prefix != sys.base_prefix
    version = ".".join(map(str, sys.version_info[:3]))
    if sys.version_info < (3, 11):
        return Check("Python", "FAIL", f"{version} - XENIO needs Python 3.11+")
    return Check("Python", "OK" if venv else "WARN", f"{version}" + (" (venv)" if venv else " (not in a venv)"))


def _check_config(settings: Settings) -> Check:
    if settings.env_file is None:
        return Check("Config", "FAIL", ".env not found - copy .env.example to .env")
    problems = settings.problems()
    if problems:
        return Check("Config", "WARN", "; ".join(problems))
    return Check("Config", "OK", f"{settings.env_file.name} loaded, data dir {settings.data_dir}")


async def _check_hermes(settings: Settings, live: bool) -> Check:
    client = HermesAcpClient(settings.hermes, session_file=None)
    try:
        if not await client.health_check():
            return Check("Hermes", "FAIL", "could not start `" + " ".join(settings.hermes.command) + "`")
        info = client.agent_info
        detail = f"{info.get('name', 'hermes')} {info.get('version', '?')} via ACP (`{' '.join(settings.hermes.command)}`)"
        if live:
            reply = await client.send_message("Health check: reply with exactly the word OK.")
            if not reply.text:
                return Check("Hermes", "FAIL", detail + "; live turn returned no text")
            detail += f"; live reply: {reply.text[:40]!r}"
        return Check("Hermes", "OK", detail)
    except HermesError as exc:
        return Check("Hermes", "FAIL", str(exc))
    finally:
        await client.close()


async def _check_tts(settings: Settings, live: bool) -> tuple[Check, bytes | None]:
    s = settings.tts
    if not s.configured:
        return Check("TTS", "FAIL", "set TTS_API_KEY and TTS_API_URL in .env"), None
    detail = f"{urlsplit(s.api_url).netloc} {s.model}/{s.voice}"
    if not live:
        return Check("TTS", "OK", detail + " (configured; --live to call it)"), None
    from app.voice.tts import TTSError, create_tts

    tts = create_tts(s)
    assert tts is not None
    try:
        audio = await tts.synthesize("System check complete.")
        return Check("TTS", "OK", f"{detail}; synthesized {len(audio)} bytes WAV"), audio
    except TTSError as exc:
        return Check("TTS", "FAIL", str(exc)), None
    finally:
        await tts.aclose()


async def _check_stt(settings: Settings, live: bool, sample_wav: bytes | None) -> Check:
    s = settings.stt
    if not s.configured:
        return Check("STT", "FAIL", "set STT_API_KEY and STT_API_URL in .env")
    detail = f"{urlsplit(s.api_url).netloc} {s.model}"
    if not live:
        return Check("STT", "OK", detail + " (configured; --live to call it)")
    from app.voice.audio import encode_wav
    from app.voice.stt import STTError, create_stt

    stt = create_stt(s)
    assert stt is not None
    try:
        wav = sample_wav or encode_wav(np.zeros(16_000, dtype=np.int16))
        text = await stt.transcribe(wav)
        return Check("STT", "OK", f"{detail}; transcribed: {text[:40]!r}")
    except STTError as exc:
        return Check("STT", "FAIL", str(exc))
    finally:
        await stt.aclose()


async def _check_audio(settings: Settings) -> Check:
    from app.voice.audio import AudioError, Microphone, Speaker, describe_devices

    try:
        devices = describe_devices(settings.voice.input_device, settings.voice.output_device)
    except AudioError as exc:
        return Check("Audio", "FAIL", str(exc))
    mic = Microphone(settings.voice.input_device)
    try:
        await mic.start()
        frames = [await mic.read(timeout=3) for _ in range(12)]  # ~1 s, never leaves this process
        samples = np.concatenate(frames[2:]).astype(np.float32)  # skip device warm-up
        level, peak = int(np.sqrt(np.mean(samples**2))), int(np.abs(samples).max())
    except AudioError as exc:
        return Check("Audio", "FAIL", f"microphone: {exc}")
    finally:
        await mic.stop()
    try:
        await Speaker(settings.voice.output_device).play(np.zeros((2_400, 1), dtype=np.int16), 24_000)  # 0.1 s silence
    except AudioError as exc:
        return Check("Audio", "FAIL", f"speaker: {exc}")
    detail = f"mic: {devices.input_name} (level {level}); speaker: {devices.output_name}"
    if peak == 0:
        # Exact digital zeros: a muted, blocked or dead capture route.
        return Check("Audio", "WARN", detail + " - microphone delivers digital silence: check mute / privacy / XENIO_INPUT_DEVICE")
    if peak < 20:
        # Noise-suppressing mic arrays output near-zero in a quiet room, so this alone is not a fault.
        detail += " - heard only silence during the 1 s check (normal in a quiet room)"
    return Check("Audio", "OK", detail)


def _check_guard(settings: Settings) -> Check:
    from app.hermes.guard import PLUGIN_NAME, guard_status

    status = guard_status(settings.hermes.command)
    if status.ok and not status.tools_visible:
        return Check("Safety", "WARN", f"{PLUGIN_NAME} enabled, but your own tools.tool_search setting may hide "
                                       "XENIO's tools from the model (they stay protected either way)")
    if status.ok:
        return Check("Safety", "OK", f"{PLUGIN_NAME} enabled: every tool call checked; XENIO file, web and personal "
                                     "tools on for ACP")
    if status.installed and status.enabled and not status.toolset_enabled:
        return Check("Safety", "WARN", "XENIO's tools are not enabled for ACP sessions - run `python -m app.main --install-guard`")
    if status.installed and status.enabled and not status.up_to_date:
        return Check("Safety", "WARN", f"{PLUGIN_NAME} is outdated - run `python -m app.main --install-guard`")
    return Check(
        "Safety",
        "WARN",
        f"{PLUGIN_NAME} not active: Hermes only asks before edits and commands it flags itself. "
        "Run `python -m app.main --install-guard`",
    )


def _check_model(settings: Settings) -> Check:
    """Hermes' own model choice (XENIO only reads it): main model, fallback chain, Codex sign-in."""
    import subprocess

    from app.hermes.model_info import read_model_setup

    setup = read_model_setup(default_hermes_home())
    if not setup.model:
        return Check("Model", "WARN", "could not read Hermes' model from its config.yaml (run `hermes model`)")
    if setup.provider == "openai-codex":
        try:
            out = subprocess.run([settings.hermes.command[0], "auth", "status", "openai-codex"], capture_output=True,
                                 text=True, timeout=60, encoding="utf-8", errors="replace").stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        if "logged in" not in out:
            return Check("Model", "FAIL", f"{setup.describe()} - Codex is not signed in in Hermes: run "
                                          "`hermes auth add openai-codex`")
    return Check("Model", "OK" if setup.fallbacks else "WARN",
                 setup.describe() + ("" if setup.fallbacks else " (add backups with `hermes fallback add`)"))


def _check_web(settings: Settings) -> Check:
    """Web search/reading/browser are Hermes' own tools; XENIO adds opening pages for you and downloads."""
    browser = "Hermes' browser visible" if settings.hermes.show_browser else "Hermes' browser runs hidden"
    return Check("Web", "OK", f"Hermes web_search/web_extract/browser; xenio_open_url opens your browser; "
                              f"downloads up to {settings.hermes.download_max_mb} MB; {browser}")


def _check_github(settings: Settings) -> Check:
    from app.tools.github import gh_status

    installed, signed_in, detail = gh_status()
    if signed_in:
        return Check("GitHub", "OK", detail)
    user = f"public data for {settings.hermes.github_user}" if settings.hermes.github_user else \
        "public repositories only (set XENIO_GITHUB_USER for 'my repositories')"
    return Check("GitHub", "WARN" if installed else "INFO", f"{user}; {detail}")


def _check_personal(settings: Settings) -> list[Check]:
    """V4 personal integrations: Google (Calendar/Gmail), email backend, tasks database, scheduled checks."""
    from app.integrations.email import EmailIntegration
    from app.integrations.google import GoogleAuth
    from app.personal.store import PersonalStore
    from app.personal.tasks import TaskList
    from app.proactive import ProactiveEngine

    checks = []
    google = GoogleAuth().state()
    checks.append(Check("Google", "OK" if google.connected else "WARN",
                        f"Calendar + Gmail {google.detail}" if google.connected else google.setup or google.detail))
    email = EmailIntegration().auth_state()
    checks.append(Check("Email", "OK" if email.connected else "WARN", email.detail if email.connected else
                        f"{email.detail} (connect Google, or set up any mailbox with `himalaya`)"))
    try:
        counts = TaskList(PersonalStore()).counts()
        checks.append(Check("Tasks", "OK", f"{counts['open']} open task(s) in {PersonalStore().path}"))
    except Exception as exc:
        checks.append(Check("Tasks", "FAIL", f"personal database unusable: {exc}"))
    engine = ProactiveEngine(default_hermes_home())
    jobs = engine.jobs()
    runner = ("a Hermes gateway is running them" if engine.gateway_serving() else
              "XENIO runs them while it is open" if settings.hermes.proactive else "switched off (XENIO_PROACTIVE=false)")
    active = [j for j in jobs if j.get("enabled", True) and j.get("state") != "completed"]
    finished = f" ({len(jobs) - len(active)} finished)" if len(jobs) > len(active) else ""
    checks.append(Check("Schedules", "OK", f"{len(active)} active XENIO scheduled check(s){finished} on Hermes cron; "
                                           f"{runner}"))
    return checks


def _check_wakeword(settings: Settings) -> Check:
    from app.voice.wakeword import kws_model_present

    try:
        import sentencepiece  # noqa: F401
        import sherpa_onnx  # noqa: F401
    except ImportError as exc:
        return Check("Wake word", "FAIL", f"missing package: {exc.name} (pip install -r requirements.txt)")
    phrase = settings.voice.wake_phrase
    if kws_model_present(settings.models_dir):
        return Check("Wake word", "OK", f"local sherpa-onnx, phrase {phrase!r}")
    return Check("Wake word", "WARN", f"phrase {phrase!r}; model (~13 MB) downloads on first --voice run")


def _check_realtime(settings: Settings) -> Check:
    """On-device speech recognition and voice-activity models, streaming replies."""
    from app.voice.local_stt import moonshine_present
    from app.voice.vad import VAD_MODEL_FILE

    v = settings.voice
    parts = []
    if v.stt_engine == "local":
        parts.append("speech recognition on device (Moonshine)" if moonshine_present(settings.models_dir)
                     else "on-device speech model downloads on first --voice run (106 MB)")
    else:
        lang = f", language {settings.stt.language}" if settings.stt.language else ""
        parts.append(f"speech recognition: cloud Whisper ({settings.stt.model}{lang})")
    if v.vad:
        parts.append("Silero VAD" if (settings.models_dir / VAD_MODEL_FILE).is_file() else "VAD model downloads on first run")
        if v.early_transcribe:
            parts.append(f"early transcription (waits up to {v.turn_patience_seconds:g} s mid-sentence)")
    parts.append("streamed replies" if v.stream_replies else "replies spoken when complete")
    if v.acknowledge:
        parts.append("acknowledgments")
    return Check("Real time", "OK", ", ".join(parts))


def blocking_model_cooldowns(hermes_home, provider: str, now: float | None = None) -> list[str]:
    """Models Hermes has put on a cooldown for ``provider``'s pooled sign-in. Hermes blocks the whole credential
    for requests that name no model (auxiliary tasks such as vision) while any of these is active."""
    import json
    import time

    try:
        data = json.loads((hermes_home / "auth.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    now = time.time() if now is None else now
    found = []
    for entry in (data.get("credential_pool") or {}).get(provider) or []:
        for model, until in ((entry or {}).get("model_cooldowns") or {}).items():
            if isinstance(until, (int, float)) and until > now:
                found.append(str(model))
    return sorted(set(found))


def _check_vision(settings: Settings) -> Check:
    """V5 screen vision: capture available, which model looks, nothing blocking it. No screenshot is taken."""
    from app.hermes.model_info import read_model_setup
    from app.vision.capture import CaptureError, ScreenCapture, scaled_size

    v = settings.hermes.vision
    if v is None or not v.enabled:
        return Check("Vision", "OK", "switched off (XENIO_VISION=false)")
    try:
        _x, _y, w, h = ScreenCapture(v.max_edge).screens()["primary"]
    except (CaptureError, OSError) as exc:
        return Check("Vision", "WARN", f"screen capture unavailable: {exc}")
    sent = "x".join(map(str, scaled_size(w, h, v.max_edge)))
    home = default_hermes_home()
    if v.provider == "main":
        setup = read_model_setup(home)
        who = f"{setup.model} via {setup.provider} (Hermes' main model; no fallback to other providers)"
        blocked = blocking_model_cooldowns(home, setup.provider) if setup.provider else []
        if blocked:
            return Check("Vision", "WARN", f"{setup.provider} is blocked for vision by a cooldown on {', '.join(blocked)}; "
                                           f"fix: hermes auth reset {setup.provider}")
    elif v.provider == "auto":
        who = "Hermes' auxiliary vision routing (may use paid providers)"
    else:
        who = f"{urlsplit(v.api_url).netloc or '?'} / {v.model or '?'} (your own endpoint)"
    return Check("Vision", "OK", f"screen {w}x{h} (sent at {sent}), looks with {who}; explicit requests only, "
                                 "nothing saved")


async def run_mic_test(settings: Settings, seconds: float = 10.0) -> int:
    """Live level meter so the user can see what XENIO hears. Audio never leaves the process."""
    from app.voice.audio import AudioError, Microphone, rms, speech_threshold

    mic = Microphone(settings.voice.input_device)
    try:
        await mic.start()
    except AudioError as exc:
        print(f"Microphone error: {exc}")
        return 1
    print(f"Mic test for {seconds:.0f} s. Stay quiet for 2 s, then say \"{settings.voice.wake_phrase}, what can you do?\"\n")
    levels: list[float] = []
    try:
        group: list[float] = []
        for _ in range(int(seconds / 0.08)):
            group.append(rms(await mic.read(timeout=3)))
            if len(group) == 3:  # one meter line per 240 ms
                level = max(group)
                levels.extend(group)
                group = []
                bar = "#" * min(60, int(level / 25))
                print(f"  {int(level):5d} |{bar}")
    except AudioError as exc:
        print(f"Microphone error: {exc}")
        return 1
    finally:
        await mic.stop()
    if not levels:
        return 1
    ordered = sorted(levels)
    quiet = ordered[len(ordered) // 5]  # 20th percentile ~ room noise
    loud = ordered[-1]
    threshold = speech_threshold(loud, quiet)
    print(f"\nRoom noise ~{int(quiet)}, your voice peaked at {int(loud)}. Speech threshold would be ~{int(threshold)}.")
    if loud < 30:
        print("XENIO heard (almost) nothing: unmute the mic, raise its input volume, or set XENIO_INPUT_DEVICE.")
        return 1
    print("Looks good: your speech is clearly above the threshold." if loud > threshold * 2 else
          "Your voice is quiet for this mic: speak closer or raise the input volume.")
    return 0


def _check_desktop() -> Check:
    """V5.5 desktop control: on macOS the app XENIO runs in needs Accessibility (hands) and Screen Recording (eyes)."""
    if sys.platform != "darwin":
        return Check("Desktop", "OK", "apps, windows, mouse and keyboard through Win32 and UI Automation"
                     if os.name == "nt" else "desktop control is available on Windows and macOS only")
    try:
        from app.desktop import macos

        hands, eyes = macos.trusted(), macos.screen_access()
    except Exception as exc:  # noqa: BLE001
        return Check("Desktop", "FAIL", f"macOS desktop access could not be checked ({exc})")
    missing = [what for ok, what in ((hands, "Accessibility (typing, clicking, window control, media keys)"),
                                     (eyes, "Screen & System Audio Recording (looking at the screen)")) if not ok]
    if not missing:
        return Check("Desktop", "OK", "Accessibility and Screen Recording allowed: apps, windows, mouse, keyboard, screen")
    return Check("Desktop", "WARN", "opening/switching apps works; still needed: " + "; ".join(missing)
                 + ". System Settings > Privacy & Security: switch them on for the app XENIO runs in, then restart XENIO")


def _check_fast(settings: Settings) -> Check:
    f = settings.fast
    if not f.enabled:
        return Check("Fast path", "OK", "off (XENIO_FAST_PATH=false): every request goes through Hermes")
    spotify = bool(f.spotify_client_id and f.spotify_client_secret)
    music = "Spotify" if f.music_service == "spotify" or (f.music_service == "auto" and spotify) else "YouTube (one tab)"
    return Check("Fast path", "OK", f"apps, windows, music ({music}), pause/next, volume and time without the model; "
                 "everything else goes to Hermes")


async def run_health(settings: Settings, live: bool = False) -> int:
    name = settings.name.upper()
    print(f"{name} SYSTEM CHECK" + (" (live)" if live else ""))
    print()
    checks = [_check_python(), _check_config(settings)]
    print("Starting Hermes for the connectivity check (can take ~30 s)...", flush=True)
    checks.append(await _check_hermes(settings, live))
    checks.append(await asyncio.to_thread(_check_guard, settings))
    checks.append(await asyncio.to_thread(_check_model, settings))
    checks.append(_check_web(settings))
    checks.append(await asyncio.to_thread(_check_github, settings))
    checks.extend(await asyncio.to_thread(_check_personal, settings))
    tts_check, sample = await _check_tts(settings, live)
    checks.append(await _check_stt(settings, live, sample))
    checks.append(tts_check)
    checks.append(await _check_audio(settings))
    checks.append(_check_wakeword(settings))
    checks.append(_check_realtime(settings))
    checks.append(await asyncio.to_thread(_check_vision, settings))
    checks.append(await asyncio.to_thread(_check_desktop))
    checks.append(_check_fast(settings))

    print()
    for c in checks:
        print(f"[{c.status}]".ljust(7) + f"{c.name.ljust(10)} {c.detail}")
    failed = [c for c in checks if c.status == "FAIL"]
    print()
    print("System ready." if not failed else f"{len(failed)} check(s) failed - see docs/SETUP.md.")
    return 0 if not failed else 1


if __name__ == "__main__":  # pragma: no cover
    from app.config import load_settings

    sys.exit(asyncio.run(run_health(load_settings())))
