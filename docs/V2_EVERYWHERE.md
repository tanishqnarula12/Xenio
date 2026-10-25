# XENIO V2 — Everywhere

XENIO within reach while you work in any app: say **"Hey Xenio"** or press **Space three times**, the orb appears
over what you are doing, listens, answers out loud, can look at your screen when you ask about it, and asks its
questions (approvals) right there. Built on V1: the same talk engine, orchestrator, guard and approvals — no second
runtime.

## What runs

```
python -m app.main --ui
 ├─ UI server (127.0.0.1, per-start key)          app/ui/*         the page, SSE, approvals, reminders, settings
 ├─ talk engine (Mac mic + speaker)               app/ui/voice.py  "Hey Xenio", VAD, hedged STT, streamed TTS
 ├─ Hermes gateway (sessions, tools, guard)       hermes_plugin/   restarted automatically after XENIO updates
 └─ XENIO Orb (child process, native)            native/orb/      floating panel + summon shortcut + location
      └─ WKWebView: the same page, ?view=orb      ui/src/overlay/  shows XENIO's real state; approvals; caption
```

* **XENIO Orb** (`native/orb/main.swift`, `TripleSpace.swift`) is a ~200 KB native helper. XENIO builds it from
  source the first time (`native/orb/build.sh`, ~30 s, Apple's command line tools), starts it as a child process and
  passes the page's address with the session key **on its stdin** (never on the command line). It quits as soon as that
  pipe closes, so it cannot outlive XENIO (verified: stopping XENIO stopped it). It is restarted after a crash, at most
  3 times in 10 minutes. `--ui-no-orb` or Settings > Floating orb switches it off.
* The orb is a borderless, **non-activating** panel: it never takes the keyboard focus from your app. It joins every
  Space, may sit beside full-screen apps (`fullScreenAuxiliary`), and is **excluded from screenshots and screen
  sharing** (`sharingType = .none`), so "look at my screen" never sees it. Drag it anywhere; the position is kept and
  clamped to a visible screen when displays change. It shows only while XENIO is listening, working, speaking or
  asking (and ~7 s after), or always as a small orb (Settings).
* The panel's content is XENIO's own web page (`?view=orb`): one codebase, the same state machine and security checks
  as Home. It never plays audio (the talk engine speaks).

## Summon shortcut

| Shortcut | Needs | Behaviour |
|---|---|---|
| **Triple-press Space** (default) | Accessibility for the app XENIO is started from (it is a child process, so no new prompt when already granted) | Space 1 reaches your app at once. A quick Space 2 is held for up to the interval (450 ms, 200–1000 in Settings): if Space 3 follows, Spaces 2 and 3 are swallowed and XENIO listens — your app received exactly one space. Otherwise Space 2 is released, in order, before any other key. Key repeat, modifiers and any other key start over, so typing "a b c" never summons. |
| **Control-Option-Space** | nothing (a registered hot key) | Used automatically when triple-Space cannot be set up. |
| Off | — | — |

The event tap runs on its own thread and run loop, so a busy page can never delay your typing; events it re-posts are
marked and pass through. Secure input (password fields) is invisible to it by design.

**Trade-offs, honestly:** a double space typed quickly arrives up to one interval late. Anything that taps Space
fast (a game, rapid play/pause) can summon XENIO — switch to Control-Option-Space or off in Settings for that. macOS
does not let any app intercept keys during secure input, and an event tap needs Accessibility; nothing private is used.

### Chat or Voice

A summon (triple Space or Control-Option-Space) asks how: **Chat** or **Voice** (Settings > Floating orb > When
summoned can skip the question and always open the chat, or always listen).

* **Chat** turns the orb into the **quick chat**, right where it is, like the Option-Space windows of ChatGPT and
  Claude: the same conversation as Mission Control, a message box with the cursor in it, approvals and plans waiting
  for you. The panel takes the keyboard the way Spotlight does, without making XENIO the active app, so your app gets
  the keyboard back when you close it (Esc, Command-W or the close button). Copy, paste, undo and select-all work.
  While you type there, triple-Space is paused (every Space is just a space). It stays open until you close it; a
  summon puts the cursor back in it. **Typed requests are answered on screen only**: XENIO does not read them aloud.
* **Voice** listens, as after "Hey Xenio".
* Back and forth: the quick chat's microphone button switches to voice (that answer is spoken, and shows in the chat);
  while XENIO listens or talks, the orb's chat button opens the quick chat on the same conversation.
* **Mission Control** (the full window) opens from the quick chat's expand button, or from links in it ("See the
  plan" opens that run's page). It is XENIO's full page in a real Mac window; Mission Control's own top bar has a
  Quick chat button that opens the orb's chat.

An earlier version opened Mission Control itself for Chat; it showed "Open XENIO from its own link" because the
helper set the page's route as soon as WebKit reported the page loaded - which can be before the page's script has
read its session key from the address - and so wiped the key. The helper no longer touches the address of a page that
has not started (a route is put into the first address instead), and a page that crashes is reloaded with its key.
XENIO can also summon the quick chat itself (``POST /api/v1/overlay/summon``, one JSON line on the helper's stdin).

## Full screen, Spaces and protected windows

* Over normal windows, on every Space: yes (`.floating` level, `canJoinAllSpaces`).
* Next to full-screen apps: `fullScreenAuxiliary` lets the panel join a full-screen Space where macOS allows it. Some
  full-screen and protected surfaces (DRM video, the login window, secure input, some games) never show other apps'
  windows; XENIO does not try to force it. There, "Hey Xenio" still works (you hear it), the desktop notification
  shows, and the page has everything.

## Screen-aware help

"Hey Xenio, explain this error" uses V5's `xenio_look_at_screen`: the screen (or the active window) is captured at that
moment, described by the configured vision model (Settings/health shows which: a free model via OpenRouter here), and
discarded; the conversation gets text, never pixels. It runs without asking only when your own words refer to the
screen, after a yes otherwise, never unattended. Text seen on the screen is untrusted data: reported, never followed.
The orb is not in the capture. If capture or vision fails, XENIO says so instead of guessing.

## Also new in this round

* **Approvals:** on Home, questions are pinned above the message box (they used to scroll away inside the
  conversation); in the orb; and out loud ("yes", "no", "yes to all"). **Allow for this task** lets XENIO finish a task in
  its workspace without asking again for 30 minutes — see docs/SECURITY_MODEL.md for exactly what it covers.
* **Reminders** run on Hermes' own scheduler as no-model script jobs: on time (measured 11 s after the minute), a
  desktop notification, spoken, on the page. "Remind me at 3 to …" is done by XENIO itself in under a second.
* **Tasks:** "add a task …", "what are my tasks", "mark … as done" are done by XENIO itself; the model is told that
  `todo_list` is its private scratchpad, not your list.
* **Desktop notifications** for reminders, scheduled checks, approvals and the engineering team (from "Script Editor":
  allow it in System Settings > Notifications if none appear).
* **Location:** set it in Settings, or let the Mac provide it (XENIO Orb asks macOS once; Apple's geocoder names the
  place). The `xenio_location` tool gives Hermes the place and local time for weather and "near me".
* **Agents page:** the planner and the reviewers now show as working during planning and review.
* **Projects:** what XENIO built in its workspace, with the engineering team's runs on each.
* **Stale code in Hermes' gateway:** the gateway keeps the XENIO code it loaded; XENIO now restarts it at start when
  the code changed (it had kept an old `xenio_schedule` alive).

## Measured on this Mac (MacBook Air, Apple silicon, macOS 26.6)

| What | Result |
|---|---|
| Idle (orb hidden, no voice): XENIO / XENIO Orb / the orb's page process | 58 MB, 0 % · 65 MB, ~0 % · 50 MB, 0 % CPU |
| Listening for "Hey Xenio" (talk engine on): XENIO / XENIO Orb | 199 MB, 2.4–5.8 % of one core · 65 MB, 0 % |
| Reminder by the fast path ("remind me in 1 minute …") | reply < 1 s; delivered 11 s after its minute |
| `hermes cron create` | 0.53 s |
| Stale-gateway check at start | 1.3 ms |
| Building XENIO Orb (first time only) | 26 s |
| Wake word → orb on screen, summon → listening | measured live by the orb; shown in Settings > Floating orb |

## Acceptance tests

| # | Test | Outcome |
|---|---|---|
| 1 | V1 behaviour intact | Automated: 1,000+ Python and 87 frontend tests pass |
| 2 | Wake word while XENIO's window is not focused | Talk engine verified earlier (mic is global); with the orb: **manual, pending** |
| 3 | No duplicate listeners | One talk engine per process; one orb per user (lock file; a second exits with "already running") — automated |
| 4 | Summon shortcut in other apps | Tap installed and reported `ok` on this Mac (Accessibility granted); typing in real apps: **manual, pending** |
| 5 | Normal typing / Space preserved | State machine self-test (in the binary, run by pytest); real typing: **manual, pending** |
| 6 | Repeated summons → one orb | One panel per helper, one helper (lock) — automated |
| 7 | Orb follows real backend states | Automated (page) + live: the helper's page connected and reported status |
| 8 | Dismiss and summon again | Automated (page) |
| 9 | Audio interruption | V1 ("Xenio, stop", Stop button) |
| 10 | Follow-up conversation | V1 talk engine (follow-up listening) |
| 11–13 | Screen-aware: current capture, denied permission, protected content | V5 tests (automated); live capture: **manual, pending** (screen locked during this work) |
| 14 | Desktop actions report verified outcomes | V1/V2 tests; live task approval and file creation verified in the workspace |
| 15 | Backend disconnect | Page shows "Not connected"; the helper exits with XENIO (verified) |
| 16 | Mic/audio released | Talk engine closes mic and speaker on stop (V1) |
| 17–18 | Multiple displays, Spaces, full screen | Clamping and collection behaviour implemented; **manual, pending** |
| 19 | Idle resource use | Measured (above) |
| 20 | Security and approval tests | Automated: task grant (23 tests incl. injection), guard, policy |

**Pending on your Mac** (needs you at the keyboard): say "Hey Xenio" in another app and watch the orb; press Space
three times in Notes and in a terminal (and type normally); try a full-screen app and a second Space; drag the orb;
"explain what's on my screen"; answer an approval by voice; allow notifications for Script Editor if none appear.

## Not done yet

* Microphone and speaker selection in the page (set `XENIO_INPUT_DEVICE` / `XENIO_OUTPUT_DEVICE` in `.env` for now).
* Launch at login.
* Per-app exclusions for the triple-Space shortcut (use Control-Option-Space where Space is tapped fast).
