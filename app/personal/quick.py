"""Reminders and the user's tasks on XENIO's fast path: understood exactly, done at once, no model.

Through the model these went wrong in practice: "mark a task to remind me to go to the office at 3" became Hermes'
private scratchpad (``todo_list``) and a schedule call without an action, which only listed; the model then said the
reminder was set. Here a request is taken only when every part is understood:

    "remind me at 3 to go to the office"        -> a reminder in Hermes' scheduler (app/reminders.py)
    "add a task: buy milk tomorrow"              -> XENIO's task list (the Tasks page)
    "what are my tasks"                          -> the open tasks
    "mark buy milk as done"                      -> that task, if exactly one matches

Anything else (no time, a vague "remind me later", two tasks match) goes to Hermes unchanged, which can ask.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from app.integrations.base import IntegrationError
from app.reminders import Parsed, parse_request

_PREFIX = r"^\s*(?:(?:hey|ok|okay)\s+)?(?:xenio|zenio)?[\s,]*(?:please\s+|can\s+you\s+|could\s+you\s+)?"
_LIST = r"(?:task|tasks|to-?dos?|to-?do\s+list|todo\s+list|task\s+list)"
_ADD = [
    re.compile(_PREFIX + rf"(?:add|create|make|put|new)\s+(?:a\s+)?(?:new\s+)?(?:task|to-?do)\b\s*(?:called|named|:|-|to|for)?\s*"
               r"(?P<title>.+?)[\s.!]*$", re.I),
    re.compile(_PREFIX + rf"(?:add|put)\s+(?P<title>.+?)\s+(?:to|on|in)\s+(?:my\s+)?{_LIST}[\s.!]*$", re.I),
]
_SHOW = re.compile(_PREFIX + rf"(?:(?:what(?:'s|\s+is|\s+are)|show(?:\s+me)?|list|read(?:\s+me)?|tell\s+me)\s+(?:on\s+)?"
                   rf"(?:all\s+)?(?:my\s+)?(?:open\s+|pending\s+)?{_LIST}|(?:my\s+)?(?:open\s+)?{_LIST})"
                   r"(?:\s+for\s+today)?[\s?.!]*$", re.I)
_DONE = [
    re.compile(_PREFIX + r"(?:mark|tick\s+off|check\s+off)\s+(?:the\s+)?(?:task\s+)?(?P<q>.+?)\s+(?:as\s+)?(?:done|complete|"
               r"completed|finished)[\s.!]*$", re.I),
    re.compile(_PREFIX + r"(?:complete|finish|tick\s+off|check\s+off)\s+(?:the\s+)?task\s+(?P<q>.+?)[\s.!]*$", re.I),
]
_DUE = re.compile(r"\s*(?:,|\bdue\b|\bby\b|\bfor\b)?\s*\b(?P<due>today|tomorrow)\b\s*$", re.I)
_URGENT = re.compile(r"\s*(?:,\s*)?\b(?:high\s+priority|urgent|important)\b\s*", re.I)


@dataclass(frozen=True)
class Request:
    kind: str  # remind | add_task | list_tasks | complete_task
    reminder: Parsed | None = None
    title: str = ""
    due: str | None = None
    priority: str = "normal"
    query: str = ""


def parse(text: str, now: dt.datetime | None = None) -> Request | None:
    text = " ".join(str(text or "").split())
    if not text or len(text) > 300:
        return None
    try:
        reminder = parse_request(text, now)
    except IntegrationError:
        return None  # e.g. "every minute": Hermes explains
    if reminder is not None:
        return Request("remind", reminder=reminder)
    if _SHOW.match(text):
        return Request("list_tasks")
    for pattern in _DONE:
        m = pattern.match(text)
        if m:
            return Request("complete_task", query=m.group("q").strip(" '\""))
    for pattern in _ADD:
        m = pattern.match(text)
        if m:
            title = m.group("title").strip(" '\":-")
            priority = "high" if _URGENT.search(title) else "normal"
            title = _URGENT.sub(" ", title).strip(" ,")
            due = None
            d = _DUE.search(title)
            if d:
                due, title = d.group("due").lower(), title[: d.start()].strip(" ,")
            if len(title) < 2 or re.fullmatch(r"(?:to|a|the|for|it|that|this)", title, re.I):
                return None
            return Request("add_task", title=title[:200], due=due, priority=priority)
    return None


def task_line(task) -> str:
    due = f", due {task.due}" if getattr(task, "due", None) else ""
    return f"{task.title}{due}"


def describe_tasks(tasks: list) -> str:
    if not tasks:
        return "You have no open tasks."
    names = [task_line(t) for t in tasks[:5]]
    more = f", and {len(tasks) - 5} more" if len(tasks) > 5 else ""
    if len(names) == 1:
        return f"You have one open task: {names[0]}."
    return f"You have {len(tasks)} open tasks: {'; '.join(names[:-1])} and {names[-1]}{more}."
