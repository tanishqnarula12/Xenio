"""The task graph of an engineering run (V6): tasks, dependencies, file scopes, status.

Statuses: ``pending`` (waiting for dependencies) -> ``ready`` -> ``running`` -> ``succeeded`` | ``failed`` |
``cancelled``; ``blocked`` when a task it depends on did not succeed. A task's own success never marks the run as
successful: the orchestrator decides that from verified evidence (tests, scope checks, review).

Parallelism: read-only tasks may always run side by side. Two tasks that write run at the same time only when their
declared file scopes do not overlap; a writer without a declared scope owns the whole repository and runs alone.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

STATUSES = ("pending", "ready", "running", "blocked", "succeeded", "failed", "cancelled")
TERMINAL = {"succeeded", "failed", "cancelled", "blocked"}
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


class PlanError(Exception):
    pass


@dataclass
class Task:
    id: str
    title: str
    role: str
    description: str = ""
    depends_on: list[str] = field(default_factory=list)
    allowed_paths: list[str] = field(default_factory=list)  # repository-relative files/folders this task may change
    context: str = ""
    expected_outputs: list[str] = field(default_factory=list)
    acceptance: list[str] = field(default_factory=list)
    status: str = "pending"
    attempts: int = 0
    failure: str = ""
    result: dict = field(default_factory=dict)  # summary, changed_files, branch, session, model, usage, timings

    @classmethod
    def from_dict(cls, data: dict) -> "Task":
        known = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        for key in ("depends_on", "allowed_paths", "expected_outputs", "acceptance"):
            value = known.get(key) or []
            known[key] = [str(v) for v in (value if isinstance(value, list) else [value])]
        task = cls(**known)
        task.id = str(task.id).strip().lower()
        return task


def normalize_scope(path: str) -> str:
    """'./src/api/' -> 'src/api'; wildcards cut at the first one ('src/**/*.ts' -> 'src')."""
    text = str(path).replace("\\", "/").strip()
    text = re.split(r"[*?\[]", text, maxsplit=1)[0]
    text = re.sub(r"^(\./)+", "", text).strip("/")
    return "/".join(p for p in text.split("/") if p not in ("", "."))


def scopes_overlap(a: list[str], b: list[str]) -> bool:
    """True when two writers could touch the same file. An empty scope means the whole repository."""
    na, nb = [normalize_scope(p) for p in a], [normalize_scope(p) for p in b]
    if not na or not nb or "" in na or "" in nb:
        return True
    return any(x == y or x.startswith(y + "/") or y.startswith(x + "/") for x in na for y in nb)


def in_scope(path: str, scope: list[str]) -> bool:
    """Is a repository-relative path inside a task's scope? (empty scope = whole repository)"""
    p = normalize_scope(path)
    if ".." in p.split("/"):
        return False
    norms = [normalize_scope(s) for s in scope]
    return not norms or "" in norms or any(p == s or p.startswith(s + "/") for s in norms)


