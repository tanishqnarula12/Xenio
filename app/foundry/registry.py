"""The Agent Registry: who is in the organization. An agent's identity (what it knows and does) is separate from the model that happens to run it, so it can move between providers.

Before an agent ever runs it is compiled to an enforced ``Role`` from ``app/engineering/roles.py``, so everything that guards the engineering team guards it too: the capability table decides its tools,
``FORBIDDEN_TOOLS`` always comes off, and xenio-guard checks every tool call. What an agent may do can only be changed by you: anything else asking is refused here, on the server side.
"""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import asdict, dataclass, field, replace

from app.engineering.roles import CAPABILITIES, DEFAULT_ROLES, Role
from app.foundry.store import FoundryStore, iso
from app.safety.policy import contains_secret

_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_WORD = re.compile(r"[a-z0-9+#]+")
_STOP = frozenset("a an and the of for to in on with engineer engineering specialist expert agent developer dev reviewer".split())
KINDS = ("permanent", "temporary")
STATES = ("enabled", "disabled", "archived")
MEMORY_SCOPES = ("private", "project", "team", "global")
PERMISSION_FIELDS = frozenset({"capabilities", "skills", "approvals", "memory_scope", "max_runtime_seconds", "max_tool_calls", "max_tokens"})
ACTORS_WHO_MAY_CHANGE_PERMISSIONS = frozenset({"user", "system"})
PROMOTE_AFTER_SUCCESSES = 1
MAX_INSTRUCTIONS = 6000


class RegistryError(Exception):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


@dataclass
class Agent:
    id: str
    name: str
    role: str
    description: str = ""
    instructions: str = ""
    responsibilities: list[str] = field(default_factory=list)
    expertise: list[str] = field(default_factory=list)
    provider: str = ""  # "" = the default for what it does; never a key
    model: str = ""
    capabilities: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    memory_scope: str = "team"
    approvals: list[str] = field(default_factory=list)  # kinds of step that always ask you, on top of what policy asks
    max_runtime_seconds: int = 1800
    max_tool_calls: int = 400
    max_tokens: int = 600000
    output_format: str = ""
    version: int = 1
    kind: str = "temporary"
    state: str = "enabled"
    source: str = "factory"  # builtin | factory | user
    workflow_id: str = ""
    created: str = ""
    updated: str = ""

    @property
    def permissions(self) -> dict:
        """In words, what this agent may do: compiled from its capabilities, never stored separately."""
        role = self.to_role()
        return {"tools": sorted(role.tools), "terminal": role.terminal_mode, "writes": role.writes, "writes_code": role.writes_code}

    def to_role(self) -> Role:
        words = "; ".join(self.responsibilities) or self.description or self.role
        return Role(self.id, self.name, tuple(self.capabilities), words[:1500], self.instructions[:MAX_INSTRUCTIONS], self.state == "enabled", self.model, self.provider)

    def public(self) -> dict:
        return {**asdict(self), "permissions": self.permissions}


def tokens(*values) -> set[str]:
    found: set[str] = set()
    for value in values:
        for text in value if isinstance(value, (list, tuple, set)) else [value]:
            found |= {w for w in _WORD.findall(str(text or "").lower()) if w not in _STOP}
    return found


