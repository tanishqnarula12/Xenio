# XENIO security model

One permission engine (`app/safety/policy.py`, standard library only) decides every action. It is
used in two places:

* **inside Hermes**, by the `xenio-guard` plugin (`pre_tool_call`), before *every* tool call;
* **in XENIO**, by the confirmation gate (`app/safety/permissions.py`), when Hermes asks for permission
  over ACP (edits, flagged commands, and everything the plugin escalates).

```
Hermes wants to run a tool
  └─ xenio-guard pre_tool_call → classify_tool_call()
        ├─ blocked            → refused; Hermes is told why; nothing runs
        ├─ READ / SAFE_WRITE  → runs
        └─ CONFIRM / HIGH     → {"action": "approve"} → Hermes' human gate → ACP request_permission
                                → XENIO asks: "This will … Do you want me to continue?" (HIGH: say "confirm")
                                → yes: allow_once        no / unclear / silence / timeout: reject
every tool result → secrets redacted; web sources recorded; browser page labels remembered;
                    outside content that tries to give instructions → flagged, and the rest of the turn asks
                    before anything but plain reading (V4)
next user messages after a "no" → a note that the "no" covered only that attempt (scoped, see below)
```

V4 additions in detail: [MEMORY_ARCHITECTURE.md](MEMORY_ARCHITECTURE.md),
[PROMPT_INJECTION_SECURITY.md](PROMPT_INJECTION_SECURITY.md), [PERSONAL_INTEGRATIONS.md](PERSONAL_INTEGRATIONS.md),
[PROACTIVE_AGENT.md](PROACTIVE_AGENT.md).

## Levels

| Level | Meaning | Examples |
|---|---|---|
| READ | runs | list/read files, `git status`, `git ls-remote`, `gh pr list`, web search, reading pages, `js("document.title")`, opening a page in your browser, reading calendar / email / GitHub / tasks, the daily briefing, `himalaya envelope list` |
| SAFE_WRITE | runs | create files/folders in your folders, `git add/commit`, create a branch, type into an ordinary field, click a link, submit a search, download a document, save a memory, add/complete a task, project notes |
| CONFIRM_REQUIRED | asks yes/no | delete/move files, `git push/merge/checkout`, delete a branch, submit forms, post/send/upload, `fetch()` from a page, accept a page dialog, coordinate clicks, `gh issue/pr create/comment`, `gh pr merge`, download a program, writing into `.git/` (hooks, config), git with a one-off setting or option that can start a program (`-c core.pager=…`, `--upload-pack`, `--template`…), create a calendar event, delete a task, create/remove a scheduled check, delete/move email, unknown commands/tools; after an injection in the turn: everything except plain reading |
| HIGH_RISK | you must say "confirm" | payments/purchases/checkout, password/2FA/account changes or deletion, typing into password/card fields, force push, `branch -D`, `reset --hard`, releases, repo settings, secrets, `gh auth login`, system folders, saved-login vault, setting up an email account, expunging mail |
| blocked | never | `.env`, SSH/private keys, credential stores, OAuth token/client files (`google_token.json`, `client_secret*.json`), saved browser passwords pages (`chrome://settings/passwords`, `about:logins`, `edge://wallet`), cookies and site storage (`document.cookie`, `localStorage`, `Network.getAllCookies`…), turning off certificate checks or CSP, printing tokens (`gh auth token`), env dumps, putting a secret into a page/link/memory/skill/task/project/event/schedule, **sending email (V4)**, Hermes' own `cronjob_manage` writes (use `xenio_schedule`), deleting system or top-level folders, links with a username/password |

## Web and browser rules

* **Links** (`classify_url`): http/https = READ; `file:` links follow the path policy; browser
  internal pages ask, their password/cookie/payment pages are blocked; `javascript:` is judged as
  JavaScript (at least CONFIRM); `mailto:`/`ms-settings:`/other app links ask; logins or API keys
  inside a link are blocked.
* **JavaScript** (`classify_js`): reading = READ; changing the page (`.value =`, navigation) =
  SAFE_WRITE; `.click()` judged by the words around it ("Next" SAFE_WRITE, "Submit/Send/Post/Delete…"
  CONFIRM, "Checkout/Pay/Buy/Place order/Delete account/Change password…" HIGH); `.submit()` asks
  unless it is a search form; `fetch`/XHR/beacon/WebSocket/clipboard always ask; `eval`/`Function`
  ask (cannot be checked); cookies/storage/credentials/password fields are blocked.
