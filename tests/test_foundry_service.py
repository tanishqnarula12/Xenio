"""The one door to The Foundry, the rules for the chat tool, and the signed-in routes."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.engineering.agents import SessionRegistry
from app.engineering.roles import RoleRegistry
from app.foundry.engine import FoundryError
from app.foundry.service import FoundryService
from app.safety import policy
from app.safety.policy import PathPolicy, RiskLevel
from tests.test_foundry_engine import Thinker, covering, finished, until
from tests.test_foundry_runtime import Scripted, call
from tests.test_foundry_skills import GOOD, copying_fetcher


class Engineering:
    def __init__(self, tmp_path):
        self.roles = RoleRegistry()
        self.registry = SessionRegistry(tmp_path / "sessions.json")
        self.agents = None
        self.runs_root = tmp_path / "runs"
        self.runs = {}
        self.orch = None
        self.notes = []

    def _notify(self, kind, text):
        self.notes.append((kind, text))

    def resolve_repository(self, project, path):
        return path, project


@pytest.fixture
def svc(tmp_path):
    settings = SimpleNamespace(data_dir=tmp_path / "data", hermes=SimpleNamespace(workspace=tmp_path / "ws"), engineering=SimpleNamespace(task_timeout=600, max_parallel=3, test_timeout=60, sandbox_tests=False))
    published = []
    service = FoundryService(settings, None, PathPolicy(workspace=tmp_path / "ws"), Engineering(tmp_path), publish=lambda t, d: published.append((t, d)))
    service.published = published
    service.router._nabh = lambda: Thinker()
    return service


def do(svc, command, confirmed=True, **a):
    return asyncio.run(svc.dispatch(command, a, confirmed=confirmed))


class TestTheDoor:
    def test_the_overview_reads_what_is_really_there(self, svc):
        out = do(svc, "overview")
        assert out["available"] and out["workflows"] == [] and {a["id"] for a in out["agents"]} >= {"backend", "reviewer", "security"} and out["seats"] and out["skills"] == []

    def test_every_change_is_published_for_mission_control(self, svc):
        do(svc, "skill_search", text="x")
        svc.registry.disable("backend")
        assert any(t == "foundry.event" and d["type"] == "agent.disabled" for t, d in svc.published)

    def test_unknown_commands_are_refused(self, svc):
        with pytest.raises(FoundryError) as err:
            do(svc, "become_root")
        assert err.value.status == 422

    @pytest.mark.parametrize("command,args", [("approve", {"workflow": "w"}), ("reject", {"workflow": "w"}), ("decide", {"workflow": "w", "decision": "d", "text": "x"}), ("discuss", {"workflow": "w", "decision": "d", "text": "x"}), ("reassign", {"workflow": "w", "node": "n", "agent": "backend"}), ("extend_budget", {"workflow": "w"}), ("agent_create", {"agent": {"name": "X"}}), ("agent_promote", {"agent": "backend"}), ("agent_delete", {"agent": "backend"})])
    def test_what_is_yours_needs_you(self, svc, command, args):
        with pytest.raises(FoundryError) as err:
            do(svc, command, confirmed=False, **args)
        assert err.value.status == 403

    @pytest.mark.parametrize("command", ["skill_install", "skill_reject", "skill_assign", "skill_unassign", "skill_enable", "skill_rollback", "skill_uninstall"])
    def test_and_so_do_skills(self, svc, command, tmp_path):
        svc.skills.fetcher = copying_fetcher(GOOD)
        skill = do(svc, "skill_request", request="npm package hello-tool", confirmed=False)
        with pytest.raises(FoundryError) as err:
            do(svc, command, confirmed=False, skill=skill["id"], agents=["backend"])
        assert err.value.status == 403

    def test_an_unconfirmed_edit_cannot_widen_an_agent(self, svc):
        with pytest.raises(FoundryError) as err:
            do(svc, "agent_update", confirmed=False, agent="reviewer", changes={"capabilities": ["scoped_file_write"]})
        assert err.value.status == 403 and not svc.registry.get("reviewer").permissions["writes"]

    def test_you_can_edit_a_model_and_an_agent_keeps_its_identity(self, svc):
        out = do(svc, "agent_update", agent="backend", changes={"model": "kimi-k2-7-code", "id": "other"})
        assert out["id"] == "backend" and out["model"] == "kimi-k2-7-code"

    def test_you_can_add_an_agent(self, svc):
        out = do(svc, "agent_create", agent={"name": "Data Engineer", "expertise": ["etl"], "capabilities": ["repository_read"]})
        assert out["source"] == "user" and out["kind"] == "permanent" and out["capabilities"] == ["repository_read"]

    def test_status_with_nothing_running_says_so(self, svc):
        assert do(svc, "status") == {"status": "nothing is running"}

    def test_a_message_with_nothing_running_says_so(self, svc):
        with pytest.raises(FoundryError):
            do(svc, "message", text="hello")

    def test_a_workflow_runs_through_the_door_as_a_supervisor_by_default(self, svc, tmp_path):
        """No Hermes: the native runtime does the work."""
        plan = {"summary": "s", "tasks": [{"id": "core", "title": "Core", "role": "backend", "allowed_paths": ["app"]}]}
        finish = "```json\n" + json.dumps({"status": "done", "summary": "wrote it", "changed_files": [], "tests_run": [], "needs": [], "notes": ""}) + "\n```"
        worker = Scripted([call("write_file", path="app/core.py", content="x = 1\n")], finish, json.dumps({"summary": "fine", "findings": []}), json.dumps({"summary": "fine", "findings": []}))
        svc.native._nabh = lambda: worker
        svc.native.models_order = ("m1",)
        svc.engine.deps_factory = lambda project, extra: SimpleNamespace(ensure=lambda cwd: {"installed": [], "refused": [], "failed": [], "unchanged": 0})
        thinker = covering([{"complete": True, "missing": []}])
        thinker.topics = []
        thinker.plan = plan
        svc.router._nabh = lambda: thinker
        svc.router.fallback = ("glm",)

        async def go():
            wf = await svc.dispatch("start", {"goal": "Build a thing"}, confirmed=False)
            await until(finished(svc.engine, wf["id"]), 40)
            return wf["id"]

        wid = asyncio.run(go())
        wf = svc.engine.get(wid)
        assert wf["autonomy"] == "supervisor" and wf["status"] == "done" and Path(wf["delivered"], "app", "core.py").read_text() == "x = 1\n"
        assert do(svc, "status")["where"] == wf["delivered"]
        assert any(k == "done" for k, _ in svc.engineering.notes)


class TestTheChatTool:
    @pytest.mark.parametrize("command", ["status", "list", "state", "overview"])
    def test_reading_needs_nothing(self, command):
        assert policy.classify_foundry_tool({"command": command}).level == RiskLevel.READ

    @pytest.mark.parametrize("command", ["message", "pause", "resume", "stop", "stop_all", "skill_request"])
    def test_running_the_work_runs_at_once(self, command):
        assert policy.classify_foundry_tool({"command": command}).level == RiskLevel.SAFE_WRITE

    def test_one_command_starts_it(self):
        decision = policy.classify_foundry_tool({"command": "start", "goal": "Build a gym app"})
        assert decision.level == RiskLevel.SAFE_WRITE and "gym app" in decision.summary

    def test_with_no_goal_the_model_is_told(self):
        assert policy.classify_foundry_tool({"command": "start"}).blocked

    @pytest.mark.parametrize("command", ["approve", "reject", "decide", "reassign", "extend_budget", "skill_install", "skill_assign", "skill_enable", "skill_rollback", "skill_uninstall", "agent_update", "agent_create", "agent_promote", "agent_delete", "agent_enable"])
    def test_anything_that_approves_or_widens_always_asks_you(self, command):
        assert policy.classify_foundry_tool({"command": command}).level == RiskLevel.CONFIRM_REQUIRED

    def test_anything_else_is_refused(self):
        assert policy.classify_foundry_tool({"command": "sudo"}).blocked

    def test_the_guard_routes_the_tool_to_those_rules(self):
        decision = policy.classify_tool_call("xenio_foundry", {"command": "skill_install", "skill": "x"}, PathPolicy(workspace=Path("/tmp")), "/tmp")
        assert decision.level == RiskLevel.CONFIRM_REQUIRED and decision.action == "foundry.skill_install"

    def test_it_is_never_available_to_an_engineering_agent(self):
        from app.engineering.roles import CAPABILITIES

        assert not any("xenio_foundry" in tools for tools, _ in CAPABILITIES.values())

    def test_an_agent_trying_to_call_it_is_blocked_by_its_role(self, tmp_path):
        scope = {"role": "backend", "tools": ["read_file"], "terminal": "none", "root": str(tmp_path), "allowed_paths": []}
        base = policy.classify_tool_call("xenio_foundry", {"command": "approve"}, PathPolicy(workspace=tmp_path), str(tmp_path))
        assert policy.classify_engineering("xenio_foundry", {"command": "approve"}, scope, base).blocked

    def test_the_tool_forwards_only_what_it_should(self, monkeypatch):
        from app.tools import foundry as tool

        sent = {}
        monkeypatch.setattr(tool, "call_service", lambda action, args, timeout=0: sent.update(action=action, args=args) or {"ok": True, "result": {"status": "x"}})
        out = tool.foundry({"command": "start", "goal": "Build it", "evil": "x"}, None)
        assert sent["action"] == "foundry" and sent["args"] == {"command": "start", "goal": "Build it"} and out.success

    def test_a_refusal_comes_back_as_a_failure(self, monkeypatch):
        from app.tools import foundry as tool

        monkeypatch.setattr(tool, "call_service", lambda action, args, timeout=0: {"ok": False, "error": "only you can do that"})
        out = tool.foundry({"command": "approve"}, None)
        assert not out.success and "only you" in str(out.error)

    def test_the_engineering_socket_hands_it_over(self, tmp_path):
        from app.engineering.service import EngineeringError, EngineeringService

        fake = SimpleNamespace(foundry=None)

        async def go():
            with pytest.raises(EngineeringError):
                await EngineeringService.dispatch(fake, "foundry", {"command": "status"}, confirmed=True)

        asyncio.run(go())
