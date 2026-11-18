# XENIO V3 — Intelligence & Control

V3 makes every important state of XENIO observable and controllable in one place, **Mission Control**, without a
second runtime: Hermes still runs the assistant, XENIO's guard still decides every tool call, and Mission Control,
the floating orb and the quick chat are views of the same backend. Built on V2 (docs/V2_EVERYWHERE.md) and V1
(docs/UI.md).

```
python -m app.main --ui
 ├─ UI server (127.0.0.1, per-start key)      app/ui/*          API, live events (SSE), approvals, settings
 │   ├─ context engine                        app/context.py    "continue where we left off", from XENIO's records
 │   ├─ memory editing                        app/memory/files.py   Hermes' files, Hermes' lock, XENIO's policy
 │   ├─ tool-call audit                       app/ui/audit.py   the guard's own log, call by call
 │   └─ skills                                app/hermes/skills.py  availability; on/off via Hermes' config
 ├─ talk engine, Hermes gateway (+ xenio-guard: xenio_resume tool, resume note, removed-memory note)
 └─ XENIO (native helper, native/orb)         the orb, the quick chat, Mission Control's window, the shortcut
```

## Mission Control

`python -m app.main --ui` opens Mission Control **in its own Mac window** (the native helper's; named XENIO, with
XENIO's icon in the Dock while it is open). `--ui-browser` opens it in the browser instead; `--no-browser` opens
nothing. Every page reads XENIO's real records; there is no sample data outside the explicit demo build.

| Page | What it shows (and where it comes from) | What you can do |
|---|---|---|
| Home | the conversation and orb; **Needs your attention** (open questions, failed runs of the last day, broken parts, a lost connection); **Pick up where you left off** (the context engine); running work; your open tasks; **Coming up** (reminders and checks from Hermes' scheduler); recent activity; system summary | talk or type; answer approvals in the tray; start planned work |
| Agents | the configured roles; for each, the task it really runs now (a live ACP session), **for how long**, its model, the run's latest milestone; blocked work | open the run; **stop the run** (asks first) |
| Orchestration | every run (live, and saved ones from before a restart); the **lifecycle that run really went through** (a small change shows no go-ahead step; an explanation has no work step; "no tests ran" and "not verified" are never green); the dependency graph; a **task inspector** (role, dependencies, the files it may change, attempts, model, timings, the agent's summary, the failure); verification (XENIO's own test runs); **reviewer findings and acceptance criteria**; the **changes** (diff stat, and the full diff read with git from the run's workspace); timeline; agent turns; report | **ask the team** (plans in its own workspace; a team plan waits for your go-ahead); start / cancel; apply, push, open a PR (each asks in Approvals) |
| Tasks & projects | XENIO's task list; your projects; what XENIO built in its workspace, with each project's last run and **next step** | add, complete, reopen, delete tasks |
| Schedules | reminders and checks as Hermes' scheduler holds them, with each check's run history | add or remove a reminder |
| Skills & tools | every tool with its care level and **what went wrong lately** (from the audit); every Hermes skill with its **real availability** (platform, prerequisite commands on PATH), version, scripts, last change | switch a skill **on or off** (asks before switching off) |
| Memory | Hermes' memory, with its real size against Hermes' limit, and what is *not* memory | **correct, delete, add** entries |
| Activity | **Events**: requests (by size only), engineering, approvals, reminders and checks, changes (tasks, memory, skills, settings), system (start, stop, recoveries, voice trouble) — paged, filtered on the server. **Tool calls**: every call the guard saw and what became of it | filter; page back |
| Approvals | what is waiting, each with **why it is asked, what it reaches, who asked, what a yes allows**; recent decisions with who asked | allow once, allow for this task (workspace only), decline, **decline and stop** |
| Integrations, System | the same checks as `--health`, plus V3's **event stream** (live views, events), **scheduler**, **audio devices** and **memory** usage | run the checks again |
| Settings | voice, orb and summon, notifications, location, **text size**, **how long Activity is kept** (7, 30, 90 or 365 days) | each takes effect at once |

While the live stream is down, a banner says so and how old the shown state is; nothing is passed off as current.

## The orb, the quick chat and Mission Control are one product

* They share one backend: the same conversation, approvals, runs, settings and errors. An approval answered in one
  place disappears from the others when XENIO settles it; a stop in one is seen by all.
* **Quick chat** (V3 change, from the user's request): Chat in the orb opens a compact chat right there (the
  Option-Space style of ChatGPT and Claude), not a big window. See docs/V2_EVERYWHERE.md, "Chat or Voice". Typed
  requests there are answered on screen only. Mission Control's top bar has a Quick chat button
  (`POST /api/v1/overlay/summon`).
* **The "Open XENIO from its own link" bug**: the old chat window showed the sign-in screen because the helper set
  the page's route as soon as WebKit reported the page loaded, which can be before the page's script read its session
  key from the address; the key was wiped. Reproduced with the real helper, then fixed: the helper never touches the
  address of a page that has not started, a route goes into the first address instead, and a crashed page is
  reloaded with its key.

## Context engine (`app/context.py`)

"Continue where we left off" and "what should we fix next?" are answered from XENIO's own records, in milliseconds,
with no model call:

* sources: the engineering team's saved runs (`<workspace>/engineering/<run>/run.json`, written at every milestone),
  `personal.db` (current project, notes, open tasks), the activity log (recent failures);
* categories kept apart: project, current work (status, progress, tests XENIO ran, verified, applied, pushed, **next
  step**), what is left to fix (failed tasks, failing tests, reviewer and security findings by severity, unmet
  acceptance criteria), tasks, recent problems — and what is *not* included (saved memory is already in the prompt; the
  conversation is in the session; the screen is never kept);
* focus: a project or run named in the request, else live work, else the current project, else the only recent work;
  when several recent pieces of work fit, it says so and XENIO asks which one instead of guessing;
* for the model: the guard adds the brief to the turn when the user's words ask to continue (no extra tool call), and
  the read-only `xenio_resume` tool covers the rest. The text says that outside text in the records is data, and that
  only what is listed counts as progress.

Measured: 1.8 ms in-process and 4.8 ms through the API with this Mac's 21 saved runs; a test bounds 60 runs (only the
newest 30 files are read) under 250 ms.

## Memory (`app/memory/files.py`)

Correct, delete and add, under Hermes' own lock and format, with XENIO's memory policy (no secrets, even typed by the
user; under 400 characters; within Hermes' size limit). Entries are addressed by a hash of their text, so an entry
Hermes changed meanwhile is refused, never overwritten. Activity records that memory changed, never the text. A
running conversation is told on its next turn which entries were removed or corrected. Details:
docs/MEMORY_ARCHITECTURE.md.

## Trust and audit

* Approvals explain themselves (why, targets, who asked, what a yes allows); the policy is unchanged.
* Tool calls: requested, allowed, blocked, asked, approved, declined, expired (silence), running, completed, failed —
  from the guard's log. Hermes' own tools report no result to the guard, so they are never shown as completed.
* **Skills never gain power silently** (policy change): adding a script to a skill or deleting a skill now asks
  first; editing a skill's instructions stays a safe write, recorded by name and visible in Skills & tools. Skill
  on/off uses Hermes' `skills.disabled` (config backed up first; Hermes' essential skill cannot be switched off).
  Installing skills from outside is not offered in Mission Control.
* Activity keeps what XENIO did, never what the user typed or said, for as long as Settings says (pruned at once and
  at every start).
* docs/SECURITY_MODEL.md has the details.

## Measured on this Mac (MacBook Air, Apple silicon, macOS 26.6)

| What | Result |
|---|---|
| A change → its event on the live stream | 1.5 ms median, 5.7 ms max (5 runs) |
| API: snapshot, memory, activity (100), runs, agents, tasks, schedules, health | 0.4 – 1.6 ms each |
| API: context brief · tool-call audit (300 calls) · skills & tools | 4.8 ms · 5.5 ms · 21.8 ms (Hermes' skill list cached 30 s) |
| Mission Control open, "Hey Xenio" listening: XENIO · XENIO helper · Hermes gateway | 157 MB, ~2 % CPU · 72 MB, 0.1 % · 171 MB, 0.4 % |
| Rebuilding the helper (with its icon) | ~3 s (first build ~30 s) |

## Tests

| Suite | Result |
|---|---|
| Python (all but the live-model test) | **1,100 passed, 3 skipped** (V2 baseline 1,074; V3 adds 26 in `tests/test_v3_mission_control.py`) |
| Frontend (vitest) | **110 passed** in 3 consecutive runs (V2: 91; V3 adds 17 in `v3-mission-control.test.tsx`, a reconnection test, and the quick-chat tests) |

`tests/test_real_hermes.py` (a live model) was not run: it uses a paid model.

## Acceptance (V3 spec, section 12)

| # | Test | How it was checked |
|---|---|---|
| 1 | A task created through XENIO appears in Mission Control | **Live**: "add a task …" through XENIO's fast path created task #2; found in `/tasks`. This exposed a gap (tools in Hermes publish no event): Mission Control now re-reads tasks, reminders and memory after every turn (tested). The test task was deleted. |
| 2 | A real engineering run shows actual agent status | Automated (the fake ACP team); real saved runs render with their real lifecycle, findings and diff. A new live run was not started (it uses a model). |
| 3 | A backend event updates the UI | Automated; live latency above |
| 4 | An approval resolved in one interface updates the other | Automated (settled in "the orb" → gone from Approvals and the store) |
| 5 | A reminder reflects the scheduler | V2 automated; Home and Schedules read Hermes' scheduler only |
| 6 | A memory change persists after reload | **Live** on the real `MEMORY.md`: added, found after reload, a token refused, deleted; the file is byte-identical to before |
| 7 | A cancelled operation does not continue | Existing engineering cancel tests |
| 8 | A disconnect is visible | Automated (the banner); V1 connection status |
| 9 | Reconnection does not duplicate subscriptions | Automated (one connection at a time, resumes after the last event, each event once); live: the System page counts 2 live views (orb + Mission Control) |
| 10 | V2 voice, orb, desktop still work | Automated (orb, voice suites); live: talk engine, "Hey Xenio", orb and shortcut OK |

**Needs you at the Mac**: type in the quick chat over another app (the keyboard goes to it, and back on Esc or
Command-W; copy and paste); the Dock icon and the "XENIO" menu name while Mission Control is open; whether Mission
Control comes to the front at start (macOS may keep the terminal in front: it is then in the Dock and Command-Tab);
switch a skill off and on (tested against a fake Hermes, not on your real skills); "Decline and stop" on a real
question; asking the team from Mission Control (uses a model).

## Solving a coding problem on the screen (fix)

Observed: asked "solve this problem on my screen" on a GeeksforGeeks page, XENIO looked at the screen and answered
"Still can't type into Safari" without trying. The causes and the fixes:

* **Frozen rules.** Hermes freezes a session's system prompt when the session starts and reuses it for every later
  turn (plugin prompt sections are "new-session-only"). XENIO resumed one session for days, so the editor rules added
  since never reached the model - while its own earlier replies ("I can't type into Safari's editor", five times in
  that conversation) did. The saved session now carries a fingerprint of the guard's prompt functions
  (`app/hermes/fingerprint.py`); when the rules change, XENIO starts a new session. (The manual `POLICY_VERSION` had
  not been bumped - the fingerprint cannot be forgotten.)
* **The steps on the turn itself.** When the user's words ask for code on the screen (`policy.wants_screen_code`), the
  guard adds the steps to that turn: read the problem, write the complete program, paste it into the editor, type it
  where pasting is refused, run the sample, never Submit unasked - and to ignore earlier claims that it cannot.
* **Reading exactly.** `xenio_gui` action `read` returns the window's text from the accessibility tree
  (`app/desktop/pagetext.py`): a web page's whole text, also what is scrolled out of view, with 10^5 and x_i marked,
  in ~0.1 s. The screenshot description had read the constraint 1<=n<=10^5 as 10^6.
* **Writing without coordinates.** `editor=true` (paste, type, copy) finds the code editor and focuses it through
  accessibility (31 ms measured), else clicks into its text. Where the page refuses copying (GeeksforGeeks), what was
  written is checked through accessibility **page by page from the top** (editors draw only the lines in view;
  measured: a wrong line 9 of 57 passed a check of the 35 lines in view).
* **Compiling and debugging.** `run=true` on paste/type (or action `run`) clicks the page's Run / Compile & Run
  button - found by its exact name, never Submit - waits for the judge (it knows "Request Queued", "Compiling"...),
  and returns the result area with a status: `error` (compiler or runtime error), `wrong_output` (Your Output differs
  from Expected Output), `matches_expected`, `ran`, `no_output`, and what to do next. The turn's steps tell the model
  to debug and write again on error or wrong output (up to 3 times). Measured on GeeksforGeeks: 5.5 s per run.
* **Bugs found on the way, fixed and tested:** the clipboard save read `NSData.length` as a string in JavaScript for
  Automation, so `+=` concatenated digits and any clipboard with several types (a copied link) counted as "too big to
  keep" - every paste was refused; and GeeksforGeeks' editor took an autocomplete suggestion on a space ("pos ==
  len" became "possible == len"): code typing closed the suggestion list after every word (with Esc - replaced on
  macOS, see below).
* **Limit:** the model writes the solution. Typing, compiling and reading the result back are XENIO's and reliable;
  the algorithm is only as good as the model (measured: the free model's first program failed 388 of 400 random
  tests - now it at least sees the wrong output and must debug instead of reporting success).

### Practice workflow reliability (2026-10-25)

From a live GeeksforGeeks session, reproduced on a local practice page with the real Ace editor in Safari
(`tests/test_practice_workflow.py` covers each point):

* **Full screen.** An Esc the page does not use (no completion list open) ends Safari's full screen, and the per-word
  Esc found none open after `#include`. The keys typed during the animation were lost (`#includedc++.h>`), so every
  program was typed twice, and out of full screen the Dock covered Compile & Run. On macOS, code is now typed as
  insertions of up to 20 characters per key event, which the editor takes in one piece (no auto-closed bracket, no
  suggestion taken), and no Esc is pressed at all; a line that ends in a word gets a space and Backspace so Enter
  takes no suggestion. Full screen is measured before and after every action and put back if XENIO's own keys ended
  it; a click or a page's own full screen that ended is reported, never faked back. Measured on the same page and
  program (483 characters): 77.4 s with full screen lost before, 13.6 s with full screen kept after.
* **One write per program.** Each user request is a task (`app/desktop/task.py`: INSPECTING → SOLVING → WRITING →
  VERIFYING → VERIFIED → RUNNING → RESULT_CONFIRMED / RESULT_FAILED / INCONCLUSIVE, plus FAILED, CANCELLED,
  TIMED_OUT). The same program is not written again while the editor holds it ("already_written"), and is rewritten
  only on evidence (the editor shows other text), at most twice; a corrupted line is named, then typed once more.
* **Pasting.** GeeksforGeeks refuses pasting and copying (the user confirmed it): XENIO types there directly and reads
  the editor through accessibility. Elsewhere a refused paste (the editor unchanged) is typed in the same call.
* **One action at a time.** A second input action while one runs is refused (`BUSY`), a newer request in the same
  session cancels the older one's typing at its next key, actions end at 330 s (Hermes abandons a tool call at 420 s
  or on a stop, but its thread would go on typing), and Stop/Ctrl+C now stop XENIO's hands too.
* **Never XENIO's own window.** Reading, running and editor writes skip the orb's quick chat (it holds the keyboard
  while open; a program had been pasted into it). The policy is shown the same window the action works in, so the
  live-test guard sees the real title.
* **Honest results.** Each code write returns `outcome`: code entered, editor verified, compiled, tests passed (each
  True, False or unknown) and `say`, the sentence to tell the user. Only the run's own result sets compiled / passed
  (GeeksforGeeks' "took more time than expected" and "Test Cases Passed: a/b" are read). A covered Run button is
  pressed through accessibility. The orchestrator corrects replies that claim a write, a compile or a pass the
  results do not show; the same request arriving twice while it runs is run once.

## Not done, and limits

* Resuming an engineering run interrupted by a restart is not supported (the page says so; ask the team again).
* Installing or updating skills from Mission Control is not offered (use `hermes skills` in a terminal).
* Conversation history retention is Hermes' (its session store); Mission Control does not manage it.
* Microphone and speaker choice is still `.env` (`XENIO_INPUT_DEVICE` / `XENIO_OUTPUT_DEVICE`); System shows which
  are in use.
* Mission Control is dark only; Settings offers larger text and reduced motion.
* The quick chat panel, like the orb, is excluded from screenshots and screen sharing.
