"""A workflow as a graph you can watch. Typed nodes, dependencies, branches. Saved on every change, so a restart picks up where it was.

There are no cycles: a graph that would contain one is refused. Repeating work is an explicit ``loop`` node with hard limits (see loop.py), never an edge pointing backwards.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from app.foundry.store import FoundryStore, iso, scrub

NODE_TYPES = ("goal", "planning", "agent", "task", "communication", "review", "testing", "decision", "approval", "loop", "completion")
STATUSES = ("pending", "running", "waiting", "done", "failed", "skipped", "blocked", "cancelled")
SETTLED = frozenset({"done", "failed", "skipped", "blocked", "cancelled"})
GOOD = frozenset({"done", "skipped"})
MAX_LOOP_ITERATIONS = 10


class GraphError(Exception):
    pass


@dataclass
class Node:
    id: str
    type: str
    title: str
    status: str = "pending"
    agent: str = ""
    deps: list[str] = field(default_factory=list)
    inputs: list[str] = field(default_factory=list)  # references: "decision:abc", "task:api"
    outputs: dict = field(default_factory=dict)
    when: dict = field(default_factory=dict)  # {"node": id, "output": key, "equals": value} or {"node": id, "status": "failed"}
    always: bool = False  # runs even when something before it failed: reports must still be written
    retries: int = 0
    max_retries: int = 0
    budget: dict = field(default_factory=dict)
    error: str = ""
    started: str = ""
    finished: str = ""
    priority: int = 0
    writes: bool = False  # changes something, so an interruption has to be checked rather than simply repeated
    during_pause: bool = False  # re-evaluating is what a pause is for
    detail: str = ""
    payload: dict = field(default_factory=dict)
    history: list[dict] = field(default_factory=list)

    def public(self) -> dict:
        return asdict(self)


class WorkflowGraph:
    def __init__(self, store: FoundryStore, workflow: str):
        self.store = store
        self.workflow = workflow
        self.nodes: dict[str, Node] = {}

    # ---- shape --------------------------------------------------------------------------------------------------------

    def add(self, node: Node) -> Node:
        if node.type not in NODE_TYPES:
            raise GraphError(f"{node.type!r} is not a kind of node")
        if node.id in self.nodes:
            raise GraphError(f"there is already a node {node.id}")
        unknown = [d for d in node.deps if d not in self.nodes]
        if unknown:
            raise GraphError(f"{node.id} depends on nodes that do not exist: {', '.join(unknown)}")
        if node.type == "loop" and not 1 <= int(node.budget.get("max_iterations") or 0) <= MAX_LOOP_ITERATIONS:
            raise GraphError(f"a loop must state a limit of 1 to {MAX_LOOP_ITERATIONS} iterations")
        self.nodes[node.id] = node
        self._save(node, "added")
        return node

    def link(self, before: str, after: str) -> None:
        """``after`` now waits for ``before``. Refused if that would make a cycle."""
        if before not in self.nodes or after not in self.nodes:
            raise GraphError("both nodes must exist")
        node = self.nodes[after]
        if before in node.deps:
            return
        node.deps.append(before)
        cycle = self._cycle()
        if cycle:
            node.deps.remove(before)
            raise GraphError("that would make a cycle: " + " -> ".join(cycle))
        self._save(node, f"now waits for {before}")

    def _cycle(self) -> list[str]:
        colour: dict[str, int] = {}
        path: list[str] = []

        def visit(name: str) -> list[str]:
            colour[name] = 1
            path.append(name)
            for dep in self.nodes[name].deps:
                if colour.get(dep) == 1:
                    return [*path[path.index(dep):], dep]
                if colour.get(dep) is None:
                    found = visit(dep)
                    if found:
                        return found
            path.pop()
            colour[name] = 2
            return []

        for name in self.nodes:
            if colour.get(name) is None:
                found = visit(name)
                if found:
                    return found
        return []

    # ---- what can run -------------------------------------------------------------------------------------------------

    def _holds(self, node: Node) -> bool:
        rule = node.when
        if not rule:
            return True
        other = self.nodes.get(str(rule.get("node") or ""))
        if other is None:
            return False
        if "status" in rule:
            return other.status == rule["status"]
        return other.outputs.get(str(rule.get("output") or "")) == rule.get("equals")

    def ready(self) -> list[Node]:
        """Nodes that can start now, most urgent first. Settles the ones that never will: a branch not taken, or work whose inputs failed."""
        changed = True
        while changed:
            changed = False
            for node in self.nodes.values():
                if node.status != "pending":
                    continue
                deps = [self.nodes[d] for d in node.deps]
                if any(d.status not in SETTLED for d in deps):
                    continue
                bad = [d for d in deps if d.status not in GOOD]
                if bad and not node.always:
                    self.set(node.id, "blocked", error=f"{bad[0].title} did not finish", note=f"blocked by {bad[0].id}")
                    changed = True
                elif not self._holds(node):
                    self.set(node.id, "skipped", note="its condition did not hold")
                    changed = True
        out = [n for n in self.nodes.values() if n.status == "pending" and all(self.nodes[d].status in SETTLED for d in n.deps)]
        return sorted(out, key=lambda n: -n.priority)

    def settled(self) -> bool:
        return all(n.status in SETTLED for n in self.nodes.values())

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for node in self.nodes.values():
            out[node.status] = out.get(node.status, 0) + 1
        return out

    # ---- changes ------------------------------------------------------------------------------------------------------

    def set(self, node_id: str, status: str, *, error: str | None = None, outputs: dict | None = None, note: str = "") -> Node:
        if status not in STATUSES:
            raise GraphError(f"{status!r} is not a status")
        node = self.nodes[node_id]
        now = iso(self.store.clock())
        node.status = status
        if status == "running" and not node.started:
            node.started = now
        if status in SETTLED:
            node.finished = now
        if error is not None:
            node.error = error[:600]
        if outputs:
            node.outputs.update(outputs)
        node.history.append({"status": status, "at": now, **({"note": note[:300]} if note else {})})
        self._save(node, note or status)
        return node

    def fail(self, node_id: str, error: str) -> Node:
        """Tries again while it may; then it has failed, and what depends on it is blocked."""
        node = self.nodes[node_id]
        if node.retries < node.max_retries:
            node.retries += 1
            return self.set(node_id, "pending", error=error, note=f"retry {node.retries} of {node.max_retries}")
        return self.set(node_id, "failed", error=error)

    def reopen(self, node_id: str, why: str) -> Node:
        """Do this one again: what it produced is out of date. The old result stays in its history."""
        node = self.nodes[node_id]
        if node.outputs:
            node.history.append({"status": "superseded", "at": iso(self.store.clock()), "note": why[:300], "outputs": node.outputs})
        node.outputs, node.error, node.started, node.finished = {}, "", "", ""
        return self.set(node_id, "pending", note=why)

    def cancel_open(self, why: str) -> list[str]:
        cancelled = []
        for node in self.nodes.values():
            if node.status not in SETTLED:
                self.set(node.id, "cancelled", error=why, note=why)
                cancelled.append(node.id)
        return cancelled

    def recover(self) -> dict:
        """After a restart. What finished stays finished and is not repeated. What was only reading can simply run again. What was changing something is flagged, to be checked before any retry."""
        repeat, check, kept = [], [], []
        for node in self.nodes.values():
            if node.status in SETTLED:
                kept.append(node.id)
            elif node.status == "running":
                if node.writes:
                    check.append(node.id)
                else:
                    repeat.append(node.id)
                self.set(node.id, "pending", note="interrupted by a restart")
        return {"kept": kept, "repeat": repeat, "check": check}

    # ---- memory -------------------------------------------------------------------------------------------------------

    def _save(self, node: Node, note: str) -> None:
        self.store.execute("INSERT INTO nodes (workflow_id, id, body, status, updated) VALUES (?,?,?,?,?) ON CONFLICT(workflow_id, id) DO UPDATE SET body = excluded.body, status = excluded.status, updated = excluded.updated",
                           (self.workflow, node.id, json.dumps(scrub(asdict(node)), ensure_ascii=False, default=str), node.status, self.store.clock()))
        self.store.event("node", self.workflow, node.agent, node.id, status=node.status, type=node.type, title=node.title, note=note, error=node.error)

    @classmethod
    def load(cls, store: FoundryStore, workflow: str) -> "WorkflowGraph":
        graph = cls(store, workflow)
        for row in store.query("SELECT body FROM nodes WHERE workflow_id = ? ORDER BY rowid", (workflow,)):
            data = json.loads(row["body"])
            graph.nodes[data["id"]] = Node(**{k: v for k, v in data.items() if k in Node.__dataclass_fields__})
        return graph

    def public(self) -> list[dict]:
        return [scrub(n.public()) for n in self.nodes.values()]
