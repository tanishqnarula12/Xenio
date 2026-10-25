"""XENIO V3 (Mission Control): memory the user can correct and delete (under Hermes' own lock and format), the context
engine ("continue where we left off" from XENIO's own records), the tool-call audit, activity paging and retention,
approvals that explain themselves, skills on/off through Hermes' config, the run diff, and the new health checks.
Every test uses the real modules; only Hermes itself is replaced (a fake ``hermes`` command for its config)."""

import json
import os
import stat
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.context import ContextEngine, wants_resume
from app.memory.files import ENTRY_DELIMITER, MemoryEditError, MemoryFiles, configured_limits, parse
from app.safety.permissions import PermissionRequest
from app.safety.policy import RiskLevel, classify_tool_call, default_path_policy
from app.ui.approvals import requested_by, scope_note, targets_of, why_asked
from app.ui.audit import tool_calls
from app.ui.events import EventHub
from tests.test_ui_backend import call, stack  # noqa: F401 - fixtures
from tests.test_engineering_service import repo, settings  # noqa: F401 - fixtures


# ---- memory: Hermes' files, Hermes' lock, XENIO's policy -------------------------------------------------------------

def _memory(tmp_path, user=(), notes=()):
    home = tmp_path / "hermes"
    (home / "memories").mkdir(parents=True)
    if user:
        (home / "memories" / "USER.md").write_text(ENTRY_DELIMITER.join(user), encoding="utf-8")
    if notes:
        (home / "memories" / "MEMORY.md").write_text(ENTRY_DELIMITER.join(notes), encoding="utf-8")
    return MemoryFiles(home), home


def test_memory_edits_keep_hermes_format_and_owner_only_files(tmp_path):
    files, home = _memory(tmp_path, user=["Name is Tanishq", "Prefers short answers"])
    files.replace("user", "Prefers short answers", "Prefers short answers in the morning")
    files.add("user", "Works on the snake game")
    files.remove("user", "Name is Tanishq")
    path = home / "memories" / "USER.md"
    assert path.read_text(encoding="utf-8") == "Prefers short answers in the morning\n§\nWorks on the snake game"
    assert parse(path.read_text(encoding="utf-8")) == files.entries("user")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not [p for p in path.parent.iterdir() if p.name.startswith(".mem_")]  # no temp file left behind


def test_memory_parse_matches_hermes(tmp_path):
    raw = "﻿  first  \n§\n\n§\nsecond § stays\n§\nfirst"
    assert parse(raw.lstrip("﻿")) == ["first", "second § stays"]  # stripped, empties dropped, a bare § kept, deduped


def test_memory_edits_wait_for_hermes_lock(tmp_path):
    """While Hermes holds USER.md.lock (another process), XENIO's change waits - so neither loses the other's entry."""
    files, home = _memory(tmp_path, user=["Prefers tea"])
    lock = home / "memories" / "USER.md.lock"
    holder = subprocess.Popen([sys.executable, "-c", (
        "import fcntl, os, sys, time\n"
        f"fd = os.open({str(lock)!r}, os.O_RDWR | os.O_CREAT, 0o600)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "print('locked', flush=True)\n"
        "time.sleep(0.6)\n"
        f"open({str(home / 'memories' / 'USER.md')!r}, 'a').write('\\n§\\nAdded by Hermes')\n")],
        stdout=subprocess.PIPE, text=True)
    assert holder.stdout.readline().strip() == "locked"
    started = time.monotonic()
    files.add("user", "Lives in Delhi")
    waited = time.monotonic() - started
    holder.wait(5)
    assert waited >= 0.4  # it waited for the lock
    assert files.entries("user") == ["Prefers tea", "Added by Hermes", "Lives in Delhi"]  # re-read under the lock