def similarity(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


class AgentRegistry:
    def __init__(self, store: FoundryStore, seed: dict[str, Role] | None = None):
        self.store = store
        self.seed_builtin(seed or DEFAULT_ROLES)

    # ---- reading ----------------------------------------------------------------------------------------------------

    @staticmethod
    def _from_row(row: dict) -> Agent:
        data = json.loads(row["body"])
        known = {k: v for k, v in data.items() if k in Agent.__dataclass_fields__}
        return Agent(**{**known, "kind": row["kind"], "state": row["state"], "source": row["source"], "workflow_id": row["workflow_id"], "version": row["version"], "created": iso(row["created"]), "updated": iso(row["updated"])})

    def get(self, agent_id: str) -> Agent | None:
        row = self.store.one("SELECT * FROM agents WHERE id = ?", (agent_id,))
        return self._from_row(row) if row else None

    def require(self, agent_id: str) -> Agent:
        found = self.get(agent_id)
        if found is None:
            raise RegistryError("there is no such agent", 404)
        return found

    def list(self, state: str = "", kind: str = "", include_archived: bool = False) -> list[Agent]:
        rows = self.store.query("SELECT * FROM agents ORDER BY (source = 'builtin') DESC, updated DESC")
        agents = [self._from_row(r) for r in rows]
        return [a for a in agents if (not state or a.state == state) and (not kind or a.kind == kind) and (include_archived or state == "archived" or a.state != "archived")]

    def search(self, text: str = "", expertise: str = "", model: str = "", skill: str = "", include_archived: bool = False) -> list[tuple[float, Agent]]:
        """Best first. Scored on what the agent knows and does, not on its name alone."""
        wanted = tokens(text, expertise)
        out = []
        for agent in self.list(include_archived=include_archived):
            if model and agent.model != model:
                continue
            if skill and skill not in agent.skills:
                continue
            score = self.fit(agent, wanted) if wanted else 1.0
            if score > 0:
                out.append((score, agent))
        return sorted(out, key=lambda hit: -hit[0])

    @staticmethod
    def fit(agent: Agent, wanted: set[str]) -> float:
        """How much of what is wanted this agent covers. Its stated expertise counts most."""
        strong = tokens(agent.expertise, agent.name)
        weak = tokens(agent.description, agent.responsibilities, agent.role)
        if not wanted:
            return 0.0
        return round((len(wanted & strong) + 0.5 * len((wanted & weak) - strong)) / len(wanted), 3)

    def find_similar(self, name: str, expertise: list[str], threshold: float = 0.6) -> Agent | None:
        """Is this agent already here? New specialists are not created for what exists."""
        wanted = tokens(name, expertise)
        best, best_score = None, 0.0
        for agent in self.list(include_archived=True):
            score = similarity(wanted, tokens(agent.name, agent.expertise))
            if score > best_score:
                best, best_score = agent, score
        return best if best_score >= threshold else None

    # ---- changing ----------------------------------------------------------------------------------------------------

    @staticmethod
    def validate(agent: Agent) -> None:
        if not _ID.match(agent.id):
            raise RegistryError(f"{agent.id!r} is not a usable agent id")
        if not agent.name.strip() or not agent.role.strip():
            raise RegistryError("an agent needs a name and a role")
        for capability in agent.capabilities:
            if capability not in CAPABILITIES:
                raise RegistryError(f"{capability!r} is not a capability XENIO knows, so it cannot be enforced")
        if agent.memory_scope not in MEMORY_SCOPES:
            raise RegistryError(f"memory scope must be one of {', '.join(MEMORY_SCOPES)}")
        if agent.kind not in KINDS or agent.state not in STATES:
            raise RegistryError("unknown kind or state")
        if len(agent.instructions) > MAX_INSTRUCTIONS:
            raise RegistryError(f"instructions are limited to {MAX_INSTRUCTIONS} characters")
        for text in (agent.instructions, agent.description, agent.output_format, *agent.responsibilities):
            if contains_secret(str(text), strict=True):
                raise RegistryError("an agent definition must not hold a key, token or password")
        if not 30 <= agent.max_runtime_seconds <= 7200 or not 1 <= agent.max_tool_calls <= 2000 or not 1000 <= agent.max_tokens <= 2_000_000:
            raise RegistryError("budgets must be sensible: up to 2 hours, 2000 tool calls, 2 million tokens")
        agent.to_role()  # the capabilities must compile

    def _write(self, agent: Agent, why: str, actor: str, new: bool) -> Agent:
        self.validate(agent)
        now = self.store.clock()
        created = now if new else None
        body = json.dumps({k: v for k, v in asdict(agent).items() if k not in ("kind", "state", "source", "workflow_id", "version", "created", "updated")}, ensure_ascii=False)
        if new:
            self.store.execute("INSERT INTO agents (id, body, kind, state, source, workflow_id, version, created, updated) VALUES (?,?,?,?,?,?,?,?,?)",
                               (agent.id, body, agent.kind, agent.state, agent.source, agent.workflow_id, agent.version, created, now))
        else:
            self.store.execute("UPDATE agents SET body = ?, kind = ?, state = ?, workflow_id = ?, version = ?, updated = ? WHERE id = ?", (body, agent.kind, agent.state, agent.workflow_id, agent.version, now, agent.id))
        self.store.execute("INSERT OR REPLACE INTO agent_versions (agent_id, version, body, why, actor, at) VALUES (?,?,?,?,?,?)", (agent.id, agent.version, body, why[:300], actor, now))
        return self.require(agent.id)

    def create(self, agent: Agent, actor: str = "factory", why: str = "created") -> Agent:
        if self.get(agent.id) is not None:
            raise RegistryError(f"there is already an agent called {agent.id}", 409)
        made = self._write(agent, why, actor, new=True)
        self.store.event("agent.created", agent.workflow_id, made.id, name=made.name, kind=made.kind, why=why, permissions=made.permissions, actor=actor)
        return made

    def update(self, agent_id: str, changes: dict, actor: str, why: str = "") -> Agent:
        """A new version. What an agent may do changes only on the user's word: no agent, and no team decision, can widen it."""
        current = self.require(agent_id)
        unknown = set(changes) - set(Agent.__dataclass_fields__) | ({"id", "version", "created", "updated", "source", "kind", "state", "workflow_id"} & set(changes))
        if unknown:
            raise RegistryError(f"these cannot be changed this way: {', '.join(sorted(unknown))}")
        touching = PERMISSION_FIELDS & set(changes)
        if touching and actor not in ACTORS_WHO_MAY_CHANGE_PERMISSIONS:
            self.store.event("agent.permission_refused", current.workflow_id, agent_id, fields=sorted(touching), actor=actor)
            raise RegistryError(f"only you can change what an agent may do ({', '.join(sorted(touching))})", 403)
        return self._write(replace(current, **changes, version=current.version + 1), why or "edited", actor, new=False)

    def _state(self, agent_id: str, state: str, why: str, actor: str = "user") -> Agent:
        current = self.require(agent_id)
        made = self._write(replace(current, state=state, version=current.version + 1), why, actor, new=False)
        self.store.event(f"agent.{state}", current.workflow_id, agent_id, why=why, actor=actor)
        return made

    def enable(self, agent_id: str, actor: str = "user") -> Agent:
        return self._state(agent_id, "enabled", "enabled", actor)

    def disable(self, agent_id: str, actor: str = "user") -> Agent:
        return self._state(agent_id, "disabled", "disabled", actor)

    def archive(self, agent_id: str, why: str = "archived", actor: str = "user") -> Agent:
        if self.require(agent_id).source == "builtin":
            raise RegistryError("a built-in agent can be switched off, not archived", 409)
        return self._state(agent_id, "archived", why, actor)

    def restore(self, agent_id: str, actor: str = "user") -> Agent:
        return self._state(agent_id, "enabled", "restored", actor)

    def delete(self, agent_id: str) -> bool:
        current = self.require(agent_id)
        if current.source == "builtin":
            raise RegistryError("a built-in agent can be switched off, not deleted", 409)
        self.store.execute("DELETE FROM agent_versions WHERE agent_id = ?", (agent_id,))
        self.store.execute("DELETE FROM agent_runs WHERE agent_id = ?", (agent_id,))
        removed = self.store.execute("DELETE FROM agents WHERE id = ?", (agent_id,))[1] > 0
        self.store.event("agent.deleted", current.workflow_id, agent_id)
        return removed

    def versions(self, agent_id: str) -> list[dict]:
        return [{"version": r["version"], "why": r["why"], "actor": r["actor"], "at": iso(r["at"])} for r in self.store.query("SELECT * FROM agent_versions WHERE agent_id = ? ORDER BY version DESC", (agent_id,))]

    def promote(self, agent_id: str, actor: str = "user", why: str = "promoted") -> Agent:
        current = self.require(agent_id)
        if current.kind == "permanent":
            return current
        made = self._write(replace(current, kind="permanent", state="enabled", workflow_id="", version=current.version + 1), why, actor, new=False)
        self.store.event("agent.promoted", current.workflow_id, agent_id, why=why, actor=actor)
        return made

    # ---- how it has gone ---------------------------------------------------------------------------------------------

    def record_run(self, agent_id: str, workflow_id: str, outcome: str, task_id: str = "", score: float | None = None, seconds: float = 0, calls: int = 0) -> None:
        self.store.execute("INSERT INTO agent_runs (agent_id, workflow_id, task_id, outcome, score, seconds, calls, at) VALUES (?,?,?,?,?,?,?,?)", (agent_id, workflow_id, task_id, outcome, score, seconds, calls, self.store.clock()))

    def performance(self, agent_id: str) -> dict:
        rows = self.store.query("SELECT * FROM agent_runs WHERE agent_id = ? ORDER BY id DESC", (agent_id,))
        scored = [r["score"] for r in rows if r["score"] is not None]
        return {"runs": len(rows), "succeeded": sum(r["outcome"] == "succeeded" for r in rows), "failed": sum(r["outcome"] == "failed" for r in rows),
                "workflows": len({r["workflow_id"] for r in rows}), "average_score": round(sum(scored) / len(scored), 2) if scored else None,
                "seconds": round(sum(r["seconds"] or 0 for r in rows), 1), "last": iso(rows[0]["at"]) if rows else ""}

    def evaluate(self, agent_id: str, workflow_id: str) -> Agent:
        """After a workflow. A specialist proven more than once stays; one that only failed is retired; the rest wait in the archive, where they can be found again."""
        agent = self.require(agent_id)
        if agent.source == "builtin" or agent.kind == "permanent":
            return agent
        done = self.performance(agent_id)
        if done["failed"] and not done["succeeded"]:
            return self.archive(agent_id, "only ever failed: retired", "system")
        return self.promote(agent_id, "system", f"stays in the organization: {done['succeeded']} task(s) done" if done["runs"] else "stays in the organization, ready for when it is needed")

    # ---- the agents XENIO already has ------------------------------------------------------------------------------------

    def seed_builtin(self, roles: dict[str, Role]) -> None:
        for name, role in roles.items():
            found = self.get(name)
            if found is None:  # no model from the older team's settings: choosing one per agent is the point here, and that file pinned everyone to one heavy route
                self.create(Agent(id=name, name=role.title, role=name, description=role.responsibilities, instructions=role.rules, responsibilities=[role.responsibilities], expertise=sorted(tokens(role.title, role.responsibilities))[:24],
                                  capabilities=list(role.capabilities), kind="permanent", source="builtin", memory_scope="project"), "system", "one of XENIO's engineering roles")
            elif found.source == "builtin" and found.version == 1 and (found.model or found.provider):  # seeded with that pin, and never chosen by you
                self._write(replace(found, model="", provider="", version=2), "released from the older team's single model", "system", new=False)


def new_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:28] or "agent"
    return f"{slug}-{secrets.token_hex(2)}"


