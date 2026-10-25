"""Reminders, the user's tasks and location: Hermes' own scheduler runs reminders without a model, the schedule tool can
no longer silently list when asked to remind, the fast path does clear requests itself, run results become reminders,
and notifications reach the desktop without the text ever entering the AppleScript."""

import asyncio
import datetime as dt
import json
import subprocess
from types import SimpleNamespace

import pytest

from app.integrations.base import IntegrationError
from app.personal import quick
from app.proactive import ProactiveEngine, parse_run_output
from app.reminders import Reminders, find_when, parse_request, parse_when, script_source, script_text
from app.safety import policy as P
from app.tools import personal

NOW = dt.datetime(2026, 10, 22, 21, 15)  # a Thursday evening


# ---- understanding the time --------------------------------------------------------------------------------------

@pytest.mark.parametrize("said, text, when", [
    ("remind me at 3 to go to the office", "go to the office", "tomorrow at 3:00 PM"),  # 3 AM is never meant
    ("Remind me to call mom in 20 minutes", "call mom", "in 20 minutes (today at 9:35 PM)"),
    ("remind me tomorrow at 9 to submit the report", "submit the report", "tomorrow at 9:00 AM"),
    ("hey xenio remind me at 10:30 pm to sleep", "sleep", "today at 10:30 PM"),
    ("remind me to buy 3 apples at 6", "buy 3 apples", "tomorrow at 6:00 PM"),  # the 3 is part of the text
    ("remind me at 7 30 to leave", "leave", "tomorrow at 7:30 AM"),
    ("can you remind me at 11 pm that the game starts", "the game starts", "today at 11:00 PM"),
    ("reminder: pay rent tomorrow morning", "pay rent", "tomorrow at 9:00 AM"),
])
def test_reminder_requests_are_understood(said, text, when):
    parsed = parse_request(said, NOW)
    assert parsed is not None and parsed.text == text and parsed.when.display == when and parsed.when.once


def test_recurring_and_unclear_reminders():
    daily = parse_request("remind me every day at 9 am to stretch", NOW)
    assert daily.when.schedule == "0 9 * * *" and not daily.when.once
    assert parse_request("remind me every monday at 10 to plan the week", NOW).when.schedule == "0 10 * * 1"
    assert parse_request("remind me every 2 hours to drink water", NOW).when.schedule == "every 2h"
    assert parse_request("remind me to buy milk", NOW) is None  # no time: the model asks
    assert parse_request("remind me later", NOW) is None
    with pytest.raises(IntegrationError):
        find_when("every 1 minute", NOW)
    assert parse_when("2026-10-23T15:00", NOW).display == "tomorrow at 3:00 PM"
    assert parse_when("in 30m", NOW).schedule == "2026-10-22T21:45:00"
    with pytest.raises(IntegrationError):
        parse_when("2026-10-20T15:00", NOW)  # already passed


def test_the_script_can_only_print_the_text(tmp_path):
    evil = "x'); import os; os.system('rm -rf ~'); print('"
    source = script_source(evil)
    path = tmp_path / "r.py"
    path.write_text(source)
    out = subprocess.run(["python3", str(path)], capture_output=True, text=True, timeout=10)
    assert out.stdout.strip() == f"Reminder: {evil}" and script_text(path) == evil


# ---- creating them in Hermes' scheduler ---------------------------------------------------------------------------

class FakeCron:
    def __init__(self):
        self.jobs = []

    def create_job(self, **kw):
        job = {"id": f"j{len(self.jobs) + 1}", **kw}
        self.jobs.append(job)
        return job

    def remove_job(self, job_id):
        self.jobs = [j for j in self.jobs if j["id"] != job_id]
        return True


def test_reminder_through_hermes_cron_api(tmp_path):
    cron = FakeCron()
    when = parse_when("in 20 minutes", NOW)
    made = Reminders(tmp_path, cron=cron).create("call mom", when)
    job = cron.jobs[0]
    assert job["no_agent"] is True and job["repeat"] == 1 and job["deliver"] == "local"
    assert job["name"].startswith("xenio: reminder: call mom [") and job["schedule"] == when.schedule
    assert script_text(tmp_path / "scripts" / job["script"]) == "call mom" and made["text"] == "call mom"


