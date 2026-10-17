"""Google Calendar: what is on the user's calendar (READ); creating an event is CONFIRM_REQUIRED.

Times are the computer's local time zone. "today", "tomorrow", "this morning/afternoon/evening",
"next" (the next event in the coming 30 days), "this week", "next 7 days" or a date (2026-10-05) are
understood; anything else is an error rather than a guess. Event titles, places and descriptions are
written by other people (invitations) and are returned as untrusted content.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta

from app.integrations.base import AuthState, Capability, Integration, IntegrationError, clip
from app.integrations.google import GoogleAuth, GoogleClient, SCOPES
from app.safety.policy import RiskLevel

EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
_SCOPE = SCOPES[1]


@dataclass
class Event:
    title: str
    start: str  # ISO local time, or a date for all-day events
    end: str
    all_day: bool = False
    location: str = ""
    description: str = ""
    status: str = "confirmed"
    attendees: int = 0
    organizer: str = ""
    link: str = ""
    id: str = ""

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in ("", 0, False) or k in ("title", "start", "end")}


def _local(now: datetime | None) -> datetime:
    return (now or datetime.now()).astimezone()


def resolve_range(when: str, now: datetime | None = None) -> tuple[datetime, datetime, str]:
    """(start, end, label) for a spoken range. Raises IntegrationError on anything not understood."""
    now = _local(now)
    tz = now.tzinfo
    text = " ".join((when or "today").lower().replace("'", "").split())
    day = datetime.combine(now.date(), time.min, tz)
    parts = {"morning": (6, 12), "afternoon": (12, 18), "evening": (17, 23), "tonight": (17, 24)}

    def span(base: datetime, h1: int, h2: int) -> tuple[datetime, datetime]:
        return base + timedelta(hours=h1), base + timedelta(hours=h2)

    if text in ("today", "rest of today", "rest of the day", "now"):
        return (now if text != "today" else day), day + timedelta(days=1), "today"
    if text == "tomorrow":
        return day + timedelta(days=1), day + timedelta(days=2), "tomorrow"
    m = re.fullmatch(r"(this |today |tomorrow )?(morning|afternoon|evening|tonight)", text)
    if m:
        base = day + timedelta(days=1) if (m.group(1) or "").strip() == "tomorrow" else day
        start, end = span(base, *parts[m.group(2)])
        label = f"{'tomorrow' if base != day else 'this'} {m.group(2)}".replace("this tonight", "tonight")
        return max(start, now) if base == day else start, end, label
    if text in ("next", "next event", "next meeting", "upcoming"):
        return now, now + timedelta(days=30), "next"
    if text in ("this week", "week", "rest of the week"):
        return day, day + timedelta(days=7 - now.weekday()), "this week"
    m = re.fullmatch(r"next (\d{1,2}) days?", text)
    if m:
        return day, day + timedelta(days=int(m.group(1))), text
    if text in ("next week",):
        start = day + timedelta(days=7 - now.weekday())
        return start, start + timedelta(days=7), "next week"
    try:
        target = date.fromisoformat(text)
    except ValueError:
        raise IntegrationError(f"I don't understand the time range {when!r}; use today, tomorrow, this afternoon, "
                               "next, this week or a date like 2026-10-05.") from None
    start = datetime.combine(target, time.min, tz)
    return start, start + timedelta(days=1), target.isoformat()


def _parse_event(item: dict) -> Event | None:
    if not isinstance(item, dict) or item.get("status") == "cancelled":
        return None
    start, end = item.get("start") or {}, item.get("end") or {}
    all_day = "date" in start and "dateTime" not in start
    begin = start.get("dateTime") or start.get("date")
    finish = end.get("dateTime") or end.get("date") or begin
    if not begin:
        return None  # malformed: no start time
    if not all_day:
        try:
            begin = datetime.fromisoformat(begin.replace("Z", "+00:00")).astimezone().isoformat(timespec="minutes")
            finish = datetime.fromisoformat(finish.replace("Z", "+00:00")).astimezone().isoformat(timespec="minutes")
        except ValueError:
            return None
    attendees = item.get("attendees") if isinstance(item.get("attendees"), list) else []
    return Event(title=clip(item.get("summary") or "(no title)", 160), start=begin, end=finish, all_day=all_day,
                 location=clip(item.get("location"), 160), description=clip(item.get("description"), 300),
                 status=str(item.get("status") or "confirmed"), attendees=len(attendees),
                 organizer=clip((item.get("organizer") or {}).get("email"), 80), link=str(item.get("htmlLink") or ""),
                 id=str(item.get("id") or ""))


def busy_summary(events: list[Event], start: datetime, end: datetime) -> dict:
    timed = [e for e in events if not e.all_day]
    minutes = 0
    for e in timed:
        try:
            a = max(datetime.fromisoformat(e.start), start)
            b = min(datetime.fromisoformat(e.end), end)
            minutes += max(0, int((b - a).total_seconds() // 60))
        except ValueError:
            continue
    level = "free" if not events else "light" if minutes < 120 else "busy" if minutes < 300 else "very busy"
    return {"events": len(events), "all_day_events": len(events) - len(timed), "booked_minutes": minutes,
            "busyness": level, "first": timed[0].start if timed else None, "last_end": timed[-1].end if timed else None}


class CalendarIntegration(Integration):
    name = "calendar"
    title = "Google Calendar"
    capabilities = (
        Capability("calendar.list_events", "read", RiskLevel.READ, "events in a time range, the next event, how busy a day is"),
        Capability("calendar.create_event", "write", RiskLevel.CONFIRM_REQUIRED, "add an event (always confirmed by the user)"),
    )

    def __init__(self, client: GoogleClient | None = None):
        self.client = client or GoogleClient()

    def auth_state(self) -> AuthState:
        state = self.client.auth.state()
        if state.status == "not_configured":
            return state
        if not self.client.auth.has_scope(_SCOPE):
            return AuthState("error", "Google is connected without Calendar permission",
                             "Run `python -m app.main --connect-google` again and allow Calendar access.")
        return state

    def events(self, when: str = "today", limit: int = 20, now: datetime | None = None) -> dict:
        state = self.auth_state()
        if not state.connected:
            raise IntegrationError(state.setup or state.detail)
        start, end, label = resolve_range(when, now)
        limit = max(1, min(int(limit or 20), 50))
        data = self.client.get(EVENTS_URL, {
            "timeMin": start.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true", "orderBy": "startTime",
            "maxResults": 1 if label == "next" else limit})
        items = data.get("items") if isinstance(data.get("items"), list) else []
        items = [i for i in items if not (isinstance(i, dict) and i.get("status") == "cancelled")]
        events = [e for e in (_parse_event(i) for i in items) if e is not None]
        skipped = len(items) - len(events)
        return {"range": label, "from": start.isoformat(timespec="minutes"), "to": end.isoformat(timespec="minutes"),
                "events": [e.to_dict() for e in events], "summary": busy_summary(events, start, end),
                **({"skipped_malformed": skipped} if skipped else {})}

    def create_event(self, title: str, start: str, end: str, location: str = "", description: str = "") -> dict:
        state = self.auth_state()
        if not state.connected:
            raise IntegrationError(state.setup or state.detail)
        try:
            begin = datetime.fromisoformat(start).astimezone()
            finish = datetime.fromisoformat(end).astimezone()
        except ValueError:
            raise IntegrationError("start and end must be ISO times like 2026-10-05T15:00") from None
        if finish <= begin:
            raise IntegrationError("the event must end after it starts")
        body = {"summary": title, "start": {"dateTime": begin.isoformat()}, "end": {"dateTime": finish.isoformat()}}
        if location:
            body["location"] = location
        if description:
            body["description"] = description
        made = self.client.post(EVENTS_URL, body)
        return {"id": made.get("id", ""), "title": made.get("summary", title), "start": begin.isoformat(timespec="minutes"),
                "end": finish.isoformat(timespec="minutes"), "link": made.get("htmlLink", "")}


def default_calendar() -> CalendarIntegration:
    return CalendarIntegration(GoogleClient(GoogleAuth()))
