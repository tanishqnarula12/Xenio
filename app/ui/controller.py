"""The UI's view of XENIO: every page reads, and every button acts on, XENIO's real components.

Nothing here is a second implementation: chat is ``Orchestrator.process_text`` (fast path, Hermes, guard), approvals
are the ``PermissionGate``, engineering is the V6 ``EngineeringService``, tasks/projects are XENIO's personal store,
schedules are Hermes' cron jobs (read here; created and removed through chat, inside the guard), memory is Hermes'
memory files (read-only: Hermes owns writing them), integrations and system are the ``--health`` checks.

Public methods are called from the HTTP server's threads; coroutines run on XENIO's loop (``run``). Results are plain
JSON-able dicts. Nothing returned contains a secret: text from tools and logs is redacted on the way out.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from app import __version__
from app.config import PROJECT_ROOT, Settings, default_hermes_home
from app.logging_setup import Redactor, event
from app.safety.policy import secret_env_values
from app.ui.approvals import WebConfirmer
from app.ui.assistant import AssistantState
from app.ui.events import EventHub

log = logging.getLogger("xenio.ui")

MAX_MESSAGE = 4000
MAX_AUDIO_BYTES = 12_000_000
HEALTH_SECONDS = 60.0
SETTINGS_DEFAULTS = {"motion": "system", "speak_replies": True, "orb_detail": "auto", "notifications": True,
                     "transcript_lines": 50, "wake_word": True, "desktop_notifications": True, "voice_approvals": True,
                     "orb_overlay": True, "orb_overlay_mode": "active", "summon_shortcut": "triple_space",
                     "summon_interval_ms": 450, "device_location": False, "orb_summon_mode": "choose",
                     "activity_retention_days": 90, "text_size": "default"}
SETTINGS_SCHEMA = {"motion": ("system", "full", "reduced"), "speak_replies": bool, "orb_detail": ("auto", "low", "high"),
                   "notifications": bool, "transcript_lines": int, "wake_word": bool, "desktop_notifications": bool,
                   "voice_approvals": bool, "orb_overlay": bool, "orb_overlay_mode": ("active", "always"),
                   "summon_shortcut": ("triple_space", "ctrl_option_space", "off"), "summon_interval_ms": int,
                   "device_location": bool, "orb_summon_mode": ("choose", "chat", "talk"),
                   "activity_retention_days": (7, 30, 90, 365), "text_size": ("default", "large")}
INT_RANGES = {"transcript_lines": (10, 200), "summon_interval_ms": (200, 1000)}


class UiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class UiController:
    def __init__(self, settings: Settings, orchestrator, gate, confirmer: WebConfirmer, hub: EventHub,
                 state: AssistantState, engineering=None, notifications=None, proactive=None, memory=None,
                 stt=None, tts=None, hermes_home: Path | None = None):
        self.settings = settings
        self.orchestrator = orchestrator
        self.gate = gate
        self.confirmer = confirmer
        self.hub = hub
        self.state = state
        self.engineering = engineering
        self.notifications = notifications
        self.proactive = proactive
        self.memory = memory
        self.stt = stt
        self.tts = tts
        self.hermes_home = hermes_home or default_hermes_home()
        self.redact = Redactor([*settings.secret_values(), *secret_env_values()])
        self.loop: asyncio.AbstractEventLoop | None = None
        self.conversation: list[dict] = []
        self._turn_task: asyncio.Task | None = None
        self._health: tuple[float, list[dict]] | None = None
        self._last_health: dict[str, str] = {}
        self._runs_seen: dict[str, str] = {}
        self._settings_file = settings.data_dir / "ui" / "settings.json"
        self.voice = None  # the talk engine (app/ui/voice.py) once it runs; None = the page records itself
        self.overlay = None  # app.overlay.OrbHelper: the floating orb and summon shortcut (macOS)
        self.timings: dict[str, list[int]] = {}  # measured latencies (ms) reported by the orb, newest last
        self.voice_problem = ""  # why the talk engine is not running, in words
        self.startup_notes: list[tuple[str, str]] = []  # recoveries done while XENIO started (recorded in Activity)

    # ---- loop plumbing ------------------------------------------------------------------------------------------

    def run(self, coro, timeout: float = 30.0):
        """Run a coroutine on XENIO's loop from an HTTP thread and wait for it."""
        if self.loop is None:
            raise UiError("XENIO is still starting", 503)
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def publish_state(self) -> None:
        self.hub.publish("assistant.state", self.state.snapshot())

    def record_system(self, what: str, tone: str = "info", **details) -> None:
        """A start, stop or recovery worth keeping in Activity (V3): plain words, no user content."""
        self.hub.publish("system.event", {"what": what, "tone": tone, **details})

    async def start(self) -> None:
        """On XENIO's loop: connect to Hermes, then keep background views fresh."""
        self.loop = asyncio.get_running_loop()
        dropped = await asyncio.to_thread(self.hub.prune, int(self.ui_settings()["activity_retention_days"]))
        self.record_system("XENIO started", version=__version__, pruned=dropped)
        for what, tone in self.startup_notes:
            self.record_system(what, tone)
        self.state.connection = "connecting"
        self.publish_state()
        await asyncio.sleep(0)
        self.state.connection = "initializing"
        self.publish_state()
        ok = await self.orchestrator.start()
        self.state.connection = "connected" if ok else "disconnected"
        if not ok:
            self.state.error = "Hermes could not be started. Run `python -m app.main --health` for details."
            self.hub.publish("assistant.error", {"error": self.state.error})
        self.publish_state()

    async def watch(self) -> None:
        """Background: the V6 team's runs (1 s), the Hermes connection, and a COMPLETED that has run its course."""
        last = None
        while True:
            try:
                self._watch_engineering()
                connected = getattr(self.orchestrator.hermes, "connected", None)
                if self.state.connection == "connected" and connected is False:
                    self.state.connection = "disconnected"
                    self.state.error = "The connection to Hermes was lost."
                    self.hub.publish("assistant.error", {"error": self.state.error})
                now = self.state.snapshot()
                if now != last:
                    self.hub.publish("assistant.state", now)
                    last = now
            except Exception:  # noqa: BLE001 - the UI's watcher must never take XENIO down
                log.exception("UI watcher failed")
            await asyncio.sleep(1.0)

    # ---- the assistant ------------------------------------------------------------------------------------------

    def snapshot(self) -> dict:
        info = getattr(self.orchestrator.hermes, "agent_info", {}) or {}
        return {"api": 1, "name": self.settings.name, "assistant": self.state.snapshot(),
                "conversation": self.conversation[-self.ui_settings()["transcript_lines"]:],
                "approvals": self.confirmer.pending(), "seq": self.hub.seq,
                "hermes": {"name": info.get("name"), "version": info.get("version")},
                "voice": self.voice_info(), "grant": self.grant_info(), "overlay": self.overlay_info(),
                "platform": sys.platform, "demo": False}

    def chat(self, text: str, via: str = "text", quiet: bool = False) -> dict:
        """``quiet``: answer on screen only, never out loud (typed in the floating orb: the user may not be able to
        listen right now)."""
        text = (text or "").strip()
        if not text:
            raise UiError("type or say something first", 422)
        if len(text) > MAX_MESSAGE:
            raise UiError(f"messages are limited to {MAX_MESSAGE} characters", 422)
        refusal = self._refusal()
        if refusal is not None:
            raise refusal
        turn_id = uuid.uuid4().hex[:12]
        self.run(self._begin(turn_id, text, via, quiet), timeout=10)
        return {"turn": turn_id}

    def turn_running(self) -> bool:
        return bool(self.orchestrator.busy) or (self._turn_task is not None and not self._turn_task.done())

    def _refusal(self) -> UiError | None:
        """Why a new request cannot start now (typed or spoken), or None."""
        if self.state.paused:
            return UiError("XENIO is paused: resume it to send a request", 409)
        if self.state.connection != "connected":
            return UiError("XENIO is not connected to Hermes", 503)
        if self.turn_running():
            return UiError("XENIO is still answering: stop it or wait for the answer", 409)
        return None

    async def _begin(self, turn_id: str, text: str, via: str, quiet: bool = False) -> None:
        # checked again here, on XENIO's loop: two quick submits both passed the check in their request threads
        # before either turn had started (and the second then ran the same request again)
        if self.turn_running():
            raise UiError("XENIO is still answering: stop it or wait for the answer", 409)
        self._turn_task = asyncio.ensure_future(self._turn(turn_id, text, via, quiet=quiet))

    async def voice_turn(self, text: str, on_chunk=None, on_tool=None, context: str | None = None):
        """A request the talk engine heard (on XENIO's loop): the same turn as a typed one, plus the engine's own
        callbacks so the answer is spoken while it is written. Returns the orchestrator's response."""
        from app.orchestrator import XenioResponse

        refusal = self._refusal()
        if refusal is not None:
            return XenioResponse(text=f"I can't take that right now: {refusal}.", ok=False, error="busy")
        turn_id = uuid.uuid4().hex[:12]
        self._turn_task = asyncio.ensure_future(self._turn(turn_id, text, "voice", on_chunk, on_tool, context))
        return await self._turn_task

    async def _turn(self, turn_id: str, text: str, via: str, voice_chunk=None, voice_tool=None,
                    context: str | None = None, quiet: bool = False):
        self.state.turn_started(turn_id)
        message = {"id": turn_id + "-u", "turn": turn_id, "role": "user", "text": self.redact(text), "via": via,
                   "at": datetime.now().isoformat(timespec="seconds"), **({"quiet": True} if quiet else {})}
        self.conversation.append(message)
        self.hub.publish("chat.user", message)
        self.hub.publish("request.received", {"turn": turn_id, "via": via, "chars": len(text)})  # no content kept
        self.publish_state()

        def on_chunk(chunk: str) -> None:
            if self.state.phase == "PROCESSING":
                self.state.first_output()
                self.publish_state()
            self.hub.publish("chat.chunk", {"turn": turn_id, "text": chunk})
            if voice_chunk is not None:
                voice_chunk(chunk)

        def on_tool(activity) -> None:
            title = self.redact(str(getattr(activity, "title", "") or ""))[:160]
            status = str(getattr(activity, "status", "") or "")
            self.state.tool_changed(title, str(getattr(activity, "kind", "") or ""), status)
            self.hub.publish("chat.tool", {"turn": turn_id, "id": self.redact(str(getattr(activity, "tool_call_id", "")
                                                                                  or ""))[:80], "title": title,
                                           "status": status, "detail": self.redact(str(getattr(activity, "detail", "")
                                                                                       or ""))[:300]})
            self.publish_state()
            if voice_tool is not None:
                voice_tool(activity)

        from app.orchestrator import XenioResponse

        try:
            extra = {"context": context} if context else {}
            response = await self.orchestrator.process_text(text, on_chunk=on_chunk, on_tool=on_tool, **extra)
        except Exception as exc:  # noqa: BLE001 - a turn failure is shown, never swallowed
            log.exception("UI turn failed")
            self.state.turn_finished(False, f"{type(exc).__name__}: {exc}")
            self.hub.publish("chat.done", {"turn": turn_id, "ok": False, "text": "", "error": self.state.error,
                                           "actions": [], "sources": []})
            self.hub.publish("assistant.error", {"turn": turn_id, "error": self.state.error})
            self.publish_state()
            return XenioResponse(text="That request failed; the details are on screen.", ok=False, error="hermes_error")
        reply = {"id": turn_id + "-a", "turn": turn_id, "role": "assistant", "text": self.redact(response.text or ""),
                 "ok": response.ok, "cancelled": response.cancelled, "error": response.error,
                 "actions": [self.redact(a.strip()) for a in response.action_lines() if a.strip() not in ("", "actions:")],
                 "sources": [{"title": s.title, "url": s.url} for s in (response.sources or [])][:8],
                 "at": datetime.now().isoformat(timespec="seconds")}
        self.conversation = (self.conversation + [reply])[-200:]
        self.state.turn_finished(response.ok, response.text if not response.ok else "", response.cancelled)
        self.hub.publish("chat.done", reply)
        if not response.ok and not response.cancelled:
            self.hub.publish("assistant.error", {"turn": turn_id, "error": response.error or "failed"})
        self.publish_state()
        if (via != "voice" and not quiet and self.voice is not None and response.ok and not response.cancelled
                and self.ui_settings()["speak_replies"]):
            asyncio.ensure_future(self.voice.speak_reply(response.text or ""))  # typed: answered out loud too
        return response

    def cancel(self) -> dict:
        if self.voice is not None:
            self.loop.call_soon_threadsafe(self.voice.hush)
        if not self.orchestrator.busy:
            return {"cancelled": False, "reason": "nothing is running"}
        self.run(self.orchestrator.interrupt(), timeout=10)
        return {"cancelled": True}

    def set_paused(self, paused: bool) -> dict:
        self.state.paused = bool(paused)
        if paused:
            self.end_grant("XENIO was paused")
        self.publish_state()
        return self.state.snapshot()

    # ---- "allow for this task": the gate's task grant ----------------------------------------------------------------

    def grant_info(self) -> dict | None:
        grant = self.gate.task_grant() if self.gate is not None and hasattr(self.gate, "task_grant") else None
        if grant is None:
            return None
        return {**grant, "until": datetime.fromtimestamp(grant["until"]).isoformat(timespec="seconds"),
                "reason": self.redact(grant.get("reason") or "")}

    def grant_changed(self, _grant=None) -> None:
        self.hub.publish("grant.changed", {"grant": self.grant_info()})

    def end_grant(self, why: str = "ended by the user") -> dict:
        if self.gate is not None and hasattr(self.gate, "end_task") and self.gate.task_grant() is not None:
            self.gate.end_task(why)
        return {"grant": None}

    def dismiss_error(self) -> dict:
        self.state.dismiss_error()
        self.publish_state()
        return self.state.snapshot()

    def new_conversation(self) -> dict:
        if self.orchestrator.busy:
            raise UiError("XENIO is still answering: stop it first", 409)
        self.end_grant("a new conversation started")
        ok = self.run(self.orchestrator.new_session(), timeout=60)
        if not ok:
            raise UiError("Hermes could not start a new conversation", 502)
        self.conversation = []
        self.hub.publish("chat.reset", {})
        return {"ok": True}

    # ---- voice: the talk engine (app/ui/voice.py) and the existing speech services, never their keys ------------

    def voice_info(self) -> dict:
        engine = self.voice
        return {"stt": self.stt is not None, "tts": self.tts is not None, "engine": engine is not None,
                "wake": bool(engine is not None and engine.wake_available and engine.wake_enabled),
                "wake_phrase": self.settings.voice.wake_phrase, "problem": self.voice_problem or None}

    def attach_voice(self, engine) -> None:
        engine.wake_enabled = bool(self.ui_settings()["wake_word"])
        if self.voice_problem:
            self.record_system("Voice works again", "recovery")
        self.voice, self.voice_problem = engine, ""
        self.hub.publish("voice.changed", self.voice_info())

    def detach_voice(self, engine) -> None:
        if self.voice is engine:
            self.voice = None
            self.state.voice = ""
            self.hub.publish("voice.changed", self.voice_info())
            self.publish_state()

    def voice_unavailable(self, reason: str) -> None:
        event(log, "Talk engine unavailable", logging.WARNING, reason=reason)
        if reason != self.voice_problem:
            self.record_system(f"Voice stopped working: {reason}", "problem")
        self.voice_problem = reason
        self.hub.publish("voice.changed", self.voice_info())

    def _engine(self):
        if self.voice is None:
            raise UiError(f"XENIO's microphone is not in use{': ' + self.voice_problem if self.voice_problem else ''}",
                          503)
        return self.voice

    def voice_listen(self) -> dict:
        """The microphone button: XENIO listens on the Mac's microphone, as after "Hey XENIO"."""
        engine = self._engine()

        async def listen() -> str:
            return engine.listen_now()

        if self.run(listen(), timeout=5) == "busy":
            raise UiError("XENIO is still working on the last request: stop it or wait for the answer", 409)
        return {"listening": True}

    def voice_finish(self) -> dict:
        engine = self._engine()

        async def finish() -> bool:
            return engine.finish_listening()

        return {"finished": self.run(finish(), timeout=5)}

    def voice_level(self) -> dict:
        engine = self.voice
        return {"level": engine.level() if engine is not None else 0.0, "phase": self.state.voice or None}

    def transcribe(self, audio: bytes, content_type: str) -> dict:
        if self.stt is None:
            raise UiError("speech recognition is not configured (STT_API_KEY / STT_API_URL in XENIO's .env)", 503)
        if not audio:
            raise UiError("no audio was received", 422)
        if len(audio) > MAX_AUDIO_BYTES:
            raise UiError("that recording is too long", 413)
        kind = "audio/wav" if "wav" in (content_type or "") else (content_type or "audio/wav").split(";")[0]
        name = "speech.wav" if kind == "audio/wav" else "speech.webm"
        from app.voice.stt import STTError

        try:
            text = self.run(self.stt.transcribe(audio, filename=name, content_type=kind), timeout=60)
        except STTError as exc:
            raise UiError(f"speech recognition failed: {exc}", 502) from None
        except TimeoutError:
            raise UiError("speech recognition did not answer in time", 504) from None
        return {"text": str(text or "").strip()}

    def speak(self, text: str) -> bytes:
        if self.tts is None:
            raise UiError("speech output is not configured (TTS_API_KEY / TTS_API_URL in XENIO's .env)", 503)
        from app.voice.speech_text import to_speakable

        words = to_speakable(text or "")
        if not words.strip():
            raise UiError("there is nothing to say", 422)
        from app.voice.tts import TTSError

        try:
            return self.run(self.tts.synthesize(words[:3000]), timeout=60)
        except TTSError as exc:
            raise UiError(f"speech output failed: {exc}", 502) from None
        except TimeoutError:
            raise UiError("speech output did not answer in time", 504) from None

    # ---- approvals ------------------------------------------------------------------------------------------------

    def approvals(self) -> dict:
        return {"pending": self.confirmer.pending(), "history": self.confirmer.history()}

    def decide(self, approval_id: str, decision: str, confirm_text: str = "") -> dict:
        return self.confirmer.decide(approval_id, decision, confirm_text)

    # ---- engineering (V6) -----------------------------------------------------------------------------------------

    def _run_summary(self, run: dict, live: bool) -> dict:
        tasks = run.get("tasks") or []
        counts: dict[str, int] = {}
        for t in tasks:
            counts[t.get("status", "pending")] = counts.get(t.get("status", "pending"), 0) + 1
        return {"id": run.get("id"), "request": self.redact(str(run.get("request") or ""))[:200],
                "mode": run.get("mode"), "status": run.get("status"), "project": run.get("project") or None,
                "created": run.get("created"), "updated": run.get("updated"), "verified": bool(run.get("verified")),
                "outcome": self.redact(str(run.get("outcome") or ""))[:300], "tasks": counts,
                "running": run.get("running", []), "live": live}

    def _live_run(self, run) -> dict:
        svc = self.engineering
        data = run.to_dict()
        graph = svc.orch.graphs.get(run.id)
        if graph is not None:
            data["tasks"] = [{k: getattr(t, k) for k in ("id", "title", "role", "description", "depends_on",
                                                          "allowed_paths", "status", "attempts", "failure")}
                             | {"result": {k: v for k, v in (t.result or {}).items()
                                           if k in ("summary", "changed_files", "model", "timings", "session")}}
                             for t in graph.tasks.values()]
        known = getattr(svc.orch, "live_tasks", {}).get(run.id, {})
        data["running"] = [{"task": tid,
                            "role": graph.tasks[tid].role if graph and tid in graph.tasks else known.get(tid, {}).get("role", ""),
                            "title": graph.tasks[tid].title if graph and tid in graph.tasks else known.get(tid, {}).get("title", tid),
                            "since": known.get(tid, {}).get("since"),
                            "model": svc.agents.models.get(session) or "Hermes main model"}
                           for tid, session in svc.orch.live.get(run.id, {}).items()]
        return data

    def _all_runs(self) -> list[tuple[dict, bool]]:
        svc = self.engineering
        if svc is None:
            return []
        live = [(self._live_run(r), True) for r in svc.runs.values()]
        saved = [(d, False) for d in svc._saved_runs()]
        return sorted(live + saved, key=lambda rl: str(rl[0].get("created") or ""), reverse=True)

    def runs(self) -> dict:
        if self.engineering is None:
            return {"available": False, "reason": "the engineering team is switched off (XENIO_ENGINEERING=false)",
                    "runs": []}
        return {"available": True, "runs": [self._run_summary(r, live) for r, live in self._all_runs()][:50]}

    def run_detail(self, run_id: str) -> dict:
        for run, live in self._all_runs():
            if run.get("id") == run_id:
                keep = ("id", "request", "mode", "status", "project", "created", "updated", "plan_summary", "risks",
                        "tasks", "events", "baseline_tests", "final_tests", "scope_violations", "test_tampering",
                        "security_findings", "reviews", "turns", "answer", "outcome", "verified", "diff_stat",
                        "approvals_needed", "report_path", "running", "repo_state")
                detail = {k: run.get(k) for k in keep if k in run}
                detail["live"] = live
                return json.loads(self.redact(json.dumps(detail, default=str)))
        raise UiError(f"no engineering run {run_id!r}", 404)

    def run_diff(self, run_id: str) -> dict:
        """The changes a run made (its integration branch against the snapshot it started from): read with git, never
        applied here. Redacted, and cut at 150,000 characters."""
        from app.engineering.repo import RepoError, Workspace

        for run, _live in self._all_runs():
            if run.get("id") != run_id:
                continue
            data = run.get("workspace") or {}
            if not data.get("repo") or not data.get("integration_branch"):
                raise UiError("this run has no workspace, so it has no changes to show", 404)
            try:
                ws = Workspace.from_dict(data)
                files = ws.changed_files(ws.integration_branch)
                text = ws.diff(ws.integration_branch, limit=150_000)
            except (RepoError, OSError, TypeError, subprocess.SubprocessError) as exc:
                raise UiError(f"the changes could not be read any more ({' '.join(str(exc).split())[:200]})", 410) from None
            return {"run": run_id, "files": [{"status": st, "path": path} for st, path in files][:500],
                    "diff": self.redact(text)}
        raise UiError(f"no engineering run {run_id!r}", 404)

    def run_report(self, run_id: str) -> dict:
        if self.engineering is None:
            raise UiError("the engineering team is switched off", 503)
        try:
            report = self.engineering.report(run_id)
        except Exception as exc:  # noqa: BLE001 - EngineeringError and file errors alike
            raise UiError(str(exc), 404) from None
        return {"run": report.get("run"), "status": report.get("status"), "report": self.redact(report.get("report", ""))}

    def run_start(self, payload: dict) -> dict:
        """Ask the engineering team for work from Mission Control: it inspects the repository and plans in its own
        workspace (the user's files are not touched), then waits for the go-ahead - never started unasked."""
        svc = self.engineering
        if svc is None:
            raise UiError("the engineering team is switched off", 503)
        request = " ".join(str((payload or {}).get("request") or "").split())[:2000]
        if len(request) < 4:
            raise UiError("say what the engineering team should do", 422)
        mode = str(payload.get("mode") or "")
        if mode not in ("", "change", "plan", "explain", "review", "test"):
            raise UiError("mode must be change, plan, explain, review or test", 422)
        from app.engineering.service import EngineeringError

        try:
            out = self.run(svc.start(request, str(payload.get("project") or "")[:120], str(payload.get("path") or "")[:500],
                                     mode, auto_run=False), timeout=60)
        except EngineeringError as exc:
            raise UiError(str(exc), 409) from None
        self.hub.publish("request.received", {"via": "mission-control", "kind": "engineering", "chars": len(request)})
        return json.loads(self.redact(json.dumps(out, default=str)))

    def run_action(self, run_id: str, action: str) -> dict:
        """approve / cancel run here; apply / push / pull_request ask through the PermissionGate (Approvals)."""
        svc = self.engineering
        if svc is None:
            raise UiError("the engineering team is switched off", 503)
        if action not in ("approve", "cancel", "apply", "push", "pull_request"):
            raise UiError(f"unknown action {action!r}", 422)
        from app.engineering.service import EngineeringError

        coro = {"approve": lambda: svc.approve(run_id), "cancel": lambda: svc.cancel(run_id),
                "apply": lambda: svc.apply(run_id), "push": lambda: svc.push(run_id),
                "pull_request": lambda: svc.pull_request(run_id)}[action]()
        try:
            out = self.run(coro, timeout=float(self.settings.hermes.permission_timeout) + 120)
        except EngineeringError as exc:
            raise UiError(str(exc), 409) from None
        return json.loads(self.redact(json.dumps(out, default=str)))

    def agents(self) -> dict:
        """The V6 roles (configured, enforced) and what each is really doing now."""
        svc = self.engineering
        if svc is None:
            return {"available": False, "reason": "the engineering team is switched off", "agents": []}
        working: dict[str, list[dict]] = {}
        blocked: dict[str, list[dict]] = {}
        for run, live in self._all_runs():
            if not live:
                continue
            busy = set()
            # every live agent session counts - planning and review turns too, which have no task in the plan
            latest = (run.get("events") or [{}])[-1]
            for r in run.get("running") or []:
                busy.add(r.get("task"))
                working.setdefault(r.get("role", ""), []).append(
                    {"run": run.get("id"), "task": r.get("task"), "title": r.get("title") or r.get("task"),
                     "status": "running", "failure": None, "since": r.get("since"), "model": r.get("model"),
                     "run_request": self.redact(str(run.get("request") or ""))[:120],
                     "latest": {"text": self.redact(str(latest.get("text") or ""))[:200], "at": latest.get("time")}
                     if latest.get("text") else None})
            for task in run.get("tasks") or []:
                if task.get("id") not in busy and task.get("status") in ("blocked", "failed"):
                    blocked.setdefault(task.get("role", ""), []).append(
                        {"run": run.get("id"), "task": task.get("id"), "title": task.get("title"),
                         "status": task.get("status"), "failure": task.get("failure") or None})
        out = []
        for role in svc.roles.roles.values():
            now = working.get(role.name, [])
            state = "disabled" if not role.enabled else "working" if now else "blocked" if blocked.get(role.name) else "idle"
            out.append({"id": role.name, "name": role.title, "description": role.responsibilities,
                        "capabilities": sorted(role.capabilities), "tools": sorted(role.tools),
                        "model": role.model or "Hermes main model", "state": state,
                        "current": now, "blocked": blocked.get(role.name, [])})
        return {"available": True, "agents": out}

    def _watch_engineering(self) -> None:
        if self.engineering is None:
            return
        live = [self._live_run(r) for r in self.engineering.runs.values()]
        self.state.set_background(live)
        for run in live:
            key = json.dumps([run.get("status"), [(t.get("id"), t.get("status")) for t in run.get("tasks") or []],
                              [r.get("task") for r in run.get("running") or []]], default=str)
            if self._runs_seen.get(run["id"]) != key:
                self._runs_seen[run["id"]] = key
                self.hub.publish("engineering.run", self._run_summary(run, True))

    # ---- tasks & projects (XENIO's personal store) ------------------------------------------------------------------

    def _tasks(self):
        from app.personal.tasks import TaskList

        return TaskList()

    def tasks(self, status: str = "all") -> dict:
        from app.integrations.base import IntegrationError

        tl = self._tasks()
        try:
            items = [t.to_dict() for t in tl.list(status=status or "all", limit=200)]
        except IntegrationError as exc:
            raise UiError(str(exc), 422) from None
        return {"tasks": items, "counts": tl.counts()}

    def task_create(self, payload: dict) -> dict:
        title = str(payload.get("title") or "").strip()
        if not title:
            raise UiError("a task needs a title", 422)
        priority = str(payload.get("priority") or "normal")
        if priority not in ("low", "normal", "high"):
            raise UiError("priority must be low, normal or high", 422)
        due = str(payload.get("due") or "").strip() or None
        if due and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", due):
            raise UiError("the due date must look like 2026-10-31", 422)
        from app.integrations.base import IntegrationError

        try:
            task = self._tasks().add(title[:200], str(payload.get("description") or "")[:2000], priority, due,
                                     str(payload.get("project") or "").strip() or None)
        except IntegrationError as exc:
            raise UiError(str(exc), 422) from None
        self.hub.publish("task.changed", {"id": task.id, "change": "created"})
        return task.to_dict()

    def task_update(self, task_id: int, payload: dict) -> dict:
        from app.integrations.base import IntegrationError

        tl = self._tasks()
        action = str(payload.get("action") or "")
        if action not in ("complete", "reopen"):
            raise UiError("action must be complete or reopen", 422)
        try:
            task = tl.complete(task_id) if action == "complete" else tl.update(task_id, status="pending")
        except IntegrationError as exc:
            raise UiError(str(exc), 404) from None
        self.hub.publish("task.changed", {"id": task_id, "change": action})
        return task.to_dict()

    def task_delete(self, task_id: int) -> dict:
        from app.integrations.base import IntegrationError

        try:
            task = self._tasks().delete(task_id)
        except IntegrationError as exc:
            raise UiError(str(exc), 404) from None
        self.hub.publish("task.changed", {"id": task_id, "change": "deleted"})
        return task.to_dict()

    def projects(self) -> dict:
        from app.personal.projects import Projects

        p = Projects()
        current = p.current()
        return {"projects": p.list(include_done=True), "current": current.get("name") if current else None,
                "workspace": self.workspace_projects(), "workspace_path": str(self._workspace() or "") or None}

    def _workspace(self) -> Path | None:
        ws = getattr(getattr(self.orchestrator, "paths", None), "workspace", None)
        return Path(ws) if ws else None

    def workspace_projects(self) -> list[dict]:
        """What XENIO built in its own workspace (a folder per project), newest first, with the engineering team's runs
        on each. Only names, kinds and dates are read - never file contents."""
        from app.context import _fixes, _next_step

        ws = self._workspace()
        if ws is None or not ws.is_dir():
            return []
        runs: dict[str, list[dict]] = {}
        for run, _live in self._all_runs():
            source = str(run.get("source") or "")
            if source:
                runs.setdefault(os.path.normcase(os.path.realpath(source)), []).append(run)
        out = []
        for folder in sorted((d for d in ws.iterdir() if d.is_dir() and not d.name.startswith(".")
                              and d.name != "engineering"), key=lambda d: d.stat().st_mtime, reverse=True)[:40]:
            names = {c.name for c in folder.iterdir()} if folder.is_dir() else set()
            kind = ("Node.js" if "package.json" in names else "Python" if names & {"pyproject.toml", "requirements.txt",
                    "setup.py", "main.py", "app.py"} else "Web page" if "index.html" in names else "Folder")
            mine = runs.get(os.path.normcase(os.path.realpath(folder)), [])
            last = max(mine, key=lambda r: str(r.get("updated") or r.get("created") or ""), default=None)
            out.append({"name": folder.name, "path": str(folder), "kind": kind, "git": ".git" in names,
                        "files": len([n for n in names if not n.startswith(".")]),
                        "updated": datetime.fromtimestamp(folder.stat().st_mtime).isoformat(timespec="seconds"),
                        "runs": len(mine), "last_run": {"id": last.get("id"), "status": last.get("status"),
                                                       "request": self.redact(str(last.get("request") or ""))[:160],
                                                       "verified": bool(last.get("verified")), "next": _next_step(last),
                                                       "left_to_fix": len(_fixes(last))}
                        if last else None})
        return out

    # ---- schedules (Hermes' cron jobs) -------------------------------------------------------------------------------

    def _reminders(self):
        from app.reminders import Reminders

        return Reminders(self.hermes_home, self.settings.hermes.command[0])

    def create_reminder(self, text: str, when: str) -> dict:
        from app.integrations.base import IntegrationError
        from app.reminders import parse_when

        text, when = str(text or "").strip(), str(when or "").strip()
        if not text or not when:
            raise UiError("say what to remind you of and when (for example: at 5 pm, in 20 minutes, tomorrow at 9)", 422)
        try:
            parsed = parse_when(when)
            if parsed is None:
                raise UiError(f"XENIO did not understand the time {when!r}: try 'at 5 pm', 'in 20 minutes', "
                              "'tomorrow at 9' or 'every day at 8 am'", 422)
            made = self._reminders().create(text, parsed)
        except IntegrationError as exc:
            raise UiError(str(exc), 422) from None
        self.hub.publish("schedule.changed", {"reminder": made["id"]})
        return made

    def remove_reminder(self, reminder_id: str) -> dict:
        from app.integrations.base import IntegrationError

        try:
            gone = self._reminders().remove(reminder_id)
        except IntegrationError as exc:
            raise UiError(str(exc), 404) from None
        self.hub.publish("schedule.changed", {"removed": gone.get("id")})
        return gone

    # ---- the floating orb (app/overlay.py, native/orb) ---------------------------------------------------------------

    def overlay_info(self) -> dict:
        info = self.overlay.info() if self.overlay is not None else {"supported": sys.platform == "darwin",
                                                                       "running": False, "problem": "not started"}
        info["timings"] = {k: {"last": v[-1], "median": sorted(v)[len(v) // 2], "n": len(v)} for k, v in self.timings.items() if v}
        return info

    def overlay_status(self, payload: dict) -> dict:
        """What the helper could set up (shortcut, Accessibility, screens, location), as the orb page relays it."""
        allowed = {"shortcut", "shortcut_status", "accessibility", "screens", "location"}
        status = {k: v for k, v in (payload or {}).items() if k in allowed and isinstance(v, (str, bool, int))}
        if self.overlay is not None:
            self.overlay.status.update(status)
        self.hub.publish("overlay.changed", self.overlay_info())
        return {"ok": True}

    def overlay_summon(self, payload: dict) -> dict:
        """Open the floating orb's quick chat (mode "chat") or start listening there ("voice"), or ask how (no mode):
        Mission Control's "Quick chat" button. Only while XENIO's own orb runs."""
        mode = (payload or {}).get("mode")
        if mode not in (None, "chat", "voice"):
            raise UiError("mode must be chat or voice", 422)
        if self.overlay is None or not self.overlay.running:
            raise UiError("the floating orb is not running (Settings > Floating orb)", 409)
        command = {"type": "summon"} | ({"mode": mode} if mode else {})
        if not self.overlay.send(command):
            raise UiError("the floating orb did not take it", 503)
        return {"ok": True}

    def overlay_metric(self, payload: dict) -> dict:
        """A latency the orb measured (e.g. microphone listening -> orb visible), kept for Diagnostics."""
        name, ms = str(payload.get("name") or ""), payload.get("ms")
        if name not in ("listening_to_orb_ms", "summon_to_listening_ms", "summon_to_orb_ms") or not isinstance(ms, (int, float)):
            raise UiError("unknown metric", 422)
        if 0 <= ms < 60_000:
            self.timings[name] = (self.timings.get(name, []) + [int(ms)])[-50:]
            event(log, "Orb timing", metric=name, ms=int(ms))
        return {"ok": True}

    def location(self) -> dict:
        from app.location import load

        data = load(self.settings.data_dir)
        return {"location": data}

    def save_location(self, payload: dict) -> dict:
        from app.integrations.base import IntegrationError
        from app.location import save

        try:
            data = save(payload.get("label") or "", payload.get("lat"), payload.get("lon"),
                        source=str(payload.get("source") or "settings"), accuracy_m=payload.get("accuracy_m"),
                        base=self.settings.data_dir)
        except IntegrationError as exc:
            raise UiError(str(exc), 422) from None
        self.hub.publish("location.changed", {"location": data})
        return {"location": data}

    def schedules(self) -> dict:
        from app.proactive import ProactiveEngine, _public, parse_run_output
        from app.reminders import is_reminder

        engine = self.proactive or ProactiveEngine(self.hermes_home, self.notifications)
        reminders = [{**r, "text": self.redact(r["text"])} for r in self._reminders().list(include_done=True)]
        reminders.sort(key=lambda r: (r["done"], str(r.get("next_run") or r.get("last_run") or "")))
        if not (engine.cron_dir / "jobs.json").exists():
            return {"available": True, "jobs": [], "reminders": [], "note": "no reminders or scheduled checks yet"}
        jobs = []
        for job in engine.jobs():
            if is_reminder(job):
                continue
            item = _public(job)
            runs = []
            folder = engine.cron_dir / "output" / str(job.get("id", ""))
            for path in sorted(folder.glob("*.md"), reverse=True)[:10] if folder.is_dir() else []:
                try:
                    kind, message = parse_run_output(path.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    continue
                runs.append({"at": path.stem, "result": kind, "message": self.redact(message)[:400]})
            item["runs"] = runs
            jobs.append(item)
        return {"available": True, "jobs": jobs, "reminders": reminders[:60],
                "ticking": bool(self.proactive) or engine.gateway_serving(), "timezone": time.strftime("%Z")}

    # ---- memory (Hermes' own files: read, and changed only by the user's explicit action) -----------------------------

    def _memory_files(self):
        from app.memory.files import MemoryFiles

        return MemoryFiles(self.hermes_home)

    @staticmethod
    def _entry_id(store: str, text: str) -> str:
        import hashlib

        return hashlib.sha256(f"{store}\0{text}".encode()).hexdigest()[:16]

    def memory_entries(self, query: str = "") -> dict:
        """Saved memory, apart from everything that is not memory (the page says what each kind is)."""
        from app.memory.files import STORES, MemoryEditError

        files = self._memory_files()
        words = [w for w in (query or "").lower().split() if w]
        entries = []
        try:
            for store in STORES:
                for text in files.entries(store):
                    if all(w in text.lower() for w in words):
                        entries.append({"id": self._entry_id(store, text), "store": store, "text": self.redact(text)})
        except MemoryEditError as exc:
            return {"available": False, "reason": str(exc), "entries": []}
        return {"available": True, "editable": sys.platform != "win32", "entries": entries, "usage": files.usage(),
                "folder": str(files.folder)}

    def memory_change(self, payload: dict) -> dict:
        """add (store, text) / correct (id, text) / delete (id): the user's own changes, under Hermes' lock and XENIO's
        memory policy (no secrets, short facts, within Hermes' size limit)."""
        from app.memory.files import STORES, MemoryEditError

        action = str((payload or {}).get("action") or "")
        files = self._memory_files()
        try:
            if action == "add":
                store = str(payload.get("store") or "")
                if store not in STORES:
                    raise UiError("store must be 'user' (about you) or 'memory' (XENIO's notes)", 422)
                result = files.add(store, str(payload.get("text") or ""))
            elif action in ("correct", "delete"):
                wanted = str(payload.get("id") or "")
                found = next(((st, t) for st in STORES for t in files.entries(st) if self._entry_id(st, t) == wanted), None)
                if found is None:
                    raise UiError("that entry is not in memory any more (Hermes may have changed it): reload", 409)
                store, old = found
                result = (files.replace(store, old, str(payload.get("text") or "")) if action == "correct"
                          else files.remove(store, old))
            else:
                raise UiError("action must be add, correct or delete", 422)
        except MemoryEditError as exc:
            raise UiError(str(exc), exc.status) from None
        self.hub.publish("memory.changed", {"store": result["store"], "action": result["action"]})  # never the text
        return {"ok": True, **{k: v for k, v in result.items() if k != "text"}}

    # ---- context (V3: where the ongoing work stands, from XENIO's own records) -----------------------------------------

    def context_brief(self, query: str = "") -> dict:
        from app.context import ContextEngine

        svc = self.engineering
        runs_root = Path(getattr(svc, "runs_root", "")) if svc is not None else self.settings.hermes.workspace / "engineering"
        live = [self._live_run(r) for r in svc.runs.values()] if svc is not None else []
        brief = ContextEngine(runs_root, activity_log=self.settings.data_dir / "ui" / "activity.jsonl").brief(query, live)
        return json.loads(self.redact(json.dumps(brief, default=str)))

    # ---- activity and the tool-call audit ------------------------------------------------------------------------------

    def activity(self, limit: int = 200, offset: int = 0, kind: str = "all") -> dict:
        from app.ui.events import ACTIVITY_KINDS

        if kind != "all" and kind not in ACTIVITY_KINDS:
            raise UiError(f"unknown kind {kind!r}", 422)
        events = self.hub.activity(limit + 1, offset, kind)
        more = len(events) > limit
        events = events[:limit]
        oldest = str(events[-1].get("at") or "") if more and events else ""  # older notes belong to later pages
        if self.notifications is not None and offset == 0 and kind in ("all", "scheduled"):
            for note in self.notifications.recent(30):
                if (note.created or "")[:19] < oldest:
                    continue
                events.append({"v": 1, "seq": 0, "id": f"note-{note.id}", "type": "notification",
                               "at": (note.created or "")[:19], "data": {"title": note.title,
                                                                        "message": self.redact(note.message)[:400],
                                                                        "priority": note.priority, "source": note.source}})
        events = {e["id"]: e for e in events}.values()
        return {"events": sorted(events, key=lambda e: str(e.get("at") or ""), reverse=True)[:limit + 30],
                "more": more, "next_offset": offset + limit}

    def audit(self, limit: int = 200) -> dict:
        from app.ui.audit import tool_calls

        log_path = self.settings.data_dir / "logs" / "xenio-guard.log"
        return {"calls": tool_calls(log_path, self.redact, limit), "source": str(log_path)}

    # ---- integrations & system (the --health checks) -----------------------------------------------------------------

    def health(self, force: bool = False) -> dict:
        now = time.monotonic()
        if not force and self._health and now - self._health[0] < HEALTH_SECONDS:
            checks = self._health[1]
        else:
            checks = self._collect_checks()
            self._health = (now, checks)
            changed = {c["name"]: c["status"] for c in checks}
            for name, status in changed.items():
                if self._last_health.get(name) not in (None, status):
                    self.hub.publish("health.changed", {"name": name, "from": self._last_health[name], "to": status})
            self._last_health = changed
        return {"checks": checks, "checked_at": datetime.now().isoformat(timespec="seconds"),
                "assistant": self.state.snapshot(), "platform": sys.platform}

    def _collect_checks(self) -> list[dict]:
        from app import health as H

        s = self.settings
        hermes_ok = self.state.connection == "connected"
        info = getattr(self.orchestrator.hermes, "agent_info", {}) or {}
        checks = [H.Check("Hermes", "OK" if hermes_ok else "FAIL",
                          f"connected: {info.get('name', 'Hermes')} {info.get('version', '')}".strip() if hermes_ok
                          else f"not connected ({self.state.connection})")]
        for fn in (H._check_config, H._check_guard, H._check_model, H._check_web, H._check_github, H._check_vision,
                   H._check_engineering, H._check_fast):
            try:
                checks.append(fn(s))
            except Exception as exc:  # noqa: BLE001 - one broken check must not hide the others
                checks.append(H.Check(fn.__name__.replace("_check_", "").title(), "FAIL", f"check failed: {exc}"))
        try:
            checks.extend(H._check_personal(s))
        except Exception as exc:  # noqa: BLE001
            checks.append(H.Check("Personal", "FAIL", f"check failed: {exc}"))
        try:
            checks.append(H._check_desktop())
        except Exception as exc:  # noqa: BLE001
            checks.append(H.Check("Desktop", "FAIL", f"check failed: {exc}"))
        checks.append(H.Check("STT", "OK" if self.stt else "FAIL", "configured" if self.stt else
                              "not configured (STT_API_KEY / STT_API_URL)"))
        checks.append(H.Check("TTS", "OK" if self.tts else "WARN", "configured" if self.tts else
                              "not configured: replies are shown as text only"))
        orb = self.overlay_info()
        if orb.get("supported"):
            if orb.get("running"):
                checks.append(H.Check("Floating orb", "OK", "over your apps; excluded from screenshots"))
            else:
                checks.append(H.Check("Floating orb", "WARN", orb.get("problem") or "not running"))
            st = orb.get("status") or {}
            shortcut = {"ok": ("OK", "triple-press Space" if st.get("shortcut") == "triple_space" else "Control-Option-Space"),
                        "needs_accessibility": ("WARN", "triple-press Space needs Accessibility for the app XENIO runs "
                                                        "from (System Settings > Privacy & Security > Accessibility); "
                                                        "Control-Option-Space works meanwhile"),
                        "off": ("OK", "switched off in Settings")}.get(str(st.get("shortcut_status") or ""),
                                                                        ("WARN", "not reported yet"))
            checks.append(H.Check("Summon shortcut", shortcut[0], shortcut[1]))
        info = self.voice_info()
        if info["engine"]:
            heard = f'listening for "{info["wake_phrase"]}"' if info["wake"] else "wake word off: use the mic button"
            checks.append(H.Check("Talk engine", "OK", f"Mac microphone and speaker; {heard}"))
        else:
            checks.append(H.Check("Talk engine", "WARN", info["problem"] or "starting"))
        checks.append(self._memory_check(H))
        checks.append(H.Check("Event stream", "OK", f"{self.hub.listeners} live view(s) connected; {self.hub.seq} "
                                                    "events since XENIO started; missed events are replayed on reconnect"))
        checks.append(self._scheduler_check(H))
        checks.append(self._audio_check(H))
        return [{"name": c.name, "status": c.status, "detail": self.redact(c.detail)} for c in checks]

    def _memory_check(self, H):
        from app.memory.files import STORES, MemoryEditError

        files = self._memory_files()
        if not files.folder.is_dir():
            return H.Check("Memory", "WARN", "no Hermes memory files yet")
        try:
            usage = files.usage()
        except MemoryEditError as exc:
            return H.Check("Memory", "FAIL", str(exc))
        parts, full = [], False
        for store, label in (("user", "about you"), ("memory", "XENIO's notes")):
            u = usage[store]
            if u["used"] is None:
                return H.Check("Memory", "FAIL", f"Hermes' {STORES[store]} could not be read")
            parts.append(f"{label} {u['used']:,} of {u['limit']:,} characters")
            full = full or u["used"] > 0.9 * u["limit"]
        detail = "; ".join(parts) + ("; nearly full: new memories are refused until something is removed (Memory page)"
                                     if full else "")
        return H.Check("Memory", "WARN" if full else "OK", detail)

    def _scheduler_check(self, H):
        try:
            data = self.schedules()
        except Exception as exc:  # noqa: BLE001
            return H.Check("Scheduler", "FAIL", f"Hermes' scheduler could not be read ({exc})")
        reminders = [r for r in data.get("reminders") or [] if not r.get("done")]
        jobs = data.get("jobs") or []
        failed = [j for j in jobs if (j.get("runs") or [{}])[0].get("result") == "failed"]
        what = f"{len(reminders)} reminder(s) and {len(jobs)} check(s) waiting"
        if not data.get("ticking") and (reminders or jobs):
            return H.Check("Scheduler", "WARN", f"{what}, but nothing runs them right now (XENIO's scheduler is off and no "
                                                "Hermes gateway is serving)")
        if failed:
            return H.Check("Scheduler", "WARN", f"{what}; the last run of {', '.join(j.get('name', '?') for j in failed[:3])} "
                                                "failed (Schedules page)")
        return H.Check("Scheduler", "OK", what + ("; on time" if data.get("ticking") else ""))

    def _audio_check(self, H):
        if sys.platform != "darwin" and self.voice is None:
            return H.Check("Audio devices", "OK", "not used (no talk engine)")
        try:
            from app.voice.audio import describe_devices

            v = self.settings.voice
            devices = describe_devices(v.input_device, v.output_device)
        except Exception as exc:  # noqa: BLE001 - MicrophoneUnavailable, SpeakerUnavailable, no PortAudio
            return H.Check("Audio devices", "WARN", f"{exc}".strip()[:200])
        chosen = " (chosen in .env)" if self.settings.voice.input_device or self.settings.voice.output_device else ""
        return H.Check("Audio devices", "OK", f"microphone: {devices.input_name}; speaker: {devices.output_name}{chosen}")

    # ---- skills & tools ------------------------------------------------------------------------------------------------

    def tools(self) -> dict:
        from app.tools import desktop, engineering, filesystem, media, personal, vision, web
        from app.tools.registry import ALL_TOOLS

        groups = {"Files": filesystem.xenio_tools(), "Web": web.xenio_tools(), "Personal": personal.xenio_tools(),
                  "Vision": vision.xenio_tools(), "Desktop": desktop.xenio_tools() if sys.platform in ("darwin", "win32")
                  else [], "Media": media.xenio_tools(), "Engineering": engineering.xenio_tools()}
        out = [{"group": group, "name": t.hermes_name, "description": t.description.split(". ")[0][:220],
                "level": t.permission_level.name, "provider": "XENIO"} for group, tools in groups.items() for t in tools]
        out += [{"group": "Hermes", "name": t.hermes_name, "description": t.description.split(". ")[0][:220],
                 "level": t.permission_level.name, "provider": "Hermes"} for t in ALL_TOOLS if t.backend == "hermes"]
        # what really happened lately, from the guard's audit log: failures and refusals per tool
        recent: dict[str, dict] = {}
        for call in self.audit(400)["calls"]:
            r = recent.setdefault(call["tool"], {"calls": 0, "failed": 0, "blocked": 0, "last_problem": None})
            r["calls"] += 1
            if call["status"] in ("failed", "blocked"):
                r[call["status"]] += 1
                r["last_problem"] = r["last_problem"] or {"at": call["at"], "status": call["status"],
                                                         "detail": call.get("detail") or call.get("summary") or ""}
        for tool in out:
            tool["recent"] = recent.get(tool["name"], {"calls": 0, "failed": 0, "blocked": 0, "last_problem": None})
        return {"tools": out, "skills": self._hermes_skills(), "health": {c["name"]: c for c in self.health()["checks"]}}

    def _hermes(self) -> str:
        command = self.settings.hermes.command
        return command[0] if command else "hermes"

    def _hermes_skills(self) -> list[dict]:
        from app.hermes.skills import disabled_skills, list_skills

        now = time.monotonic()
        cached = getattr(self, "_disabled_cache", None)
        if cached is None or now - cached[0] > 30:
            try:
                cached = (now, disabled_skills(self._hermes()))
            except (OSError, subprocess.SubprocessError):
                cached = (now, set())
            self._disabled_cache = cached
        return list_skills(self.hermes_home, cached[1])

    def skill_toggle(self, name: str, enabled: bool) -> dict:
        """Switch an installed Hermes skill on or off (Hermes' own skills.disabled list, config backed up first)."""
        from app.hermes.skills import SkillError, set_enabled

        try:
            disabled = set_enabled(self._hermes(), self.hermes_home, self.settings.data_dir / "backups", name, enabled)
        except SkillError as exc:
            raise UiError(str(exc), exc.status) from None
        except (OSError, subprocess.SubprocessError) as exc:
            raise UiError(f"Hermes could not be asked ({exc})", 502) from None
        self._disabled_cache = (time.monotonic(), disabled)
        self.hub.publish("skill.changed", {"name": name, "enabled": enabled})
        return {"name": name, "enabled": name not in disabled}

    # ---- settings (the UI's own preferences, in XENIO's data folder) -------------------------------------------------

    def ui_settings(self) -> dict:
        try:
            saved = json.loads(self._settings_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        def valid(key, value) -> bool:  # a value an older XENIO saved that no longer exists falls back to the default
            rule = SETTINGS_SCHEMA[key]
            return value in rule if isinstance(rule, tuple) else isinstance(value, rule)

        return {**SETTINGS_DEFAULTS, **{k: v for k, v in (saved or {}).items() if k in SETTINGS_DEFAULTS and valid(k, v)}}

    def save_ui_settings(self, payload: dict) -> dict:
        current = self.ui_settings()
        before = dict(current)
        for key, value in (payload or {}).items():
            rule = SETTINGS_SCHEMA.get(key)
            if rule is None:
                raise UiError(f"unknown setting {key!r}", 422)
            if rule is bool and not isinstance(value, bool):
                raise UiError(f"{key} must be true or false", 422)
            low, high = INT_RANGES.get(key, (10, 200))
            if rule is int and (not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high):
                raise UiError(f"{key} must be a number from {low} to {high}", 422)
            if isinstance(rule, tuple) and value not in rule:
                raise UiError(f"{key} must be one of {', '.join(map(str, rule))}", 422)
            current[key] = value
        self._settings_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._settings_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(current, indent=1), encoding="utf-8")
        tmp.replace(self._settings_file)
        changed = sorted(k for k in (payload or {}) if before.get(k) != current.get(k))
        if changed:  # saving the same value again changes nothing (and is not worth an Activity line)
            self.hub.publish("settings.changed", {**current, "changed": changed})
        if "activity_retention_days" in (payload or {}):
            import threading

            threading.Thread(target=self.hub.prune, args=(int(current["activity_retention_days"]),),
                             name="xenio-activity-prune", daemon=True).start()
        if self.overlay is not None and "orb_overlay" in (payload or {}):
            import threading

            threading.Thread(target=self.overlay.start if current["orb_overlay"] else self.overlay.stop,
                             name="xenio-orb-toggle", daemon=True).start()
        if self.voice is not None and self.voice.wake_enabled != current["wake_word"]:
            self.voice.wake_enabled = bool(current["wake_word"])
            self.hub.publish("voice.changed", self.voice_info())
        return current

    # ---- notifications from scheduled checks -------------------------------------------------------------------------

    def deliver_notifications(self, notes: list) -> None:
        """Results of scheduled checks and reminders: on the page, on the desktop, and (reminders) said out loud."""
        settings = self.ui_settings()
        for note in notes:
            message = self.redact(note.message)[:600]
            self.hub.publish("notification", {"title": note.title, "message": message,
                                              "priority": note.priority, "source": note.source})
            reminder = note.source == "reminder"
            if settings["notifications"] or reminder or note.priority == "high":
                self.desktop_notify(note.title, message, "Reminder" if reminder else "Scheduled check")
            if self.voice is not None and self.loop is not None:
                spoken = f"Reminder: {message}" if reminder else f"{note.title}: {message}"
                asyncio.run_coroutine_threadsafe(self.voice.announce(spoken), self.loop)
        if self.notifications is not None and notes:
            self.notifications.mark_delivered([n.id for n in notes if n.id is not None])

    def desktop_notify(self, title: str, message: str, subtitle: str = "") -> None:
        """A macOS notification (in the background: never delays XENIO), when the user has them switched on."""
        if not self.ui_settings()["desktop_notifications"]:
            return
        import threading

        from app.desktop.notify import notify

        threading.Thread(target=notify, args=(self.redact(title), self.redact(message), subtitle), daemon=True,
                         name="xenio-notify").start()

    def announce_approval(self, record: dict) -> None:
        """A question from the gate: also on the desktop, so it is seen even when the page is not."""
        self.desktop_notify("XENIO needs your approval", f"XENIO wants to {record.get('description', '')}.",
                            "Approve on Home or say yes")

    async def ask_aloud(self, request, strict: bool, approval_id: str) -> None:
        """The question asked by the talk engine too (Settings: answer approvals by voice)."""
        engine = self.voice
        if engine is None or not self.ui_settings()["voice_approvals"]:
            return
        await engine.ask_approval(request, strict, lambda value: self.confirmer.answer(approval_id, value))

    def log_event(self, message: str, **fields: Any) -> None:
        event(log, message, **fields)


def ui_dist() -> Path:
    return PROJECT_ROOT / "ui" / "dist"
