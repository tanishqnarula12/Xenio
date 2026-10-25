# XENIO UI V1

A local interface over XENIO: the orb, a conversation you can type or speak into, and Mission Control (Home, Agents,
Orchestration, Tasks & projects, Schedules, Skills & tools, Memory, Activity, Approvals, Integrations, System,
Settings). It is a presentation layer: chat, approvals, the engineering team, schedules and memory are XENIO's real
components, and XENIO decides everything.

## Launch

```bash
cd ~/Desktop/Xenio/"Xenio Personal Ai"
cd ui && npm install && npm run build && cd ..   # once, and after UI changes
.venv/bin/python -m app.main --ui                 # opens http://127.0.0.1:8765 with this session's key
```

Options: `--ui-port 8766`, `--no-browser` (prints the address), `--new-session`, `--ui-no-voice` (leave the Mac's
microphone alone; the page then records in the browser), `--ui-no-orb` (no floating orb or summon shortcut; see
docs/V2_EVERYWHERE.md). Stop with Ctrl+C. The address XENIO
prints holds a per-start key in its `#token=…` part: opening plain `http://127.0.0.1:8765` shows a sign-in note
instead. Text, voice (`--voice`) and the UI are separate ways to run the same XENIO; run one at a time (they share
XENIO's saved conversation and the engineering service's socket).

Development: `--ui --ui-dev-origin http://localhost:5173`, then `cd ui && npm run dev` and open
`http://localhost:5173/#token=<the key XENIO printed>`. Demo data without XENIO: `npm run dev:demo` (every screen then
says "Demo data"; it is never mixed with a real XENIO). Tests: `cd ui && npm test` (frontend) and
`.venv/bin/python -m pytest tests/test_ui_backend.py` (backend).

## Architecture

```
browser (ui/)                                   XENIO process (python -m app.main --ui)
  React + TypeScript (strict), GSAP, lucide      app/ui/server.py      127.0.0.1 only, per-start token, Host/Origin
  src/api/client.ts   typed /api/v1 client  ───► app/ui/controller.py  every page -> a real component:
  src/api/client.ts   EventStream (SSE)     ◄─── app/ui/events.py        Orchestrator.process_text (fast path, Hermes)
  src/state/          store + controller         app/ui/assistant.py     PermissionGate + app/ui/approvals.py
  src/orb/            state machine, renderer    app/ui/approvals.py     EngineeringService (V6), TaskList, Projects,
  src/voice/          mic -> WAV, playback                               Hermes cron jobs, Hermes memory files,
  src/pages/          Mission Control                                    --health checks, tool registry, STT, TTS
```

* No second agent runtime, scheduler, memory or task manager: the controller reads and calls XENIO's.
* Voice is `--voice`'s own talk engine running inside the UI process (`app/ui/voice.py`, a `VoiceAssistant`): the
  Mac's microphone and speaker, "Hey XENIO", VAD endpointing, hedged speech recognition with name hints, the answer
  spoken sentence by sentence while Hermes writes it, "Xenio, stop", and listening for a follow-up. Spoken requests go
  through the controller's turn, so they appear in the conversation and drive the orb like typed ones. The page's
  microphone button asks the engine to listen; typed answers are spoken by it too when "Speak every reply" is on. The
  page only sees the engine's phase and a level for the orb; no audio and no key reach the browser. When the engine
  cannot run (no speech recognition, no microphone, `--ui-no-voice`), the reason is shown and the page falls back to
  recording 16 kHz WAV in the browser and playing XENIO's TTS itself.
* Pages load lazily; the main bundle is ~121 kB gzipped (React, GSAP, the shell).

## API (v1)

All under `/api/v1`, all with `Authorization: Bearer <token>`; changes also need `Origin: http://127.0.0.1:<port>`.