def test_memory_policy_holds_for_the_users_own_edits(tmp_path):
    files, home = _memory(tmp_path, user=["Prefers short answers"])
    with pytest.raises(MemoryEditError, match="secret"):
        files.add("user", "my openai key is sk-proj-abcdefghijklmnopqrstuvwxyz0123456789")
    with pytest.raises(MemoryEditError, match="same thing"):
        files.add("user", "Prefers short answers")
    with pytest.raises(MemoryEditError, match="400"):
        files.add("user", "x " * 300)
    with pytest.raises(MemoryEditError, match="not in memory any more") as gone:
        files.remove("user", "something Hermes already removed")
    assert gone.value.status == 409
    (home / "config.yaml").write_text("memory:\n  user_char_limit: 40\n  memory_char_limit: 900\n", encoding="utf-8")
    assert configured_limits(home) == {"memory": 900, "user": 40}
    with pytest.raises(MemoryEditError, match="limit"):
        files.add("user", "Works on a very long project name here")
    assert files.entries("user") == ["Prefers short answers"]  # nothing changed by any refusal


def test_memory_over_http_by_id_never_logs_the_text(stack, tmp_path):  # noqa: F811
    mem = stack.ctl.hermes_home / "memories"
    mem.mkdir(parents=True)
    (mem / "USER.md").write_text("Prefers tea", encoding="utf-8")
    status, data, _ = call(stack.server, "GET", "/api/v1/memory")
    assert status == 200 and data["editable"] and data["usage"]["user"]["used"] == len("Prefers tea")
    entry = data["entries"][0]
    assert call(stack.server, "POST", "/api/v1/memory", {"action": "correct", "id": entry["id"], "text": "Prefers green tea"})[0] == 200
    assert call(stack.server, "POST", "/api/v1/memory", {"action": "add", "store": "memory", "text": "Home server is atlas"})[0] == 200
    assert call(stack.server, "POST", "/api/v1/memory", {"action": "delete", "id": entry["id"]})[0] == 409  # changed since
    _, data, _ = call(stack.server, "GET", "/api/v1/memory")
    assert {(e["store"], e["text"]) for e in data["entries"]} == {("user", "Prefers green tea"), ("memory", "Home server is atlas")}
    assert call(stack.server, "POST", "/api/v1/memory", {"action": "add", "store": "elsewhere", "text": "x"})[0] == 422
    assert call(stack.server, "POST", "/api/v1/memory", {"action": "wipe"})[0] == 422
    log = (tmp_path / "ui" / "activity.jsonl").read_text(encoding="utf-8")
    assert '"memory.changed"' in log and "green tea" not in log and "atlas" not in log  # what changed, never the text


def test_running_sessions_are_told_about_removed_memories(monkeypatch, tmp_path):
    from tests.test_guard_plugin import load

    (tmp_path / "hermes" / "memories").mkdir(parents=True)
    (tmp_path / "hermes" / "memories" / "USER.md").write_text("Lives in Mumbai\n§\nPrefers tea", encoding="utf-8")
    module, ctx, _ws = load(monkeypatch, tmp_path)
    MemoryFiles(tmp_path / "hermes").remove("user", "Lives in Mumbai")
    note = module._personal_note()
    assert "no longer in memory" in note and "Lives in Mumbai" in note and "Prefers tea" not in note


# ---- the context engine ----------------------------------------------------------------------------------------------

def _run(root: Path, rid: str, **fields):
    data = {"id": rid, "request": "Build a snake game", "source": "/Users/me/Xenio/snake", "mode": "change", "status": "done",
            "created": "2026-10-24T10:00:00", "updated": "2026-10-24T10:30:00", "project": "snake", "tasks": [], "events": [],
            "final_tests": [], "security_findings": [], "reviews": {}, "verified": False, "outcome": ""} | fields
    (root / rid).mkdir(parents=True, exist_ok=True)
    (root / rid / "run.json").write_text(json.dumps(data), encoding="utf-8")
    return data


NOW = datetime(2026, 10, 24, 12, 0, 0)


