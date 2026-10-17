# Personal integrations (V4)

XENIO can read your calendar, email (read-only), GitHub, tasks and projects, and build a daily briefing from
them. Every integration runs as a XENIO tool inside Hermes, is checked by the same permission policy as
everything else, and says plainly when it is not connected. It never fills gaps with invented data.

## Architecture

```
app/integrations/
  base.py       Integration: name, title, capabilities (each with its permission level), auth_state();
                IntegrationError (plain, credential-free messages); UNTRUSTED_NOTE
  google.py     Google sign-in (OAuth, loopback + PKCE, standard library only) and a small REST client
  calendar.py   Google Calendar  - read; create event = CONFIRM_REQUIRED
  email.py      Email, READ ONLY - Gmail (through the Google sign-in) or Himalaya (any IMAP account)
  github.py     GitHub, read     - gh (when signed in) or GitHub's public API
app/personal/
  store.py      personal.db (SQLite): tasks, projects, project notes, notifications
  tasks.py      the task list
  projects.py   projects, current focus, notes (fact | decision | tech | file | issue)
app/briefing.py       daily briefing
app/notifications.py  notifications (see PROACTIVE_AGENT.md)
app/tools/personal.py the Hermes tools: xenio_calendar_events, xenio_calendar_create_event,
                      xenio_email_search, xenio_email_read, xenio_github, xenio_tasks, xenio_project,
                      xenio_daily_briefing, xenio_schedule, xenio_notifications, xenio_integrations
```

All of it uses the Python standard library only, because the xenio-guard plugin runs it inside Hermes'
process. Results containing other people's text (emails, invitations, issues) carry
`"untrusted_content": true` and a note that the text is data, not instructions.

## Status on this PC

`python -m app.main --health` shows every integration. `xenio_integrations` lets you ask XENIO
("what is connected?").

| Integration | Status | What you need to do |
|---|---|---|
| Google Calendar | not connected | the one-time Google sign-in below |
| Email (Gmail) | not connected | the same Google sign-in |
| Email (any IMAP mailbox) | Himalaya v2.2.1 installed, no account | optional: run `himalaya` and follow its wizard |
| GitHub | public data for `tanishqnarula12` | optional: `gh auth login` for private repos and higher limits |
| Tasks / projects | ready | nothing |
| Scheduled checks / notifications | ready | nothing (see PROACTIVE_AGENT.md) |

## Authentication

### Google (Calendar + Gmail): one sign-in, minimal permissions

> **MANUAL STEP REQUIRED: only you can sign in to your Google account.**
> 1. https://console.cloud.google.com/projectcreate: create a project (e.g. "XENIO").
> 2. https://console.cloud.google.com/apis/library: enable **Gmail API** and **Google Calendar API**.
> 3. https://console.cloud.google.com/auth/audience: User type **External**; under *Test users* add every
>    Google address you will connect (your Gmail, your skit.ac.in account).
>    While the app is in "Testing", Google expires the sign-in after **7 days**. To avoid re-connecting every
>    week, press **Publish app** on the same page. For your own private app this only means Google shows an
>    "unverified app" notice at sign-in (click *Advanced → Go to XENIO*).
> 4. https://console.cloud.google.com/apis/credentials: **Create credentials → OAuth client ID → Desktop
>    app → Download JSON** into your Downloads folder (`client_secret_....json`). The application type must
>    be **Desktop app**: a "Web application" client is refused. XENIO also finds a renamed file
>    (`*credentials*.json`) in Downloads, the XENIO folder or its `user_required_steps` folder, always prefers
>    a Desktop client over other projects' web clients, and accepts `--client-file PATH`. These files hold a
>    client secret and are git-ignored. After connecting, XENIO keeps what it needs in
>    `%LOCALAPPDATA%\xenio\google_token.json`, so you may delete them.
> 5. Run:
>    ```powershell
>    .\.venv\Scripts\python.exe -m app.main --connect-google
>    ```
>    Your browser opens Google's consent page. Choose the account, press *Allow*, and the tab says "XENIO is
>    connected". XENIO never sees your password.

What XENIO asks Google for:

| Permission | Why |
|---|---|
| `gmail.readonly` | search and read email. Google itself refuses any send, delete or label change with this token. |
| `calendar.events` | read events; creating one is always confirmed by you first |

Hermes' own `google-workspace` skill asks for much more (send mail, Drive, Docs, Sheets, full Calendar), so
XENIO does not reuse it. The token is stored in `%LOCALAPPDATA%\xenio\google_token.json`:
* the policy classifies it as a **secret file**: the model can never read it;
* it is never logged and never returned by a tool; error messages contain Google's error text only.

