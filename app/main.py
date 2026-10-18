"""XENIO entry point.

    python -m app.main                    terminal text mode
    python -m app.main --voice            voice mode (wake word -> STT -> Hermes -> TTS)
    python -m app.main --ui               XENIO's interface in your browser (http://127.0.0.1:8765)
    python -m app.main --health           system check
    python -m app.main --connect-google   sign in to Google for Calendar + Gmail (read-only mail)
    python -m app.main --briefing         print the daily briefing (no model call)
    python -m app.main --tick             run due scheduled checks once and show new notifications
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

try:  # started with a Python that does not have XENIO's packages (e.g. plain `python` outside the venv)
    import dotenv  # noqa: F401
    import httpx  # noqa: F401
except ModuleNotFoundError as _missing:
    sys.exit(f"XENIO's packages are not installed for this Python ({sys.executable}): missing '{_missing.name}'.\n"
             "Start XENIO with the project's virtual environment:\n"
             + ("    .\\.venv\\Scripts\\python.exe -m app.main --voice\n"
                "or activate it first:  .\\.venv\\Scripts\\Activate.ps1" if os.name == "nt" else
                "    .venv/bin/python -m app.main --voice\nor activate it first:  source .venv/bin/activate"))

from app import __version__
from app.config import ConfigError, Settings, default_hermes_home, load_settings
from app.hermes.adapter import create_hermes_client
from app.logging_setup import event, setup_logging
from app.memory.manager import MemoryManager
from app.orchestrator import Orchestrator
from app.safety.permissions import PermissionGate
from app.safety.policy import PathPolicy

log = logging.getLogger("xenio.main")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m app.main", description="XENIO personal AI (powered by Hermes)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--voice", action="store_true", help="voice mode: wake word, speech in, speech out")
    mode.add_argument("--ui", action="store_true", help="XENIO's interface in your browser (local only)")
    mode.add_argument("--health", action="store_true", help="run the system check and exit")
    mode.add_argument(
        "--install-guard", action="store_true", help="install/update the xenio-guard Hermes plugin (safety policy)"
    )
    mode.add_argument("--mic-test", action="store_true", help="show live microphone levels for 10 s")
    mode.add_argument("--barge-in-test", action="store_true", help="check that you can interrupt XENIO while it talks")
    mode.add_argument("--connect-google", action="store_true",
                      help="sign in to Google (Calendar + read-only Gmail) with your own OAuth client")
    mode.add_argument("--disconnect-google", action="store_true", help="revoke and delete XENIO's Google sign-in")
    mode.add_argument("--briefing", action="store_true", help="print the daily briefing from the connected services")
    mode.add_argument("--tick", action="store_true", help="run due scheduled checks once (guarded) and print notifications")
    parser.add_argument("--client-file", help="with --connect-google: path of the downloaded client_secret*.json")
    parser.add_argument("--live", action="store_true", help="with --health: also make real Hermes/STT/TTS calls")
    parser.add_argument("--no-wake", action="store_true", help="with --voice: press Enter to talk instead of a wake word")
    parser.add_argument("--new-session", action="store_true", help="start a fresh Hermes conversation")
    parser.add_argument("--engineer", metavar="REQUEST", help="V6: have the engineering team work on a repository")
    parser.add_argument("--repo", help="with --engineer: the repository folder")
    parser.add_argument("--project", help="with --engineer: a project XENIO knows (its local folder is used)")
    parser.add_argument("--mode", choices=["change", "plan", "explain", "review", "test"],
                        help="with --engineer: what kind of work (default: from the request)")
    parser.add_argument("--yes", action="store_true", help="with --engineer: start a team plan without asking")
    parser.add_argument("--ui-port", type=int, default=8765, help="with --ui: the local port (default 8765)")
    parser.add_argument("--no-browser", action="store_true", help="with --ui: print the address instead of opening it")
    parser.add_argument("--ui-no-voice", action="store_true",
                        help="with --ui: leave the Mac's microphone alone (no \"Hey XENIO\"; the page records itself)")
    parser.add_argument("--ui-dev-origin", action="append", default=[], metavar="ORIGIN",
                        help="with --ui: also accept requests from a development server (e.g. http://localhost:5173)")
    parser.add_argument("-v", "--verbose", action="store_true", help="show INFO logs on the console")
    parser.add_argument("--version", action="version", version=f"XENIO {__version__}")
    return parser.parse_args(argv)


def build_path_policy(settings: Settings) -> PathPolicy:
    return PathPolicy(workspace=settings.hermes.workspace, extra_roots=settings.hermes.allowed_dirs,
                      hermes_home=default_hermes_home())


def build_orchestrator(settings: Settings, gate: PermissionGate) -> Orchestrator:
    hermes = create_hermes_client(settings.hermes, settings.data_dir)
    paths = build_path_policy(settings)
    return Orchestrator(hermes, gate, paths, sources_file=settings.data_dir / "sources.jsonl",
                        fast_path=build_fast_path(settings, paths))


def build_fast_path(settings: Settings, paths):
    """XENIO's own fast path for simple commands (open apps, music, volume, time), warmed up at start."""
    if not settings.fast.enabled:
        return None
    from app.fastpath import FastPath
    from app.media import Music

    fast = FastPath(paths, music=Music(settings.fast.music_service, settings.fast.spotify_client_id,
                                       settings.fast.spotify_client_secret))
    fast.warm()
    return fast