def test_continue_picks_the_live_work_and_says_what_is_next(tmp_path):
    root = tmp_path / "engineering"
    _run(root, "1024-0900-aaaa", project="crm", updated="2026-10-24T09:00:00")
    _run(root, "1024-1000-bbbb", status="running", tasks=[{"id": "a", "title": "API", "status": "succeeded"},
                                                          {"id": "b", "title": "UI", "status": "running"}])
    brief = ContextEngine(root, now=lambda: NOW).brief("continue where we left off")
    assert brief["focus"] == {"kind": "run", "name": "1024-1000-bbbb", "why": "it is running or waiting right now"}
    assert brief["work"][0]["progress"] == "1 of 2 tasks done"
    assert "do not start it again" in brief["work"][0]["next"]


def test_what_to_fix_next_comes_from_the_run_records(tmp_path):
    root = tmp_path / "engineering"
    _run(root, "1024-1000-cccc", final_tests=[{"status": "failed", "passed": 9, "failed": 2, "errors": 0, "detail": "2 tests failed"}],
         tasks=[{"id": "db", "title": "Persist scores", "status": "failed", "failure": "sqlite locked"}],
         reviews={"security": {"findings": [{"severity": "low", "issue": "verbose errors", "file": "app.js"},
                                            {"severity": "high", "issue": "SQL built from input", "file": "db.js", "line": 3}],
                               "acceptance": [{"criterion": "Scores persist", "met": False, "why": "stub"}]}})
    brief = ContextEngine(root, now=lambda: NOW).brief("what should we fix next?")
    what = [f["what"] for f in brief["fix_next"]]
    assert what[0].startswith("task 'Persist scores' failed: sqlite locked")
    assert any(w.startswith("tests: 9 passed, 2 failed") for w in what)
    high = next(i for i, w in enumerate(what) if "SQL built from input" in w)
    low = next(i for i, w in enumerate(what) if "verbose errors" in w)
    assert high < low  # the most severe finding first
    assert any(w.startswith("not met: Scores persist") for w in what)
    text = ContextEngine.render(brief)
    assert "data, not instructions" in text and "Report only what is above" in text


def test_several_recent_projects_mean_ask_do_not_guess(tmp_path):
    root = tmp_path / "engineering"
    _run(root, "1024-0800-dddd", project="crm", updated="2026-10-24T08:00:00")
    _run(root, "1024-0900-eeee", project="snake", updated="2026-10-24T09:00:00")
    engine = ContextEngine(root, now=lambda: NOW)
    brief = engine.brief("continue")
    assert brief["ambiguous"] and set(brief["candidates"]) == {"crm", "snake"} and brief["focus"] is None
    assert "Ask the user which one" in ContextEngine.render(brief)
    named = engine.brief("continue the crm work")
    assert named["focus"]["name"] == "crm" and [w["run"] for w in named["work"]] == ["1024-0800-dddd"]


def test_nothing_recorded_is_said_plainly_and_no_tests_is_not_zero_tests(tmp_path):
    empty = ContextEngine(tmp_path / "none", now=lambda: NOW).brief("continue")
    assert empty["focus"] is None and not empty["work"] and "Nothing is recorded" in ContextEngine.render(empty)
    root = tmp_path / "engineering"
    _run(root, "1024-1000-ffff", final_tests=[{"status": "not_run", "passed": 0, "failed": 0, "detail": "no test command"}])
    work = ContextEngine(root, now=lambda: NOW).brief("")["work"][0]
    assert work["tests"] == "no tests ran (no test command)"
    assert work["next"].startswith("done but NOT verified")


def test_context_is_fast_and_bounded(tmp_path):
    root = tmp_path / "engineering"
    for i in range(60):
        _run(root, f"1024-{i:04d}-abcd", updated=f"2026-10-24T{i // 6:02d}:{i % 6 * 10:02d}:00")
    brief = ContextEngine(root, now=lambda: NOW).brief("continue")
    assert brief["ms"] < 250  # measured: a few ms on this Mac; only the newest 30 run files are read
    assert len(brief["work"]) <= 3


def test_resume_phrases():
    for text in ("continue where we left off", "resume the snake work", "what should we fix next?", "what's next",
                 "how far did we get", "pick it up from there"):
        assert wants_resume(text), text
    for text in ("open spotify", "remind me at 6", "what is the weather", "continue playing music on spotify"):
        assert not wants_resume(text) or text.startswith("continue"), text