To disconnect: `python -m app.main --disconnect-google` (revokes at Google and deletes the file). Or remove
"XENIO" at https://myaccount.google.com/permissions.

A Google Workspace account (such as skit.ac.in) can only connect if its administrator allows third-party
apps. If Google says *"access blocked by your administrator"*, use Himalaya with that mailbox instead, if
the school allows IMAP.

### Email through Himalaya (any provider)

Himalaya v2.2.1 is installed at `%LOCALAPPDATA%\Programs\himalaya\himalaya.exe` (on your PATH). Downloaded
from the official GitHub release, SHA-256 verified. To add a mailbox, run `himalaya` in a terminal and answer
its wizard (address; for Gmail use an *app password*: Google Account → Security → 2-Step Verification → App
passwords). The password stays in Himalaya's own config. If you have several accounts, set
`XENIO_HIMALAYA_ACCOUNT=<name>` in `.env`. When both Gmail and Himalaya are set up, Gmail (through Google) is
used.

### GitHub

Works now for public data of `XENIO_GITHUB_USER` (`tanishqnarula12`, set in `.env`; verified against the
GitHub API: 24 public repositories). Without sign-in GitHub allows 60 requests an hour. For private
repositories and higher limits:

```powershell
gh auth login      # your browser signs you in; XENIO never sees or stores the token
```

## Capabilities and permissions

| Tool | Does | Permission |
|---|---|---|
| `xenio_calendar_events` | today, tomorrow, this morning/afternoon/evening, tonight, next, this week, next week, "next 3 days", a date; busy summary | READ |
| `xenio_calendar_create_event` | add an event | CONFIRM_REQUIRED |
| `xenio_email_search` | search (Gmail syntax), important/unread of the last 7 days | READ |
| `xenio_email_read` | one message (text, 6,000 characters max) | READ |
| *(sending email)* | **not available in V4.** No tool exists; `himalaya ... send/write/reply/forward` is blocked by policy | blocked |
| `xenio_github` | repositories, overview, issues, pulls, branches, commits, runs (CI / test results) | READ |
| git / gh writes | create branch, commit, push, PR, issue: through the terminal, as in V3 | V3 rules (CONFIRM / HIGH) |
| `xenio_tasks` | list; add / update / complete; delete | READ; SAFE_WRITE; CONFIRM |
| `xenio_project` | list / context; set_current / update / note | READ; SAFE_WRITE |
| `xenio_daily_briefing` | calendar + important email + tasks + current project + GitHub + scheduled checks | READ |
| `xenio_schedule` | list; create / remove a scheduled check | READ; CONFIRM |
| `xenio_notifications` | unread / recent; clear | READ; SAFE_WRITE |
| `xenio_integrations` | what is connected, and the setup step for what is not | READ |

Nothing secret is stored in tasks, projects, events or schedules: a password, key, token or card number
there is blocked.

## Tasks

Fields: title, description, status (pending, in_progress, done, cancelled), priority (low, normal, high), due
(today, tomorrow or a date), project, source (user, xenio, schedule), created, updated. Overdue tasks are
flagged. Stored in `personal.db`, separate from memory.

## Projects and context

"I'm working on the CRM analytics feature" → `xenio_project set_current` (focus: analytics feature). From
then on every turn carries one short line such as
`[XENIO context] current project: CRM (focus: analytics feature), repository tanishqnarula12/final-crm; other projects: Xenio; 2 open task(s)`.
So "check the backend" or "what issues are open in my repository?" refer to that project. If several projects
could match, XENIO asks which one. If nothing is recorded, it says it doesn't know. It never guesses.

"What was the last decision about this project?" → `xenio_project context` returns `last_decision` from
the notes (kind `decision`).

## Daily briefing

"Give me my daily briefing" → `xenio_daily_briefing` gathers each source separately. Its spoken `summary`
is built from the gathered records only. For example, on this PC today:

> Good evening. No pending tasks. On GitHub, your latest active repository is final-crm, with no open issues
> or pull requests across your recent repositories. I couldn't check calendar and email.

`python -m app.main --briefing` prints the same without a model call.

## Known limitations

* Calendar: the primary Google calendar only; events can be created but not edited or deleted (V4).
* Email: read-only; no sending, no labels or archiving. The first `important` query uses Gmail's own
  importance and star markers.
* Google sign-in in "Testing" mode expires after 7 days (publish the app to avoid it).
* GitHub without sign-in: public repositories only, 60 requests an hour (an overview costs 5).
* No Outlook / Microsoft calendar yet. Himalaya can read Outlook mail over IMAP.
