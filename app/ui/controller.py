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
import re
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

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
                     "transcript_lines": 50, "wake_word": True}
SETTINGS_SCHEMA = {"motion": ("system", "full", "reduced"), "speak_replies": bool, "orb_detail": ("auto", "low", "high"),
                   "notifications": bool, "transcript_lines": int, "wake_word": bool}


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
        self.voice_problem = ""  # why the talk engine is not running, in words

    # ---- loop plumbing ------------------------------------------------------------------------------------------

    def run(self, coro, timeout: float = 30.0):
        """Run a coroutine on XENIO's loop from an HTTP thread and wait for it."""
        if self.loop is None:
            raise UiError("XENIO is still starting", 503)
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def publish_state(self) -> None:
        self.hub.publish("assistant.state", self.state.snapshot())

    async def start(self) -> None:
        """On XENIO's loop: connect to Hermes, then keep background views fresh."""
        self.loop = asyncio.get_running_loop()
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
                "voice": self.voice_info(),
                "platform": sys.platform, "demo": False}

    def chat(self, text: str, via: str = "text") -> dict:
        text = (text or "").strip()
        if not text:
            raise UiError("type or say something first", 422)
        if len(text) > MAX_MESSAGE:
            raise UiError(f"messages are limited to {MAX_MESSAGE} characters", 422)
        refusal = self._refusal()
        if refusal is not None:
            raise refusal
        turn_id = uuid.uuid4().hex[:12]
        self.run(self._begin(turn_id, text, via), timeout=10)
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

    async def _begin(self, turn_id: str, text: str, via: str) -> None:
        self._turn_task = asyncio.ensure_future(self._turn(turn_id, text, via))

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
                    context: str | None = None):
        self.state.turn_started(turn_id)
        message = {"id": turn_id + "-u", "turn": turn_id, "role": "user", "text": self.redact(text), "via": via,
                   "at": datetime.now().isoformat(timespec="seconds")}
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
        if (via != "voice" and self.voice is not None and response.ok and not response.cancelled
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
        self.publish_state()
        return self.state.snapshot()

    def dismiss_error(self) -> dict:
        self.state.dismiss_error()
        self.publish_state()
        return self.state.snapshot()

    def new_conversation(self) -> dict:
        if self.orchestrator.busy:
            raise UiError("XENIO is still answering: stop it first", 409)
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
        data["running"] = [{"task": tid, "role": graph.tasks[tid].role if graph and tid in graph.tasks else "",
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

    def run_report(self, run_id: str) -> dict:
        if self.engineering is None:
            raise UiError("the engineering team is switched off", 503)
        try:
            report = self.engineering.report(run_id)
        except Exception as exc:  # noqa: BLE001 - EngineeringError and file errors alike
            raise UiError(str(exc), 404) from None
        return {"run": report.get("run"), "status": report.get("status"), "report": self.redact(report.get("report", ""))}

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
            for task in run.get("tasks") or []:
                entry = {"run": run.get("id"), "task": task.get("id"), "title": task.get("title"),
                         "status": task.get("status"), "failure": task.get("failure") or None}
                if any(r.get("task") == task.get("id") for r in run.get("running") or []):
                    working.setdefault(task.get("role", ""), []).append(entry)
                elif task.get("status") in ("blocked", "failed"):
                    blocked.setdefault(task.get("role", ""), []).append(entry)
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
        return {"projects": p.list(include_done=True), "current": current.get("name") if current else None}

    # ---- schedules (Hermes' cron jobs) -------------------------------------------------------------------------------

    def schedules(self) -> dict:
        from app.proactive import ProactiveEngine, _public, parse_run_output

        engine = self.proactive or ProactiveEngine(self.hermes_home, self.notifications)
        if not (engine.cron_dir / "jobs.json").exists():
            return {"available": True, "jobs": [], "note": "no scheduled checks yet"}
        jobs = []
        for job in engine.jobs():
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
        return {"available": True, "jobs": jobs, "ticking": bool(self.proactive) or engine.gateway_serving(),
                "timezone": time.strftime("%Z")}

    # ---- memory (Hermes' files; read-only here) ----------------------------------------------------------------------

    def memory_entries(self, query: str = "") -> dict:
        if self.memory is None:
            return {"available": False, "entries": []}
        entries = self.run(self.memory.recall(query, limit=200), timeout=10)
        return {"available": True, "editable": False,
                "entries": [{"store": e.store, "text": self.redact(e.text)} for e in entries]}

    # ---- activity ----------------------------------------------------------------------------------------------------

    def activity(self, limit: int = 200) -> dict:
        events = self.hub.activity(limit)
        if self.notifications is not None:
            for note in self.notifications.recent(30):
                events.append({"v": 1, "seq": 0, "id": f"note-{note.id}", "type": "notification",
                               "at": (note.created or "")[:19], "data": {"title": note.title,
                                                                        "message": self.redact(note.message)[:400],
                                                                        "priority": note.priority, "source": note.source}})
        events = {e["id"]: e for e in events}.values()
        return {"events": sorted(events, key=lambda e: str(e.get("at") or ""), reverse=True)[:limit]}

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
        info = self.voice_info()
        if info["engine"]:
            heard = f'listening for "{info["wake_phrase"]}"' if info["wake"] else "wake word off: use the mic button"
            checks.append(H.Check("Talk engine", "OK", f"Mac microphone and speaker; {heard}"))
        else:
            checks.append(H.Check("Talk engine", "WARN", info["problem"] or "starting"))
        memory_dir = self.hermes_home / "memories"
        checks.append(H.Check("Memory", "OK" if memory_dir.is_dir() else "WARN",
                              f"Hermes memory in {memory_dir}" if memory_dir.is_dir() else "no Hermes memory files yet"))
        return [{"name": c.name, "status": c.status, "detail": self.redact(c.detail)} for c in checks]

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
        return {"tools": out, "skills": self._hermes_skills(), "health": {c["name"]: c for c in self.health()["checks"]}}

    def _hermes_skills(self) -> list[dict]:
        skills = []
        for path in sorted((self.hermes_home / "skills").glob("*/*/SKILL.md"))[:300]:
            try:
                head = path.read_text(encoding="utf-8", errors="replace")[:1500]
            except OSError:
                continue
            name = re.search(r"^name:\s*(.+)$", head, re.M)
            desc = re.search(r"^description:\s*(.+)$", head, re.M)
            skills.append({"name": (name.group(1) if name else path.parent.name).strip().strip("'\""),
                           "category": path.parent.parent.name,
                           "description": (desc.group(1) if desc else "").strip().strip("'\"")[:220]})
        return skills

    # ---- settings (the UI's own preferences, in XENIO's data folder) -------------------------------------------------

    def ui_settings(self) -> dict:
        try:
            saved = json.loads(self._settings_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        return {**SETTINGS_DEFAULTS, **{k: v for k, v in (saved or {}).items() if k in SETTINGS_DEFAULTS}}

    def save_ui_settings(self, payload: dict) -> dict:
        current = self.ui_settings()
        for key, value in (payload or {}).items():
            rule = SETTINGS_SCHEMA.get(key)
            if rule is None:
                raise UiError(f"unknown setting {key!r}", 422)
            if rule is bool and not isinstance(value, bool):
                raise UiError(f"{key} must be true or false", 422)
            if rule is int and (not isinstance(value, int) or isinstance(value, bool) or not 10 <= value <= 200):
                raise UiError(f"{key} must be a number from 10 to 200", 422)
            if isinstance(rule, tuple) and value not in rule:
                raise UiError(f"{key} must be one of {', '.join(rule)}", 422)
            current[key] = value
        self._settings_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._settings_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(current, indent=1), encoding="utf-8")
        tmp.replace(self._settings_file)
        self.hub.publish("settings.changed", current)
        if self.voice is not None and self.voice.wake_enabled != current["wake_word"]:
            self.voice.wake_enabled = bool(current["wake_word"])
            self.hub.publish("voice.changed", self.voice_info())
        return current

    # ---- notifications from scheduled checks -------------------------------------------------------------------------

    def deliver_notifications(self, notes: list) -> None:
        for note in notes:
            self.hub.publish("notification", {"title": note.title, "message": self.redact(note.message)[:600],
                                              "priority": note.priority, "source": note.source})
        if self.notifications is not None and notes:
            self.notifications.mark_delivered([n.id for n in notes if n.id is not None])

    def log_event(self, message: str, **fields: Any) -> None:
        event(log, message, **fields)


def ui_dist() -> Path:
    return PROJECT_ROOT / "ui" / "dist"