class AgentMemory:
    """What agents remember, by scope. An agent reads its own private notes, its project's, its team's, and only what you approved for everyone. Nothing else, whatever it asks."""

    def __init__(self, store: FoundryStore):
        self.store = store

    def write(self, agent: Agent, scope: str, owner: str, text: str, approved: bool = False) -> int:
        if scope not in MEMORY_SCOPES:
            raise RegistryError("unknown memory scope")
        if scope == "private" and owner != agent.id:
            raise RegistryError("private memory is the agent's own", 403)
        if scope == "global" and not approved:
            raise RegistryError("only what you approve goes in everyone's memory", 403)
        if contains_secret(text, strict=True):
            raise RegistryError("memory must not hold a key, token or password")
        return self.store.execute("INSERT INTO memory (scope, owner, text, approved, at) VALUES (?,?,?,?,?)", (scope, owner, text[:4000], int(approved), self.store.clock()))[0]

    def read(self, agent: Agent, project: str = "", team: str = "", limit: int = 20) -> list[dict]:
        rows = self.store.query("SELECT * FROM memory WHERE (scope = 'private' AND owner = ?) OR (scope = 'project' AND owner = ? AND owner != '') OR (scope = 'team' AND owner = ? AND owner != '') OR (scope = 'global' AND approved = 1) ORDER BY id DESC LIMIT ?",
                                (agent.id, project, team, limit))
        return [{"scope": r["scope"], "owner": r["owner"], "text": r["text"], "at": iso(r["at"])} for r in rows]
