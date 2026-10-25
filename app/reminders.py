"""Reminders: "remind me at 3 to go to the office", run by Hermes' own scheduler without a model.

A reminder is a Hermes cron job named ``xenio: reminder: <text> [id]`` whose job IS a one-line script (``--no-agent``)
that prints the reminder. Hermes decides when it is due, runs it (once, or on its recurring schedule) and records the
run; XENIO's proactive loop turns the run into a notification: on the desktop, spoken, and on the page. No model, no
tokens, nothing a model can get wrong or claim without doing.

Measured: ``hermes cron create`` takes ~0.5 s; a due reminder runs in about 5 s of a scheduler tick.

The script is written by XENIO, never by the model: the text goes in as a Python string literal (``repr``), so whatever
the text says, the script can only print it.
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import os
import re
import subprocess
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.integrations.base import IntegrationError

REMINDER_PREFIX = "xenio: reminder: "
SCRIPT_FOLDER = "xenio_reminders"
MAX_TEXT = 300
MIN_EVERY_MINUTES = 5

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
          "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
          "forty five": 45, "fifty": 50, "half an": 0.5, "half a": 0.5}
_NUMBER = r"\d+(?:\.\d+)?|half an?|forty five|" + "|".join(sorted((w for w in _WORDS if " " not in w), key=len, reverse=True))
_UNIT = r"seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|m|h|s"
_IN = re.compile(rf"\b(?:in|after)\s+(?P<n>{_NUMBER})\s*(?P<unit>{_UNIT})\b(?:\s+from\s+now)?", re.I)
_DAY = rf"today|tonight|tomorrow|this\s+(?:morning|afternoon|evening)|(?:on\s+|next\s+)?(?:{'|'.join(_WEEKDAYS)})"
_AMPM = r"a\.?\s?m\.?|p\.?\s?m\.?|o'?\s?clock"
_AT = re.compile(
    rf"(?:\b(?P<day>{_DAY})\s+)?(?:\bat\s+(?P<h>\d{{1,2}})(?:[:.\s](?P<m>[0-5]\d))?(?:\s*(?P<ap>{_AMPM}))?"
    rf"|\b(?P<h2>\d{{1,2}})(?:[:.](?P<m2>\d{{2}}))?\s*(?P<ap2>{_AMPM}))(?:\s+(?P<day2>{_DAY}))?\b", re.I)
_DAY_ONLY = re.compile(rf"\b(?P<day>tomorrow\s+morning|tonight|this\s+evening|tomorrow)\b", re.I)
_EVERY = re.compile(
    rf"\b(?:every\s*(?P<what>day|morning|evening|night|weekday|{'|'.join(_WEEKDAYS)})|(?P<daily>daily))"
    rf"(?:\s+at\s+(?P<h>\d{{1,2}})(?:[:.](?P<m>\d{{2}}))?(?:\s*(?P<ap>{_AMPM}))?)?\b", re.I)
_EVERY_N = re.compile(rf"\bevery\s+(?P<n>\d+|{_NUMBER})?\s*(?P<unit>minutes?|mins?|hours?|hrs?)\b", re.I)


@dataclass(frozen=True)
class When:
    schedule: str  # what Hermes is given: an ISO local time (once), a cron expression or "every 2h" (recurring)
    display: str  # "today at 3:00 PM", "every day at 9:00 AM"
    once: bool
    at: dt.datetime | None = None  # the moment, for a one-time reminder


@dataclass(frozen=True)
class Parsed:
    text: str
    when: When


def _number(word: str) -> float:
    word = " ".join(word.lower().split())
    return float(_WORDS[word]) if word in _WORDS else float(word)


def _clock(t: dt.datetime) -> str:
    return t.strftime("%-I:%M %p") if os.name != "nt" else t.strftime("%I:%M %p").lstrip("0")


def _hour(h: int, ap: str | None, hint: str | None) -> list[int]:
    """Candidate 24 h hours for what was said ("3" alone could be 3 AM or 3 PM)."""
    ap = (ap or "").lower().replace(".", "").replace(" ", "")
    if ap.startswith("p"):
        return [h % 12 + 12]
    if ap.startswith("a"):
        return [h % 12]
    if h >= 13 or h == 0:
        return [h % 24]
    if hint in ("evening", "tonight", "afternoon", "night"):
        return [h % 12 + 12]
    if hint == "morning":
        return [h % 12]
    return [h % 12, h % 12 + 12] if h != 12 else [12]


def _day_hint(day: str) -> str | None:
    d = day.lower()
    for word in ("morning", "afternoon", "evening", "tonight", "night"):
        if word in d:
            return "tonight" if word == "night" else word
    return None


def _resolve_day(day: str | None, now: dt.datetime) -> dt.date | None:
    if not day:
        return None
    d = " ".join(day.lower().split())
    if d.startswith(("today", "tonight", "this ")):
        return now.date()
    if d.startswith("tomorrow"):
        return now.date() + dt.timedelta(days=1)
    name = d.split()[-1]
    if name in _WEEKDAYS:
        ahead = (_WEEKDAYS.index(name) - now.weekday()) % 7
        if ahead == 0 or d.startswith("next "):
            ahead = ahead or 7
        return now.date() + dt.timedelta(days=ahead)
    return None


def _describe(at: dt.datetime, now: dt.datetime) -> str:
    if at.date() == now.date():
        return f"today at {_clock(at)}"
    if at.date() == now.date() + dt.timedelta(days=1):
        return f"tomorrow at {_clock(at)}"
    return f"on {at:%A} at {_clock(at)}" if (at.date() - now.date()).days < 7 else f"on {at:%-d %B} at {_clock(at)}"


def _once(at: dt.datetime, now: dt.datetime, display: str | None = None) -> When:
    return When(at.strftime("%Y-%m-%dT%H:%M:%S"), display or _describe(at, now), True, at)


def find_when(text: str, now: dt.datetime | None = None) -> tuple[When, tuple[int, int]] | None:
    """The time said in ``text`` and where it is, or None. Recurring first ("every day at 9"), then "in 20 minutes",
    then a clock time ("at 3", "3:30 pm", "tomorrow at 9")."""
    now = (now or dt.datetime.now()).replace(microsecond=0)
    m = _EVERY.search(text)
    if m:
        what = (m.group("what") or "day").lower()
        default = {"morning": 9, "evening": 18, "night": 21}.get(what, 9)
        hint = what if what in ("morning", "evening", "night") else None
        h = int(m.group("h")) if m.group("h") else default
        minute = int(m.group("m") or 0)
        hour = _hour(h, m.group("ap"), hint)[-1 if hint in ("evening", "night") else 0] if m.group("h") else h
        if m.group("h") and not m.group("ap") and not hint and 1 <= h <= 7:
            hour = h + 12  # "every day at 5" means the afternoon
        days = {"weekday": "1-5"}.get(what) or (str((_WEEKDAYS.index(what) + 1) % 7) if what in _WEEKDAYS else "*")
        label = {"weekday": "every weekday", "*": "every day"}.get(what if what == "weekday" else days,
                                                                   f"every {what.capitalize()}")
        t = dt.datetime.combine(now.date(), dt.time(hour, minute))
        return When(f"{minute} {hour} * * {days}", f"{label} at {_clock(t)}", False), m.span()
    m = _EVERY_N.search(text)
    if m:
        n = _number(m.group("n")) if m.group("n") else 1
        minutes = n * (60 if m.group("unit").lower().startswith("h") else 1)
        if minutes < MIN_EVERY_MINUTES:
            raise IntegrationError(f"Recurring reminders can repeat at most every {MIN_EVERY_MINUTES} minutes.")
        spec = f"every {int(minutes // 60)}h" if minutes % 60 == 0 else f"every {int(minutes)}m"
        return When(spec, f"every {int(minutes // 60)} hour(s)" if minutes % 60 == 0 else f"every {int(minutes)} minutes",
                    False), m.span()
    m = _IN.search(text)
    if m:
        n, unit = _number(m.group("n")), m.group("unit").lower()
        seconds = n * (1 if unit.startswith("s") else 60 if unit.startswith("m") else 3600 if unit.startswith("h") else 86400)
        if seconds < 30:
            seconds = 30
        at = now + dt.timedelta(seconds=round(seconds))
        span = int(seconds // 60)
        nice = (f"in {span} minute{'s' if span != 1 else ''}" if seconds < 3600 else
                f"in {seconds / 3600:g} hour{'s' if seconds != 3600 else ''}")
        return _once(at, now, f"{nice} ({_describe(at, now)})"), m.span()
    m = _AT.search(text)
    if m:
        h = int(m.group("h") or m.group("h2"))
        minute = int(m.group("m") or m.group("m2") or 0)
        if h > 23 or minute > 59:
            return None
        day_word = m.group("day") or m.group("day2")
        hint = _day_hint(day_word or "")
        day = _resolve_day(day_word, now)
        hours = _hour(h, m.group("ap") or m.group("ap2"), hint)
        if day is not None and day != now.date() and len(hours) > 1:
            hours = [hours[1] if 1 <= h <= 7 else hours[0]]  # "tomorrow at 3" is the afternoon, "at 9" the morning
        base = day or now.date()
        options = sorted(dt.datetime.combine(base, dt.time(x, minute)) for x in hours)
        if day is None:  # today, or tomorrow once today's have passed
            options += [o + dt.timedelta(days=1) for o in options]
        future = [o for o in options if o > now] or options
        # "at 3" means 3 PM, "at 9" said at night means tomorrow morning: prefer waking hours when it is ambiguous
        at = next((o for o in future if 7 <= o.hour <= 22), future[0])
        return _once(at, now), m.span()
    m = _DAY_ONLY.search(text)
    if m:
        d = m.group("day").lower()
        hour = 9 if "morning" in d or d == "tomorrow" else 20
        day = now.date() + dt.timedelta(days=1) if d.startswith("tomorrow") else now.date()
        at = dt.datetime.combine(day, dt.time(hour, 0))
        if at <= now:
            return None
        return _once(at, now), m.span()
    return None


def parse_when(value: str, now: dt.datetime | None = None) -> When | None:
    """A time given on its own: "at 3 pm", "in 20 minutes", "every day at 9", or an ISO time like 2026-10-18T15:00."""
    now = (now or dt.datetime.now()).replace(microsecond=0)
    value = " ".join(str(value or "").split())
    try:
        at = dt.datetime.fromisoformat(value)
        if at.tzinfo is not None:
            at = at.astimezone().replace(tzinfo=None)
        if at <= now:
            raise IntegrationError(f"{_describe(at, now)} has already passed.")
        return _once(at, now)
    except ValueError:
        pass
    found = find_when(value if value.lower().startswith(("at ", "in ", "every", "daily", "after ")) or
                      not re.match(r"^\d", value) else f"at {value}", now)
    return found[0] if found else None


_LEAD = re.compile(r"^(?:(?:to|that|about|of|for|me|i\s+(?:have|need|should)\s+to|i\s+must|please)\b[\s,]*)+", re.I)
_REQUEST = re.compile(
    r"^\s*(?:(?:hey|ok|okay)\s+)?(?:xenio|zenio)?[\s,]*(?:please\s+|can\s+you\s+|could\s+you\s+|will\s+you\s+)?"
    r"(?:(?:set|create|add|make|put)\s+(?:a|an|one|me\s+a)?\s*reminder\b|remind\s+me\b|reminder\b\s*:?)\s*(?P<rest>.*?)"
    r"[\s.!?]*(?:please)?[\s.!?]*$", re.I | re.S)


def parse_request(text: str, now: dt.datetime | None = None) -> Parsed | None:
    """ "remind me at 3 to go to the office", "set a reminder to call mom in 20 minutes", "remind me every day at 9 to
    stretch". None when it is not a reminder request with both a time and something to be reminded of."""
    m = _REQUEST.match(text or "")
    if not m:
        return None
    rest = m.group("rest").strip()
    found = find_when(rest, now)
    if not found:
        return None
    when, (a, b) = found
    words = (rest[:a] + " " + rest[b:]).strip(" ,.-")
    words = _LEAD.sub("", " ".join(words.split())).strip(" ,.-")
    words = re.sub(r"\s+(?:to|at|on|by)$", "", words, flags=re.I)
    if not words or len(words) < 2:
        return None
    return Parsed(clean_text(words), when)


