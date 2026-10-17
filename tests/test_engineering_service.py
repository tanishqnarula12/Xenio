"""V6 plumbing: agent sessions over real ACP stdio (several at once, per-session cwd/model, permissions labelled by
task, provider failures, cancellation), the guard's session registry, and the engineering service (local socket with
a token, project resolution, apply only with the user's yes)."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.engineering.agents import AgentSessions, ProviderUnavailable, SessionRegistry
from app.engineering.roles import RoleRegistry
from app.engineering.service import EngineeringError, EngineeringService
from app.safety.permissions import PermissionGate
from app.safety.policy import PathPolicy
from app.tools import engineering as tool

FAKE = Path(__file__).with_name("fake_acp_team.py")


@pytest.fixture
def settings(make_settings):
    return make_settings(HERMES_COMMAND=f'"{sys.executable}" "{FAKE}"')


# ---- agent sessions over real ACP ----------------------------------------------------------------------------------

def test_sessions_run_in_parallel_in_their_own_folders_with_their_own_models(settings, tmp_path):
    asked = []

    async def ask(session_id, request):
        asked.append((session_id, request.title))
        return True

    a_dir, b_dir = tmp_path / "a", tmp_path / "b"
    a_dir.mkdir(), b_dir.mkdir()

    async def go():
        agents = AgentSessions(settings.hermes, settings.data_dir, ask=ask, turn_timeout=20)
        try:
            a = await agents.open(a_dir)
            b = await agents.open(b_dir, model="nvidia/nemotron-3-super-120b-a12b:free")
            ta, tb = await asyncio.gather(agents.prompt(a, "write one.txt"), agents.prompt(b, "hello"))
            perm = await agents.prompt(a, "permission")
            with pytest.raises(ProviderUnavailable, match="empty or broken"):
                await agents.prompt(b, "broken")
            with pytest.raises(ProviderUnavailable, match="could not switch"):
                await agents.open(a_dir, model="missing/model")
            return a, b, ta, tb, perm
        finally:
            await agents.close()

    a, b, ta, tb, perm = asyncio.run(go())
    assert (a_dir / "one.txt").exists() and not (b_dir / "one.txt").exists()
    assert ta.text.endswith(str(a_dir)) and "nemotron-3-super" in tb.text and tb.model.endswith(":free")
    assert ta.usage == {"totalTokens": 42}
    assert perm.text == "permission=yes" and asked == [(a, "npm run build")]  # the asker knows which session asked


def test_a_running_session_can_be_cancelled(settings, tmp_path):
    async def go():
        agents = AgentSessions(settings.hermes, settings.data_dir, turn_timeout=20)
        try:
            sid = await agents.open(tmp_path)
            turn = asyncio.ensure_future(agents.prompt(sid, "slow"))
            while sid not in agents.running():
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.1)
            assert await agents.cancel(sid)
            return await turn
        finally:
            await agents.close()

    turn = asyncio.run(go())
    assert turn.cancelled and turn.text == "working"


def test_without_an_answer_from_the_user_permissions_are_denied(settings, tmp_path):
    async def go():
        agents = AgentSessions(settings.hermes, settings.data_dir, ask=None, turn_timeout=20)
        try:
            return await agents.prompt(await agents.open(tmp_path), "permission")
        finally:
            await agents.close()

    assert asyncio.run(go()).text == "permission=no"


def test_the_registry_holds_scopes_until_the_task_ends(tmp_path):
    reg = SessionRegistry(tmp_path / "eng" / "sessions.json")
    reg.add("s1", {"run": "r1", "role": "backend"})
    reg.add("s2", {"run": "r2", "role": "reviewer"})
    assert reg.get("s1")["role"] == "backend" and oct(reg.path.stat().st_mode)[-3:] == "600"
    reg.remove("s1")
    reg.clear_run("r2")
    assert json.loads(reg.path.read_text()) == {}


# ---- the service --------------------------------------------------------------------------------------------------

class Confirmer:
    def __init__(self, answer):
        self.answer, self.asked = answer, []

    async def confirm(self, request, *, strict):
        self.asked.append(request.description)
        return self.answer


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=U", "-c", "user.email=u@e.x", *args], cwd=cwd, capture_output=True,
                          text=True, check=True).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "README.md").write_text("# Teh project\n")
    git(root, "init", "-q", "-b", "main"), git(root, "add", "-A"), git(root, "commit", "-q", "-m", "i")
    return root


class Projects:
    def __init__(self, records):
        self.records = records

    def resolve(self, name):
        from app.integrations.base import IntegrationError

        matches = [r for r in self.records if (name or "crm").lower() in r["name"].lower()]
        if len(matches) != 1:
            raise IntegrationError(f"Several projects match {name!r}. Ask the user which one." if matches
                                   else f"No project called {name!r} is recorded.")
        return matches[0]


def service(settings, tmp_path, answer=True, script=None, projects=()):
    from tests.test_engineering_orchestrator import FakeTeam

    def default(kind, prompt, cwd):
        if kind == "task":
            (cwd / "README.md").write_text("# The project\n")
        return '```json\n{"status": "done", "summary": "fixed", "findings": []}\n```'

    confirmer = Confirmer(answer)
    svc = EngineeringService(settings, PermissionGate(confirmer), PathPolicy(workspace=tmp_path),
                             agents=FakeTeam(script or default), projects=Projects(list(projects)))
    svc.confirmer = confirmer
    return svc


def test_repository_resolution_by_path_and_project(settings, tmp_path, repo):
    svc = service(settings, tmp_path, projects=[{"name": "CRM", "path": str(repo)}, {"name": "CRM 2.0", "path": ""},
                                                {"name": "Blog", "path": str(tmp_path / "missing")}])
    assert svc.resolve_repository(path=str(repo)) == (str(repo.resolve()), "")
    with pytest.raises(EngineeringError, match="Several projects match"):
        svc.resolve_repository(project="crm")  # ambiguous: XENIO asks, never guesses
    with pytest.raises(EngineeringError, match="no local folder"):
        svc.resolve_repository(project="blog")


def test_a_small_change_runs_and_is_applied_only_with_a_yes(settings, tmp_path, repo):
    async def go(answer):
        svc = service(settings, tmp_path / answer.__class__.__name__ / str(answer), answer)
        try:
            out = await svc.dispatch("start", {"request": "Fix the typo in README.md", "path": str(repo)})
            assert out["status"] == "started"
            for _ in range(200):
                if svc.runs and next(iter(svc.runs.values())).status in ("done", "failed"):
                    break
                await asyncio.sleep(0.05)
            run = next(iter(svc.runs.values()))
            status = svc.status(run.id)
            try:
                applied = await svc.apply(run.id)
            except EngineeringError as exc:
                applied = str(exc)
            return svc, run, status, applied
        finally:
            await svc.close()

    svc, run, status, applied = asyncio.run(go(False))
    assert run.status == "done" and status["tasks"] == {"succeeded": 1}
    assert "declined" in applied and (repo / "README.md").read_text() == "# Teh project\n"  # no: nothing written
    assert "into your working tree" in svc.confirmer.asked[0]
    svc, run, status, applied = asyncio.run(go(True))
    assert applied["applied"] == ["README.md"] and (repo / "README.md").read_text() == "# The project\n"


def test_the_socket_needs_the_token_and_the_tool_reaches_the_service(settings, tmp_path, monkeypatch):
    svc = service(settings, tmp_path)

    async def go():
        await svc.serve()
        try:
            monkeypatch.setenv("XENIO_ENGINEERING_SOCKET", str(svc.socket_path))
            monkeypatch.setenv("XENIO_ENGINEERING_TOKEN", "wrong")
            bad = await asyncio.to_thread(tool.call_service, "status", {})
            monkeypatch.setenv("XENIO_ENGINEERING_TOKEN", svc.token)
            good = await asyncio.to_thread(tool.engineering, {"action": "status"}, None)
            unknown = await asyncio.to_thread(tool.engineering, {"action": "explode"}, None)
            return bad, good, unknown
        finally:
            await svc.close()

    bad, good, unknown = asyncio.run(go())
    assert bad == {"ok": False, "error": "not authorized"}
    assert good.success and "not working on anything" in good.data["summary"]
    assert not unknown.success and "unknown action" in unknown.error
    assert oct(svc.root.stat().st_mode)[-3:] == "700"


def test_agents_permission_requests_are_labelled_with_task_and_role(settings, tmp_path):
    from app.hermes.client import HermesPermissionRequest

    svc = service(settings, tmp_path, answer=True)
    svc.registry.add("eng-9", {"run": "r", "task": "api", "role": "backend"})
    req = HermesPermissionRequest(title="npm run build", kind="execute", raw_input={"command": "npm run build"})
    assert asyncio.run(svc._ask("eng-9", req)) is True
    assert svc.confirmer.asked[0].startswith("(engineering task api, backend)")


def test_status_never_invents_progress(settings, tmp_path):
    svc = service(settings, tmp_path)
    assert svc.status() == {"active": False, "summary": "The engineering team is not working on anything."}
    assert "percent" not in json.dumps(svc.status()).lower()


def test_engineering_tool_levels():
    from app.safety import policy as p

    lv = lambda a: "BLOCKED" if (d := p.classify_engineering_tool(a)).blocked else d.level.name  # noqa: E731
    assert lv({"action": "status"}) == "READ" and lv({"action": "report"}) == "READ"
    assert lv({"action": "start", "request": "add password reset"}) == "SAFE_WRITE"
    assert lv({"action": "start"}) == "BLOCKED"
    for action in ("apply", "push", "pull_request"):
        assert lv({"action": action}) == "CONFIRM_REQUIRED"  # leaving the isolated workspace always asks
    assert lv({"action": "deploy"}) == "BLOCKED"
    assert RoleRegistry().get("security").terminal_mode == "read"


def test_engineering_edits_are_decided_by_the_engineering_rules_only(settings, tmp_path):
    from app.engineering.roles import RoleRegistry as Roles
    from app.hermes.client import HermesPermissionRequest

    svc = service(settings, tmp_path, answer=False)
    root = tmp_path / "wt"
    (root / "web").mkdir(parents=True)
    role = Roles().get("frontend")
    svc.registry.add("eng-1", {"run": "r", "task": "ui", "role": "frontend", "tools": sorted(role.tools),
                               "terminal": role.terminal_mode, "root": str(root), "allowed_paths": ["web"]})

    def edit(path):
        return HermesPermissionRequest(title=f"Approve edit: {path}", kind="edit",
                                       content=[{"type": "diff", "path": str(path), "oldText": None, "newText": "x"}])

    assert asyncio.run(svc._ask("eng-1", edit(root / "web" / "app.js"))) is True  # a script, but in scope: no question
    assert svc.confirmer.asked == []
    assert asyncio.run(svc._ask("eng-1", edit(root / "src" / "x.py"))) is False  # out of scope: never
    assert asyncio.run(svc._ask("eng-1", edit(tmp_path / "elsewhere.txt"))) is False


def test_engineering_refuses_an_unguarded_hermes_gateway(tmp_path):
    import os

    from app.hermes.guard import guard_live

    home, data = tmp_path / "hermes", tmp_path / "data"
    home.mkdir(), data.mkdir()
    assert guard_live(data, home)[0] is None  # no gateway yet: the one XENIO starts will carry the guard
    (home / "gateway.lock").write_text(json.dumps({"pid": os.getpid()}))
    live, why = guard_live(data, home)
    assert live is False and "hermes gateway stop" in why  # running, but not started by XENIO
    (data / "guard.json").write_text(json.dumps({"pid": os.getpid()}))
    assert guard_live(data, home)[0] is True
    (home / "gateway.lock").write_text(json.dumps({"pid": 999999}))
    assert guard_live(data, home)[0] is None  # a dead gateway
