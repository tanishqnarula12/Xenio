"""XENIO Daily Briefing: today's calendar, important email, pending tasks, GitHub activity, scheduled checks.

Every section is gathered from its real source. A source that is not connected or fails is reported
as unavailable with the reason - never filled in. The spoken summary is built from the gathered
numbers only, so the briefing cannot claim meetings or emails that do not exist.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from app.integrations.base import IntegrationError, clip


def _section(fetch: Callable[[], Any]) -> dict:
    try:
        return {"available": True, **fetch()}
    except IntegrationError as exc:
        return {"available": False, "reason": str(exc)}
    except Exception as exc:  # one broken source must not break the briefing
        return {"available": False, "reason": f"{type(exc).__name__}: {clip(exc, 160)}"}


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def gather(calendar=None, email=None, tasks=None, projects=None, github=None, schedules: Callable[[], list] | None = None,
           notifications=None, now: datetime | None = None) -> dict:
    now = (now or datetime.now()).astimezone()
    out: dict[str, Any] = {"date": now.strftime("%A %d %B %Y"), "time": now.strftime("%H:%M")}
    if calendar is not None:
        out["calendar"] = _section(lambda: calendar.events("today", 20, now=now))
    if email is not None:
        out["email"] = _section(lambda: email.search(important_only=True, limit=8))
    if tasks is not None:
        def task_info():
            open_tasks = tasks.list("open", limit=50)
            return {"counts": tasks.counts(), "top": [t.to_dict() for t in open_tasks[:5]]}
        out["tasks"] = _section(task_info)
    if projects is not None:
        def project_info():
            current = projects.current()
            if current is None:
                return {"current": None}
            return {"current": current["name"], "focus": current["focus"], "repository": current["repository"]}
        out["project"] = _section(project_info)
    if github is not None:
        repo = (out.get("project") or {}).get("repository")
        if repo:
            out["github"] = _section(lambda: github.overview(repo))
        else:
            out["github"] = _section(lambda: {"repositories": github.repositories(5)["repositories"]})
    if schedules is not None:
        out["scheduled_checks"] = _section(lambda: {"checks": schedules()})
    if notifications is not None:
        out["notifications"] = _section(lambda: {"unread": [n.line() for n in notifications.pending(5)]})
    out["summary"] = summarize(out, now)
    return out


def summarize(data: dict, now: datetime | None = None) -> str:
    now = now or datetime.now()
    hour = now.hour
    parts = ["Good morning." if hour < 12 else "Good afternoon." if hour < 17 else "Good evening."]
    unavailable = []

    cal = data.get("calendar")
    if cal is not None:
        if cal.get("available"):
            events = cal.get("events") or []
            if events:
                first = events[0]
                when = "all day" if first.get("all_day") else f"at {first['start'][11:16]}"
                parts.append(f"You have {_plural(len(events), 'event')} today; the first is {first['title']} {when}.")
            else:
                parts.append("Your calendar is free today.")
        else:
            unavailable.append("calendar")

    mail = data.get("email")
    if mail is not None:
        if mail.get("available"):
            n = len(mail.get("messages") or [])
            parts.append(f"{_plural(n, 'important unread email')}." if n else "No important unread email.")
        else:
            unavailable.append("email")

    tasks = data.get("tasks")
    if tasks is not None and tasks.get("available"):
        counts = tasks.get("counts") or {}
        n = counts.get("open", 0)
        if n:
            extra = []
            if counts.get("overdue"):
                extra.append(f"{counts['overdue']} overdue")
            if counts.get("due_today"):
                extra.append(f"{counts['due_today']} due today")
            parts.append(f"{_plural(n, 'pending task')}" + (f", {' and '.join(extra)}." if extra else "."))
        else:
            parts.append("No pending tasks.")

    project = data.get("project")
    if project is not None and project.get("available") and project.get("current"):
        focus = f", focusing on {project['focus']}" if project.get("focus") else ""
        parts.append(f"Current project: {project['current']}{focus}.")

    gh = data.get("github")
    if gh is not None:
        if gh.get("available") and "open_issues" in gh:
            issues, prs = len(gh.get("open_issues") or []), len(gh.get("open_pull_requests") or [])
            parts.append(f"On GitHub, {gh['repo']} has {_plural(issues, 'open issue')} and {_plural(prs, 'open pull request')}.")
        elif gh.get("available") and gh.get("repositories"):
            latest = gh["repositories"][0]
            open_items = sum(int(r.get("open_issues_and_prs") or 0) for r in gh["repositories"])
            counted = ("no open issues or pull requests" if not open_items else
                       "1 open issue or pull request" if open_items == 1 else f"{open_items} open issues or pull requests")
            parts.append(f"On GitHub, your latest active repository is {latest['repo'].split('/')[-1]}, with {counted} "
                         "across your recent repositories.")
        elif not gh.get("available"):
            unavailable.append("GitHub")

    checks = data.get("scheduled_checks")
    active = [c for c in (checks or {}).get("checks") or [] if c.get("enabled", True)]  # not finished one-time checks
    if checks is not None and checks.get("available") and active:
        parts.append(f"{_plural(len(active), 'scheduled check')} active.")

    if unavailable:
        names = unavailable[0] if len(unavailable) == 1 else ", ".join(unavailable[:-1]) + " and " + unavailable[-1]
        parts.append(f"I couldn't check {names}.")
    return " ".join(parts)
