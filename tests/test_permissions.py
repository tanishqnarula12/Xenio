"""XENIO-side permission engine: Hermes prompts -> requests -> gate -> answers."""

import asyncio
from pathlib import Path

import pytest

from app.hermes.client import HermesPermissionRequest
from app.safety import policy as p
from app.safety.permissions import (
    PermissionGate,
    PermissionRequest,
    RiskLevel,
    classify_action,
    from_hermes_request,
    interpret_answer,
)

DESKTOP = p.known_folders()["Desktop"]


@pytest.fixture
def pp(tmp_path):
    return p.PathPolicy(workspace=tmp_path / "ws")


class Scripted:
    def __init__(self, answer=True, delay=0.0, error=None):
        self.answer, self.delay, self.error, self.calls = answer, delay, error, []

    async def confirm(self, request, *, strict):
        self.calls.append((request, strict))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.answer


def test_named_actions_from_the_spec():
    assert classify_action("read_file") is RiskLevel.READ
    assert classify_action("git_status") is RiskLevel.READ
    assert classify_action("create_directory") is RiskLevel.SAFE_WRITE
    assert classify_action("create_project_file") is RiskLevel.SAFE_WRITE
    assert classify_action("git_push") is RiskLevel.CONFIRM_REQUIRED
    assert classify_action("install_package") is RiskLevel.CONFIRM_REQUIRED
    assert classify_action("delete_file") is RiskLevel.CONFIRM_REQUIRED
    assert classify_action("production_database_operation") is RiskLevel.HIGH_RISK
    assert classify_action("credential_change") is RiskLevel.HIGH_RISK
    assert classify_action("something_new") is RiskLevel.CONFIRM_REQUIRED


def test_edit_requests_use_the_path_policy(pp):
    new_file = HermesPermissionRequest("Approve edit", "edit",
                                       content=[{"type": "diff", "path": str(DESKTOP / "XenioTest/hello.txt"), "oldText": None}])
    r = from_hermes_request(new_file, pp)
    assert r.risk is RiskLevel.SAFE_WRITE and "hello.txt" in r.description
    secret = HermesPermissionRequest("Approve edit", "edit", content=[{"type": "diff", "path": "C:/p/.env", "oldText": None}])
    assert from_hermes_request(secret, pp).blocked


def test_plugin_escalations_are_parsed(pp):
    msg = p.escalation_message(p.Decision(RiskLevel.HIGH_RISK, "terminal.x", "format your disk"), "terminal:format C:")
    req = HermesPermissionRequest("x", "execute", raw_input={"command": "<terminal> (plugin approval rule)", "description": msg})
    r = from_hermes_request(req, pp)
    assert (r.risk, r.description, r.dedupe_key) == (RiskLevel.HIGH_RISK, "format your disk", "terminal:format C:")


def test_hermes_detector_prompts_are_reclassified(pp):
    req = HermesPermissionRequest("x", "execute", raw_input={"command": "git status", "description": "flagged"})
    r = from_hermes_request(req, pp)
    assert r.risk is RiskLevel.CONFIRM_REQUIRED  # never below confirm when Hermes itself flagged it
    assert r.dedupe_key == "terminal:git status"
    blocked = from_hermes_request(HermesPermissionRequest("x", "execute", raw_input={"command": "cat ~/.ssh/id_rsa"}), pp)
    assert blocked.blocked


async def test_gate_auto_confirm_strict_blocked():
    confirmer = Scripted(True)
    gate = PermissionGate(confirmer)
    assert (await gate.check(PermissionRequest("x", "read", RiskLevel.READ))).approved
    assert (await gate.check(PermissionRequest("x", "write", RiskLevel.SAFE_WRITE))).approved
    assert confirmer.calls == []
    assert (await gate.check(PermissionRequest("x", "push", RiskLevel.CONFIRM_REQUIRED))).approved
    assert (await gate.check(PermissionRequest("x", "format", RiskLevel.HIGH_RISK))).approved
    assert [s for _, s in confirmer.calls] == [False, True]
    verdict = await gate.check(PermissionRequest("x", "read key", RiskLevel.READ, blocked=True, reason="secret"))
    assert not verdict.approved and "secret" in verdict.reason


