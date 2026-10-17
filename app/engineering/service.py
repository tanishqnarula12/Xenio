"""XENIO's engineering team as a service (V6), living in XENIO's own process.

* ``start``: resolve the repository (a named project, a path, or the current project; ambiguous names are asked
  about, the user's current project is never switched), inspect, isolate and plan in the background. A one-task
  change runs at once; a team plan waits for the user's "go ahead" (``approve``) unless the user already said to
  just do it (``auto_run``).
* ``status`` / ``report`` / ``list``: real state only (task counts, running agents with role, model and time; no
  invented percentages).
* ``cancel``: no new tasks start, running agents are asked to stop, and the answer says what is still stopping.
* ``apply`` / ``push`` / ``pull_request``: the only steps that leave the run's isolated workspace. Each needs the
  user's confirmation through XENIO's PermissionGate (the xenio_engineering tool's call is confirmed by the guard;
  the CLI asks itself).

Milestones go to XENIO's notifications (spoken when XENIO is idle). Agents' permission requests reach the user
labelled with the task and role that asked. The chat's Hermes reaches this service through a local socket guarded by
a per-start token that only the chat's Hermes process gets (never the engineering agents' process).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import hashlib
import secrets
import subprocess
import tempfile
from pathlib import Path

from app.config import Settings
from app.engineering import repo as R
from app.engineering.agents import AgentSessions, SessionRegistry
from app.engineering.orchestrator import EngineeringOrchestrator, Run
from app.engineering.roles import RoleRegistry
from app.hermes.client import HermesPermissionRequest
from app.logging_setup import event
from app.safety.permissions import PermissionGate, PermissionRequest, from_hermes_request
from app.safety.policy import PathPolicy, RiskLevel, classify_command

log = logging.getLogger("xenio.engineering")

SOCKET_NAME = "engineering.sock"


class EngineeringError(Exception):
    pass


class EngineeringService:
    def __init__(self, settings: Settings, gate: PermissionGate | None, paths: PathPolicy, notifications=None,
                 agents: AgentSessions | None = None, projects=None):
        self.settings = settings
        self.gate = gate
        self.paths = paths
        self.notifications = notifications
        self.root = settings.data_dir / "engineering"
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.runs_root = settings.hermes.workspace / "engineering"
        self.registry = SessionRegistry(self.root / "sessions.json")
        self.roles = RoleRegistry.load(settings.data_dir / "engineering_roles.toml")
        e = settings.engineering
        self.agents = agents or AgentSessions(settings.hermes, settings.data_dir, ask=self._ask,
                                              turn_timeout=e.task_timeout)
        self.orch = EngineeringOrchestrator(self.agents, self.roles, self.runs_root, self.registry, notify=self._notify,
                                            max_parallel=e.max_parallel, test_timeout=e.test_timeout,
                                            sandbox=e.sandbox_tests)
        self._projects = projects
        self.runs: dict[str, Run] = {}
        self._jobs: dict[str, asyncio.Task] = {}
        self._server: asyncio.AbstractServer | None = None
        self.token = secrets.token_urlsafe(24)
        self.socket_path = self.root / SOCKET_NAME
        if len(str(self.socket_path)) > 100:  # macOS: a Unix socket path holds at most 104 bytes
            tag = hashlib.sha256(str(self.root).encode()).hexdigest()[:10]
            self.socket_path = Path(tempfile.gettempdir()) / f"xenio-eng-{tag}.sock"  # the per-user temp folder
        self.registry.path.unlink(missing_ok=True)  # nothing is registered from an earlier XENIO

    # ---- plumbing ---------------------------------------------------------------------------------------------

    def endpoint_env(self) -> dict[str, str]:
        """Only for the chat's Hermes process: how its xenio_engineering tool reaches this service."""
        return {"XENIO_ENGINEERING_SOCKET": str(self.socket_path), "XENIO_ENGINEERING_TOKEN": self.token}

    def _notify(self, kind: str, text: str) -> None:
        if self.notifications is None or kind in ("inspected", "verifying"):
            return
        priority = "high" if kind in ("failed", "task_failed", "done", "cancelled", "planned") else "normal"
        try:
            self.notifications.add("Engineering", text, priority=priority, source="engineering",
                                   action_required=kind == "planned")
        except Exception:  # noqa: BLE001 - a notification must never break a run
            log.debug("notification failed", exc_info=True)

    async def _ask(self, session_id: str, req: HermesPermissionRequest) -> bool:
        scope = self.registry.get(session_id) or {}
        if scope and req.kind == "edit":
            # an engineering session's edit is decided by the engineering rules only: in its own worktree and file
            # scope (the guard already allowed it) - or not at all, whatever the normal policy would allow
            return self._edit_in_scope(scope, req)
        request = from_hermes_request(req, self.paths)
        if scope:
            request.description = f"(engineering task {scope.get('task')}, {scope.get('role')}) {request.description}"
        if self.gate is None:
            return False
        return (await self.gate.check(request)).approved

    def _edit_in_scope(self, scope: dict, req: HermesPermissionRequest) -> bool:
        """Hermes 0.22's gateway asks before every edit (it has no accept-edits mode for ACP). For an engineering
        session XENIO answers itself when every edited file passes the same engineering rules the guard applied."""
        from app.safety.policy import classify_engineering, classify_tool_call

        paths = [str(c.get("path")) for c in req.content if isinstance(c, dict) and c.get("type") == "diff" and c.get("path")]
        if not paths:
            return False
        for path in paths:
            args = {"path": path, "content": ""}
            decision = classify_engineering("write_file", args, scope,
                                            classify_tool_call("write_file", args, self.paths, scope.get("root")))
            if decision.blocked or decision.level > RiskLevel.SAFE_WRITE:
                return False
        return True

    def projects(self):
        if self._projects is None:
            from app.personal.projects import Projects

            self._projects = Projects()
        return self._projects

    def resolve_repository(self, project: str = "", path: str = "") -> tuple[str, str]:
        """(folder, project name). A path wins; else the named (or current) project's local folder."""
        if path:
            folder = Path(path).expanduser()
            if not folder.is_dir():
                raise EngineeringError(f"{folder} is not a folder")
            return str(folder.resolve()), project
        from app.integrations.base import IntegrationError

        try:
            record = self.projects().resolve(project or None)  # several matches -> IntegrationError: ask the user
        except IntegrationError as exc:
            raise EngineeringError(str(exc)) from None
        folder = str(record.get("path") or "")
        if not folder or not Path(folder).expanduser().is_dir():
            raise EngineeringError(f"The project {record['name']!r} has no local folder recorded. Tell me where it is "
                                   "on this Mac (a path), and I can remember it for the project.")
        return str(Path(folder).expanduser().resolve()), record["name"]

    # ---- actions ----------------------------------------------------------------------------------------------

    async def start(self, request: str, project: str = "", path: str = "", mode: str = "", auto_run: bool = False) -> dict:
        if not request or not request.strip():
            raise EngineeringError("what should the engineering team do?")
        folder, project_name = self.resolve_repository(project, path)
        busy = [r for r in self.runs.values() if r.source == folder and r.status in ("planning", "running", "verifying",
                                                                                         "reviewing")]
        if busy:
            raise EngineeringError(f"run {busy[0].id} is still working on that repository; wait for it or cancel it")
        state = await asyncio.to_thread(R.inspect, folder)
        job = asyncio.ensure_future(self._plan_then_maybe_run(request, folder, mode, project_name, auto_run))
        self._jobs[f"start:{id(job)}"] = job
        return {"status": "started", "repository": state.summary(), "mode": mode or "detected from the request",
                "note": "Planning in the background; the plan (and, for team work, a request to go ahead) arrives as a "
                        "notification. Ask for the status any time.", "project": project_name}

    async def _plan_then_maybe_run(self, request: str, folder: str, mode: str, project: str, auto_run: bool) -> None:
        try:
            run = await self.orch.plan_project(request, folder, mode, project)
        except Exception as exc:  # noqa: BLE001 - PlanError, RepoError, RoleError, Hermes errors: all reported
            self._notify("failed", f"The engineering request could not be planned: {' '.join(str(exc).split())[:300]}")
            event(log, "ENGINEERING_PLAN_FAILED", logging.WARNING, error=str(exc)[:300])
            return
        self.runs[run.id] = run
        graph = self.orch.graphs.get(run.id)
        team = graph is not None and len(graph.tasks) > 1
        if run.mode == "plan" or (team and not auto_run and not self.settings.engineering.auto_start):
            if team and run.mode != "plan":
                self._notify("planned", f"Run {run.id}: say 'go ahead' to start it, or ask to see the plan.")
            if run.mode == "plan":
                await self.orch.execute_plan(run)
            return
        self._launch(run)

    def _launch(self, run: Run) -> None:
        self._jobs[run.id] = asyncio.ensure_future(self.orch.execute_plan(run))

    def get(self, run_id: str = "") -> Run:
        if run_id:
            run = self.runs.get(run_id)
            if run is None:
                raise EngineeringError(f"no engineering run {run_id!r}")
            return run
        if not self.runs:
            raise EngineeringError("there is no engineering run yet")
        return max(self.runs.values(), key=lambda r: r.created)

    async def approve(self, run_id: str = "") -> dict:
        run = self.get(run_id)
        if run.status != "planned":
            raise EngineeringError(f"run {run.id} is {run.status}, not waiting for a go-ahead")
        if run.mode == "plan":
            run.mode = "change"
        self._launch(run)
        return {"status": "running", "run": run.id}

    def status(self, run_id: str = "") -> dict:
        if not self.runs and not any(not j.done() for j in self._jobs.values()):
            return {"active": False, "summary": "The engineering team is not working on anything."}
        if not self.runs:
            return {"active": True, "summary": "Inspecting the repository and planning; no plan yet."}
        run = self.get(run_id)
        graph = self.orch.graphs.get(run.id)
        running = []
        for tid, session in self.orch.live.get(run.id, {}).items():
            task = graph.tasks.get(tid) if graph else None
            running.append({"task": tid, "title": task.title if task else tid, "role": task.role if task else "",
                            "model": self.agents.models.get(session) or "Hermes main model"})
        return {"run": run.id, "request": run.request, "status": run.status, "mode": run.mode,
                "tasks": graph.counts() if graph else {}, "running": running,
                "failed": [{"task": t.id, "why": t.failure} for t in (graph.tasks.values() if graph else [])
                           if t.status in ("failed", "blocked")],
                "plan": [{"id": t.id, "title": t.title, "role": t.role, "depends_on": t.depends_on,
                          "allowed_paths": t.allowed_paths, "status": t.status} for t in (graph.tasks.values() if graph else [])],
                "risks": run.risks, "last_events": [e["text"] for e in run.events[-5:]], "outcome": run.outcome}

    def list_runs(self) -> list[dict]:
        return [{"run": r.id, "request": r.request[:80], "status": r.status, "outcome": r.outcome[:120]}
                for r in sorted(self.runs.values(), key=lambda r: r.created, reverse=True)[:10]]

    def report(self, run_id: str = "", limit: int = 6000) -> dict:
        run = self.get(run_id)
        text = Path(run.report_path).read_text(encoding="utf-8") if run.report_path else self.orch.generate_report(run)
        return {"run": run.id, "status": run.status, "report": text[:limit], "report_file": run.report_path}

    async def cancel(self, run_id: str = "") -> dict:
        run = self.get(run_id)
        if run.status in ("done", "failed", "cancelled"):
            return {"run": run.id, "status": run.status, "note": "already finished"}
        if run.status == "planned":
            run.status, run.outcome = "cancelled", "Cancelled before it started."
            self.orch.save(run)
            return {"run": run.id, "status": "cancelled", "still_running": []}
        out = await self.orch.cancel(run)
        return {"run": run.id, "status": "cancelling", **out}

    async def _confirm(self, action: str, description: str, risk: RiskLevel, confirmed: bool) -> None:
        if confirmed:
            return  # the xenio_engineering tool call was already confirmed by the user through the guard
        if self.gate is None:
            raise EngineeringError("there is no way to ask you for confirmation here")
        decision = await self.gate.check(PermissionRequest(action, description, risk, "xenio"))
        if not decision.approved:
            raise EngineeringError(f"not done: {decision.reason}")

    async def apply(self, run_id: str = "", confirmed: bool = False) -> dict:
        run = self.get(run_id)
        ws = self.orch.workspaces.get(run.id)
        if ws is None or run.mode != "change":
            raise EngineeringError("this run has no changes to apply")
        ok, why = await asyncio.to_thread(ws.apply_check)
        if not ok:
            raise EngineeringError(f"the changes cannot be applied cleanly: {why or 'nothing to apply'}")
        files = len(await asyncio.to_thread(ws.changed_files, ws.integration_branch))
        await self._confirm("engineering.apply", f"write XENIO's changes from run {run.id} ({files} file(s)) into your "
                            f"working tree at {run.source} (not committed)", RiskLevel.CONFIRM_REQUIRED, confirmed)
        changed = await asyncio.to_thread(ws.apply_to_source)
        run.add_event("applied", f"Applied {len(changed)} file(s) to your working tree.")
        self.orch.save(run)
        return {"run": run.id, "applied": changed, "note": "changed in your working tree, not committed"}

    async def push(self, run_id: str = "", remote: str = "origin", confirmed: bool = False) -> dict:
        run = self.get(run_id)
        ws = self.orch.workspaces.get(run.id)
        if ws is None or ws.copied:
            raise EngineeringError("this run's result is not in a git repository with a remote")
        if remote not in (run.repo_state.get("remotes") or []):
            raise EngineeringError(f"the repository has no remote called {remote!r}")
        command = f"git push -u {remote} {ws.integration_branch}"
        decision = classify_command(command, self.paths, run.source)
        if decision.blocked:
            raise EngineeringError(f"blocked by XENIO policy: {decision.reason}")
        await self._confirm("engineering.push", f"push the branch {ws.integration_branch} to {remote} (not your main "
                            "branch; no force)", max(decision.level, RiskLevel.CONFIRM_REQUIRED), confirmed)
        done = await asyncio.to_thread(subprocess.run, ["git", "-c", "core.hooksPath=/dev/null", "push", "-u", remote,
                                                        ws.integration_branch], cwd=run.source, capture_output=True,
                                       text=True, timeout=180)
        if done.returncode != 0:
            raise EngineeringError(f"git push failed: {(done.stderr or done.stdout).strip()[-300:]}")
        run.add_event("pushed", f"Pushed {ws.integration_branch} to {remote}.")
        self.orch.save(run)
        return {"run": run.id, "pushed": ws.integration_branch, "remote": remote}

    async def pull_request(self, run_id: str = "", draft: bool = True, confirmed: bool = False) -> dict:
        run = self.get(run_id)
        ws = self.orch.workspaces.get(run.id)
        if ws is None or not any(e["kind"] == "pushed" for e in run.events):
            raise EngineeringError("push the branch first (a separate confirmation)")
        await self._confirm("engineering.pull_request", f"open a {'draft ' if draft else ''}pull request on GitHub from "
                            f"{ws.integration_branch}", RiskLevel.CONFIRM_REQUIRED, confirmed)
        body = Path(run.report_path).read_text(encoding="utf-8")[:60000] if run.report_path else run.outcome
        cmd = ["gh", "pr", "create", "--head", ws.integration_branch, "--title", run.request[:70], "--body", body]
        done = await asyncio.to_thread(subprocess.run, cmd + (["--draft"] if draft else []), cwd=run.source,
                                       capture_output=True, text=True, timeout=120)
        if done.returncode != 0:
            raise EngineeringError(f"gh pr create failed: {(done.stderr or done.stdout).strip()[-300:]}")
        url = done.stdout.strip().splitlines()[-1] if done.stdout.strip() else ""
        run.add_event("pull_request", f"Opened {url}")
        self.orch.save(run)
        return {"run": run.id, "pull_request": url}

    # ---- the local socket (the xenio_engineering tool in the chat's Hermes) -------------------------------------

    async def serve(self) -> None:
        self.socket_path.unlink(missing_ok=True)
        self._server = await asyncio.start_unix_server(self._handle, path=str(self.socket_path))
        os.chmod(self.socket_path, 0o600)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), 10)
            req = json.loads(line or b"{}")
            if not secrets.compare_digest(str(req.get("token") or ""), self.token):
                out = {"ok": False, "error": "not authorized"}
            else:
                out = {"ok": True, "result": await self.dispatch(str(req.get("action") or ""), req.get("args") or {},
                                                                 confirmed=True)}
        except EngineeringError as exc:
            out = {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            log.exception("engineering request failed")
            out = {"ok": False, "error": f"{type(exc).__name__}: {' '.join(str(exc).split())[:200]}"}
        writer.write((json.dumps(out, default=str) + "\n").encode())
        try:
            await writer.drain()
        finally:
            writer.close()

    async def dispatch(self, action: str, a: dict, confirmed: bool = False):
        run_id = str(a.get("run_id") or "")
        if action == "start":
            return await self.start(str(a.get("request") or ""), str(a.get("project") or ""), str(a.get("path") or ""),
                                    str(a.get("mode") or ""), bool(a.get("auto_run")))
        if action == "status":
            return self.status(run_id)
        if action == "list":
            return self.list_runs()
        if action in ("plan", "report"):
            return self.report(run_id) if action == "report" else {k: v for k, v in self.status(run_id).items()
                                                                     if k in ("run", "status", "plan", "risks")}
        if action == "approve":
            return await self.approve(run_id)
        if action == "cancel":
            return await self.cancel(run_id)
        if action == "apply":
            return await self.apply(run_id, confirmed=confirmed)
        if action == "push":
            return await self.push(run_id, str(a.get("remote") or "origin"), confirmed=confirmed)
        if action == "pull_request":
            return await self.pull_request(run_id, bool(a.get("draft", True)), confirmed=confirmed)
        raise EngineeringError(f"unknown action {action!r}")

    async def close(self) -> None:
        for job in self._jobs.values():
            if not job.done():
                job.cancel()
        if self._server is not None:
            self._server.close()
        self.socket_path.unlink(missing_ok=True)
        await self.agents.close()
