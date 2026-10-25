"""'Allow for this task': one approval lets XENIO finish a task inside its own workspace without asking again - and
nothing more. Enforced by the gate (backend), checked against the policy's record of every path an action writes;
high-risk actions, anything that reaches outside the workspace, and anything after injected instructions still ask."""

import asyncio
import time
from pathlib import Path

import pytest

from app.hermes.client import HermesPermissionRequest
from app.safety import policy as p
from app.safety.permissions import APPROVE_TASK, PermissionGate, PermissionRequest, from_hermes_request
from app.ui.approvals import ApprovalError, WebConfirmer
from tests.test_guard_plugin import load


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "Xenio"
    (root / "snake").mkdir(parents=True)
    return root


@pytest.fixture
def paths(ws):
    return p.PathPolicy(workspace=ws)


# ---- what counts as "inside the workspace" -------------------------------------------------------------------------

@pytest.mark.parametrize("command, cwd, confined", [
    ("pip install requests", "", True),
    ("cd snake && npm install", "", True),
    ("npm run dev", "snake", True),
    ("node snake/game.js", "", True),
    ("rm -rf snake", "", True),
    ("cd ~/Documents && npm install", "", False),  # starts inside, installs outside
    ("rm -rf ~/Documents/old", "", False),
    ("cp snake/a.js ~/Desktop/a.js", "", False),
    ("python /etc/evil.py", "", False),
    ("python -c 'import os'", "", False),  # inline code is never "a file in the workspace"
    ("git push", "snake", False),
    ("sudo rm -rf /", "", False),
    ("./run", "", False),
])
def test_confinement(command, cwd, confined, paths, ws):
    decision = p.classify_command(command, paths, str(ws / cwd) if cwd else str(ws))
    assert paths.confined(decision) is confined, decision


def test_file_edits_and_the_workspace_itself(paths, ws):
    inside = paths.check(str(ws / "snake" / "game.js"), "create")
    outside = paths.check(str(Path.home() / "Documents" / "game.js"), "create")
    assert paths.confined(inside) and not paths.confined(outside)
    assert not paths.confined(p.combine([inside, outside], inside))  # one target outside is enough
    whole = p.Decision(p.RiskLevel.CONFIRM_REQUIRED, "filesystem.delete", "delete it", touches=(str(ws),))
    assert not paths.confined(whole)
    assert not p.PathPolicy().confined(inside)  # no workspace configured: nothing is confined


def test_escalation_carries_confinement():
    decision = p.Decision(p.RiskLevel.CONFIRM_REQUIRED, "terminal.install_package", "install: pip install x")
    message = p.escalation_message(decision, "terminal:pip install x", confined=True)
    assert p.parse_escalation_confined(message) and p.parse_escalation(message)[2] == "terminal:pip install x"
    assert not p.parse_escalation_confined(p.escalation_message(decision, ""))


# ---- the gate ------------------------------------------------------------------------------------------------------

class Answers:
    def __init__(self, *answers):
        self.answers, self.asked = list(answers), []

    async def confirm(self, request, *, strict):
        self.asked.append(request.description)
        return self.answers.pop(0) if self.answers else False


def req(description, confined=True, risk=p.RiskLevel.CONFIRM_REQUIRED):
    return PermissionRequest("filesystem.create", description, risk, "hermes", confined=confined)


def test_one_task_approval_covers_the_workspace_and_nothing_else():
    answers = Answers(APPROVE_TASK, True)
    gate = PermissionGate(answers)
    changes = []
    gate.on_grant_change = changes.append

    async def run():
        first = await gate.check(req("create snake/game.js"))
        second = await gate.check(req("create snake/style.css"))
        third = await gate.check(req("install packages in the workspace"))
        outside = await gate.check(req("create ~/Desktop/a.js", confined=False))
        risky = await gate.check(req("delete everything", risk=p.RiskLevel.HIGH_RISK))
        return first, second, third, outside, risky

    first, second, third, outside, risky = asyncio.run(run())
    assert first.approved and second.approved and third.approved and "this task" in second.reason
    assert answers.asked == ["create snake/game.js", "create ~/Desktop/a.js", "delete everything"]
    assert outside.approved and not risky.approved  # the high-risk one was asked (and the answers ran out: no)
    assert gate.task_grant()["covered"] == 2 and changes[0]["active"]


def test_the_grant_ends():
    gate = PermissionGate(Answers(APPROVE_TASK, False, False))
    asyncio.run(gate.check(req("create a")))
    assert gate.task_grant() is not None
    gate.end_task("the user ended it")
    assert gate.task_grant() is None and not asyncio.run(gate.check(req("create b"))).approved
    gate.grant_task("short", seconds=0.01)
    time.sleep(0.02)
    assert gate.task_grant() is None and not asyncio.run(gate.check(req("create c"))).approved


