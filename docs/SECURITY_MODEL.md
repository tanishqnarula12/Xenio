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
  "always allow".
* "no", silence, an unrelated sentence, a timeout or any error = **no**; unclear answers get "Please
  confirm yes or no." (up to 3 tries). Only an explicit yes counts. Silence is reported truthfully as
  "no clear answer, so it was cancelled", not as "you declined".
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

## Secrets
* XENIO reads its own `.env` without putting keys into the environment; Hermes keeps its keys.
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