def test_resume_note_and_tool_inside_hermes(monkeypatch, tmp_path):
    from tests.test_guard_plugin import load

    module, ctx, ws = load(monkeypatch, tmp_path)
    _run(ws / "engineering", "1024-1000-gggg", status="planned", tasks=[{"id": "a", "title": "x", "status": "pending"},
                                                                         {"id": "b", "title": "y", "status": "pending"}])
    assert module._resume_note("open spotify") == ""
    note = module._resume_note("continue where we left off")
    assert note.startswith("[XENIO resume]") and "1024-1000-gggg" in note and "go-ahead" in note
    _toolset, schema, handler = ctx.tools["xenio_resume"]
    out = json.loads(handler({"about": ""}, session_id="s1"))
    assert out["success"] and out["data"]["untrusted_content"] and out["data"]["work"][0]["run"] == "1024-1000-gggg"
    decision = classify_tool_call("xenio_resume", {}, default_path_policy())
    assert decision.level == RiskLevel.READ


# ---- the tool-call audit --------------------------------------------------------------------------------------------

def test_tool_calls_tell_requested_blocked_approved_and_unproven_apart(tmp_path):
    log = tmp_path / "xenio-guard.log"
    lines = [
        {"event": "TOOL_REQUESTED", "tool": "terminal"},
        {"event": "PERMISSION_CHECK", "tool": "terminal", "level": "READ", "blocked": False, "summary": "run: ls"},
        {"event": "TOOL_REQUESTED", "tool": "xenio_delete_path"},
        {"event": "PERMISSION_CHECK", "tool": "xenio_delete_path", "level": "HIGH_RISK", "blocked": True, "summary": "delete /"},
        {"event": "TOOL_REQUESTED", "tool": "xenio_gui"},
        {"event": "PERMISSION_CHECK", "tool": "xenio_gui", "level": "CONFIRM_REQUIRED", "blocked": False, "summary": "type «secret words» into Notes"},
        {"event": "CONFIRMATION_REQUESTED", "tool": "xenio_gui"},
        {"event": "CONFIRMATION_RECEIVED", "rule": "xenio_gui", "choice": "once"},
        {"event": "TOOL_EXECUTION_STARTED", "tool": "desktop.gui"},
        {"event": "TOOL_EXECUTION_COMPLETED", "tool": "desktop.gui", "ms": 812},
        {"event": "TOOL_REQUESTED", "tool": "xenio_schedule"},
        {"event": "PERMISSION_CHECK", "tool": "xenio_schedule", "level": "CONFIRM_REQUIRED", "blocked": False, "summary": "remind"},
        {"event": "CONFIRMATION_REQUESTED", "tool": "xenio_schedule"},
        {"event": "CONFIRMATION_RECEIVED", "rule": "xenio_schedule", "choice": "timeout"},
        {"event": "TOOL_REQUESTED", "tool": "xenio_open_url"},
        {"event": "PERMISSION_CHECK", "tool": "xenio_open_url", "level": "SAFE_WRITE", "blocked": False, "summary": "open a page"},
        {"event": "TOOL_EXECUTION_FAILED", "tool": "web.open_url", "error": "no browser"},
    ]
    log.write_text("\n".join(json.dumps({"time": f"2026-10-24T10:00:{i:02d}", **e}) for i, e in enumerate(lines)), encoding="utf-8")
    calls = {c["tool"]: c for c in tool_calls(log)}
    assert calls["terminal"]["status"] == "allowed"  # Hermes' own tool: no result is reported, so never "completed"
    assert calls["xenio_delete_path"]["status"] == "blocked"
    assert calls["xenio_gui"]["status"] == "completed" and calls["xenio_gui"]["ms"] == 812
    assert "secret words" not in calls["xenio_gui"]["summary"]  # typed text never leaves the log
    assert calls["xenio_schedule"]["status"] == "expired"  # silence is not a yes
    assert calls["xenio_open_url"]["status"] == "failed" and calls["xenio_open_url"]["detail"] == "no browser"
    assert [c["tool"] for c in tool_calls(log)][0] == "xenio_open_url"  # newest first


