"""The Foundry as one service: the War Room and the chat both come through here. Anything that widens what an agent may do, or approves something, only happens when the call really comes from you: ``confirmed`` is set by XENIO's own authenticated UI, or by the guard after it asked you."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from app.foundry.decisions import DecisionError
from app.foundry.capabilities import Capabilities
from app.foundry.computer import Computer
from app.foundry.engine import ACTIVE, FoundryEngine, FoundryError, TeamTools
from app.foundry.hands import Hands, HandsError, colour_for
from app.foundry.sight import Sight
from app.foundry.toolbox import Toolbox
from app.foundry.vault import Vault, VaultError
from app.integrations import nabh as nabh_module
from app.foundry.messages import TEAM
from app.foundry.models import AcpThinker, ModelRouter
from app.foundry.registry import Agent, AgentRegistry, RegistryError, new_id
from app.foundry.runtime import NativeSessions, TeamSessions
from app.foundry.skills.manager import SkillError, SkillManager
from app.foundry.store import FoundryStore
from app.safety.permissions import PermissionRequest

log = logging.getLogger("xenio.foundry")
EDITABLE = {"name", "role", "description", "instructions", "responsibilities", "expertise", "provider", "model", "capabilities", "skills", "memory_scope", "approvals", "max_runtime_seconds", "max_tool_calls", "max_tokens", "output_format"}


def _desktop(settings):
    from app.desktop.service import DesktopService

    return DesktopService(data_dir=Path(settings.data_dir))


def _github_ready() -> bool:
    from app.integrations.github import GitHubIntegration

    return bool(GitHubIntegration().gh_signed_in())


def _connectors() -> dict:
    def mail():
        from app.integrations.email import EmailIntegration

        return EmailIntegration()

    def calendar():
        from app.integrations.calendar import default_calendar

        return default_calendar()

    def github():
        from app.integrations.github import GitHubIntegration

        return GitHubIntegration()

    return {"mail": mail, "calendar": calendar, "github": github}


def _make_picture(prompt: str) -> bytes:
    client = nabh_module.client()
    if client is None:
        raise VaultError("pictures need a Nabh key")
    return client.image(prompt)[0]


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
        self.vault = Vault(Path(settings.data_dir) / "vault")
        self.uploads_folder = Path(settings.data_dir) / "uploads"
        self.hands = Hands(root / "live", vault_dir=self.vault.folder)
        self.sight = Sight()
        self.capabilities = Capabilities(nabh_module.client, self.vault, google_ready=lambda: (Path(settings.data_dir) / "google_token.json").is_file(), github_ready=lambda: _github_ready())
        self.computer = Computer(lambda: _desktop(settings), self._ask, event=self._computer_event, sight=self.sight, stop_work=self._computer_taken, resume_work=self._computer_back)
        self.engine = FoundryEngine(self.store, self.registry, self.router, hands=self.hands, sight=self.sight, capabilities=self.capabilities, computer=self.computer, pictures=_make_picture, sessions=self.team, session_registry=engineering.registry, runs_root=engineering.runs_root, projects_root=Path(settings.hermes.workspace) / "projects", roles=engineering.roles, notify=engineering._notify, max_parallel=e.max_parallel, test_timeout=e.test_timeout, sandbox=e.sandbox_tests, shared_runs=engineering.runs, shared_orch=engineering.orch, skills=self.skills)
        self.toolbox = Toolbox(self.engine, sight=self.sight, hands=self.hands, vault=self.vault, computer=self.computer, images=_make_picture, ask=self._ask, connectors=_connectors(), own_browser=self._own_browser, paths=paths)
        self.native.extra_tools = TeamTools(self.engine, self.skills, self.toolbox)
        from app.foundry.mcp import McpHub

        self.mcp = McpHub(root)  # the CEO's MCP servers. Their tokens are in the Keychain, not in this folder
        self.engine.mcp = self.mcp
        from app.foundry.figma_bridge import FigmaBridge

        self.figma = FigmaBridge(root)
        self.figma.start()  # this Mac only, and only a plugin holding the pairing code
        self.engine.figma = self.figma
        self.capabilities.figma = lambda: self.figma.connected() is not None
        self.native.on_heard = self.engine.agent_heard
        self.recovered = self.engine.recover()

    def _on_event(self, event: dict) -> None:
        if self.publish is not None:
            self.publish("foundry.event", {"seq": event["seq"], "type": event["type"], "workflow": event["workflow"]})

    def _own_browser(self):
        from app.desktop.own_browser import OwnBrowser

        return OwnBrowser(self.computer.desktop)  # Safari's own scripting on any desktop when Safari allows it, else the screen-reader route

    def _computer_event(self, kind: str, scope: dict, facts: dict) -> None:
        self.store.event(kind, str(scope.get("workflow") or ""), str(scope.get("role") or ""), str(scope.get("task") or ""), **facts)

    def _computer_taken(self, why: str) -> None:
        """Every job that may use your screen waits, and everyone is told why. Work that does not need it carries on."""
        for workflow in list(self.computer.allowed):
            live = self.engine.flows.get(workflow)
            if live is not None and live.wf.get("status") in ACTIVE and not live.wf.get("paused"):
                self.engine.pause(workflow, "the CEO has the computer")
                self.engine.bus.send(workflow, "xenio", "you", "ACK", f"All yours. Nobody here will touch your mouse or keyboard until you hand it back ({why}).", trusted=True)

    def _computer_back(self) -> None:
        for workflow in list(self.computer.allowed):
            live = self.engine.flows.get(workflow)
            if live is not None and live.wf.get("pause_reason") == "the CEO has the computer":
                self.engine._tell_tasks(live, "The CEO had the computer and may have changed things. Read the screen again before you act on what you remember.")
                self.engine.resume(workflow)
                self.engine.bus.send(workflow, "xenio", "you", "ACK", "Thanks. Everyone looks at the screen again before doing anything, because you may have changed things.", trusted=True)

    async def _ask(self, session: str, scope: dict, decision) -> bool:
        """An agent's step that really does need you goes through the same approvals as everything else in XENIO."""
        if self.gate is None:
            return False
        answer = await self.gate.check(PermissionRequest(decision.action, f"{scope.get('role')} ({scope.get('task')}): {decision.summary}", decision.level, "xenio"))
        return bool(answer.approved)

    # ---- what the War Room reads --------------------------------------------------------------------------------------

    def overview(self) -> dict:
        return {"available": True, "can_think": self.router.available(), "workflows": [self._line(w) for w in self.engine.list()], "agents": [self._agent(a) for a in self.registry.list(include_archived=True)], "skills": self._skills_with_use(self.skills.list()), "seats": list(self.router.seats), "judge": self.router.judge, "recovered": self.recovered, "limits": self.engine.limits(), "mcp": self.mcp.public() if getattr(self, "mcp", None) is not None else []}

    def _skills_with_use(self, skills: list[dict]) -> list[dict]:
        use = self.skills.usage()
        return [{**s, "use": use.get(s.get("id"), {"used": 0, "added": 0, "by": []})} for s in skills]

    @staticmethod
    def _line(wf: dict) -> dict:
        return {k: wf.get(k) for k in ("id", "goal", "status", "created", "updated", "outcome", "attention", "milestone", "autonomy", "project", "delivered", "summary", "paused", "pause_reason", "size", "budget", "budget_tokens", "task_tokens")}

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
                    seen = await self._look_at_uploads(a.get("uploads"), str(a.get("goal") or ""))
                    return await engine.start(str(a.get("goal") or a.get("request") or ""), folder, autonomy=str(a.get("autonomy") or "supervisor"), constraints=[str(c) for c in a.get("constraints") or []], seen=seen,
                                              job_tokens=a.get("job_tokens") if by and a.get("job_tokens") not in (None, "") else None, task_tokens=a.get("task_tokens") if by and a.get("task_tokens") not in (None, "") else None)
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
                    words = await self._with_pictures(a)
                    if engine.get(target).get("status") not in ACTIVE:  # it has finished: what you say now is a change to it
                        return await engine.change(target, words)
                    return engine.message(target, words, str(a.get("to") or TEAM))
                case "change":
                    return await engine.change(wid, await self._with_pictures(a))
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
                case "limits":
                    return engine.limits()
                case "set_limits":
                    self._yours(by)
                    return engine.set_limits(a.get("job_tokens") or 0, a.get("task_tokens") or 0)
                case "set_budget":
                    self._yours(by)
                    return engine.set_budget(wid, a.get("job_tokens") if "job_tokens" in a else None, a.get("task_tokens") if "task_tokens" in a else None)
                case "github_status":
                    return await engine.github_action(wid, "status")
                case "github_publish" | "github_push" | "github_pull" | "github_rename":
                    self._yours(by)
                    return await engine.github_action(wid, command.removeprefix("github_"), str(a.get("name") or ""), a.get("private") is not False)
                case "figma_status":
                    self._yours(by)  # it shows the pairing code
                    return self.figma.status()
                case "mcp_list":
                    return {"servers": self.mcp.public()}
                case "mcp_add" | "mcp_update" | "mcp_remove" | "mcp_test":
                    self._yours(by)
                    return await self._mcp(command, a)
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
                case "capabilities":
                    return {"capabilities": await asyncio.to_thread(self.capabilities.report, bool(a.get("fresh")))}
                case "artifacts":
                    return await asyncio.to_thread(engine.artifacts, wid)
                case "live":
                    return self._live_view(wid)
                case "take_over":
                    self._yours(by)
                    return self.computer.give_way(str(a.get("why") or "you took over"))
                case "hand_back":
                    self._yours(by)
                    return self.computer.hand_back()
                case "vault_status":
                    return await asyncio.to_thread(self.vault.status)
                case "vault_setup":
                    self._yours(by)
                    return await asyncio.to_thread(self.vault.setup)
                case "vault_add":
                    self._yours(by)
                    return await asyncio.to_thread(self.vault.add, str(a.get("host") or ""))
                case "vault_touch":
                    self._yours(by)
                    return await asyncio.to_thread(self.vault.enrol_touch)
                case "vault_open_site":
                    self._yours(by)
                    return await asyncio.to_thread(self.vault.open_site, str(a.get("url") or ""))
                case "vault_remove":
                    self._yours(by)
                    return await asyncio.to_thread(self.vault.remove, str(a.get("item") or ""))
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
        except (VaultError, HandsError) as exc:
            raise FoundryError(str(exc), 409) from None
        except DecisionError as exc:
            raise FoundryError(str(exc), 409) from None
        except Exception as exc:  # the engineering service's own errors, with its wording
            if type(exc).__name__ == "EngineeringError":
                raise FoundryError(str(exc), 409) from None
            raise

    async def _with_pictures(self, a: dict) -> str:
        words = str(a.get("text") or "").strip()
        seen = await self._look_at_uploads(a.get("uploads"), words)
        if seen:
            words = f"{words or 'Look at this.'}\n\n(You attached a picture. XENIO looked at it and saw: {seen})"
        return words

    async def _look_at_uploads(self, ids, about: str) -> str:
        """What the CEO attached, described once, so that it costs a look and not a picture on every step."""
        if not ids:
            return ""
        from app.foundry.sight import PICTURES, SightError
        from app.ui.uploads import Uploads

        store = Uploads(self.uploads_folder)
        ask = f"The CEO attached this to a message to their team. The message: {about[:400]}\nDescribe exactly what it shows, for someone who cannot see it: layout, colours, type, and every word that matters. If it shows a fault, say where it is. Under 180 words."
        said = []
        for upload_id in [str(i) for i in ids][:4]:
            info = store.get(upload_id)
            if info is None or Path(str(info.get("name"))).suffix.lower() not in PICTURES:
                continue
            try:
                seen, _model, _tokens = await self.sight.look_at_file(store.path(info), ask)
            except SightError as exc:
                seen = f"(it could not be looked at: {exc})"
            said.append(seen.strip())
        return " ".join(said)[:2400]

    def _live_view(self, workflow: str) -> dict:
        """What is really happening on the computer, from real actions. Nothing here is animated or made up."""
        actions = [{"agent": e["agent"], "at": e["at"], **e["data"]} for e in self.store.events(workflow, limit=2000) if e["type"] == "computer.action"][-30:]
        yours = [{**w, "colour": colour_for(w["name"]), "signed_in": True, "kind": "your own browser", "picture": False} for (job, _agent), w in self.toolbox.windows.items() if job == workflow] if self.toolbox is not None else []
        return {"browsers": [*yours, *self.hands.state(workflow)], "computer": self.computer.state(), "allowed": workflow in self.computer.allowed, "actions": actions}

    def live_picture(self, workflow: str, agent: str) -> bytes:
        path = self.hands.live_path(workflow, agent)
        if not path.resolve().is_relative_to(self.hands.live_root.resolve()) or not path.is_file():
            raise FoundryError("no picture yet", 404)
        return path.read_bytes()

    @staticmethod
    def _yours(by: str) -> None:
        if by != "user":
            raise FoundryError("only you can do that", 403)

    def _current(self) -> str:
        active = [w for w in self.engine.list(10) if w["status"] in ACTIVE]
        if not active:
            raise FoundryError("nothing is running right now", 409)
        return active[0]["id"]

    async def _mcp(self, command: str, a: dict) -> dict:
        """Connectors are the CEO's: an agent can use one they switched on, and nothing more."""
        from app.foundry.mcp import McpError

        hub, name = self.mcp, str(a.get("name") or "")
        try:
            if command == "mcp_add":
                if a.get("config"):
                    made = await asyncio.to_thread(hub.add_config, str(a["config"]), str(a.get("token") or ""))
                else:
                    made = [await asyncio.to_thread(hub.add, name, command=str(a.get("command") or ""), url=str(a.get("url") or "").strip(), token=str(a.get("token") or ""), token_env=str(a.get("token_env") or ""), for_agents=bool(a.get("for_agents")))]
                return {"added": [m.name for m in made], "servers": hub.public()}
            if command == "mcp_update":
                await asyncio.to_thread(hub.update, name, for_agents=a.get("for_agents") if "for_agents" in a else None, token=str(a.get("token") or "") or None)
            elif command == "mcp_remove":
                await asyncio.to_thread(hub.remove, name)
            elif command == "mcp_test":
                return {**await asyncio.to_thread(hub.test, name), "servers": hub.public()}
            return {"servers": hub.public()}
        except McpError as exc:
            raise FoundryError(str(exc), exc.status) from None

    async def close(self) -> None:
        await self.engine.close()
        if getattr(self, "mcp", None) is not None:
            self.mcp.close()
        if getattr(self, "figma", None) is not None:
            self.figma.stop()
        self.toolbox = None
        await self.native.close()

