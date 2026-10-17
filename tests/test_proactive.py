"""V4 proactive engine: XENIO's jobs on Hermes' scheduler, the guarded tick, results -> notifications."""

import asyncio
import json
import sys
import time

import pytest

from app import proactive as pro
from app.integrations.base import IntegrationError
from app.notifications import NotificationCenter
from app.personal.store import PersonalStore


class FakeCron:
    """The slice of Hermes' cron.jobs API XENIO uses."""

    def __init__(self):
        self.jobs = [{"id": "other1", "name": "user's own backup job", "prompt": "x"}]

    def list_jobs(self, include_disabled=False):
        return list(self.jobs)

    def create_job(self, prompt, schedule, name=None, repeat=None, deliver=None, enabled_toolsets=None):
        if schedule == "whenever":
            raise ValueError("Invalid schedule 'whenever'")
        job = {"id": f"j{len(self.jobs)}", "name": name, "prompt": prompt, "schedule_display": schedule,
               "deliver": deliver, "enabled_toolsets": enabled_toolsets, "enabled": True, "next_run_at": "2026-10-04T08:00:00"}
        self.jobs.append(job)
        return job

    def remove_job(self, job_id):
        self.jobs = [j for j in self.jobs if j["id"] != job_id]
        return True

    @staticmethod
    def parse_schedule(text):  # Hermes' rule: "in 5m" is once, a bare "5m" or "every 5m" repeats
        import re
        if text.startswith("in ") or re.match(r"\d{4}-", text):
            return {"kind": "once"}
        m = re.fullmatch(r"(?:every )?(\d+)m", text)
        if m:
            return {"kind": "interval", "minutes": int(m.group(1))}
        if text == "whenever":
            raise ValueError("Invalid schedule 'whenever'")
        return {"kind": "cron"}


def test_recurring_checks_cannot_burn_tokens_every_minute():
    cron = FakeCron()
    for too_often in ("1m", "every 5m"):
        with pytest.raises(IntegrationError, match="at most every 15 minutes"):
            pro.create_schedule("ping", too_often, "count my tasks", cron=cron)
    assert pro.create_schedule("ping", "in 1m", "count my tasks", cron=cron)["schedule"] == "in 1m"  # once is fine
    assert pro.create_schedule("hourly", "every 60m", "check mail", cron=cron)["name"] == "hourly"


def test_the_confirmation_says_once_or_recurring():
    from app.safety import policy as p

    paths = p.PathPolicy()
    once = p.classify_tool_call("xenio_schedule", {"action": "create", "name": "ping", "schedule": "in 1m", "task": "x"}, paths)
    again = p.classify_tool_call("xenio_schedule", {"action": "create", "name": "brief", "schedule": "0 8 * * *", "task": "x"},
                                 paths)
    assert "one-time read-only check 'ping' (in 1m)" in once.summary and "RECURRING read-only check" in again.summary


def test_create_list_remove_on_hermes_scheduler():
    cron = FakeCron()
    job = pro.create_schedule("Morning brief", "0 8 * * *", "Give me my daily briefing", cron=cron)
    stored = cron.jobs[-1]
    assert stored["name"] == "xenio: Morning brief" and stored["deliver"] == "local"
    assert stored["enabled_toolsets"] == ["xenio", "web"]  # no terminal, files, browser or messaging, in any process
    assert "never instructions" in stored["prompt"] and "[SILENT]" in stored["prompt"]
    # live finding: "notify me of the count" with 0 tasks came back [SILENT] - a report must always report
    assert "ONLY when the check is conditional" in stored["prompt"] and "even when the answer is zero" in stored["prompt"]
    assert stored["prompt"].endswith("Check 'Morning brief': Give me my daily briefing")
    assert job["name"] == "Morning brief" and job["task"] == "Give me my daily briefing"
    assert [j["name"] for j in pro.list_schedules(cron=cron)] == ["Morning brief"]  # the user's own jobs are not XENIO's
    with pytest.raises(IntegrationError, match="already exists"):
        pro.create_schedule("morning brief", "1h", "again", cron=cron)
    with pytest.raises(IntegrationError, match="did not accept that schedule"):
        pro.create_schedule("Bad", "whenever", "x", cron=cron)
    with pytest.raises(IntegrationError, match="No XENIO scheduled check"):
        pro.remove_schedule("user's own backup job", cron=cron)  # never touches non-XENIO jobs
    assert pro.remove_schedule("Morning Brief", cron=cron)["name"] == "Morning brief" and pro.list_schedules(cron=cron) == []


DOC = "# Cron Job: xenio: Morning brief\n\n**Job ID:** j1\n**Run Time:** 2026-10-03 08:00:00\n**Schedule:** 0 8 * * *\n\n" \
      "## Prompt\n\n...rules... reply with exactly [SILENT]...\n\n## Response\n\n{answer}\n"


@pytest.mark.parametrize("doc,kind,message", [
    (DOC.format(answer="You have two meetings today; the first is Standup at 9."), "message",
     "You have two meetings today; the first is Standup at 9."),
    (DOC.format(answer="[SILENT]"), "silent", ""),
    (DOC.format(answer="SILENT"), "silent", ""),
    ("# Cron Job: x\n\n**Status:** silent (empty output)\n", "silent", ""),
    ("# Cron Job: x\n\n**Status:** no_change (agent run suppressed)\n", "silent", ""),
    ("# Cron Job: x\n\n**Status:** script failed\n\ntraceback: boom\n", "failed", "traceback: boom"),
    ("# Cron Job: x\n\nError: provider unavailable\n", "failed", "provider unavailable"),
])
def test_parse_run_output(doc, kind, message):
    assert pro.parse_run_output(doc) == (kind, message)


