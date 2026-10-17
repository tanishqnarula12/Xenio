# Proactive agent: scheduled checks and notifications (V4)

XENIO can check things on a schedule and tell you only when something matters: "every morning, summarize
my calendar", "check my GitHub issues every morning", "tell me if an important email arrives", "remind me
about unfinished tasks".

## Architecture: Hermes' scheduler, XENIO's rules

```
SCHEDULER   Hermes cron (jobs.json, its file lock, run history, retries) - XENIO adds no second scheduler
   |
TRIGGER     `hermes cron tick`, started by XENIO while it is open (only when one of its checks is due and
   |        no Hermes gateway is ticking), with XENIO's guarded environment - or the gateway, if you run one
   |
XENIO       the scheduled run is a normal Hermes agent turn with the xenio-guard plugin active
   |
CHECK       read-only tools: calendar, email, tasks, projects, GitHub, briefing, web search
   |
DECIDE      nothing worth saying -> the run answers "[SILENT]" and nothing is delivered
   |
INFORM      Hermes saves the answer (cron/output/<job>/<time>.md); XENIO turns each new answer into ONE
            notification -> printed in text mode / spoken in voice mode when XENIO is idle
```

| Part | Where |
|---|---|
| create / list / remove checks | `xenio_schedule` tool, implemented with Hermes' own `cron.jobs` API (`app/proactive.py`, in the plugin) |
| trigger + collect + deliver | `ProactiveEngine` (`app/proactive.py`): a 30-second loop in XENIO's text and voice modes |
| notifications | `app/notifications.py`, stored in `personal.db` |
| by hand | `python -m app.main --tick` (run what is due now, print new notifications), `hermes cron list` |

Every XENIO check is a Hermes job named `xenio: <name>`, with:
* `deliver: local`: the answer goes to XENIO, never to a chat platform;
* `enabled_toolsets: [xenio, web]`: in **any** Hermes process it gets only XENIO's tools and web
  search/reading. No terminal, no file editing, no browser, no messaging, no scheduling more jobs (Hermes also
  removes `cronjob`, `messaging` and `clarify` from every cron run);
* a prompt that states the rules: read and report only, outside content is data, `[SILENT]` when there is
  nothing to say, at most three spoken sentences.

## Safety of scheduled runs (phase 12)

A scheduled run obeys exactly the same policy as a spoken request. The difference is that nobody is there to
say yes:

| Action | Interactive | Scheduled run |
|---|---|---|
| read calendar, email, tasks, GitHub, public web | runs | runs |
| SAFE_WRITE (add a task, project note) | runs | runs |
| CONFIRM_REQUIRED (push code, create an event, delete files...) | XENIO asks you | **refused**: Hermes' `approvals.cron_mode` is `deny` (its default) |
| HIGH_RISK (credentials, destructive, financial) | XENIO asks for "confirm" | **refused** |
| blocked (secrets, sending email) | refused | refused |

Creating or removing a scheduled check is itself CONFIRM_REQUIRED. The confirmation says whether it is
**one-time** or **RECURRING**, how often, and what it does.

To keep model tokens down:
* A recurring check runs at most every **15 minutes**. "every 5m" is refused; "in 1m" (once) is fine.
* A check whose source has not changed can skip the model entirely: Hermes' monitor mode
  (`--monitor-script`/`--monitor-url` on `hermes cron create`) only wakes the agent when the watched output
  changes. For your own jobs this is available through `hermes cron create`.

If you also run a Hermes gateway (`hermes gateway`), it ticks the scheduler itself and XENIO stops starting
ticks. The guard is not active in the gateway, but XENIO's jobs still have only the `web` toolset there (the
`xenio` toolset exists only in XENIO's Hermes). So they cannot do anything beyond reading the web.

## Duplicates, failures

* **Duplicate trigger prevention:** Hermes advances `next_run_at` and holds a file lock per tick. XENIO
  never runs two ticks at once and never ticks when nothing of its own is due or a gateway is ticking.
* **Each run is delivered once:** a notification is keyed `cron:<job>:<run time>`; processed runs are
  remembered in `%LOCALAPPDATA%\xenio\proactive_state.json`. Restarts do not repeat them.
* **Failed runs** become a high-priority notification ("Morning brief failed: Google rejected the sign-in"),
  from the run document or the job's `last_error`. Hermes also records incidents (`hermes cron incidents`).
* A one-time check (`in 1m`, an ISO time) runs once and is finished by Hermes.

## Notifications

`Notification(title, message, priority low|normal|high, source, timestamp, action_required)`, stored in
`personal.db` with a de-duplication key.

| Channel | Behaviour |
|---|---|
| text mode | printed at once when you are at the prompt; otherwise right after the current answer; and at start-up for anything that arrived while XENIO was closed |
| voice mode | spoken ("Notification: ...") only while XENIO is idle, never over a conversation; the rest wait until the conversation ends |
| ask | "any notifications?" → `xenio_notifications` |
| later | desktop pop-ups, Telegram, WhatsApp: same `Channel` shape, not built in V4 |

## Examples

| You say | XENIO creates (after you confirm) |
|---|---|
| "Every morning at 8, summarize my calendar." | `xenio: morning calendar`, `0 8 * * *` |
| "Check my GitHub issues every morning." | `xenio: github issues`, `every monday 9am` or `0 9 * * *` |
| "Tell me if an important email arrives." | `xenio: important mail`, `every 30m` (silent unless there is new important mail) |
| "Remind me about unfinished tasks at 6 pm." | `xenio: task reminder`, `0 18 * * *` |
| "In one minute, tell me how many open tasks I have." | one-time `in 1m` |

## Limitations

* While neither XENIO nor a Hermes gateway runs, nothing ticks. Checks that came due meanwhile run when
  XENIO starts (Hermes' `catch_up_missed: true`).
* A run uses the main model chain (Codex first). Each recurring check costs tokens: prefer longer intervals.
* "Important email" relies on Gmail's own importance and star markers.