def build_engineering(settings: Settings, gate: PermissionGate, paths, notifications=None):
    """V6: XENIO's engineering team (None when switched off with XENIO_ENGINEERING=false)."""
    if not settings.engineering.enabled:
        return None
    from app.engineering.service import EngineeringService

    return EngineeringService(settings, gate, paths, notifications)


async def _with_engineering(settings: Settings, gate: PermissionGate, orchestrator, notifications, body):
    """Run a mode with the engineering service available to the chat's Hermes (its tool reaches it by socket)."""
    engineering = build_engineering(settings, gate, orchestrator.paths, notifications)
    if engineering is None:
        return await body()
    if hasattr(orchestrator.hermes, "extra_env"):
        orchestrator.hermes.extra_env = engineering.endpoint_env()
    await engineering.serve()
    try:
        return await body()
    finally:
        await engineering.close()


def build_proactive(settings: Settings, hermes=None):
    """Scheduled checks + notifications (V4). The tick runs Hermes' own scheduler with XENIO's guarded environment."""
    from app.notifications import NotificationCenter
    from app.proactive import ProactiveEngine

    if hermes is None:
        hermes = create_hermes_client(settings.hermes, settings.data_dir)
    notifications = NotificationCenter()
    engine = ProactiveEngine(default_hermes_home(), notifications, tick_command=[settings.hermes.command[0], "cron", "tick"],
                             env_factory=getattr(hermes, "guard_env", None))
    return notifications, (engine if settings.hermes.proactive else None)


def _share_settings_with_integrations(settings: Settings) -> None:
    """XENIO's integrations (in this process and in its Hermes) read these non-secret settings from the environment."""
    import os

    os.environ["XENIO_DATA_DIR"] = str(settings.data_dir)
    for key, value in (("XENIO_GITHUB_USER", settings.hermes.github_user),
                       ("XENIO_HIMALAYA_ACCOUNT", settings.hermes.himalaya_account)):
        if value:
            os.environ[key] = value


async def _run_text(settings: Settings, args: argparse.Namespace) -> int:
    from app.interface.console import ConsoleConfirmer, ConsoleIO, run_text_mode

    io = ConsoleIO()
    gate = PermissionGate(ConsoleConfirmer(io), timeout=settings.hermes.permission_timeout)
    orchestrator = build_orchestrator(settings, gate)
    if args.new_session:
        _forget_session(settings)
    notifications, proactive = build_proactive(settings, orchestrator.hermes)
    return await _with_engineering(settings, gate, orchestrator, notifications, lambda: run_text_mode(
        settings, orchestrator, io, MemoryManager(orchestrator.hermes), notifications, proactive))


async def _run_voice(settings: Settings, args: argparse.Namespace) -> int:
    from app.voice.assistant import run_voice_mode

    if args.new_session:
        _forget_session(settings)
    gate = PermissionGate(None, timeout=settings.hermes.permission_timeout)  # confirmer wired in voice mode
    orchestrator = build_orchestrator(settings, gate)
    notifications, proactive = build_proactive(settings, orchestrator.hermes)
    return await _with_engineering(settings, gate, orchestrator, notifications, lambda: run_voice_mode(
        settings, orchestrator, gate, push_to_talk=args.no_wake, notifications=notifications, proactive=proactive))