# ---- activity: kinds, paging, retention, system events ---------------------------------------------------------------

def test_activity_pages_filters_and_prunes(tmp_path):
    hub = EventHub(tmp_path / "activity.jsonl")
    for i in range(7):
        hub.publish("task.changed", {"id": i, "change": "created"})
    hub.publish("system.event", {"what": "XENIO started"})
    hub.publish("chat.chunk", {"text": "not kept"})
    first = hub.activity(3)
    assert [e["type"] for e in first] == ["system.event", "task.changed", "task.changed"]
    second = hub.activity(3, offset=3)
    assert [e["data"]["id"] for e in second] == [4, 3, 2]
    assert [e["type"] for e in hub.activity(10, kind="system")] == ["system.event"]
    assert len(hub.activity(50, kind="changes")) == 7
    old = {"v": 1, "seq": 0, "id": "old", "type": "system.event", "at": (datetime.now() - timedelta(days=40)).isoformat(timespec="seconds"),
           "data": {"what": "long ago"}}
    with (tmp_path / "activity.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(old) + "\n")
    assert hub.prune(30) == 1
    assert all(e["id"] != "old" for e in hub.activity(100)) and len(hub.activity(100)) == 8


def test_activity_api_validates_and_records_starts_and_settings(stack):  # noqa: F811
    status, data, _ = call(stack.server, "GET", "/api/v1/activity?limit=2&offset=0&kind=system")
    assert status == 200 and data["events"][0]["data"]["what"] == "XENIO started"
    assert call(stack.server, "GET", "/api/v1/activity?limit=abc")[0] == 422
    assert call(stack.server, "GET", "/api/v1/activity?kind=secrets")[0] == 422
    assert call(stack.server, "PUT", "/api/v1/settings", {"activity_retention_days": 30, "text_size": "large"})[0] == 200
    assert call(stack.server, "PUT", "/api/v1/settings", {"activity_retention_days": 12})[0] == 422
    assert call(stack.server, "PUT", "/api/v1/settings", {"text_size": "large"})[0] == 200  # the same value again
    _, data, _ = call(stack.server, "GET", "/api/v1/activity?kind=changes")
    changes = [e for e in data["events"] if e["type"] == "settings.changed"]
    assert len(changes) == 1 and changes[0]["data"]["changed"] == ["activity_retention_days", "text_size"]


def test_system_records_voice_trouble_once_and_the_recovery(stack):  # noqa: F811
    from types import SimpleNamespace

    stack.ctl.voice_unavailable("the microphone stopped delivering audio")
    stack.ctl.voice_unavailable("the microphone stopped delivering audio")
    stack.ctl.attach_voice(SimpleNamespace(wake_enabled=True, wake_available=True))
    whats = [e["data"]["what"] for e in stack.hub.activity(20, kind="system")]
    assert whats.count("Voice stopped working: the microphone stopped delivering audio") == 1
    assert whats[0] == "Voice works again"


# ---- approvals explain themselves ------------------------------------------------------------------------------------

def test_approvals_say_why_what_who_and_how_long():
    req = PermissionRequest("terminal.install_package", "run: pip install requests", RiskLevel.CONFIRM_REQUIRED, "hermes",
                            {"command": "pip install requests", "engineering": {"run": "1024-1000-abcd", "task": "deps", "role": "backend"}},
                            confined=True)
    assert why_asked(req.action, False) == "Asked because it installs or runs software that comes from outside."
    assert targets_of(req) == ["command: pip install requests"]
    assert requested_by(req) == "the backend agent, engineering task deps (run 1024-1000-abcd)"
    assert "30 minutes" in scope_note(True, False) and "type confirm" in scope_note(False, True)
    delete = PermissionRequest("terminal.delete", "permanently delete old", RiskLevel.HIGH_RISK, "hermes",
                               scopes=("delete:/Users/me/old",))
    assert why_asked(delete.action, True) == "High risk: it permanently deletes something."
    assert targets_of(delete) == ["delete: /Users/me/old"] and requested_by(delete) == "XENIO, for your request"


def test_the_approval_record_carries_the_explanation(stack):  # noqa: F811
    import asyncio

    req = PermissionRequest("filesystem.modify", "change notes.txt", RiskLevel.CONFIRM_REQUIRED, "hermes",
                            {"paths": ["/Users/me/Xenio/notes.txt"]}, confined=True)
    fut = asyncio.run_coroutine_threadsafe(stack.gate.check(req), stack.loop)
    deadline = time.monotonic() + 5
    while not stack.ctl.confirmer.pending() and time.monotonic() < deadline:
        time.sleep(0.02)
    record = stack.ctl.confirmer.pending()[0]
    assert record["why"] == "Asked because it changes files." and record["targets"] == ["file: /Users/me/Xenio/notes.txt"]
    assert record["requested_by"] == "XENIO, for your request" and "Allow for this task" in record["scope"]
    stack.ctl.decide(record["id"], "deny")
    assert fut.result(5).approved is False


# ---- skills: availability and Hermes' own on/off ---------------------------------------------------------------------

FAKE_HERMES = """import json, sys
state = {state!r}
args = sys.argv[1:]
with open(state) as fh:
    data = json.load(fh)
data["calls"].append(args)
if args[:2] == ["config", "get"]:
    print(json.dumps(data["disabled"]) if data["disabled"] is not None else "Config key not set: skills.disabled")
elif args[:2] == ["config", "set"]:
    data["disabled"] = json.loads(args[3])
with open(state, "w") as fh:
    json.dump(data, fh)
"""


def _skills_home(tmp_path):
    home = tmp_path / "hermes"
    for category, name, extra in (("apple", "apple-notes", "platforms: [macos]\nprerequisites:\n  commands: [memo-not-here]\n"),
                                  ("autonomous-ai-agents", "hermes-agent", ""), ("web", "recover", "")):
        folder = home / "skills" / category / name
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: \"{name} skill\"\nversion: 1.0.0\n{extra}---\n# x\n")
    (home / "skills" / "web" / "recover" / "scripts").mkdir()
    (home / "skills" / "web" / "recover" / "scripts" / "go.py").write_text("print(1)")
    (home / "config.yaml").write_text("model: x\n")
    state = tmp_path / "hermes-state.json"
    state.write_text(json.dumps({"disabled": None, "calls": []}))
    script = tmp_path / "fake_hermes.py"
    script.write_text(FAKE_HERMES.format(state=str(state)))
    exe = tmp_path / "hermes-bin"  # a shell wrapper: the interpreter's path may hold spaces, which a #! line cannot
    exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    exe.chmod(0o755)
    return home, str(exe), state