@dataclass
class TaskGraph:
    tasks: dict[str, Task] = field(default_factory=dict)

    @classmethod
    def from_plan(cls, items: list[dict]) -> "TaskGraph":
        if not isinstance(items, list):
            raise PlanError("the plan's tasks must be a list")
        graph = cls()
        for raw in items:
            if not isinstance(raw, dict):
                raise PlanError("every task must be an object")
            task = Task.from_dict(raw)
            if task.id in graph.tasks:
                raise PlanError(f"duplicate task id {task.id!r}")
            graph.tasks[task.id] = task
        return graph

    def validate(self, roles) -> list[str]:
        """Problems with this graph ([] = runnable). ``roles``: a RoleRegistry."""
        problems = []
        if not self.tasks:
            return ["the plan has no tasks"]
        for task in self.tasks.values():
            if not _ID.match(task.id):
                problems.append(f"task id {task.id!r} must be short lowercase letters, digits, - or _")
            if not task.title.strip():
                problems.append(f"task {task.id} has no title")
            try:
                roles.get(task.role)
            except Exception as exc:  # noqa: BLE001 - RoleError
                problems.append(f"task {task.id}: {exc}")
            for dep in task.depends_on:
                if dep == task.id:
                    problems.append(f"task {task.id} depends on itself")
                elif dep not in self.tasks:
                    problems.append(f"task {task.id} depends on unknown task {dep!r}")
            for path in task.allowed_paths:
                if ".." in normalize_scope(path).split("/") or str(path).startswith(("/", "~")) or ":" in str(path)[:3]:
                    problems.append(f"task {task.id}: allowed path {path!r} must stay inside the repository")
        cycle = self._cycle()
        if cycle:
            problems.append("the tasks depend on each other in a circle: " + " -> ".join(cycle))
        return problems

    def _cycle(self) -> list[str]:
        state: dict[str, int] = {}
        path: list[str] = []

        def visit(tid: str) -> list[str]:
            state[tid] = 1
            path.append(tid)
            for dep in self.tasks[tid].depends_on:
                if dep not in self.tasks:
                    continue
                if state.get(dep) == 1:
                    return path[path.index(dep):] + [dep]
                if state.get(dep) is None:
                    found = visit(dep)
                    if found:
                        return found
            state[tid] = 2
            path.pop()
            return []

        for tid in self.tasks:
            if state.get(tid) is None:
                found = visit(tid)
                if found:
                    return found
        return []

    def order(self) -> list[str]:
        """Task ids in dependency order (stable for independent tasks)."""
        done: list[str] = []
        remaining = list(self.tasks)
        while remaining:
            progressed = False
            for tid in list(remaining):
                if all(d in done for d in self.tasks[tid].depends_on):
                    done.append(tid)
                    remaining.remove(tid)
                    progressed = True
            if not progressed:
                raise PlanError("the tasks depend on each other in a circle")
        return done

    # ---- scheduling -----------------------------------------------------------------------------------------------

    def refresh(self) -> None:
        """Promote waiting tasks whose dependencies succeeded; block those whose dependencies did not."""
        changed = True
        while changed:
            changed = False
            for task in self.tasks.values():
                if task.status != "pending":
                    continue
                deps = [self.tasks[d] for d in task.depends_on]
                bad = [d for d in deps if d.status in ("failed", "cancelled", "blocked")]
                if bad:
                    task.status = "blocked"
                    task.failure = "depends on " + ", ".join(f"{d.id} ({d.status})" for d in bad)
                    changed = True
                elif all(d.status == "succeeded" for d in deps):
                    task.status = "ready"
                    changed = True

    def startable(self, max_parallel: int, writes) -> list[Task]:
        """Ready tasks that can start now. ``writes(task) -> bool`` says whether a task's role changes files."""
        self.refresh()
        running = [t for t in self.tasks.values() if t.status == "running"]
        free = max(0, max_parallel - len(running))
        chosen: list[Task] = []
        for tid in self.order():
            task = self.tasks[tid]
            if not free or task.status != "ready":
                continue
            busy = [t for t in running + chosen if writes(t)]
            if writes(task) and any(scopes_overlap(task.allowed_paths, t.allowed_paths) for t in busy):
                continue  # another writer owns (part of) these files right now
            chosen.append(task)
            free -= 1
        return chosen

    def cancel_open(self, reason: str) -> list[str]:
        """Cancel every task that has not finished (running tasks are the caller's to stop)."""
        cancelled = []
        for task in self.tasks.values():
            if task.status in ("pending", "ready", "running"):
                task.status, task.failure = "cancelled", reason
                cancelled.append(task.id)
        return cancelled

    @property
    def finished(self) -> bool:
        self.refresh()
        return all(t.status in TERMINAL for t in self.tasks.values())

    def counts(self) -> dict[str, int]:
        out = {s: 0 for s in STATUSES}
        for task in self.tasks.values():
            out[task.status] += 1
        return {k: v for k, v in out.items() if v}

    def to_list(self) -> list[dict]:
        return [asdict(self.tasks[tid]) for tid in self.tasks]