class _UiNotifications:
    """The notification center the engineering service writes to, and the UI's milestone events."""

    def __init__(self, inner, publish):
        self._inner, self._publish = inner, publish

    def add(self, title, message, **kw):
        self._publish("engineering.milestone", {"title": title, "message": message, "priority": kw.get("priority"),
                                                "action_required": bool(kw.get("action_required"))})
        return self._inner.add(title, message, **kw) if self._inner is not None else None


async def _run_ui(settings: Settings, args: argparse.Namespace) -> int:
    """XENIO's interface in the browser: the same orchestrator, gate, engineering team and scheduler as the other
    modes; the browser answers the PermissionGate's questions (app/ui)."""
    import signal
    import webbrowser

    from app.logging_setup import Redactor
    from app.safety.policy import secret_env_values
    from app.ui.approvals import WebConfirmer
    from app.ui.assistant import AssistantState
    from app.ui.controller import UiController
    from app.ui.events import EventHub
    from app.ui.server import UiServer
    from app.voice.stt import create_stt
    from app.voice.tts import create_tts

    hub = EventHub(settings.data_dir / "ui" / "activity.jsonl",
                   redact=Redactor([*settings.secret_values(), *secret_env_values()]))
    state = AssistantState()
    holder: dict = {}

    def approvals_changed(count: int) -> None:
        state.approvals = count
        if "ctl" in holder:
            holder["ctl"].publish_state()

    confirmer = WebConfirmer(hub.publish, timeout=settings.hermes.permission_timeout, on_change=approvals_changed)
    gate = PermissionGate(confirmer, timeout=settings.hermes.permission_timeout)
    if args.new_session:
        _forget_session(settings)
    orchestrator = build_orchestrator(settings, gate)
    notifications, proactive = build_proactive(settings, orchestrator.hermes)
    engineering = build_engineering(settings, gate, orchestrator.paths, _UiNotifications(notifications, hub.publish))
    ctl = UiController(settings, orchestrator, gate, confirmer, hub, state, engineering, notifications, proactive,
                       MemoryManager(orchestrator.hermes), create_stt(settings.stt), create_tts(settings.tts))
    holder["ctl"] = ctl
    try:
        server = UiServer(ctl, port=args.ui_port, extra_origins=tuple(args.ui_dev_origin))
    except OSError as exc:
        print(f"The XENIO interface could not use port {args.ui_port} ({exc.strerror or exc}). Is XENIO's UI already "
              f"running? Otherwise start it with --ui-port <another port>.")
        return 1
    if engineering is not None:
        if hasattr(orchestrator.hermes, "extra_env"):
            orchestrator.hermes.extra_env = engineering.endpoint_env()
        await engineering.serve()
    server.start()
    print(f"XENIO interface: {server.url}")
    print(f"Open this address to sign in to it (it holds this session's key; do not share it):\n  {server.launch_url()}")
    if not args.no_browser:
        webbrowser.open(server.launch_url())

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # Windows: Ctrl+C arrives as KeyboardInterrupt
            pass
    background = [asyncio.ensure_future(ctl.start()), asyncio.ensure_future(ctl.watch())]
    if proactive is not None:
        background.append(asyncio.ensure_future(proactive.run_forever(deliver=ctl.deliver_notifications)))
    if not args.ui_no_voice:
        from app.ui.voice import run_ui_voice

        background.append(asyncio.ensure_future(run_ui_voice(ctl)))  # "Hey XENIO", the Mac's mic and speaker
    else:
        ctl.voice_problem = "switched off with --ui-no-voice: the page records in the browser"
    try:
        await stop.wait()
    finally:
        print("Stopping XENIO...")
        confirmer.cancel_all()
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)  # the talk engine releases the mic and speaker
        server.stop()
        if engineering is not None:
            await engineering.close()
        await orchestrator.close()
        for speech in (ctl.stt, ctl.tts):
            if speech is not None and hasattr(speech, "aclose"):
                await speech.aclose()
    return 0


class _PrintNotifications:
    """--engineer: milestones are printed as they happen."""

    def add(self, title, message, **_kw):
        print(f"[{title}] {message}", flush=True)