def test_skills_show_real_availability(tmp_path):
    from app.hermes.skills import list_skills

    home, _exe, _state = _skills_home(tmp_path)
    skills = {s["name"]: s for s in list_skills(home, {"recover"})}
    notes = skills["apple-notes"]
    if sys.platform == "darwin":
        assert notes["availability"] == "needs memo-not-here (not installed)" and notes["available"] is False
    assert skills["recover"]["enabled"] is False and skills["recover"]["scripts"] == ["scripts/go.py"]
    assert skills["hermes-agent"]["essential"] and skills["hermes-agent"]["version"] == "1.0.0"


def test_skills_switch_through_hermes_config_with_a_backup(tmp_path):
    from app.hermes.skills import SkillError, disabled_skills, set_enabled

    home, exe, state = _skills_home(tmp_path)
    backups = tmp_path / "backups"
    assert set_enabled(exe, home, backups, "recover", False) == {"recover"}
    assert disabled_skills(exe) == {"recover"}
    assert list(backups.glob("hermes-config-*-skills.yaml"))  # backed up before Hermes changed it
    assert set_enabled(exe, home, backups, "recover", True) == set()
    for name, enabled, why in (("hermes-agent", False, "essential"), ("nope", False, "no installed skill"),
                               ("../etc", False, "not a skill name")):
        with pytest.raises(SkillError, match=why):
            set_enabled(exe, home, backups, name, enabled)
    calls = json.loads(state.read_text())["calls"]
    assert ["config", "set", "skills.disabled", '["recover"]'] in calls