def clean_text(text: str) -> str:
    text = "".join(ch for ch in str(text or "") if ch.isprintable()).strip()
    text = re.sub(r"\bmy\b", "your", text, flags=re.I)
    text = re.sub(r"^i\s+(?:have|need)\s+to\s+", "", text, flags=re.I)
    if not text:
        raise IntegrationError("a reminder needs something to remind you of")
    return text[:MAX_TEXT]


# ---- the job in Hermes' scheduler -----------------------------------------------------------------------------------

def script_source(text: str) -> str:
    return f"# XENIO reminder (written by XENIO; it only prints the reminder)\nprint({('Reminder: ' + text)!r})\n"


def script_text(path: Path) -> str:
    """The reminder a script prints (read back without running it)."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "print")
        value = ast.literal_eval(call.args[0])
        return str(value).removeprefix("Reminder: ")
    except (OSError, SyntaxError, ValueError, StopIteration, IndexError):
        return ""


def is_reminder(job: Mapping[str, Any]) -> bool:
    return str(job.get("name") or "").startswith(REMINDER_PREFIX)


def public(job: Mapping[str, Any], home: Path) -> dict:
    name = str(job.get("name") or "")
    script = str(job.get("script") or "")
    text = script_text(home / "scripts" / script) if script else ""
    if not text:
        text = re.sub(r"\s*\[[0-9a-f]{6}\]$", "", name[len(REMINDER_PREFIX):])
    repeat = job.get("repeat") or {}
    done = isinstance(repeat, dict) and repeat.get("times") and repeat.get("completed", 0) >= repeat.get("times")
    return {"id": job.get("id"), "kind": "reminder", "text": text,
            "schedule": job.get("schedule_display") or (job.get("schedule") or {}).get("display"),
            "enabled": job.get("enabled", True), "next_run": job.get("next_run_at"), "last_run": job.get("last_run_at"),
            "last_status": job.get("last_status"), "done": bool(done or job.get("state") == "completed")}


class Reminders:
    """Create, list and remove reminders - with Hermes' cron API inside Hermes (the xenio-guard plugin), and with
    Hermes' own CLI from XENIO's process (``hermes cron create/remove``), always in the same Hermes home."""

    def __init__(self, home: Path, command: str = "hermes", env_factory: Callable[[], dict] | None = None, cron=None,
                 run: Callable[..., subprocess.CompletedProcess] = subprocess.run):
        self.home = Path(home)
        self.command = command
        self.env_factory = env_factory or (lambda: dict(os.environ))
        self.cron = cron
        self._run = run

    def jobs(self) -> list[dict]:
        try:
            data = json.loads((self.home / "cron" / "jobs.json").read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return []
        jobs = data.get("jobs") if isinstance(data, dict) else data
        return [j for j in jobs or [] if isinstance(j, dict) and is_reminder(j)]

    def list(self, include_done: bool = False) -> list[dict]:
        items = [public(j, self.home) for j in self.jobs()]
        return [i for i in items if include_done or not i["done"]]

    def create(self, text: str, when: When) -> dict:
        text = clean_text(text)
        rid = uuid.uuid4().hex[:6]
        rel = f"{SCRIPT_FOLDER}/r-{rid}.py"
        script = self.home / "scripts" / rel
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(script_source(text), encoding="utf-8")
        name = f"{REMINDER_PREFIX}{text[:60]} [{rid}]"
        try:
            if self.cron is not None:
                job = self.cron.create_job(prompt=None, schedule=when.schedule, name=name, repeat=1 if when.once else None,
                                           deliver="local", script=rel, no_agent=True)
                job_id = job.get("id")
            else:
                args = [self.command, "cron", "create", when.schedule, "--name", name, "--script", rel, "--no-agent",
                        "--deliver", "local"] + (["--repeat", "1"] if when.once else [])
                done = self._cli(args)
                found = re.search(r"Created job:\s*([0-9a-f]+)", done.stdout or "")
                if done.returncode != 0 or not found:
                    raise IntegrationError("Hermes did not accept the reminder: " + _last_line(done))
                job_id = found.group(1)
        except (ValueError, TypeError) as exc:
            script.unlink(missing_ok=True)
            raise IntegrationError(f"Hermes did not accept that time: {exc}") from None
        except Exception:
            script.unlink(missing_ok=True)
            raise
        return {"id": job_id, "kind": "reminder", "text": text, "when": when.display, "schedule": when.schedule,
                "once": when.once}

    def remove(self, id_or_text: str) -> dict:
        wanted = str(id_or_text or "").strip().lower()
        items = [(j, public(j, self.home)) for j in self.jobs()]
        match = next(((j, p) for j, p in items if str(p["id"]).lower() == wanted), None) or next(
            ((j, p) for j, p in items if not p["done"] and wanted and wanted in p["text"].lower()), None)
        if match is None:
            raise IntegrationError(f"No reminder matches {id_or_text!r}.")
        job, item = match
        if self.cron is not None:
            if not self.cron.remove_job(job["id"]):
                raise IntegrationError("Hermes could not remove that reminder.")
        else:
            done = self._cli([self.command, "cron", "remove", str(job["id"])])
            if done.returncode != 0:
                raise IntegrationError("Hermes could not remove that reminder: " + _last_line(done))
        if job.get("script"):
            (self.home / "scripts" / str(job["script"])).unlink(missing_ok=True)
        return item

    def _cli(self, args: list[str]) -> subprocess.CompletedProcess:
        env = self.env_factory()
        env["HERMES_HOME"] = str(self.home)
        try:
            return self._run(args, env=env, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise IntegrationError(f"could not reach Hermes' scheduler: {exc}") from None


def _last_line(done: subprocess.CompletedProcess) -> str:
    lines = [ln.strip() for ln in ((done.stderr or "") + "\n" + (done.stdout or "")).splitlines() if ln.strip()]
    return lines[-1][:200] if lines else f"exit code {done.returncode}"