| Method | Path | What |
|---|---|---|
| GET | `/snapshot` | assistant state, conversation, pending approvals, Hermes, voice availability, `seq` |
| GET | `/events?since=<seq>` | Server-Sent Events (below) |
| POST | `/chat` `{text, via}` · `/chat/cancel` · `/chat/new` | a request (409 while one runs) · stop · new conversation |
| POST | `/assistant/pause` `{paused}` · `/assistant/dismiss` | pause/resume · dismiss the error state |
| GET/POST | `/approvals` · `/approvals/<id>` `{decision, confirm_text}` | open + recent · answer (`approve` / `deny`) |
| GET | `/engineering/runs` · `/engineering/runs/<id>` · `.../report` · `.../diff` | runs (live and from earlier starts) · detail · report · the run's changes (git, read-only) |
| POST | `/engineering/runs` `{request, project, path, mode}` | V3: ask the team from Mission Control (it plans in its own workspace; a team plan waits for the go-ahead) |
| POST | `/engineering/runs/<id>/<approve or cancel or apply or push or pull_request>` | apply/push/PR ask in Approvals |
| GET | `/agents` | the 8 roles, their enforced tools, what each is really doing |
| GET/POST/DELETE | `/tasks` · `/tasks/<id>` `{action: complete or reopen}` | XENIO's personal task list |
| GET | `/projects` · `/schedules` · `/health?force=1` · `/tools` | views (tools carry their recent failures; skills their availability) |
| GET/POST | `/memory?q=` · `/memory` `{action: add (store, text) / correct (id, text) / delete (id)}` | V3: Hermes' memory with its usage; the user's own changes, under Hermes' lock and XENIO's memory policy |
| GET | `/activity?limit=&offset=&kind=` | V3: persisted activity, newest first, paged; kind = requests, engineering, approvals, scheduled, changes, system |
| GET | `/audit?limit=` | V3: every tool call the guard saw and what became of it |
| GET | `/context?q=` | V3: the context engine's brief (focus, work, what is left to fix, tasks) |
| PUT | `/skills/<name>` `{enabled}` | V3: a Hermes skill on or off (Hermes' `skills.disabled`, config backed up) |
| GET/PUT | `/settings` | `motion`, `speak_replies`, `orb_detail`, `notifications`, `transcript_lines`, `wake_word`, `desktop_notifications`, `voice_approvals`, `orb_overlay`, `orb_overlay_mode`, `summon_shortcut`, `summon_interval_ms`, `device_location`, `orb_summon_mode`, `activity_retention_days` (7/30/90/365), `text_size` (validated) |
| POST | `/approvals/<id>` `{decision: approve_task}` · `/approvals/grant/end` | allow the rest of this task in the workspace (only for confined requests) · end it |
| POST/DELETE | `/reminders` `{text, when}` · `/reminders/<id>` | a reminder on Hermes' scheduler (no model) · remove it |
| GET/PUT | `/location` `{label, lat, lon, source}` | where the user is (typed in Settings, or from the Mac via XENIO Orb) |
| POST | `/overlay/status` · `/overlay/metric` `{name, ms}` · `/overlay/summon` `{mode?: chat or voice}` | what XENIO Orb set up · a latency it measured · open the quick chat / listen (Mission Control's Quick chat) |
| POST | `/voice/listen` · `/voice/finish` | the talk engine listens now (409 while XENIO works) · the user is done talking |
| GET | `/voice/level` | the engine's level for the orb (its microphone, or XENIO's voice while speaking), 0..1 |
| POST | `/voice/transcribe` (audio/wav) · `/voice/speak` `{text}` -> audio/wav | XENIO's STT / TTS (the browser fallback) |

Errors are `{"error": "<plain sentence>"}` with a real status (401, 403, 404, 409, 413, 421, 422, 502, 503, 504).

### Events

Envelope: `{"v": 1, "seq": 42, "id": "…", "type": "…", "at": "2026-10-18T10:00:00", "data": {…}}`. `seq` grows by one;
a reconnecting page asks `since=<last seq>` and gets what it missed, or `stream.reset` when that is too old (it then
reloads the snapshot). `id` lets the page drop duplicates; events older than the last seen are ignored.

Types: `assistant.state`, `chat.user`, `chat.chunk`, `chat.tool`, `chat.done`, `chat.reset`, `request.received`,
`assistant.error`, `approval.requested`, `approval.settled`, `engineering.run`, `engineering.milestone`,
`notification`, `task.changed`, `settings.changed`, `health.changed`, `voice.changed` (the talk engine started, stopped
or "Hey XENIO" was switched), `grant.changed` (a task approval started, was used or ended), `schedule.changed`,
`location.changed`, `overlay.changed`, V3: `memory.changed` (store and action, never the text), `skill.changed`,
`system.event` (XENIO started or stopped, recoveries such as a gateway restart or the orb restarted, voice trouble),
`stream.reset`. `settings.changed` carries `changed` (the keys). Activity-type events are also
kept in `<data dir>/ui/activity.jsonl` (redacted; requests by size only; typed text quoted «…» by the policy is
replaced by «…»).

## The orb

Sixteen states. The backend decides what XENIO is doing (`app/ui/assistant.py`), including what its talk engine is
really doing with the Mac's microphone and speaker; with the browser fallback the page adds what it observes itself:
its microphone recording (LISTENING), the recording being transcribed (PROCESSING), TTS audio actually playing
(SPEAKING).

| State | From | Looks like |
|---|---|---|
| DISCONNECTED | no backend / stream lost | dim grey, small, still |
| CONNECTING / INITIALIZING | Hermes starting | slow forming sphere |
| IDLE | nothing running | calm xenon sphere |
| LISTENING | the talk engine (or the page) is recording | warm, ripples with the real mic level |
| PROCESSING | a request just started | tighter, faster |
| THINKING | the model is working, no tool | a slow knot |
| SEARCHING | a lookup tool runs (web, files, mail, calendar, screen) | three scanning orbits |
| PLANNING | the engineering team plans | ordered latitude rings turning in steps |
| ORCHESTRATING | 2+ agents really working | a core with one satellite per agent |
| EXECUTING | an acting tool runs, or one agent works | a lit meridian sweeping round |
| SPEAKING | XENIO's voice is playing (the Mac's speaker, or the page) | lobed sphere, a ring per syllable of the real audio |
| WAITING_APPROVAL | the gate asks the user | amber, slow swell |
| PAUSED | the user paused XENIO | frozen, grey |
| COMPLETED | a request finished well (2.5 s) | ring and check |
| ERROR | the last request failed (until the next one or a dismissal) | coral, slight unrest |

