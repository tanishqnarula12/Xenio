"""XENIO's engineering orchestrator (V6): from a request to a verified, reviewed change, with evidence.

    plan_project(request, repository)  inspect, isolate, plan (one task for small changes, a team for big ones)
    create_tasks(plan)                 validated task graph (roles, dependencies, file scopes)
    execute_plan(run)                  run the graph: Hermes sessions per task, XENIO records/checks/merges each result
    review_results(run)                tests (XENIO runs them), deterministic security scan, security + code review
    generate_report(run)               what changed, what was verified, what failed, what still needs the user

A task's own "done" never makes the run successful: the outcome comes from evidence XENIO collected itself (scope
checks on the real diff, tests run by XENIO against the baseline, review findings). Nothing is pushed, merged into
the user's branch, applied to the user's files or deployed by a run; those are separate steps the user confirms.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app.engineering import prompts, security, verify
from app.engineering import repo as R
from app.engineering.agents import AgentSessions, ProviderUnavailable, SessionRegistry
from app.engineering.graph import PlanError, Task, TaskGraph, in_scope
from app.engineering.roles import Role, RoleError, RoleRegistry
from app.hermes.client import HermesError, HermesTimeout
from app.logging_setup import event
from app.safety.policy import redact_secrets

log = logging.getLogger("xenio.engineering")

MODES = ("change", "plan", "explain", "review", "test")
RUN_STATUSES = ("planning", "planned", "running", "verifying", "reviewing", "done", "failed", "cancelled")
_TEST_FILE = re.compile(r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]+\.py$|_test\.(py|go)$|\.(test|spec)\.[jt]sx?$")
_ASSERT = re.compile(r"\b(assert|expect\(|assertEqual|assertTrue|should\.|t\.Error|require\.)")
_SKIP = re.compile(r"(@pytest\.mark\.skip|pytest\.skip\(|unittest\.skip|\bx(it|describe)\(|\.skip\(|t\.Skip\()")
_SMALL = re.compile(r"\b(typo|rename|one[- ]line|small|trivial|readme|comment|docstring|version bump|bump the version|"
                    r"spelling|single file|in (?:the )?file)\b", re.I)


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def detect_mode(request: str) -> str:
    """Fallback when the caller did not say what kind of work this is."""
    t = request.lower()
    if re.search(r"\b(run|execute)\b.*\btests?\b|\btest suite\b", t) and not re.search(r"\b(write|add|implement)\b", t):
        return "test"
    if re.search(r"\b(explain|architecture|how does|what does|walk me through|overview of|understand)\b", t) and \
            not re.search(r"\b(add|implement|fix|build|change|create|write)\b", t):
        return "explain"
    if re.search(r"\b(review|audit)\b", t) and not re.search(r"\b(implement|add|build|fix)\b", t):
        return "review"
    if re.search(r"\bplan\b", t) and not re.search(r"\b(and (then )?(implement|build|do) it)\b", t):
        return "plan"
    return "change"


@dataclass
class Run:
    id: str
    request: str
    source: str
    mode: str = "change"
    status: str = "planning"
    created: str = field(default_factory=_now)
    updated: str = field(default_factory=_now)
    project: str = ""
    repo_state: dict = field(default_factory=dict)
    workspace: dict = field(default_factory=dict)
    plan_summary: str = ""
    risks: list[str] = field(default_factory=list)
    tasks: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    baseline_tests: list[dict] = field(default_factory=list)
    final_tests: list[dict] = field(default_factory=list)
    scope_violations: list[str] = field(default_factory=list)
    test_tampering: list[str] = field(default_factory=list)
    security_findings: list[dict] = field(default_factory=list)
    security_notes: list[str] = field(default_factory=list)
    reviews: dict = field(default_factory=dict)
    answer: str = ""  # explain mode
    outcome: str = ""
    verified: bool = False
    diff_stat: str = ""
    approvals_needed: list[str] = field(default_factory=list)
    report_path: str = ""
    cancel_requested: bool = False

    def add_event(self, kind: str, text: str) -> dict:
        entry = {"time": _now(), "kind": kind, "text": text}
        self.events.append(entry)
        self.updated = entry["time"]
        return entry

    def to_dict(self) -> dict:
        return asdict(self)


class EngineeringOrchestrator:
    def __init__(self, agents: AgentSessions | None, roles: RoleRegistry, runs_root: Path, registry: SessionRegistry,
                 notify: Callable[[str, str], None] | None = None, max_parallel: int = 3, test_timeout: float = 600,
                 sandbox: bool = True):
        self.agents = agents
        self.roles = roles
        self.runs_root = runs_root
        self.registry = registry
        self.notify = notify or (lambda kind, text: None)
        self.max_parallel = max(1, min(max_parallel, 4))  # Hermes runs at most 4 agent turns per process
        self.test_timeout = test_timeout
        self.sandbox = sandbox
        self.graphs: dict[str, TaskGraph] = {}
        self.workspaces: dict[str, R.Workspace] = {}
        self.live: dict[str, dict[str, str]] = {}  # run id -> {task id: session id} while agents work

    # ---- bookkeeping ------------------------------------------------------------------------------------------

    def _milestone(self, run: Run, kind: str, text: str, speak: bool = True) -> None:
        run.add_event(kind, text)
        event(log, "ENGINEERING", run=run.id, kind=kind, text=text[:200])
        if speak:
            self.notify(kind, text)
        self.save(run)

    def save(self, run: Run) -> None:
        if run.id in self.graphs:
            run.tasks = self.graphs[run.id].to_list()
        if run.id in self.workspaces:
            run.workspace = self.workspaces[run.id].to_dict()
        folder = self.runs_root / run.id
        folder.mkdir(parents=True, exist_ok=True)
        R.save_json(folder / "run.json", run.to_dict())

    def _context(self, run: Run, cwd: Path) -> str:
        tests = [" ".join(c.argv[1:]) if c.name == "pytest" else " ".join(c.argv) for c in verify.detect(cwd, Path(run.source))]
        return prompts.repo_context(run.repo_state, tests, prompts.tree(cwd))

    # ---- 1. planning ------------------------------------------------------------------------------------------

    async def plan_project(self, request: str, repository: str | Path, mode: str = "", project: str = "") -> Run:
        """Inspect the repository, isolate the work, and plan it (no change to the user's files)."""
        run = Run(f"{dt.datetime.now():%m%d-%H%M}-{secrets.token_hex(2)}", request.strip(), str(Path(repository).expanduser()),
                  mode or detect_mode(request), project=project)
        if run.mode not in MODES:
            raise PlanError(f"unknown mode {run.mode!r}")
        state = await asyncio.to_thread(R.inspect, run.source)
        run.repo_state, run.source = state.to_dict(), state.root
        ws = await asyncio.to_thread(R.Workspace.create, state, run.id, self.runs_root)
        self.workspaces[run.id] = ws
        self._milestone(run, "inspected", "Repository: " + state.summary() + ". Your files stay untouched; the work "
                        f"happens in {ws.run_dir}.", speak=False)
        if run.mode in ("explain", "review", "test"):
            run.status = "planned"
            self.save(run)
            return run
        if run.mode == "change" and self._is_small(request):
            graph = self.create_tasks({"summary": "A small change: one engineer, then XENIO verifies it.", "tasks": [
                {"id": "change", "title": request[:80], "role": self._guess_role(request), "description": request,
                 "acceptance": ["the request is fulfilled", "existing tests still pass"]}]})
            run.plan_summary = "Small change: one engineer, verified by XENIO (no team needed)."
        else:
            plan = await self._ask_planner(run, ws)
            graph = self.create_tasks(plan)
            run.plan_summary, run.risks = str(plan.get("summary") or ""), [str(r) for r in plan.get("risks") or []][:10]
        self.graphs[run.id] = graph
        run.status = "planned"
        names = ", ".join(f"{t.title} ({t.role})" for t in graph.tasks.values())
        self._milestone(run, "planned", f"Plan ready: {len(graph.tasks)} task(s): {names}.")
        return run

    @staticmethod
    def _is_small(request: str) -> bool:
        return len(request) < 160 and bool(_SMALL.search(request) or re.search(r"\b[\w./-]+\.\w{1,5}\b", request))

    @staticmethod
    def _guess_role(request: str) -> str:
        t = request.lower()
        if re.search(r"\b(write|add) (unit )?tests?\b|\btest coverage\b", t):
            return "tester"
        if re.search(r"\b(readme|docs?|documentation|changelog)\b", t) and not re.search(r"\.(py|js|ts|go|rs)\b", t):
            return "docs"
        if re.search(r"\b(ui|css|style|component|page|button|layout|frontend|html|jsx|tsx|react|vue)\b", t):
            return "frontend"
        if re.search(r"\b(migration|schema|database|sql|index|table|query)\b", t):
            return "database"
        return "backend"

    async def _ask_planner(self, run: Run, ws: R.Workspace) -> dict:
        role = self.roles.get("planner")
        cwd = Path(ws.integration_dir)
        prompt = prompts.planner_prompt(run.request, self._context(run, cwd), self.roles.available())
        text = await self._agent_turn(run, Task("plan", "Plan the work", "planner"), role, cwd, prompt)
        plan = prompts.extract_json(text)
        if plan is None or not isinstance(plan.get("tasks"), list):
            text = await self._agent_turn(run, Task("plan", "Plan the work", "planner"), role, cwd,
                                          "Your answer did not end with the plan JSON. Reply with ONLY the JSON:\n"
                                          + prompts.PLAN_SCHEMA)
            plan = prompts.extract_json(text)
        if plan is None or not isinstance(plan.get("tasks"), list):
            raise PlanError("the planner did not produce a usable plan")
        return plan

    def create_tasks(self, plan: dict) -> TaskGraph:
        """A validated task graph from a plan (raises PlanError with every problem found)."""
        graph = TaskGraph.from_plan(list(plan.get("tasks") or []))
        problems = graph.validate(self.roles)
        writers = [t for t in graph.tasks.values() if t.role in self.roles.roles and self.roles.roles[t.role].writes]
        if not writers and plan.get("tasks"):
            problems.append("the plan has no task that changes anything")
        if problems:
            raise PlanError("; ".join(problems))
        return graph

    # ---- 2. execution -----------------------------------------------------------------------------------------

    async def execute_plan(self, run: Run) -> Run:
        """Run the graph (or the read-only work of explain/review/test runs), then review and report."""
        ws = self.workspaces[run.id]
        try:
            if run.mode == "explain":
                await self._explain(run, ws)
            elif run.mode == "test":
                await self._test_only(run, ws)
            elif run.mode == "review":
                await self._review_only(run, ws)
            elif run.mode == "plan":
                run.status, run.outcome = "done", "Plan only: nothing was implemented (say 'go ahead' to run it)."
            else:
                run.status = "running"
                run.baseline_tests = [t.to_dict() for t in await self._run_tests(ws, Path(ws.integration_dir), [])]
                await self._run_graph(run, ws)
                if not run.cancel_requested:
                    await self.review_results(run)
        except Exception as exc:  # noqa: BLE001 - reported, never hidden
            if run.status not in ("done", "cancelled"):
                run.status = "failed"
                run.outcome = f"The run stopped: {_short(exc)}"
                self._milestone(run, "failed", run.outcome)
        finally:
            self.registry.clear_run(run.id)
        if run.cancel_requested and run.status != "done":
            run.status = "cancelled"
        if run.mode != "plan" or not run.outcome:
            run.outcome = run.outcome or self._decide(run)
        self.generate_report(run)
        self._milestone(run, run.status, run.outcome)
        return run

    async def _run_graph(self, run: Run, ws: R.Workspace) -> None:
        graph = self.graphs[run.id]
        running: dict[asyncio.Task, Task] = {}

        def writes(task: Task) -> bool:
            return self.roles.roles[task.role].writes

        while not run.cancel_requested:
            for task in graph.startable(self.max_parallel, writes):
                task.status = "running"
                running[asyncio.ensure_future(self._run_task(run, ws, task))] = task
            if not running:
                break
            done, _pending = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
            for fut in done:
                task = running.pop(fut)
                if fut.exception() is not None and task.status == "running":
                    task.status, task.failure = "failed", _short(fut.exception())
            self.save(run)
        if run.cancel_requested:
            for fut in running:
                fut.cancel()
            await asyncio.gather(*running, return_exceptions=True)
        graph.refresh()
        for task in graph.tasks.values():
            if task.status == "blocked" and not any(e.get("task") == task.id for e in run.events):
                run.add_event("blocked", f"{task.title}: not started, {task.failure}")

    async def _run_task(self, run: Run, ws: R.Workspace, task: Task) -> None:
        role = self.roles.get(task.role)
        started = time.monotonic()
        note = ""
        while task.attempts < role.max_attempts and not run.cancel_requested:
            task.attempts += 1
            cwd = await asyncio.to_thread(ws.task_worktree, task.id) if role.writes else Path(ws.integration_dir)
            start_ref = ws.head(ws.task_branches[task.id]) if role.writes else ""
            try:
                text = await self._agent_turn(run, task, role, cwd,
                                              prompts.task_prompt(task, role, run.request, self._context(run, cwd), str(cwd), note))
            except (ProviderUnavailable, HermesTimeout, HermesError) as exc:
                task.failure = f"attempt {task.attempts}: {_short(exc)}"
                note = f"(A previous attempt ended with: {_short(exc)}. Continue from the files as they are now.)"
                if isinstance(exc, ProviderUnavailable):
                    await asyncio.sleep(5)
                continue
            claim = prompts.extract_json(text) or {}
            task.result.update(summary=str(claim.get("summary") or _first_line(text))[:600],
                               agent_status=str(claim.get("status") or "unknown"),
                               needs=[str(n) for n in claim.get("needs") or []][:10],
                               agent_tests=[str(t) for t in claim.get("tests_run") or []][:10],
                               seconds=round(time.monotonic() - started, 1))
            if run.cancel_requested:
                break
            if role.writes:
                ok = await self._record_writer(run, ws, task, start_ref)
            else:
                ok = claim.get("status") != "failed"
                task.failure = "" if ok else str(claim.get("summary") or "the agent reported a failure")
            if ok:
                task.status, task.failure = "succeeded", ""
                self._milestone(run, "task_done", f"{role.title} finished: {task.title}.")
                return
            if task.result.get("agent_status") == "blocked" or task.failure.startswith(
                    ("changed files outside", "merge conflict", "tests were deleted")):
                break  # a protocol violation or a real conflict: retrying the same agent will not help
            note = f"(Your previous attempt was not accepted: {task.failure}. Fix that.)"
        task.status = "cancelled" if run.cancel_requested else "failed"
        if task.status == "failed":
            self._milestone(run, "task_failed", f"{role.title} could not finish {task.title}: {task.failure}")

    async def _agent_turn(self, run: Run, task: Task, role: Role, cwd: Path, prompt: str) -> str:
        if self.agents is None:
            raise HermesError("no Hermes connection for the engineering team")
        session = await self.agents.open(cwd, role.model, role.provider)
        self.registry.add(session, {"run": run.id, "task": task.id, "role": role.name, "tools": sorted(role.tools),
                                    "terminal": role.terminal_mode, "root": str(cwd),
                                    "allowed_paths": list(task.allowed_paths) if role.writes else [],
                                    "docs_only": role.writes_docs_only})
        self.live.setdefault(run.id, {})[task.id] = session
        try:
            turn = await self.agents.prompt(session, prompt)
        finally:
            self.registry.remove(session)
            self.live.get(run.id, {}).pop(task.id, None)
        usage = dict(turn.usage or {})
        task.result.setdefault("turns", []).append({"session": session, "model": turn.model or "Hermes main model",
                                                     "seconds": turn.seconds, "stop": turn.stop_reason, "usage": usage or None,
                                                     "cost": "unknown" if "cost" not in usage else usage["cost"],
                                                     "tools": len(turn.tools),
                                                     "failed_tools": [t.title[:80] for t in turn.tools if t.status == "failed"][:5]})
        if turn.cancelled:
            raise HermesError("cancelled")
        return turn.text

    async def _record_writer(self, run: Run, ws: R.Workspace, task: Task, start_ref: str) -> bool:
        """XENIO's own checks on a writing task: commit, scope, test tampering, focused tests, merge."""
        commit = await asyncio.to_thread(ws.commit_task, task.id, f"{task.id}: {task.title}"[:72])
        branch = ws.task_branches[task.id]
        changed = await asyncio.to_thread(ws.changed_files, branch, start_ref)
        task.result["changed_files"] = [f"{s} {p}" for s, p in changed]
        if not commit:
            if task.result.get("agent_status") == "done" and "no change" in task.result.get("summary", "").lower():
                return True
            task.failure = "no file was changed"
            return False
        outside = [p for _s, p in changed if not in_scope(p, task.allowed_paths)]
        if outside:  # the guard should have stopped this: never merge it
            task.failure = f"changed files outside its scope: {', '.join(outside[:5])}"
            run.scope_violations += [f"{task.id}: {p}" for p in outside]
            return False
        diff = await asyncio.to_thread(ws.diff, branch, start_ref)
        tampering = find_test_tampering(diff, changed)
        if tampering:
            run.test_tampering += [f"{task.id}: {t}" for t in tampering]
            task.result["test_tampering"] = tampering
            if task.role == "tester":
                task.failure = "tests were deleted, skipped or weakened: " + "; ".join(tampering[:3])
                return False
        focused = verify.focused(verify.detect(Path(ws.task_dirs[task.id]), Path(run.source)),
                                 [p for _s, p in changed], Path(ws.task_dirs[task.id]))
        if focused:
            results = await asyncio.to_thread(lambda: [verify.run(c, Path(ws.task_dirs[task.id]), self.test_timeout,
                                                                  self.sandbox, redact_secrets) for c in focused])
            task.result["focused_tests"] = [r.to_dict() for r in results]
        merged, conflicts = await asyncio.to_thread(ws.merge_task, task.id)
        if not merged:
            merged = await self._resolve_conflict(run, ws, task, conflicts)
            if not merged:
                task.failure = f"merge conflict in {', '.join(conflicts[:5])}"
                return False
        task.result["merged"] = True
        await asyncio.to_thread(ws.remove_task_worktree, task.id)
        return True

    async def _resolve_conflict(self, run: Run, ws: R.Workspace, task: Task, conflicts: list[str]) -> bool:
        """One controlled attempt: the same role re-applies the task's intent on top of the integrated work."""
        self._milestone(run, "conflict", f"{task.title} conflicts with work already integrated ({', '.join(conflicts[:3])}); "
                                         "re-applying it on top.", speak=False)
        original = await asyncio.to_thread(ws.diff, ws.task_branches[task.id])
        retry = Task(f"{task.id}-fix"[:40], f"Re-apply: {task.title}", task.role, task.description,
                     allowed_paths=task.allowed_paths, context=task.context, acceptance=task.acceptance)
        role = self.roles.get(task.role)
        cwd = await asyncio.to_thread(ws.task_worktree, retry.id)
        start = ws.head(ws.task_branches[retry.id])
        note = (f"(Your first version conflicted with changes integrated meanwhile in {', '.join(conflicts)}. Re-apply "
                f"the same intent on the current files. Your first version's diff:\n{original[:20000]})")
        try:
            await self._agent_turn(run, retry, role, cwd, prompts.task_prompt(retry, role, run.request,
                                                                               self._context(run, cwd), str(cwd), note))
        except HermesError:
            return False
        if not await asyncio.to_thread(ws.commit_task, retry.id, f"{task.id}: re-applied"):
            return False
        changed = await asyncio.to_thread(ws.changed_files, ws.task_branches[retry.id], start)
        if any(not in_scope(p, task.allowed_paths) for _s, p in changed):
            return False
        merged, _ = await asyncio.to_thread(ws.merge_task, retry.id)
        if merged:
            task.result["conflict_resolved"] = conflicts
        return merged

    # ---- 3. review -------------------------------------------------------------------------------------------

    async def review_results(self, run: Run) -> None:
        ws = self.workspaces[run.id]
        graph = self.graphs[run.id]
        integration = Path(ws.integration_dir)
        changed = await asyncio.to_thread(ws.changed_files, ws.integration_branch)
        run.diff_stat = await asyncio.to_thread(ws.diff_stat, ws.integration_branch)
        if not changed:
            return
        run.status = "verifying"
        self._milestone(run, "verifying", f"Verifying {len(changed)} changed file(s): running the tests.", speak=False)
        run.final_tests = [t.to_dict() for t in await self._run_tests(ws, integration, [])]
        tests_line = "; ".join(verify.TestRun(**t).line() for t in run.final_tests) or "no test command found"
        self._milestone(run, "tested", f"Tests: {tests_line}")
        diff = await asyncio.to_thread(ws.diff, ws.integration_branch)
        paths = [p for _s, p in changed]
        run.security_findings = [f.to_dict() for f in security.scan_diff(diff, paths)]
        extra, run.security_notes = await asyncio.to_thread(security.external_scanners, integration, paths)
        run.security_findings += [f.to_dict() for f in extra]
        run.status = "reviewing"
        acceptance = [a for t in graph.tasks.values() for a in t.acceptance]
        for name in ("security", "reviewer"):
            try:
                role = self.roles.get(name)
            except RoleError as exc:
                run.reviews[name] = {"skipped": str(exc)}
                continue
            if run.cancel_requested:
                break
            review = Task(f"{name}-review", f"{role.title} review", name)
            try:
                text = await self._agent_turn(run, review, role, integration,
                                              prompts.review_prompt(role, run.request, self._context(run, integration),
                                                                    diff[:60000], acceptance, str(integration)))
                found = prompts.extract_json(text) or {"summary": _first_line(text), "findings": []}
                found["findings"] = self._checked_findings(found.get("findings"), integration)
                run.reviews[name] = found
            except HermesError as exc:
                run.reviews[name] = {"error": _short(exc)}
        self.save(run)

    @staticmethod
    def _checked_findings(findings, root: Path) -> list[dict]:
        """Keep reviewer findings that point at something real (the file exists, the line is in it)."""
        out = []
        for f in findings if isinstance(findings, list) else []:
            if not isinstance(f, dict):
                continue
            path = root / str(f.get("file") or "")
            line = int(f.get("line") or 0) if str(f.get("line") or "0").isdigit() else 0
            exists = path.is_file() and path.resolve().is_relative_to(root.resolve())
            lines = path.read_text(errors="replace").count("\n") + 1 if exists else 0
            f["verified_location"] = bool(exists and 0 <= line <= lines)
            out.append({k: (redact_secrets(str(v))[:400] if isinstance(v, str) else v) for k, v in f.items()})
        return out

    async def _run_tests(self, ws: R.Workspace, cwd: Path, changed: list[str]) -> list[verify.TestRun]:
        commands = verify.detect(cwd, Path(ws.source))
        if not commands:
            return [verify.TestRun("(none)", "not_run", detail="no test command was found in the project")]
        return await asyncio.to_thread(lambda: [verify.run(c, cwd, self.test_timeout, self.sandbox, redact_secrets)
                                                for c in commands])

    # ---- read-only modes ----------------------------------------------------------------------------------------

    async def _explain(self, run: Run, ws: R.Workspace) -> None:
        run.status = "running"
        cwd = Path(ws.integration_dir)
        role = self.roles.get("planner")
        run.answer = await self._agent_turn(run, Task("explain", "Explain", "planner"), role, cwd,
                                            prompts.explain_prompt(run.request, self._context(run, cwd)))
        run.status, run.verified = "done", False
        run.outcome = "Answered from the repository (read-only; nothing was changed)."

    async def _test_only(self, run: Run, ws: R.Workspace) -> None:
        run.status = "verifying"
        run.final_tests = [t.to_dict() for t in await self._run_tests(ws, Path(ws.integration_dir), [])]
        failing = [t for t in run.final_tests if t["status"] in ("failed", "error", "blocked", "timeout")]
        if failing:
            role = self.roles.get("reviewer")
            output = "\n\n".join(f"$ {t['command']}\n{t['tail']}" for t in failing)
            try:
                run.answer = await self._agent_turn(run, Task("explain-tests", "Explain test failures", "reviewer"), role,
                                                    Path(ws.integration_dir),
                                                    prompts.explain_prompt(f"{run.request}\n\nTest output (from XENIO's "
                                                                           f"own run):\n{output[:12000]}",
                                                                           self._context(run, Path(ws.integration_dir))))
            except HermesError as exc:
                run.answer = f"(the explanation could not be produced: {_short(exc)})"
        run.status = "done"
        run.outcome = "Tests: " + ("; ".join(verify.TestRun(**t).line() for t in run.final_tests))

    async def _review_only(self, run: Run, ws: R.Workspace) -> None:
        """Review the user's own uncommitted changes (HEAD..snapshot) without changing anything."""
        state = run.repo_state
        if not state.get("is_git") or not state.get("has_commits") or ws.base == state.get("head"):
            run.status, run.outcome = "done", "There are no uncommitted changes to review."
            return
        diff = await asyncio.to_thread(ws.diff, ws.base, state["head"])
        changed = [p for _s, p in await asyncio.to_thread(ws.changed_files, ws.base, state["head"])]
        run.diff_stat = await asyncio.to_thread(ws.diff_stat, ws.base, state["head"])
        run.security_findings = [f.to_dict() for f in security.scan_diff(diff, changed)]
        run.status = "reviewing"
        for name in ("security", "reviewer"):
            if run.cancel_requested:
                break
            role = self.roles.get(name)
            try:
                text = await self._agent_turn(run, Task(f"{name}-review", f"{role.title} review", name), role,
                                              Path(ws.integration_dir),
                                              prompts.review_prompt(role, run.request, self._context(run, Path(ws.integration_dir)),
                                                                    diff[:60000], [], ws.integration_dir))
                found = prompts.extract_json(text) or {"summary": _first_line(text), "findings": []}
                found["findings"] = self._checked_findings(found.get("findings"), Path(ws.integration_dir))
                run.reviews[name] = found
            except HermesError as exc:
                run.reviews[name] = {"error": _short(exc)}
        run.status = "done"
        run.outcome = f"Reviewed your uncommitted changes ({len(changed)} file(s)); nothing was changed."

    # ---- 4. outcome and report -----------------------------------------------------------------------------------

    def _decide(self, run: Run) -> str:
        graph = self.graphs.get(run.id)
        if graph is None:
            return run.outcome or "Nothing ran."
        counts = graph.counts()
        failed = [t for t in graph.tasks.values() if t.status in ("failed", "blocked", "cancelled")]
        base = {t["command"]: t for t in run.baseline_tests}
        new_failures = [t for t in run.final_tests if t["status"] in ("failed", "error", "timeout")
                        and base.get(t["command"], {}).get("status") != t["status"]]
        old_failures = [t for t in run.final_tests if t["status"] == "failed" and base.get(t["command"], {}).get("status") == "failed"
                        and t.get("failed", 0) <= base[t["command"]].get("failed", 0)]
        passed = [t for t in run.final_tests if t["status"] == "passed"]
        high = [f for f in run.security_findings if f["severity"] == "high"]
        review_high = [f for r in run.reviews.values() if isinstance(r, dict) for f in r.get("findings") or []
                       if f.get("severity") == "high" and f.get("verified_location")]
        if run.cancel_requested:
            run.status = "cancelled"
            return f"Cancelled. Finished before the stop: {counts.get('succeeded', 0)} task(s)."
        if failed:
            run.status = "failed"
            return "Not completed: " + "; ".join(f"{t.title} {t.status} ({t.failure})" for t in failed[:3])
        if run.scope_violations:
            run.status = "failed"
            return "Not accepted: changes outside the planned files."
        run.status = "done"
        problems = []
        if new_failures:
            problems.append("tests fail that passed before: " + "; ".join(t["command"] for t in new_failures))
        if not passed and not old_failures:
            problems.append("no test suite verified the change (" + (", ".join(t["status"] for t in run.final_tests) or
                                                                     "none ran") + ")")
        if old_failures:
            problems.append("some tests were already failing before the change")
        if high or review_high:
            problems.append(f"{len(high) + len(review_high)} high-severity security/review finding(s)")
        if run.test_tampering:
            problems.append("tests were removed, skipped or weakened")
        run.verified = not problems
        return "Done and verified: tests pass, reviews found no high-severity issue." if run.verified else \
            "Done, but NOT verified: " + "; ".join(problems) + "."

    def generate_report(self, run: Run) -> str:
        """A Markdown report with the evidence (also saved next to the run)."""
        ws = self.workspaces.get(run.id)
        graph = self.graphs.get(run.id)
        lines = [f"# Engineering run {run.id}", "", f"**Request:** {run.request}", "",
                 f"**Outcome:** {run.outcome}", f"**Repository:** {run.source}"]
        if run.repo_state:
            st = R.RepoState(**{k: v for k, v in run.repo_state.items() if k in R.RepoState.__dataclass_fields__})
            lines.append(f"**Your working tree:** {st.summary()} - untouched by this run; your uncommitted work is "
                         "part of the base, never part of XENIO's changes.")
        if run.answer:
            lines += ["", "## Answer", "", run.answer]
        if run.plan_summary or graph is not None:
            lines += ["", "## Plan", "", run.plan_summary]
            for risk in run.risks:
                lines.append(f"- risk: {risk}")
        if graph is not None:
            lines += ["", "## Tasks", "", "| task | role | status | changed | time | model |", "|---|---|---|---|---|---|"]
            for t in graph.tasks.values():
                turns = t.result.get("turns") or []
                model = ", ".join(sorted({tr["model"] for tr in turns})) or "-"
                secs = sum(tr.get("seconds", 0) for tr in turns)
                lines.append(f"| {t.title} | {t.role} | {t.status}{' - ' + t.failure if t.failure else ''} | "
                             f"{len(t.result.get('changed_files') or [])} file(s) | {secs:.0f} s | {model} |")
        if run.diff_stat:
            lines += ["", "## What changed (XENIO's changes only)", "", "```", run.diff_stat, "```"]
        if run.baseline_tests or run.final_tests:
            lines += ["", "## Tests (run by XENIO)", ""]
            for label, items in (("before", run.baseline_tests), ("after", run.final_tests)):
                for t in items:
                    lines.append(f"- {label}: {verify.TestRun(**t).line()}")
        if run.security_findings or run.security_notes:
            lines += ["", "## Security checks (deterministic)", "", security.summary(
                [security.Finding(**f) for f in run.security_findings])]
            lines += [f"- {security.Finding(**f).text()}" for f in run.security_findings[:30]]
            lines += [f"- note: {n}" for n in run.security_notes]
        for name, review in run.reviews.items():
            lines += ["", f"## {name.title()} review (a model's opinion; findings with checked file/line are marked)", ""]
            if review.get("skipped") or review.get("error"):
                lines.append(f"- not done: {review.get('skipped') or review.get('error')}")
                continue
            lines.append(str(review.get("summary") or ""))
            for f in review.get("findings") or []:
                mark = "" if f.get("verified_location") else " (location not found in the code)"
                lines.append(f"- [{f.get('severity')}] {f.get('file')}:{f.get('line')} {f.get('issue')}{mark}")
        if run.scope_violations or run.test_tampering:
            lines += ["", "## Problems XENIO caught", ""] + [f"- out of scope: {v}" for v in run.scope_violations] + \
                [f"- tests: {v}" for v in run.test_tampering]
        if ws is not None and graph is not None:
            run.approvals_needed = [f"apply the changes to your working tree ({run.source})",
                                    f"push the branch {ws.integration_branch}" if not ws.copied else "",
                                    "create a pull request" if not ws.copied else ""]
            run.approvals_needed = [a for a in run.approvals_needed if a]
            lines += ["", "## Where the result is", "",
                      f"- branch `{ws.integration_branch}` in {ws.repo}" + (" (a private copy)" if ws.copied else ""),
                      f"- a worktree you can open: {ws.integration_dir}",
                      f"- the patch: {Path(ws.run_dir) / 'changes.patch'}", "",
                      "Nothing was pushed, merged into your branch, applied to your files or deployed. Each of these "
                      "needs your confirmation: " + "; ".join(run.approvals_needed) + "."]
            try:
                (Path(ws.run_dir) / "changes.patch").write_text(ws.patch(), encoding="utf-8")
            except (OSError, R.RepoError):
                pass
        turns = [tr for t in (graph.tasks.values() if graph else []) for tr in t.result.get("turns") or []]
        if turns:
            tokens = sum((tr.get("usage") or {}).get("totalTokens", 0) or 0 for tr in turns)
            lines += ["", "## Cost", "", f"{len(turns)} agent turn(s), {tokens or 'unknown'} tokens reported by Hermes; "
                      "money cost: unknown (free models were used unless a role was pinned to another model)."]
        text = "\n".join(lines) + "\n"
        path = self.runs_root / run.id / "report.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        run.report_path = str(path)
        self.save(run)
        return text

    # ---- control ----------------------------------------------------------------------------------------------

    async def cancel(self, run: Run) -> dict:
        """Stop scheduling, ask Hermes to stop the running sessions, and say exactly what is still running."""
        run.cancel_requested = True
        graph = self.graphs.get(run.id)
        live = dict(self.live.get(run.id, {}))
        sent = {tid: await self.agents.cancel(sid) for tid, sid in live.items()} if self.agents else {}
        if graph is not None:
            graph.cancel_open("cancelled by the user")
        for _ in range(40):  # up to ~10 s for Hermes to stop the turns
            if not self.live.get(run.id):
                break
            await asyncio.sleep(0.25)
        still = sorted(self.live.get(run.id, {}))
        self._milestone(run, "cancelling", "Cancel requested: no new tasks start." + (
            f" Still stopping: {', '.join(still)}." if still else " No agent is running any more."))
        return {"stop_requested": sorted(t for t, ok in sent.items() if ok), "still_running": still}


def find_test_tampering(diff: str, changed: list[tuple[str, str]]) -> list[str]:
    """Deleted test files, removed assertions, newly skipped tests in a diff."""
    found = [f"deleted test file {p}" for s, p in changed if s == "D" and _TEST_FILE.search(p)]
    current, removed, added = "", {}, {}
    for line in diff.splitlines():
        if line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else line[4:]
        elif _TEST_FILE.search(current or ""):
            if line.startswith("-") and not line.startswith("---") and _ASSERT.search(line):
                removed[current] = removed.get(current, 0) + 1
            elif line.startswith("+") and not line.startswith("+++"):
                if _ASSERT.search(line):
                    added[current] = added.get(current, 0) + 1
                if _SKIP.search(line):
                    found.append(f"a test was marked skipped in {current}")
    for path, n in removed.items():
        if n > added.get(path, 0):
            found.append(f"{n - added.get(path, 0)} assertion(s) removed from {path}")
    return found


def _short(exc: BaseException) -> str:
    return " ".join(str(exc).split())[:240] or type(exc).__name__


def _first_line(text: str) -> str:
    return next((line.strip() for line in (text or "").splitlines() if line.strip()), "")[:300]