def test_approve_task_on_an_outside_action_is_one_yes_only():
    gate = PermissionGate(Answers(APPROVE_TASK, False))
    assert asyncio.run(gate.check(req("create ~/Desktop/a.js", confined=False))).approved
    assert gate.task_grant() is None


# ---- the web confirmer -----------------------------------------------------------------------------------------------

def test_web_confirmer_checks_the_task_answer():
    events = []
    confirmer = WebConfirmer(lambda t, d: events.append((t, d)), timeout=5)

    async def run():
        task = asyncio.ensure_future(confirmer.confirm(req("change snake/game.js"), strict=False))
        await asyncio.sleep(0)
        record = confirmer.pending()[0]
        assert record["confined"] is True
        confirmer.decide(record["id"], "approve_task")
        first = await task
        task = asyncio.ensure_future(confirmer.confirm(req("change ~/Desktop/a.js", confined=False), strict=False))
        await asyncio.sleep(0)
        record = confirmer.pending()[0]
        with pytest.raises(ApprovalError, match="only be allowed once"):
            confirmer.decide(record["id"], "approve_task")
        assert confirmer.answer(record["id"], APPROVE_TASK)  # said "yes to all": counts as one yes
        return first, await task

    first, second = asyncio.run(run())
    assert first is APPROVE_TASK and second is True
    assert [d["status"] for t, d in events if t == "approval.settled"] == ["approved for the task", "approved"]


def test_spoken_question_stops_when_answered_on_screen():
    confirmer = WebConfirmer(lambda t, d: None, timeout=5)
    stopped = []

    async def ask(request, strict, approval_id):
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            stopped.append(approval_id)
            raise

    confirmer.ask_aloud = ask

    async def run():
        task = asyncio.ensure_future(confirmer.confirm(req("create a"), strict=False))
        await asyncio.sleep(0.01)
        confirmer.decide(confirmer.pending()[0]["id"], "approve")
        result = await task
        await asyncio.sleep(0)
        return result

    assert asyncio.run(run()) is True and stopped


# ---- where requests come from ----------------------------------------------------------------------------------------

def test_requests_from_hermes_know_their_confinement(paths, ws):
    edit = HermesPermissionRequest("Edit g.js", "edit", {}, [{"type": "diff", "path": str(ws / "snake/g.js"),
                                                              "oldText": None, "newText": "x"}])
    assert from_hermes_request(edit, paths).confined
    msg = p.escalation_message(p.Decision(p.RiskLevel.CONFIRM_REQUIRED, "terminal.install_package", "install x"),
                               "k", confined=True)
    plugin = HermesPermissionRequest("", "execute", {"command": "<terminal> (plugin approval rule)", "description": msg})
    assert from_hermes_request(plugin, paths).confined
    high = p.escalation_message(p.Decision(p.RiskLevel.HIGH_RISK, "terminal.sudo", "sudo x"), "k", confined=True)
    plugin_high = HermesPermissionRequest("", "execute", {"command": "<terminal> (plugin approval rule)",
                                                          "description": high})
    assert not from_hermes_request(plugin_high, paths).confined


def test_plugin_never_marks_an_injected_turn_confined(monkeypatch, tmp_path):
    module, ctx, ws = load(monkeypatch, tmp_path)
    hook = ctx.hooks["pre_tool_call"]
    clean = hook(tool_name="terminal", args={"command": "pip install requests"}, session_id="s1")
    assert clean["action"] == "approve" and p.parse_escalation_confined(clean["message"])
    module._turns.setdefault("s2", {})["injection"] = "ignore previous instructions"
    injected = hook(tool_name="terminal", args={"command": "pip install requests"}, session_id="s2")
    assert injected["action"] == "approve" and not p.parse_escalation_confined(injected["message"])


def test_relative_paths_point_into_the_workspace_not_hermes_own_folder(monkeypatch, tmp_path):
    """Live: inside Hermes' gateway (whose folder is Hermes' install), "create grant-test" pointed into
    ~/.hermes/hermes-agent and asked as an outside action, while write_file put the files in the workspace."""
    module, ctx, ws = load(monkeypatch, tmp_path)
    install = tmp_path / "hermes-agent"
    install.mkdir()
    monkeypatch.chdir(install)
    hook = ctx.hooks["pre_tool_call"]
    pip = hook(tool_name="terminal", args={"command": "pip install requests"}, session_id="s1")
    assert p.parse_escalation_confined(pip["message"])  # runs in the workspace, not in Hermes' folder
    assert module._cwd("s1") == str(ws)