async def test_gate_does_not_ask_twice_for_the_same_command():
    confirmer = Scripted(True)
    gate = PermissionGate(confirmer)
    req = PermissionRequest("x", "push", RiskLevel.CONFIRM_REQUIRED, dedupe_key="terminal:git push")
    assert (await gate.check(req)).approved
    assert (await gate.check(req)).approved  # Hermes' own prompt for the same command
    assert len(confirmer.calls) == 1


async def test_approving_a_folder_delete_covers_its_contents_and_retries(pp):
    folder = DESKTOP / "XenioTest"
    confirmer = Scripted(True)
    gate = PermissionGate(confirmer)

    def request(command):
        d = p.classify_command(command, pp)
        return PermissionRequest(d.action, d.summary, d.level, scopes=d.scopes, dedupe_key=f"terminal:{command}")

    assert (await gate.check(request(f'rm -r "{folder}"'))).approved
    assert (await gate.check(request(f'rm "{folder}/hello.txt"'))).approved  # inside the approved folder
    assert (await gate.check(request(f'rm -rf "{folder}"'))).approved  # a retry with other flags
    assert len(confirmer.calls) == 1
    assert (await gate.check(request(f'rm -r "{DESKTOP / "Other"}"'))).approved  # different target: asks again
    assert len(confirmer.calls) == 2


async def test_scope_approval_never_covers_high_risk(pp):
    confirmer = Scripted(True)
    gate = PermissionGate(confirmer)
    approved = PermissionRequest("x", "delete", RiskLevel.CONFIRM_REQUIRED, scopes=("delete:C:\\data",))
    assert (await gate.check(approved)).approved
    high = PermissionRequest("y", "delete", RiskLevel.HIGH_RISK, scopes=("delete:C:\\data\\x",))
    assert (await gate.check(high)).approved and len(confirmer.calls) == 2


def test_scopes_travel_through_plugin_escalations(pp):
    d = pp.check(str(DESKTOP / "XenioTest"), "delete")
    msg = p.escalation_message(d, "terminal:rm -r x")
    req = HermesPermissionRequest("x", "execute", raw_input={"command": "<terminal> (plugin approval rule)", "description": msg})
    r = from_hermes_request(req, pp)
    assert r.scopes == (f"delete:{DESKTOP / 'XenioTest'}",) and r.dedupe_key == "terminal:rm -r x"
    assert p.scope_covers(r.scopes[0], f"delete:{DESKTOP / 'XenioTest' / 'a.txt'}")
    assert not p.scope_covers(r.scopes[0], f"delete:{DESKTOP / 'XenioTest2'}")
    assert not p.scope_covers(r.scopes[0], f"move:{DESKTOP / 'XenioTest'}")


async def test_gate_fails_closed():
    req = PermissionRequest("x", "push", RiskLevel.CONFIRM_REQUIRED)
    assert not (await PermissionGate(None).check(req)).approved
    assert not (await PermissionGate(Scripted(False)).check(req)).approved
    assert not (await PermissionGate(Scripted(error=RuntimeError("mic died"))).check(req)).approved
    assert not (await PermissionGate(Scripted(delay=1), timeout=0.05).check(req)).approved


@pytest.mark.parametrize("answer,expected", [
    ("yes", "yes"), ("Yes.", "yes"), ("yeah go ahead", "yes"), ("Sure, do it", "yes"), ("Yes, delete the folder", "yes"),
    ("okay", "yes"), ("no", "no"), ("No, don't.", "no"), ("cancel", "no"), ("stop", "no"), ("wait", "no"),
    ("yes... actually no", "no"), ("I'm not sure", "no"), ("", "unclear"), ("hmm", "unclear"),
    ("what's the weather like today", "unclear"), ("okay what is the weather like in Delhi today", "unclear"),
])
def test_interpret_answer(answer, expected):
    assert interpret_answer(answer) == expected


def test_interpret_strict():
    assert interpret_answer("confirm", strict=True) == "yes"
    assert interpret_answer("I confirm", strict=True) == "yes"
    assert interpret_answer("yes", strict=True) == "unclear"  # high risk needs the word confirm
    assert interpret_answer("don't confirm", strict=True) == "no"
