"""V4 personal context: tasks, projects, notifications and the daily briefing."""

from datetime import date, datetime, timedelta, timezone

import pytest

from app.briefing import gather, summarize
from app.integrations.base import IntegrationError
from app.notifications import NotificationCenter
from app.personal.projects import Projects
from app.personal.store import PersonalStore
from app.personal.tasks import TaskList


@pytest.fixture
def store(tmp_path):
    return PersonalStore(tmp_path / "personal.db")


def test_set_current_keeps_the_repository_the_user_named(store, monkeypatch):
    """Live (V5 regression run): "I'm working on final CRM, it's the final-crm repository on my GitHub" was saved
    without the repository, so "what issues are open in my repository?" had nothing to look up."""
    from app.safety.policy import default_path_policy
    from app.tools import personal
    from app.tools.base import ToolContext

    monkeypatch.setitem(personal.FACTORIES, "projects", lambda: Projects(store))
    tool = next(t for t in personal.xenio_tools() if t.hermes_name == "xenio_project")
    assert "just its name" in tool.description
    result = tool.run({"action": "set_current", "name": "Final CRM", "focus": "analytics", "repository": "final-crm"},
                      ToolContext(paths=default_path_policy())).to_dict()
    assert result["success"] and result["data"]["repository"] == "final-crm"
    assert Projects(store).current()["repository"] == "final-crm"


def test_tasks_lifecycle(store):
    tasks = TaskList(store)
    a = tasks.add("Finish CRM report", priority="urgent", due="today", project="CRM")
    b = tasks.add("Read Hermes docs", due=(date.today() - timedelta(days=2)).isoformat())
    assert a.priority == "high" and a.due == date.today().isoformat() and a.status == "pending"
    assert [t.title for t in tasks.list()] == ["Finish CRM report", "Read Hermes docs"]  # high priority first
    assert b.to_dict()["overdue"] is True
    assert tasks.counts() == {"open": 2, "done": 0, "overdue": 1, "due_today": 1, "high_priority": 1}
    tasks.complete(b.id)
    assert [t.title for t in tasks.list("done")] == ["Read Hermes docs"] and tasks.counts()["open"] == 1
    assert tasks.update(a.id, status="doing").status == "in_progress"
    assert [t.title for t in tasks.find("crm report")] == ["Finish CRM report"]
    assert [t.title for t in tasks.list(project="crm")] == ["Finish CRM report"]
    tasks.delete(a.id)
    with pytest.raises(IntegrationError, match="no task #"):
        tasks.complete(a.id)
    with pytest.raises(IntegrationError, match="Due date"):
        tasks.add("x", due="whenever")
    with pytest.raises(IntegrationError, match="needs a title"):
        tasks.add("   ")


def test_projects_current_notes_and_context(store):
    projects = Projects(store)
    with pytest.raises(IntegrationError, match="No current project"):
        projects.resolve(None)  # nothing is invented
    projects.upsert("CRM", description="Customer CRM", repository="tanishqnarula12/final-crm")
    projects.upsert("CRM Analytics Dashboard")
    projects.set_current("CRM", focus="analytics feature")
    assert projects.current()["name"] == "CRM"
    projects.add_note("crm", "tech", "Backend uses PostgreSQL")
    projects.add_note("CRM", "decision", "Use FastAPI for the API")
    projects.add_note("CRM", "decision", "Switch charts to Recharts")
    again = projects.add_note("CRM", "decision", "switch charts to recharts")
    assert again["already_known"] is True
    ctx = projects.context(tasks=TaskList(store))  # current project by default
    assert ctx["project"] == "CRM" and ctx["focus"] == "analytics feature" and ctx["is_current"]
    assert ctx["last_decision"]["text"] == "Switch charts to Recharts"
    assert ctx["tech"][0]["text"] == "Backend uses PostgreSQL" and len(ctx["decisions"]) == 2
    # partial names resolve when they are unambiguous; ambiguity is surfaced, never guessed
    assert projects.resolve("CRM Analytics")["name"] == "CRM Analytics Dashboard"
    assert projects.resolve("the crm backend")["name"] == "CRM"
    projects.upsert("Analytics Platform")
    with pytest.raises(IntegrationError, match="Several projects match 'analytics'.*Ask the user"):
        projects.resolve("analytics")
    with pytest.raises(IntegrationError, match="No project called"):
        projects.resolve("Strava")
    with pytest.raises(IntegrationError, match="kind must be"):
        projects.add_note("CRM", "gossip", "x")


def test_notifications_are_stored_once(store):
    center = NotificationCenter(store)
    first = center.add("Morning check", "Two meetings today", key="cron:abc:2026-10-03_08-00-00")
    assert first is not None and center.add("Morning check", "dup", key="cron:abc:2026-10-03_08-00-00") is None
    center.add("Build failed", "CI is red", priority="high", action_required=True)
    pending = center.pending()
    assert [n.title for n in pending] == ["Build failed", "Morning check"]  # high priority first
    assert pending[0].line() == "!! Build failed [action needed]: CI is red"
    center.mark_delivered([n.id for n in pending])
    assert center.pending() == [] and len(center.recent()) == 2


class FakeCalendar:
    def events(self, when, limit, now=None):
        return {"events": [{"title": "Standup", "start": "2026-10-03T09:00+05:30", "end": "2026-10-03T09:15+05:30"},
                           {"title": "Review", "start": "2026-10-03T15:00+05:30", "end": "2026-10-03T16:00+05:30"}]}


class BrokenEmail:
    def search(self, **kw):
        raise IntegrationError("No email account is connected.")


def test_briefing_reports_only_real_data(store):
    tasks = TaskList(store)
    tasks.add("Finish CRM report", due=date.today().isoformat())
    projects = Projects(store)
    projects.set_current("CRM", focus="analytics")
    now = datetime(2026, 10, 3, 8, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    data = gather(calendar=FakeCalendar(), email=BrokenEmail(), tasks=tasks, projects=projects,
                  notifications=NotificationCenter(store), now=now)
    assert data["calendar"]["available"] and data["email"] == {"available": False, "reason": "No email account is connected."}
    summary = data["summary"]
    assert summary.startswith("Good morning. You have 2 events today; the first is Standup at 09:00.")
    assert "1 pending task, 1 due today." in summary and "Current project: CRM, focusing on analytics." in summary
    assert "I couldn't check email." in summary and "important unread email" not in summary
    # with nothing connected and nothing recorded, the briefing says so instead of inventing a day
    empty = summarize({"calendar": {"available": False, "reason": "x"}, "tasks": {"available": True, "counts": {"open": 0}}},
                      now)
    assert empty == "Good morning. No pending tasks. I couldn't check calendar."
    # a finished one-time check is not "active"
    checks = {"available": True, "checks": [{"name": "Task Count", "enabled": False}, {"name": "Inbox", "enabled": True}]}
    assert "1 scheduled check active." in summarize({"scheduled_checks": checks}, now)
    checks["checks"].pop()
    assert "scheduled check" not in summarize({"scheduled_checks": checks}, now)