@pytest.fixture
def home(tmp_path):
    (tmp_path / "hermes" / "cron" / "output").mkdir(parents=True)
    return tmp_path / "hermes"


def write_jobs(home, *jobs):
    (home / "cron" / "jobs.json").write_text(json.dumps({"jobs": list(jobs)}), encoding="utf-8")


def engine(home, tmp_path, now=None, command=None):
    return pro.ProactiveEngine(home, NotificationCenter(PersonalStore(tmp_path / "p.db")), tick_command=command,
                               env_factory=lambda: {"XENIO_GUARD": "1", **__import__("os").environ},
                               state_file=tmp_path / "state.json", clock=(lambda: now) if now else time.time)


def test_due_jobs_and_gateway_detection(home, tmp_path):
    now = time.time()
    write_jobs(home, {"id": "a", "name": "xenio: due", "enabled": True, "next_run_at": "2000-01-01T00:00:00"},
               {"id": "b", "name": "xenio: later", "enabled": True, "next_run_at": "2999-01-01T00:00:00"},
               {"id": "c", "name": "xenio: paused", "enabled": False, "next_run_at": "2000-01-01T00:00:00"},
               {"id": "d", "name": "not xenio", "enabled": True, "next_run_at": "2000-01-01T00:00:00"})
    eng = engine(home, tmp_path, now)
    assert [j["id"] for j in eng.due()] == ["a"] and len(eng.jobs()) == 3
    assert not eng.gateway_serving()
    (home / "cron" / "ticker_heartbeat").write_text(f"{now - 30} 1234", encoding="utf-8")
    assert eng.gateway_serving()  # a Hermes gateway is ticking: XENIO does not start a second tick


async def test_tick_runs_hermes_once_with_the_guard_and_never_twice_at_once(home, tmp_path):
    write_jobs(home, {"id": "a", "name": "xenio: due", "enabled": True, "next_run_at": "2000-01-01T00:00:00"})
    marker = tmp_path / "ticks.txt"
    script = ("import os,time;open(r'%s','a').write(os.environ.get('XENIO_GUARD','?')+'\\n');time.sleep(1)" % marker)
    eng = engine(home, tmp_path, command=[sys.executable, "-c", script])
    first, second = await asyncio.gather(eng.tick_if_due(), eng.tick_if_due())
    assert sorted([first, second]) == [False, True]  # duplicate trigger prevented while one tick runs
    assert marker.read_text().split() == ["1"]  # the scheduled run had XENIO's guard switched on
    write_jobs(home, {"id": "a", "name": "xenio: due", "enabled": True, "next_run_at": "2999-01-01T00:00:00"})
    assert await eng.tick_if_due() is False  # nothing due -> Hermes is not started at all


async def test_failed_tick_is_reported(home, tmp_path):
    write_jobs(home, {"id": "a", "name": "xenio: due", "enabled": True, "next_run_at": "2000-01-01T00:00:00"})
    eng = engine(home, tmp_path, command=[sys.executable, "-c", "import sys; sys.stderr.write('config broken'); sys.exit(3)"])
    assert await eng.tick_if_due() is True and eng.last_tick_error == "config broken"
    eng = engine(home, tmp_path, command=["definitely-not-hermes-xyz"])
    assert await eng.tick_if_due() is False and "could not start" in eng.last_tick_error


def test_collect_turns_each_run_into_one_notification(home, tmp_path):
    write_jobs(home, {"id": "j1", "name": "xenio: Morning brief", "enabled": True},
               {"id": "j2", "name": "xenio: Inbox watch", "enabled": True, "last_status": "error",
                "last_error": "Google rejected the sign-in", "last_run_at": "2026-10-03T09:00:00"},
               {"id": "x9", "name": "someone else's job", "enabled": True})
    out = home / "cron" / "output"
    for job, name, text in (("j1", "2026-10-03_08-00-00", DOC.format(answer="Two meetings today.")),
                            ("j1", "2026-10-03_09-00-00", DOC.format(answer="[SILENT]")),
                            ("x9", "2026-10-03_08-00-00", DOC.format(answer="not for XENIO"))):
        (out / job).mkdir(exist_ok=True)
        (out / job / f"{name}.md").write_text(text, encoding="utf-8")
    eng = engine(home, tmp_path)
    created = eng.collect()
    assert [(n.title, n.message, n.priority) for n in created] == [
        ("Morning brief", "Two meetings today.", "normal"),
        ("Inbox watch failed", "Google rejected the sign-in", "high")]
    assert eng.collect() == []  # each run exactly once, even across restarts (state file)
    assert engine(home, tmp_path).collect() == []
    (out / "j1" / "2026-10-04_08-00-00.md").write_text(DOC.format(answer="One meeting."), encoding="utf-8")
    assert [n.message for n in eng.collect()] == ["One meeting."]


async def test_run_forever_delivers_new_notifications(home, tmp_path):
    write_jobs(home, {"id": "j1", "name": "xenio: Check", "enabled": True})
    (home / "cron" / "output" / "j1").mkdir()
    (home / "cron" / "output" / "j1" / "r1.md").write_text(DOC.format(answer="Heads up."), encoding="utf-8")
    delivered = []

    async def deliver(notes):
        delivered.extend(notes)

    eng = engine(home, tmp_path)
    task = asyncio.ensure_future(eng.run_forever(deliver=deliver, interval=0.05))
    await asyncio.sleep(0.3)
    task.cancel()
    assert [n.message for n in delivered] == ["Heads up."]