def test_reminder_through_the_hermes_cli(tmp_path):
    calls = []

    def run(args, **kw):
        calls.append((args, kw["env"]["HERMES_HOME"]))
        return subprocess.CompletedProcess(args, 0, stdout="Created job: abc123def456\n  Next run: ...", stderr="")

    made = Reminders(tmp_path, "hermes", run=run).create("stretch", parse_when("every day at 9 am", NOW))
    args, home = calls[0]
    assert args[:4] == ["hermes", "cron", "create", "0 9 * * *"] and "--no-agent" in args and "--repeat" not in args
    assert home == str(tmp_path) and made["id"] == "abc123def456"

    def refuse(args, **kw):
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="Invalid schedule")

    with pytest.raises(IntegrationError, match="Invalid schedule"):
        Reminders(tmp_path, run=refuse).create("x", parse_when("in 5 minutes", NOW))
    assert len(list((tmp_path / "scripts" / "xenio_reminders").glob("*.py"))) == 1  # the refused one was cleaned up


def test_schedule_tool_never_silently_lists_a_reminder(monkeypatch, tmp_path):
    """The real failure: {"schedule": "2026-10-18T15:00", "task": "Remind to go to office"} with no action listed
    "0 scheduled checks" and the model said the reminder was set."""
    made = []
    monkeypatch.setattr(personal, "_reminders", lambda: SimpleNamespace(
        create=lambda text, when: made.append((text, when)) or {"id": "r1", "text": text}))
    when = (dt.datetime.now() + dt.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
    result = personal.schedule({"schedule": when, "task": "go to office"}, None)
    assert result.success and made and made[0][0] == "go to office" and "Reminder set" in result.message
    assert personal.schedule_action({"action": "clear"}) == "remove"
    bad = personal.schedule({"action": "remind", "text": "x", "when": "sometime soonish"}, None)
    assert not bad.success and "could not understand the time" in bad.error
    # and the guard treats it as the harmless thing it is
    assert P.classify_tool_call("xenio_schedule", {"schedule": when, "task": "go"}, P.PathPolicy()).level == P.RiskLevel.SAFE_WRITE


# ---- the run becomes a reminder ------------------------------------------------------------------------------------

NO_AGENT_RUN = """# Cron Job: xenio: reminder: stretch [a1b2c3]

**Job ID:** b5e2615b8156
**Run Time:** 2026-10-22 21:05:37
**Mode:** no_agent (script)

---

Reminder: stretch your legs
"""


def test_no_agent_output_and_reminder_notification(tmp_path):
    assert parse_run_output(NO_AGENT_RUN) == ("message", "Reminder: stretch your legs")
    cron = tmp_path / "cron"
    (cron / "output" / "b5e2615b8156").mkdir(parents=True)
    (cron / "output" / "b5e2615b8156" / "2026-10-22_21-05-37.md").write_text(NO_AGENT_RUN)
    (cron / "jobs.json").write_text(json.dumps({"jobs": [{"id": "b5e2615b8156", "name": "xenio: reminder: stretch [a1b2c3]",
                                                          "state": "completed", "enabled": False}]}))
    notes = []
    center = SimpleNamespace(add=lambda title, message, **kw: notes.append((title, message, kw)) or SimpleNamespace(id=1))
    engine = ProactiveEngine(tmp_path, center, state_file=tmp_path / "state.json")
    assert len(engine.collect()) == 1 and engine.collect() == []  # once
    title, message, kw = notes[0]
    assert (title, message, kw["priority"], kw["source"]) == ("Reminder", "stretch your legs", "high", "reminder")


def test_overdue_reminder_ticks_even_when_a_gateway_serves(tmp_path):
    now = 1_800_000_000.0
    cron = tmp_path / "cron"
    cron.mkdir()
    (cron / "ticker_heartbeat").write_text(str(now - 5))  # a gateway ticks (once a minute)
    due = dt.datetime.fromtimestamp(now - 20).isoformat()
    (cron / "jobs.json").write_text(json.dumps({"jobs": [{"id": "j", "name": "xenio: reminder: x [abcdef]",
                                                          "enabled": True, "next_run_at": due}]}))
    ran = []

    async def fake_exec(*args, **kw):
        ran.append(args)
        return SimpleNamespace(communicate=lambda: _done(), returncode=0)

    async def _done():
        return b"", b""

    engine = ProactiveEngine(tmp_path, SimpleNamespace(), clock=lambda: now, state_file=tmp_path / "s.json")
    import app.proactive as proactive

    orig = proactive.asyncio.create_subprocess_exec
    proactive.asyncio.create_subprocess_exec = fake_exec
    try:
        assert asyncio.run(engine.tick_if_due()) is True and ran
    finally:
        proactive.asyncio.create_subprocess_exec = orig


# ---- the fast path: tasks and reminders without the model --------------------------------------------------------

def test_personal_requests_parse_exactly():
    assert quick.parse("add a task: buy milk tomorrow", NOW) == quick.Request("add_task", title="buy milk", due="tomorrow")
    assert quick.parse("create a task to call the plumber, urgent", NOW).priority == "high"
    assert quick.parse("what are my tasks", NOW).kind == "list_tasks"
    assert quick.parse("mark buy milk as done", NOW).query == "buy milk"
    assert quick.parse("remind me at 3 to go to office", NOW).kind == "remind"
    for unclear in ("add a task", "add milk", "remind me later", "tell me about tasks in python",
                    "what tasks did the engineering team do"):
        assert quick.parse(unclear, NOW) is None


class Tasks:
    def __init__(self):
        self.items = []

    def add(self, title, priority="normal", due=None):
        t = SimpleNamespace(id=len(self.items) + 1, title=title, due=due, status="open", priority=priority)
        self.items.append(t)
        return t

    def list(self, status="open"):
        return [t for t in self.items if t.status == status]

    def find(self, text):
        return [t for t in self.items if text.lower() in t.title.lower()]

    def complete(self, task_id):
        t = self.items[task_id - 1]
        t.status = "done"
        return t


def test_fast_path_does_tasks_and_reminders(tmp_path):
    from app.fastpath import FastPath

    tasks, cron = Tasks(), FastCron()
    fast = FastPath(P.PathPolicy(), music=object(), desktop=object(), clock=lambda: NOW,
                    reminders=Reminders(tmp_path, cron=cron), tasks=lambda: tasks)
    reply = asyncio.run(fast.handle("add a task: buy milk tomorrow"))
    assert reply.ok and reply.text == "Added to your tasks: buy milk, due tomorrow." and tasks.items[0].title == "buy milk"
    assert asyncio.run(fast.handle("what are my tasks")).text == "You have one open task: buy milk, due tomorrow."
    assert asyncio.run(fast.handle("mark buy milk as done")).text == "Marked done: buy milk."
    assert asyncio.run(fast.handle("mark buy milk as done")) is None  # nothing open matches: Hermes takes it
    reply = asyncio.run(fast.handle("remind me at 3 to go to the office"))
    assert reply.text == "Okay, I'll remind you to go to the office tomorrow at 3:00 PM." and cron.jobs
    assert "reminder set" in reply.note


class FastCron(FakeCron):
    pass


# ---- location and the desktop ------------------------------------------------------------------------------------

def test_location_is_only_what_the_user_or_the_mac_said(tmp_path):
    from app.location import describe, load, save

    assert describe(tmp_path)["known"] is False
    save("New Delhi, India", base=tmp_path)
    data = describe(tmp_path)
    assert data["known"] and data["place"] == "New Delhi, India" and data["source"] == "the user's setting"
    with pytest.raises(IntegrationError):
        save("x", lat=95, lon=10, base=tmp_path)
    with pytest.raises(IntegrationError):
        save("x", lat=10, base=tmp_path)
    save("", base=tmp_path)
    assert load(tmp_path) is None
    assert P.classify_tool_call("xenio_location", {}, P.PathPolicy()).level == P.RiskLevel.READ


def test_desktop_notification_text_never_enters_the_script(monkeypatch):
    from app.desktop import notify as N

    monkeypatch.setattr(N.sys, "platform", "darwin")
    seen = []

    def run(args, **kw):
        seen.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    hostile = 'x" & do shell script "rm -rf ~" & "'
    assert N.notify("Reminder", hostile, "Reminder", run=run)
    args = seen[0]
    assert args[:2] == ["osascript", "-e"] and hostile not in args[2] and args[4:6] == ["Reminder", hostile]