async def _run_engineer(settings: Settings, args: argparse.Namespace) -> int:
    """V6 from the terminal: plan, (confirm a team plan), run, report."""
    from app.engineering.service import EngineeringError
    from app.interface.console import ConsoleConfirmer, ConsoleIO
    from app.safety.permissions import PermissionRequest
    from app.safety.policy import RiskLevel

    io = ConsoleIO()
    gate = PermissionGate(ConsoleConfirmer(io), timeout=settings.hermes.permission_timeout)
    service = build_engineering(settings, gate, build_path_policy(settings), _PrintNotifications())
    if service is None:
        print("The engineering team is switched off (XENIO_ENGINEERING=false).")
        return 1
    try:
        folder, project = service.resolve_repository(args.project or "", args.repo or "")
        run = await service.orch.plan_project(args.engineer, folder, args.mode or "", project)
        service.runs[run.id] = run
        graph = service.orch.graphs.get(run.id)
        if graph is not None:
            for task in graph.tasks.values():
                print(f"  - {task.id}: {task.title} [{task.role}] after {', '.join(task.depends_on) or 'nothing'}; "
                      f"may change: {', '.join(task.allowed_paths) or 'any file'}")
            if run.mode == "change" and len(graph.tasks) > 1 and not args.yes:
                ok = await gate.check(PermissionRequest("engineering.start", f"start the team run ({len(graph.tasks)} "
                                                        "tasks) in isolated copies", RiskLevel.CONFIRM_REQUIRED))
                if not ok.approved:
                    print("Not started.")
                    return 1
        run = await service.orch.execute_plan(run)
        print()
        print(service.report(run.id, limit=20000)["report"])
        return 0 if run.status == "done" else 1
    except EngineeringError as exc:
        print(f"Not started: {exc}")
        return 1
    finally:
        await service.close()


async def _run_connect_google(settings: Settings, args: argparse.Namespace) -> int:
    import webbrowser
    from pathlib import Path

    from app.integrations.base import IntegrationError
    from app.integrations.google import find_client_file, sign_in
    from app.safety.policy import known_folders

    project = Path(__file__).resolve().parents[1]
    client = Path(args.client_file).expanduser() if args.client_file else find_client_file(
        [known_folders()["Downloads"], Path.home() / "Downloads", settings.data_dir, project,
         project / "user_required_steps"])
    if client is None or not client.is_file():
        print("No Google OAuth client file found (client_secret_*.json / credentials.json in Downloads or the "
              "XENIO folder).\n"
              "One-time setup in your own Google Cloud project (docs/PERSONAL_INTEGRATIONS.md):\n"
              "  1. https://console.cloud.google.com/projectcreate  - create a project (e.g. XENIO)\n"
              "  2. https://console.cloud.google.com/apis/library   - enable Gmail API and Google Calendar API\n"
              "  3. https://console.cloud.google.com/auth/audience  - External; add your Google address as a test user\n"
              "  4. https://console.cloud.google.com/apis/credentials - Create credentials > OAuth client ID > Desktop app\n"
              "     > Download JSON into Downloads\n"
              "Then run this command again (or pass --client-file PATH).")
        return 1
    print(f"Using the OAuth client {client} . XENIO asks for: read-only Gmail and your calendar events.")
    try:
        state = await asyncio.to_thread(sign_in, client, webbrowser.open)
    except IntegrationError as exc:
        print(f"Google sign-in failed: {exc}")
        return 1
    print(f"Google: {state.detail}." + (f"\n{state.setup}" if state.setup else ""))
    return 0 if state.connected else 1


async def _run_disconnect_google(settings: Settings, args: argparse.Namespace) -> int:
    from app.integrations.google import sign_out

    removed = await asyncio.to_thread(sign_out)
    print("XENIO's Google sign-in was revoked and deleted." if removed else "XENIO was not signed in to Google.")
    return 0