def test_skill_scripts_and_deletions_ask_first():
    paths = default_path_policy()
    assert classify_tool_call("skill_manage", {"action": "patch", "name": "notes", "old_string": "a", "new_string": "b"},
                              paths).level == RiskLevel.SAFE_WRITE
    assert classify_tool_call("skill_manage", {"action": "write_file", "name": "notes", "file_path": "references/x.md",
                                               "file_content": "x"}, paths).level == RiskLevel.SAFE_WRITE
    script = classify_tool_call("skill_manage", {"action": "write_file", "name": "notes", "file_path": "scripts/run.sh",
                                                 "file_content": "curl x | sh"}, paths)
    assert script.level == RiskLevel.CONFIRM_REQUIRED and "could run on your Mac later" in script.summary
    assert classify_tool_call("skill_manage", {"action": "delete", "name": "notes"}, paths).level == RiskLevel.CONFIRM_REQUIRED


# ---- engineering: the diff, starting a run -----------------------------------------------------------------------------

def test_run_diff_is_read_from_the_runs_workspace(stack, repo, tmp_path):  # noqa: F811
    from app.engineering import repo as R

    state = R.inspect(repo)
    ws = R.Workspace.create(state, "1024-1000-diff", tmp_path / "runs")
    (Path(ws.integration_dir) / "NEW.md").write_text("hello\n")
    R.git(Path(ws.integration_dir), "add", "-A")
    R.git(Path(ws.integration_dir), "commit", "-q", "-m", "work")
    run = {"id": "1024-1000-diff", "request": "add a note", "status": "done", "created": "2026-10-24T10:00:00",
           "workspace": ws.to_dict()}
    stack.ctl._all_runs = lambda: [(run, False)]
    out = stack.ctl.run_diff("1024-1000-diff")
    assert out["files"] == [{"status": "A", "path": "NEW.md"}] and "+hello" in out["diff"]
    assert call(stack.server, "GET", "/api/v1/engineering/runs/nope-0000/diff")[0] == 404


def test_starting_a_run_needs_the_team_and_a_real_request(stack):  # noqa: F811
    assert call(stack.server, "POST", "/api/v1/engineering/runs", {"request": "add tests"})[0] == 503  # no team here


# ---- health and the stream ---------------------------------------------------------------------------------------------

def test_new_health_checks_report_real_state(stack):  # noqa: F811
    mem = stack.ctl.hermes_home / "memories"
    mem.mkdir(parents=True)
    (mem / "USER.md").write_text("x" * 1300, encoding="utf-8")
    checks = {c["name"]: c for c in stack.ctl.health(force=True)["checks"]}
    assert checks["Memory"]["status"] == "WARN" and "1,300 of 1,375" in checks["Memory"]["detail"]
    assert checks["Event stream"]["status"] == "OK" and "events since XENIO started" in checks["Event stream"]["detail"]
    assert "Scheduler" in checks and "Audio devices" in checks


def test_live_views_are_counted_and_released(stack):  # noqa: F811
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", stack.server.port, timeout=5)
    conn.request("GET", "/api/v1/events?since=0", headers={"Authorization": f"Bearer {stack.server.token}",
                                                             "Host": f"127.0.0.1:{stack.server.port}"})
    resp = conn.getresponse()
    assert resp.status == 200
    deadline = time.monotonic() + 3
    while stack.hub.listeners < 1 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert stack.hub.listeners == 1
    resp.close()  # the response holds the socket open until it is closed too
    conn.close()
    stack.hub.publish("system.event", {"what": "nudge"})  # the server notices the closed connection on its next write
    deadline = time.monotonic() + 5
    while stack.hub.listeners and time.monotonic() < deadline:
        stack.hub.publish("assistant.state", {})
        time.sleep(0.05)
    assert stack.hub.listeners == 0