Rules (`src/orb/machine.ts`): COMPLETED and SPEAKING can never appear without their cause (an impossible transition
keeps the current state); anything else the backend reports after a reconnect is shown as it is; priority is
no backend > a human decision > what the page observes > XENIO's state; background work (V6) is reported separately so
"speaking while two agents work" is not a contradiction. The renderer (WebGL particles, GSAP-tweened transitions on
GSAP's ticker) draws nothing off-screen or in a hidden tab, and one still frame per state with reduced motion. Without
WebGL the orb is a still glow; its meaning is always in words too (`role="img"` + label, the heading on Home).
The big orb on Home also plays with the pointer (particles part around it and light up, the sphere turns towards it,
a click sends a ripple) and glows in its state's colour; neither carries meaning, and both stop with reduced motion.

## Layout

Home puts the orb on a stage beside the conversation (above it in a narrow window); the conversation fills the
height, with the composer at the bottom. The sidebar collapses to an icon rail (top-left button); Home's side panel
can be dragged wider or narrower (or resized with the arrow keys), hidden to focus on the conversation (top-right
button), and each of its sections minimised to one line. The orb's size has five steps (the − and + under it) and it
grows into the room freed by hiding panels. These choices are kept in this browser only (localStorage).

## Security boundaries

* 127.0.0.1 only; the `Host` must name this server (DNS rebinding); changes need XENIO's own `Origin`; every API call
  needs the per-start token. The token lives in the URL fragment (never sent, removed from the address bar) and this
  tab's session storage. Other pages, other users and commands an agent runs cannot call the API.
* The page is served with `script-src 'self'`, `connect-src 'self'`, `frame-ancestors 'none'`, no inline script.
* Approvals are decided by the PermissionGate: the browser can only answer an open question; HIGH_RISK needs
  "confirm", checked on the server; silence expires as "not approved"; an answer to a closed question is refused.
* Everything returned is redacted with XENIO's secret redactor (configured keys and secret-looking environment values);
  tool titles, ids and replies included. Outside text (answers, reports, web titles) is rendered as text, never HTML;
  only http(s) links are links.
* Demo data exists only in a demo build and says so on every screen.

## Known limitations

* Memory is read-only here (Hermes owns writing it; ask XENIO to remember or forget). Scheduled checks are created
  through chat (inside the guard); reminders and tasks can be added and removed here too.
* While the UI runs with the talk engine, the Mac's microphone is open the whole time (macOS shows its microphone
  indicator), as in `--voice`. Switch off "Hey XENIO" in Settings (the microphone button still works) or start with
  `--ui-no-voice`. Approvals are asked out loud too and can be answered by voice ("yes", "no", "yes to all"),
  on Home, in the floating orb or on the Approvals page; the first answer counts.
* One XENIO at a time: the UI, text and voice modes share the saved conversation and the engineering socket.
* CPU, memory and uptime are not measured, so they are shown as not measured.
* The orb's look was checked in Safari on this Mac; other browsers were not tried.
* Activity history keeps the last ~2 MB (one rotation).
