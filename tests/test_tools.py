"""XENIO filesystem tools + registry: structured results, validation, permission flow."""

import json
import subprocess
from pathlib import Path

import pytest

from app.safety import policy as p
from app.safety.permissions import PermissionGate
from app.tools import base, filesystem
from app.tools.base import ToolContext, ToolResult
from app.tools.registry import ToolRegistry


@pytest.fixture
def ctx(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return ToolContext(paths=p.PathPolicy(workspace=ws), cwd=str(ws))


def tool(name):
    return next(t for t in filesystem.TOOLS if t.hermes_name == name)


def test_create_list_inspect(ctx):
    ws = ctx.paths.workspace
    created = tool("xenio_create_directory").run({"path": str(ws / "XenioTest")}, ctx)
    assert created.success and created.data["created"] is True and (ws / "XenioTest").is_dir()
    again = tool("xenio_create_directory").run({"path": "XenioTest"}, ctx)  # relative to cwd
    assert again.success and again.data["created"] is False
    (ws / "XenioTest" / "hello.txt").write_text("Hello from Xenio", encoding="utf-8")
    listing = tool("xenio_list_directory").run({"path": str(ws / "XenioTest")}, ctx)
    assert listing.success and [e["name"] for e in listing.data["entries"]] == ["hello.txt"]
    assert "hello.txt" in listing.message
    info = tool("xenio_inspect_path").run({"path": str(ws / "XenioTest" / "hello.txt")}, ctx)
    assert info.success and info.data["type"] == "file" and info.data["size_bytes"] == 16
    assert "Hello" not in json.dumps(info.to_dict())  # never file contents


def test_results_are_structured_json(ctx):
    ok = tool("xenio_create_directory").run({"path": "a"}, ctx).to_dict()
    assert set(ok) == {"success", "tool", "message", "data"} and ok["success"] is True
    bad = tool("xenio_list_directory").run({"path": "does-not-exist"}, ctx).to_dict()
    assert bad == {"success": False, "tool": "filesystem.list_directory", "error": bad["error"]}
    assert "not found" in bad["error"].lower()


def test_failures_are_never_reported_as_success(ctx):
    ws = ctx.paths.workspace
    (ws / "taken").write_text("x")
    r = tool("xenio_create_directory").run({"path": "taken"}, ctx)
    assert not r.success and "file with that name" in r.error
    assert not tool("xenio_delete_path").run({"path": "ghost", "permanent": True}, ctx).success
    assert not tool("xenio_move_path").run({"source": "ghost", "destination": "x"}, ctx).success


def test_move_and_delete(ctx):
    ws = ctx.paths.workspace
    (ws / "a.txt").write_text("x")
    moved = tool("xenio_move_path").run({"source": "a.txt", "destination": "b.txt"}, ctx)
    assert moved.success and (ws / "b.txt").exists() and not (ws / "a.txt").exists()
    (ws / "c.txt").write_text("y")
    refused = tool("xenio_move_path").run({"source": "b.txt", "destination": "c.txt"}, ctx)
    assert not refused.success and (ws / "b.txt").exists()  # no silent overwrite
    (ws / "d" / "e").mkdir(parents=True)
    gone = tool("xenio_delete_path").run({"path": "d", "permanent": True}, ctx)
    assert gone.success and not (ws / "d").exists() and gone.data["recycled"] is False


def test_delete_defaults_to_recycle_bin(ctx, monkeypatch):
    ws = ctx.paths.workspace
    (ws / "trash.txt").write_text("x")
    calls = []

    def fake_recycle(path):
        calls.append(path)
        path.unlink()

    monkeypatch.setattr(filesystem, "_recycle", fake_recycle)
    monkeypatch.setattr(filesystem, "_trash_supported", lambda: True)
    r = tool("xenio_delete_path").run({"path": "trash.txt"}, ctx)
    assert r.success and r.data["recycled"] is True and calls == [ws / "trash.txt"]


def test_macos_trash_uses_the_system_trash_command(ctx, monkeypatch):
    ws = ctx.paths.workspace
    (ws / "old.txt").write_text("x")
    seen = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        Path(cmd[-1]).unlink()
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(filesystem.sys, "platform", "darwin")
    monkeypatch.setattr(filesystem, "_trash_supported", lambda: True)
    monkeypatch.setattr(filesystem.subprocess, "run", fake_run)
    r = tool("xenio_delete_path").run({"path": "old.txt"}, ctx)
    assert r.success and r.data["recycled"] is True
    assert seen == [[filesystem.MACOS_TRASH, "-s", "--", str(ws / "old.txt")]]


def test_a_failed_trash_is_reported_and_the_file_kept(ctx, monkeypatch):
    ws = ctx.paths.workspace
    (ws / "keep.txt").write_text("x")
    monkeypatch.setattr(filesystem.sys, "platform", "darwin")
    monkeypatch.setattr(filesystem.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "permission denied"))
    with pytest.raises(OSError):
        filesystem._recycle(ws / "keep.txt")
    assert (ws / "keep.txt").exists()


