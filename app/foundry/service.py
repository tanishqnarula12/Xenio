"""The Foundry as one service: the War Room and the chat both come through here. Anything that widens what an agent may do, or approves something, only happens when the call really comes from you: ``confirmed`` is set by XENIO's own authenticated UI, or by the guard after it asked you."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from app.foundry.decisions import DecisionError
from app.foundry.engine import ACTIVE, FoundryEngine, FoundryError, TeamTools
from app.foundry.messages import TEAM
from app.foundry.models import AcpThinker, ModelRouter
from app.foundry.registry import Agent, AgentRegistry, RegistryError, new_id
from app.foundry.runtime import NativeSessions, TeamSessions
from app.foundry.skills.manager import SkillError, SkillManager
from app.foundry.store import FoundryStore
from app.safety.permissions import PermissionRequest

log = logging.getLogger("xenio.foundry")
EDITABLE = {"name", "role", "description", "instructions", "responsibilities", "expertise", "provider", "model", "capabilities", "skills", "memory_scope", "approvals", "max_runtime_seconds", "max_tool_calls", "max_tokens", "output_format"}


class FoundryService:
    def __init__(self, settings, gate, paths, engineering, publish=None):
        self.engineering = engineering
        self.gate = gate
        self.publish = publish
        root = Path(settings.data_dir) / "foundry"
        self.store = FoundryStore(root / "foundry.db")
        self.store.on_event = self._on_event
        self.registry = AgentRegistry(self.store, seed=engineering.roles.roles)
        self.skills = SkillManager(self.store, self.registry, root / "skills")
        self.native = NativeSessions(engineering.registry, paths, ask=self._ask, extra_tools=self.skills, turn_timeout=settings.engineering.task_timeout)
        self.team = TeamSessions(self.native, engineering.agents)
        scratch = root / "thinking"
        scratch.mkdir(parents=True, exist_ok=True)
        self.router = ModelRouter(acp=AcpThinker(engineering.agents, engineering.registry, engineering.roles.cost, scratch))
        e = settings.engineering
        self.engine = FoundryEngine(self.store, self.registry, self.router, sessions=self.team, session_registry=engineering.registry, runs_root=engineering.runs_root, projects_root=Path(settings.hermes.workspace) / "projects", roles=engineering.roles, notify=engineering._notify, max_parallel=e.max_parallel, test_timeout=e.test_timeout, sandbox=e.sandbox_tests, shared_runs=engineering.runs, shared_orch=engineering.orch, skills=self.skills)
        self.native.extra_tools = TeamTools(self.engine, self.skills)
        self.recovered = self.engine.recover()

    def _on_event(self, event: dict) -> None:
        if self.publish is not None:
            self.publish("foundry.event", {"seq": event["seq"], "type": event["type"], "workflow": event["workflow"]})

    async def _ask(self, session: str, scope: dict, decision) -> bool:
        """An agent's step that really does need you goes through the same approvals as everything else in XENIO."""
        if self.gate is None:
            return False
        answer = await self.gate.check(PermissionRequest(decision.action, f"{scope.get('role')} ({scope.get('task')}): {decision.summary}", decision.level, "xenio"))
        return bool(answer.approved)

    # ---- what the War Room reads --------------------------------------------------------------------------------------

    def overview(self) -> dict:
        return {"available": True, "can_think": self.router.available(), "workflows": [self._line(w) for w in self.engine.list()], "agents": [self._agent(a) for a in self.registry.list(include_archived=True)], "skills": self._skills_with_use(self.skills.list()), "seats": list(self.router.seats), "judge": self.router.judge, "recovered": self.recovered}

    def _skills_with_use(self, skills: list[dict]) -> list[dict]:
        use = self.skills.usage()
        return [{**s, "use": use.get(s.get("id"), {"used": 0, "added": 0, "by": []})} for s in skills]

    @staticmethod
    def _line(wf: dict) -> dict:
        return {k: wf.get(k) for k in ("id", "goal", "status", "created", "updated", "outcome", "attention", "milestone", "autonomy", "project", "delivered", "summary", "paused", "pause_reason", "size", "budget")}

    def _agent(self, agent: Agent) -> dict:
        return {**agent.public(), "performance": self.registry.performance(agent.id), "versions": self.registry.versions(agent.id)[:5]}

    def status(self, workflow_id: str = "") -> dict:
        """Short enough to say aloud."""
        items = self.engine.list(5)
        if not workflow_id:
            active = [w for w in items if w["status"] in ACTIVE]
            if not active and not items:
                return {"status": "nothing is running"}
            workflow_id = (active or items)[0]["id"]
        state = self.engine.state(workflow_id)
        wf = state["workflow"]
        working = [{"agent": m["name"], "doing": m["task"], "state": m["state"]} for m in state["team"] if m["state"] != "idle"]
        return {"workflow": wf["id"], "goal": wf["goal"][:200], "status": wf["status"], "milestone": wf.get("milestone"), "steps": state["counts"], "working": working, "needs_you": wf.get("attention") or "", "outcome": wf.get("outcome") or "", "where": wf.get("delivered") or wf.get("project") or "", "running_at": state["preview"].get("url", ""), "recent": [f"{m['sender']}: {m['content'][:160]}" for m in state["messages"][-5:]]}

    # ---- doing -------------------------------------------------------------------------------------------------------

    async def dispatch(self, command: str, a: dict, confirmed: bool = False) -> dict:
        by = "user" if confirmed else ""
        wid = str(a.get("workflow") or "")
        engine, skills, registry = self.engine, self.skills, self.registry
        try:
            match command:
                case "start":
                    folder = ""
                    if a.get("path") or a.get("project"):
                        folder, _ = self.engineering.resolve_repository(str(a.get("project") or ""), str(a.get("path") or ""))
                    return await engine.start(str(a.get("goal") or a.get("request") or ""), folder, autonomy=str(a.get("autonomy") or "supervisor"), constraints=[str(c) for c in a.get("constraints") or []])
                case "overview":
                    return self.overview()
                case "state":
                    return engine.state(wid)
                case "status":
                    return self.status(wid)
                case "list":
                    return {"workflows": [self._line(w) for w in engine.list()]}
                case "pause":
                    return engine.pause(wid)
                case "resume":
                    return engine.resume(wid)
                case "stop":
                    return await engine.stop(wid, str(a.get("reason") or "you stopped it"))
                case "stop_all":
                    return {"stopped": await engine.stop_all()}
                case "message":
                    target = wid or self._current()
                    if engine.get(target).get("status") not in ACTIVE:  # it has finished: what you say now is a change to it
                        return await engine.change(target, str(a.get("text") or ""))
                    return engine.message(target, str(a.get("text") or ""), str(a.get("to") or TEAM))
                case "change":
                    return await engine.change(wid, str(a.get("text") or ""))
                case "delete":
                    self._yours(by)
                    return engine.delete(wid, bool(a.get("with_files")))
                case "preview_start":
                    return await engine.start_preview(wid)
                case "preview_stop":
                    return engine.stop_preview(wid)
                case "approve":
                    self._yours(by)
                    return engine.approve(wid, str(a.get("node") or ""), str(a.get("note") or ""))
                case "reject":
                    self._yours(by)
                    return await engine.reject(wid, str(a.get("node") or ""), str(a.get("note") or ""))
                case "decide":
                    self._yours(by)
                    return engine.decide(wid, str(a.get("decision") or ""), str(a.get("choice") or ""), str(a.get("text") or ""))
                case "discuss":
                    self._yours(by)
                    return engine.discuss(wid, str(a.get("decision") or ""), str(a.get("text") or ""))
                case "require_evidence":
                    return engine.require_evidence(wid, bool(a.get("required", True)))
                case "constraints":
                    return engine.set_constraints(wid, [str(c) for c in a.get("constraints") or []])
                case "priority":
                    return engine.prioritize(wid, str(a.get("node") or ""), int(a.get("priority") or 0))
                case "reassign":
                    self._yours(by)
                    return engine.reassign(wid, str(a.get("node") or ""), str(a.get("agent") or ""))
                case "cancel_task":
                    return engine.cancel_node(wid, str(a.get("node") or ""))
                case "review":
                    return engine.request_review(wid, str(a.get("agent") or "reviewer"), str(a.get("text") or "please review this"))
                case "extend_budget":
                    self._yours(by)
                    return engine.extend_budget(wid)
                case "agent_create":
                    self._yours(by)
                    data = {k: v for k, v in (a.get("agent") or {}).items() if k in EDITABLE}
                    return self._agent(registry.create(Agent(id=new_id(str(data.get("name") or "agent")), source="user", kind="permanent", **{"role": "specialist", **data}), "user", "you created it"))
                case "agent_update":
                    return self._agent(registry.update(str(a.get("agent") or ""), {k: v for k, v in (a.get("changes") or {}).items() if k in EDITABLE}, "user" if confirmed else "unconfirmed", str(a.get("why") or "edited")))
                case "agent_enable":
                    return self._agent(registry.enable(str(a.get("agent") or "")))
                case "agent_disable":
                    return self._agent(registry.disable(str(a.get("agent") or "")))
                case "agent_archive":
                    return self._agent(registry.archive(str(a.get("agent") or "")))
                case "agent_restore":
                    return self._agent(registry.restore(str(a.get("agent") or "")))
                case "agent_promote":
                    self._yours(by)
                    return self._agent(registry.promote(str(a.get("agent") or "")))
                case "agent_delete":
                    self._yours(by)
                    return {"deleted": registry.delete(str(a.get("agent") or ""))}
                case "skill_request":
                    return await asyncio.to_thread(skills.acquire, str(a.get("request") or a.get("text") or ""), [str(x) for x in a.get("agents") or []] or None)
                case "skill_install":
                    return await asyncio.to_thread(skills.install, str(a.get("skill") or ""), by, bool(a.get("network")), [str(x) for x in a["agents"]] if a.get("agents") is not None else None, bool(a.get("accept_risk")), [str(x) for x in a["picks"]] if a.get("picks") is not None else None)
                case "skill_everyone":
                    return skills.set_everyone(str(a.get("skill") or ""), bool(a.get("everyone", True)), by)
                case "skill_work":
                    return skills.set_work(str(a.get("skill") or ""), [str(w) for w in a.get("work") or []] if isinstance(a.get("work"), list) else [], by)
                case "skill_reject":
                    return skills.reject(str(a.get("skill") or ""), by)
                case "skill_assign":
                    return skills.assign(str(a.get("skill") or ""), [str(x) for x in a.get("agents") or []], by)
                case "skill_unassign":
                    return skills.unassign(str(a.get("skill") or ""), [str(x) for x in a.get("agents") or []], by)
                case "skill_enable":
                    return skills.enable(str(a.get("skill") or ""), by)
                case "skill_disable":
                    return skills.disable(str(a.get("skill") or ""))
                case "skill_update":
                    return await asyncio.to_thread(skills.update, str(a.get("skill") or ""))
                case "skill_pin":
                    return skills.pin(str(a.get("skill") or ""), bool(a.get("pinned", True)))
                case "skill_rollback":
                    return skills.rollback(str(a.get("skill") or ""), str(a.get("version") or ""), by)
                case "skill_uninstall":
                    return skills.uninstall(str(a.get("skill") or ""), by)
                case "skill_history":
                    return {"history": skills.history(str(a.get("skill") or ""))}
                case "skill_search":
                    return {"skills": skills.search(str(a.get("text") or ""))}
            raise FoundryError(f"unknown command {command!r}", 422)
        except (RegistryError, SkillError, FoundryError) as exc:
            raise FoundryError(str(exc), getattr(exc, "status", 409)) from None
        except DecisionError as exc:
            raise FoundryError(str(exc), 409) from None
        except Exception as exc:  # the engineering service's own errors, with its wording
            if type(exc).__name__ == "EngineeringError":
                raise FoundryError(str(exc), 409) from None
            raise

    @staticmethod
    def _yours(by: str) -> None:
        if by != "user":
            raise FoundryError("only you can do that", 403)

    def _current(self) -> str:
        active = [w for w in self.engine.list(10) if w["status"] in ACTIVE]
        if not active:
            raise FoundryError("nothing is running right now", 409)
        return active[0]["id"]

    async def close(self) -> None:
        await self.engine.close()
        await self.native.close()