* **`browser_exec` code** (`classify_browser_code`): parsed as Python; every helper call is judged
  (`new_tab`/`goto_url` by link, `js` by JavaScript, `fill_input` by field and text, `click_at_xy`
  asks, `cdp` by method, uploads ask and their files follow the path policy); `os`/`subprocess`/file
  access asks (same as `execute_code`); unknown functions ask (fail closed).
* **Step-by-step tools**: clicks are judged by the element label from the latest snapshot
  ("button "Place order"" → HIGH); Enter after typing into a search box is a search, otherwise it
  may submit a form and asks.
* **Downloads**: see [WEB_CAPABILITIES.md](WEB_CAPABILITIES.md#xenio_download_file-apptoolswebpy).
  Never executed; never over secrets/system files; never overwriting without asking.

## Git and GitHub rules
See [WEB_CAPABILITIES.md](WEB_CAPABILITIES.md#git-and-github) — reading is free, local safe changes
run, anything that publishes or changes shared state asks, history rewriting, releases, settings
and credentials are HIGH_RISK, printing tokens is blocked.

## Memory
Hermes' `memory` tool is reused; see [MEMORY_ARCHITECTURE.md](MEMORY_ARCHITECTURE.md). Every memory write is
checked: API keys, tokens, private keys, card numbers, cookies, recovery codes, values of secret environment
variables and "password is …"-style statements are **blocked**, even when you ask. Saves the model makes on its
own (you did not ask) are dropped when they are temporary, guesses, conversation logs or duplicates. Skills
(`skill_manage`) are checked for real key/token values (placeholders like `${API_KEY}` are fine).

**V3, from Mission Control:** you can correct, delete and add memory entries yourself. XENIO edits Hermes' files the
way Hermes does (its `<file>.lock`, a re-read under the lock, an atomic replace, owner-only files), so neither can
lose the other's entries, and the same policy applies to your own edits: secrets are refused even when you type them,
entries stay under 400 characters and within Hermes' size limit. Activity records that memory changed, never the
text. A conversation already running is told on its next turn which entries were removed or corrected.

**V3, skills never gain power silently:** the model editing a skill's instructions stays a safe write (Hermes' learning
loop does this), now recorded by skill name. Adding a **script** to a skill (it could run later) and **deleting** a
skill ask first. Switching skills on and off is yours, in Mission Control, through Hermes' own `skills.disabled`
(Hermes' config backed up first; its essential skill cannot be switched off). Installing skills from outside is not
offered in Mission Control.

## Personal services and scheduled runs (V4)
* Google is connected with **your own** OAuth client and the smallest scopes (`gmail.readonly`,
  `calendar.events`); the token is a protected secret file, never logged or returned. Email is read-only.
* Scheduled checks run on Hermes' scheduler with only the `xenio` and `web` toolsets, under this same policy;
  every action that would need your "yes" is refused because nobody is there (`approvals.cron_mode: deny`).

## Screen vision (V5)
Details: [VISION_SECURITY.md](VISION_SECURITY.md).
* **Explicit capture.** XENIO looks without asking only when your own words refer to your screen ("look at my
  screen", "what's wrong here?") or right after a look. Otherwise it asks. Scheduled runs can never look.
* **Observe only.** `xenio_look_at_screen` cannot click, type or scroll; Hermes' `computer_use` is not
  available to XENIO. Every action after a look passes the normal policy.
* **Text, not pixels, and nothing kept.** The screenshot exists in memory only and is discarded after the
  description is made (also on errors). The audit log has sizes and timings, never content.
* **Secrets.** The vision model must not copy credentials, XENIO masks any it still copied, and the guard's
  redaction runs again. Nothing seen goes to memory unless you ask.
* **Instructions on screen are data.** They are flagged like a web page's, and the rest of that turn needs your
  yes for anything beyond reading.
* **No silent paid fallback.** The main model does the looking; if it cannot, the look fails with the reason.
* Hermes' `vision_analyze` (any image file or link) now follows the file and link rules: secret files blocked.

## Confirmation behaviour
* "yes" (or "confirm" for HIGH_RISK) runs that one action — XENIO always answers `allow_once`, never
  "always allow". The one exception is the task approval below, which is time-limited and confined.
* "no", silence, an unrelated sentence, a timeout or any error = **no**; short unclear answers get "Please
  confirm yes or no." (up to 3 tries). Only an explicit yes counts. Silence is reported truthfully as
  "no clear answer, so it was cancelled", not as "you declined".
* A new request is never an answer (text and voice alike): something with three or more real words beyond
  yes/no ("What time is it in Tokyo?", "No, play some music instead") ends the question at once as not approved, and
  XENIO handles it as the next request after the current one. Lines typed while XENIO was busy, before the question
  appeared, are kept as requests too (they used to be discarded).
* A "no" cancels only that attempt. Hermes' denial text says "Do NOT retry", which made the model
  refuse later, separate requests. So for up to 3 user messages after a "no", the plugin adds a note
  to the user's message. The note names the declined operation and says that asking again starts a
  new attempt, which XENIO will confirm again. The note stops as soon as that operation is attempted
  again (whatever the answer), and it never covers "Blocked by XENIO policy" actions.
* Under every answer XENIO lists what ran (`[done]`), what failed and why (`[FAILED] – reason`) and
  what you declined (`[declined]`). XENIO never claims success that a tool did not report.
* The tool result, not the model, is the source of truth (`truth_check` in `app/orchestrator.py`):
  - The reply claims success ("Done.", "I've created…") although every action failed or was declined:
    the reply is **replaced before it is spoken** by what really happened, e.g. "The operation failed:
    Google refused the request (HTTP 403)."
  - The reply says "you declined" although you said yes (observed live: `git push` was approved, git
    failed, and the model blamed a "no"): a correction is appended.
  - The reply says an action failed although the matching tool reported success: a correction is
    appended.

  - The reply says a reminder is set, no reminder was made in that turn, and Hermes' scheduler holds no XENIO
    reminder at all (observed: "reminder already set" when nothing was): a correction is appended.

## What an approval says (V3)

Every question carries, written by XENIO (not the model): **why** it asks (from the kind of action: "it permanently
deletes something", "it publishes or changes something outside this Mac"...), what it **reaches** (files, the command,
destructive targets), **who asked** (XENIO for your request, or which engineering agent on which task and run), and
**what a yes allows** (this action once; or, for "Allow for this task", the workspace for 30 minutes). "Decline and
stop" declines and stops the request that asked. The same record is answered from Home, Approvals, the orb, the quick
chat or by voice: the first answer decides, and every view drops the question when XENIO settles it.

## Writing into apps and web editors (V3)

XENIO can paste text (code, long answers) into the focused field through the clipboard - the user's clipboard is kept
and put back, and if it is too large to keep XENIO does not paste at all - type code line by line where pasting is
refused, and copy a page's or editor's text to read it. Pasting follows the typing rules (no secrets, no password
fields, a terminal asks first); copying off the screen needs the user's words to point at the screen, like a screen
look, and copied text is untrusted. What was written is read back before it is reported as written.

Reading a window's text (`xenio_gui` action `read`, macOS) uses the accessibility tree, the text VoiceOver reads: no
screenshot, no clipboard, nothing pressed. It follows the copy rules (the user's words must point at the screen; the
text is untrusted; refused in a live test), never includes password fields (they are not part of it), and for a web
page reads the page only, not the browser's toolbar or tabs. `editor=true` puts the keyboard focus into the window's
code editor (Ace, Monaco, CodeMirror, or a large text area) through accessibility, or by clicking into its text when
that does not take; it never picks a password field. Where a page refuses copying, what was written is checked against
the lines the editor shows, page by page. `run` (and `run=true` after writing) clicks only a button named Run,
Compile & Run, Run Code, Run Tests or Execute - never Submit, which stays the user's - and is refused in a live test
like reading and writing; the result area it returns is untrusted page content.

XENIO does not read questions or write answers in a window titled as a **live** test where outside help is not
allowed: a proctored exam, a hiring assessment (Codility, CodeSignal, HackerRank for Work...), an interview round, a
running contest. Practice is not affected: problem pages, mock tests and interviews, test series, quizzes, practice
exams, virtual or past contests. The title is a best-effort signal, not a proctor.

## The audit (V3)

Mission Control's Activity > Tool calls shows every tool call the guard saw, from its own log: requested, allowed,
blocked, asked, approved, declined, expired (silence), running, completed, failed. Hermes' own tools (terminal, files,
browser) report no result to the guard, so they are never shown as completed - "allowed to run" is all that is known.
Typed text quoted by the policy («…») never leaves the log.

## "Allow for this task" (the task grant)
Building something means many small steps in XENIO's own workspace (`~/Xenio` by default): create `game.js`, change it,
`npm install`, run it, delete a scratch folder. Asking for each one (16 times in a few days, 15 of them approved) is
noise, so a question can be answered once for the whole task:

* **Offered only for confined actions.** The policy records every resolved path an action writes, creates, moves or
  deletes (`Decision.touches`), plus the folder an install, a project run or a script runs in — after following any
  `cd` in the command. `PathPolicy.confined` is true only when all of them are inside the workspace and the action is a
  confirmable file change, an install, `npm run`-style project run, or a script **file** inside the workspace (never
  inline code such as `python -c` / `node -e`). One target outside — `cd ~/Documents && npm install`, `cp a.js
  ~/Desktop` — and it is not confined.
* **Checked in the backend.** The web confirmer accepts `approve_task` only for a request it marked confined; the
  voice's "yes to all" counts as a single yes otherwise. The gate (`PermissionGate`) then opens a grant: for 30 minutes,
  confined `CONFIRM_REQUIRED` requests are approved without asking and logged ("covered by the user's approval for this
  task"). It ends early on the page's End button, a new conversation or a pause. The page only shows it.
* **Never covered:** `HIGH_RISK` actions, blocked actions, anything with a target outside the workspace, git pushes,
  messages, settings, credentials — and, after the turn read outside content that tried to give instructions, nothing
  at all (the plugin never marks such a request confined, because the injected order could be that very action).
* Limits: an install run from the workspace still writes to the language's package cache or site-packages; a script
  XENIO wrote may itself do anything when run. The grant is the user's explicit, time-limited choice for a task they
  asked for, in the folder XENIO works in.

## Honest results and the user's own apps
* Every XENIO tool result carries `status`: `SUCCEEDED` only when nothing says otherwise, `UNVERIFIED` for an action
  without proof (`verified` not true: e.g. a system media key, a YouTube tab), `RUNNING`, `FAILED`, `NOT_SUPPORTED`
  (e.g. the app is not installed), `NEEDS_CLARIFICATION` (several apps or players could be meant),
  `NEEDS_PERMISSION` (the macOS setting is named, for the app XENIO runs in). Approval is not a result status: the
  guard asks before the call. The fast path never says "Paused." unless the player read back "paused".
* "Open Terminal and run X" runs X in a **new, visible window of the user's Terminal** (`xenio_open_app` with
  `command`, AppleScript `do script`; never typed into an existing window), and X is judged by exactly the rules of
  the hidden terminal tool: `echo` runs, `rm -rf` asks or is refused, secrets and `printenv` are blocked. The
  window's text is read back as outside content. The hidden terminal tool never counts as opening Terminal.
* Media control acts on the player that really plays: a paused Spotify is not what "pause" means while a browser
  tab plays (then the system key is pressed and reported as unverified); two paused players are a question.

## Secrets
* XENIO reads its own `.env` without putting keys into the environment; Hermes keeps its keys.
* The engineering service's socket answers only XENIO, its own `hermes acp` and the guarded Hermes gateway (peer
  pid), so an agent's command that obtained the token is still refused.
* `google_token.json` and `engineering/token.json` are owner-only files.
* All logs and the guard's audit log are redacted; tool results are redacted before the model sees them.
* Source records keep only short excerpts, redacted.

## What this does not protect against
* Commands run with your full Windows user rights (Hermes' local backend; no OS sandbox).
* A click's risk is judged from its label/selector text; a misleadingly labelled button can look harmless.
* Screen descriptions are text produced by a model: a secret written in an unusual format could slip past the
  masking, and the description becomes part of the Hermes conversation like any tool result.
* Allowlists are fixed in code; anything not recognised asks instead of running.


## Engineering sessions (V6)

Engineering agents are Hermes sessions XENIO registers with a role, a worktree and a file scope. For their tool
calls the guard applies, on top of everything above: only the role's tools (never `delegate_task`, memory, cron,
browser or desktop control, personal tools); writes only inside the task's worktree and allowed paths (never `.git`
internals); read-only roles run only READ-level commands; no git writes, package installs, network tools,
deployment tools or database clients for any role. The gateway's edit approvals for these sessions are answered by
the same engineering rules, never by the normal "your folders" rule. Applying a run's result to your files, pushing
its branch and opening a pull request are CONFIRM_REQUIRED actions of the `xenio_engineering` tool. Hermes 0.22 runs
ACP sessions inside its gateway: XENIO checks that the gateway carries its guard and refuses engineering work (and
`--health` fails) when it does not. Full details: [ENGINEERING_SECURITY.md](ENGINEERING_SECURITY.md).