async def _run_briefing(settings: Settings, args: argparse.Namespace) -> int:
    from app.briefing import gather
    from app.integrations.calendar import default_calendar
    from app.integrations.email import EmailIntegration
    from app.integrations.github import GitHubIntegration
    from app.personal.projects import Projects
    from app.personal.tasks import TaskList
    from app.proactive import _public

    notifications, engine = build_proactive(settings)
    data = await asyncio.to_thread(
        gather, calendar=default_calendar(), email=EmailIntegration(), tasks=TaskList(), projects=Projects(),
        github=GitHubIntegration(), schedules=lambda: [_public(j) for j in (engine.jobs() if engine else [])],
        notifications=notifications)
    print(data.pop("summary"))
    for key, section in data.items():
        if isinstance(section, dict):
            print(f"  {key}: " + ("ok" if section.get("available") else f"unavailable - {section.get('reason')}"))
    return 0


async def _run_tick(settings: Settings, args: argparse.Namespace) -> int:
    notifications, engine = build_proactive(settings)
    if engine is None:
        print("Scheduled checks are switched off (XENIO_PROACTIVE=false).")
        return 1
    due = [j.get("name") for j in engine.due()]
    print(f"Due XENIO checks: {', '.join(due) if due else 'none'}" + (" (a Hermes gateway is running them)"
                                                                      if engine.gateway_serving() else ""))
    if due:
        await engine.tick_if_due()
        if engine.last_tick_error:
            print(f"Hermes' scheduler reported: {engine.last_tick_error}")
    for note in await asyncio.to_thread(engine.collect):
        print(note.line())
    return 0


async def _run_health(settings: Settings, args: argparse.Namespace) -> int:
    from app.health import run_health

    return await run_health(settings, live=args.live)


async def _run_mic_test(settings: Settings, args: argparse.Namespace) -> int:
    from app.health import run_mic_test

    return await run_mic_test(settings)


async def _run_barge_in_test(settings: Settings, args: argparse.Namespace) -> int:
    from app.voice.assistant import run_barge_in_test

    return await run_barge_in_test(settings)


async def _run_install_guard(settings: Settings, args: argparse.Namespace) -> int:
    from app.hermes.guard import PLUGIN_NAME, install_guard

    backups = settings.data_dir / "backups"
    print(f"Installing the {PLUGIN_NAME} Hermes plugin (backing up Hermes config.yaml to {backups})...")
    try:
        status = await asyncio.to_thread(install_guard, settings.hermes.command, backups)
    except (OSError, RuntimeError) as exc:
        print(f"Install failed: {exc}")
        return 1
    if not status.ok:
        print(f"Installed={status.installed} up_to_date={status.up_to_date} enabled={status.enabled} - check `hermes plugins show {PLUGIN_NAME}`")
        return 1
    print(f"{PLUGIN_NAME} installed and enabled. It is active only in Hermes processes started by XENIO.")
    print(f"Undo any time with: hermes plugins disable {PLUGIN_NAME}")
    return 0


def _forget_session(settings: Settings) -> None:
    (settings.data_dir / "hermes_session.json").unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = _parse_args(argv)
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Configuration error: {exc}\nFix it in .env (see .env.example).", file=sys.stderr)
        return 2
    log_file = setup_logging(settings.logs_dir, settings.secret_values(), settings.log_level, verbose=args.verbose)
    _share_settings_with_integrations(settings)
    runners = {
        "health": _run_health,
        "install-guard": _run_install_guard,
        "mic-test": _run_mic_test,
        "barge-in-test": _run_barge_in_test,
        "connect-google": _run_connect_google,
        "disconnect-google": _run_disconnect_google,
        "briefing": _run_briefing,
        "tick": _run_tick,
        "engineer": _run_engineer,
        "voice": _run_voice,
        "ui": _run_ui,
        "text": _run_text,
    }
    mode = next(
        (m for m, on in (("health", args.health), ("install-guard", args.install_guard),
                         ("mic-test", args.mic_test), ("barge-in-test", args.barge_in_test),
                         ("connect-google", args.connect_google), ("disconnect-google", args.disconnect_google),
                         ("briefing", args.briefing), ("tick", args.tick), ("engineer", args.engineer),
                         ("voice", args.voice), ("ui", args.ui)) if on),
        "text",
    )
    event(log, "XENIO started", version=__version__, mode=mode, log_file=log_file)

    runner = runners[mode]
    try:
        return asyncio.run(runner(settings, args))
    except KeyboardInterrupt:
        return 130
    finally:
        event(log, "XENIO stopped", mode=mode)


if __name__ == "__main__":
    sys.exit(main())