def test_policy_is_enforced_inside_the_tools(ctx, tmp_path):
    ws = ctx.paths.workspace
    (ws / ".env").write_text("SECRET=1")
    assert not tool("xenio_inspect_path").run({"path": ".env"}, ctx).success
    r = tool("xenio_delete_path").run({"path": str(ws), "permanent": True}, ctx)
    assert not r.success and "Blocked" in r.error and ws.exists()  # the workspace root itself
    traversal = tool("xenio_list_directory").run({"path": "../../../../../../.ssh"}, ctx)
    assert not traversal.success


@pytest.mark.parametrize("args,message", [
    ({}, "'path' is required"), ({"path": ""}, "'path' is required"), ({"path": 5}, "must be of type string"),
    ({"path": "x", "bogus": 1}, "unknown argument"), ("not-a-dict", "must be an object"),
])
def test_malformed_arguments(ctx, args, message):
    r = tool("xenio_create_directory").run(args, ctx)
    assert not r.success and message in r.error


def test_os_errors_become_failures(ctx, monkeypatch):
    def boom(*_a, **_k):
        raise PermissionError(13, "Access is denied", "C:/locked")

    monkeypatch.setattr(Path, "mkdir", boom)
    r = tool("xenio_create_directory").run({"path": "new"}, ctx)
    assert not r.success and "Permission denied" in r.error


def test_audit_events(ctx, monkeypatch):
    events = []
    monkeypatch.setattr(base, "AUDIT", lambda name, fields: events.append(name))
    tool("xenio_create_directory").run({"path": "z"}, ctx)
    assert events == ["TOOL_EXECUTION_STARTED", "TOOL_EXECUTION_COMPLETED"]


class Answer:
    def __init__(self, approved):
        self.approved, self.asked = approved, 0

    async def confirm(self, request, *, strict):
        self.asked += 1
        return self.approved


async def test_registry_discovery_and_execution(ctx):
    reg = ToolRegistry(ctx.paths, gate=PermissionGate(Answer(True)))
    names = {t["name"] for t in reg.describe()}
    assert {"filesystem.create_directory", "filesystem.delete_path", "filesystem.read_file", "terminal.run"} <= names
    assert {t["backend"] for t in reg.describe()} == {"xenio", "hermes"}
    r = await reg.execute("filesystem.create_directory", {"path": "made"}, cwd=str(ctx.paths.workspace))
    assert r.success and (ctx.paths.workspace / "made").is_dir()
    hermes_backed = await reg.execute("terminal.run", {"command": "ls"})
    assert not hermes_backed.success and "Hermes" in hermes_backed.error
    assert not (await reg.execute("nope.tool", {})).success


async def test_registry_confirmation_yes_and_no(ctx):
    ws = ctx.paths.workspace
    (ws / "x").mkdir()
    no = Answer(False)
    r = await ToolRegistry(ctx.paths, gate=PermissionGate(no)).execute(
        "filesystem.delete_path", {"path": "x", "permanent": True}, cwd=str(ws))
    assert not r.success and no.asked == 1 and (ws / "x").exists()  # cancelled -> nothing happened
    yes = Answer(True)
    r = await ToolRegistry(ctx.paths, gate=PermissionGate(yes)).execute(
        "filesystem.delete_path", {"path": "x", "permanent": True}, cwd=str(ws))
    assert r.success and yes.asked == 1 and not (ws / "x").exists()


async def test_registry_blocks_without_asking(ctx):
    asked = Answer(True)
    r = await ToolRegistry(ctx.paths, gate=PermissionGate(asked)).execute("filesystem.list_directory", {"path": "~/.ssh"})
    assert not r.success and asked.asked == 0


def test_tool_result_helpers():
    assert ToolResult.ok("t", "done", a=1).to_dict() == {"success": True, "tool": "t", "message": "done", "data": {"a": 1}}
    assert ToolResult.fail("t", "nope").to_dict() == {"success": False, "tool": "t", "error": "nope"}
