# XENIO — personal AI operating system (powered by Hermes)

XENIO is the voice, interface, safety and orchestration layer around an installed
[Hermes Agent](https://github.com/NousResearch/hermes-agent). Hermes does all the thinking —
memory, skills, tools, subagents, web, terminal, files. XENIO gives it a terminal and a
hands-free voice interface, and makes sure dangerous actions are confirmed by you first.

```
microphone → local wake word → STT → XENIO → Hermes (ACP) → reply → TTS → speaker
```

Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · how Hermes is reached:
[docs/HERMES_INTEGRATION.md](docs/HERMES_INTEGRATION.md) · setup: [docs/SETUP.md](docs/SETUP.md) ·
web/browser/GitHub: [docs/WEB_CAPABILITIES.md](docs/WEB_CAPABILITIES.md) · permissions:
[docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md) · V4: [memory](docs/MEMORY_ARCHITECTURE.md) ·
[personal integrations](docs/PERSONAL_INTEGRATIONS.md) · [proactive agent](docs/PROACTIVE_AGENT.md) ·
[prompt-injection security](docs/PROMPT_INJECTION_SECURITY.md)

## Quick start

macOS (this Mac):

```bash
cd ~/Desktop/Xenio/"Xenio Personal Ai"
.venv/bin/python -m app.main --health           # system check (names any missing macOS permission)
.venv/bin/python -m app.main                    # text mode
.venv/bin/python -m app.main --voice            # voice mode: say "Hey Xenio", then your request
```

Windows:

```powershell
.\.venv\Scripts\Activate.ps1          # or prefix commands with .\.venv\Scripts\python.exe
python -m app.main --health           # system check (add --live to call Hermes/STT/TTS for real)
python -m app.main                    # text mode
python -m app.main --voice            # voice mode: say "Hey Xenio", then your request
python -m app.main --connect-google   # one-time: Calendar + read-only Gmail (your own Google OAuth client)
python -m app.main --briefing         # the daily briefing, printed (no model call)
```

First-time setup on a new machine: see [docs/SETUP.md](docs/SETUP.md).

## Instant commands (fast path)

Simple commands never wait for the model: XENIO does them itself and answers in well under a second (measured on
the MacBook Air: the time 0-2 ms, volume 0.13 s, opening an app with its window verified 0.8 s, quitting one 0.08 s,
finding a song 0.9 s and opening exactly one tab).

* "Open Spotify" · "switch to Safari" · "minimize Discord" · "close Calculator" · "open YouTube"
* "Play Kesariya" · "Tum Hi Ho chalao" · "play Believer on Spotify" · "pause" · "agla gaana" · "previous song"
* "Volume up" · "awaaz kam karo" · "volume 40" · "mute" · "what time is it" · "kitne baje hain"

The same safety policy decides first; anything that needs your yes, or that is more than such a command, goes to
Hermes as before (and Hermes is told what XENIO just did, so "close it" works). Songs open on YouTube in one tab;
with `SPOTIFY_CLIENT_ID`/`SPOTIFY_CLIENT_SECRET` in `.env` they play in the Spotify app. `XENIO_FAST_PATH=false`
turns it off.

## Text mode

```
XENIO > hello

XENIO:
Hello! I'm XENIO. How can I help you today?
```

Commands: `/new` (fresh conversation), `/memory [words]` (what Hermes remembers), `/help`,
`/exit`. **Ctrl+C** while XENIO is answering stops the answer; at the prompt it quits. The
conversation continues across restarts (`--new-session` starts over).

## Voice mode

* Say **"Hey Xenio"** (or just "Xenio") and your request — in one breath or after the beep. Replies
  are printed and spoken.
* **XENIO waits for you to finish, like a person.** It starts transcribing at your first pause; once
  your words form a finished sentence your turn ends right away, but if they trail off ("open the…",
  "search for, um…") it waits up to 2 s for the rest instead of cutting you off. Silero voice-activity
  detection runs on the device and ignores fans and background noise.
* **Speech recognition**: nabh.cloud's Whisper large-v3-turbo (set to English in `.env`), about
  1.3–2 s per request. The connection is kept open and pre-warmed the moment you start talking.
  (On-device Moonshine is still available with `XENIO_STT_ENGINE=local`, but it misheard most real
  requests, e.g. "Stop Xenio" became "Stubbs's Inyo".)
* **Ending a conversation**: "thanks", "that's all", "stop", "never mind", "bye" get an instant reply
  ("Anytime.", "Okay.") and XENIO goes back to sleep — Hermes is not asked. "Okay" / "no" right after
  XENIO asked you something count as your answer. Background sounds that are not words are ignored.
* **Real time**: XENIO starts speaking the first sentence of the answer while Hermes is still writing the
  rest. If the answer takes a moment it says so right away ("One moment.") and tells you what it is doing
  ("Checking your calendar.", "Searching the web."). A sentence that claims a result ("Done!") is only
  spoken after the tool results confirm it. One audio stream stays open, so speech starts in milliseconds.
  Every part can be switched off in `.env` (see `.env.example`).
* **Conversation mode**: after each answer XENIO keeps listening for 8 s, so just keep talking —
  no wake word needed. Stay quiet and it goes back to sleep until "Hey Xenio".
* Spoken answers are short (1–3 sentences) unless you ask for detail.
* **Interrupt any time** by saying **"Xenio"** (or "Xenio, stop" / "Hey Xenio") while it thinks or
  talks: it goes quiet within a fraction of a second and listens to what you say next
  ("Xenio — actually, what about tomorrow?"). When XENIO's own voice does not reach the mic
  (headphones, or this laptop's built-in echo cancellation) you can interrupt by **just talking**:
  XENIO measures its echo while it speaks and enables this automatically; it reacts only to speech
  (not to a cough or music), and the words you said while interrupting are kept. Check it with
  `python -m app.main --barge-in-test`.
* `--no-wake` = push-to-talk (press Enter, then speak).
* Wake-word and voice-activity detection run locally. Only the audio of your request (never anything
  before the wake word, and nothing while XENIO is idle) is sent to the speech-recognition API.

## What XENIO can do on your computer

Just say it — no tool names needed. Hermes picks the tool, XENIO checks it, then it runs:

* "Create a folder called Projects on my Desktop" · "List the files in Downloads"
* "Create notes.txt in that folder containing …" · "Rename report.docx to final.docx"
* "Delete the old folder" (asks first; goes to the Recycle Bin) · "What's my Python version?"
* "Run the tests" · "What's the git status of this project?"

## Web, browser and GitHub

* "Search the official Hermes documentation" · "Research how MCP works" · "Find the cause of this error"
  — Hermes searches (`web_search`), reads the best 1–3 sources (`web_extract`) and answers from them.
* "Research whether I should use PostgreSQL or MongoDB for this project" — looks at the project
  first, then compares sourced facts and keeps them apart from its own conclusion.
* "Open GeeksforGeeks" / "show me the Strava API docs on my laptop" — opens the page in **your**
  browser (`xenio_open_url`). Hermes' own browser (for reading dynamic pages, clicking, typing) is
  invisible unless you set `XENIO_SHOW_BROWSER=true`.
* "Download the PDF from …" — saved to Downloads, never over an existing file, never opened or run.
* "Create a branch", "commit these changes", "push the branch", "show me open issues" — git through
  Hermes' terminal; GitHub through `gh` when installed (otherwise public GitHub data via the web).

Under every reply XENIO prints what actually ran and how it ended (`[done]` / `[FAILED] – reason` /
`[declined]`), straight from Hermes' tool results, and the **sources** a web answer used (title,
link) — XENIO never reports something as done that the tool didn't confirm, and never shows a
source that was not actually searched or read.

## Personal context (V4)

* "What do I have tomorrow?" · "What's my next meeting?" · "How busy is my calendar today?" (Google Calendar)
* "Do I have any important emails?" · "Find the email from the dean about exams" (read-only; XENIO cannot send email)
* "What issues are open in my repository?" · "Did the tests pass on final-crm?" (GitHub)
* "Add a task: finish the CRM report by Friday" · "What tasks are pending?" · "Mark it done"
* "I'm working on the CRM analytics feature" → later "check the backend" means that project; "What was the
  last decision on this project?"
* "Remember that the CRM backend uses PostgreSQL." Lasting facts only, never secrets; see
  [docs/MEMORY_ARCHITECTURE.md](docs/MEMORY_ARCHITECTURE.md).
* "Give me my daily briefing." Calendar, important email, tasks, project and GitHub, with unavailable parts named.
* "Every morning at 8, summarize my calendar." A read-only scheduled check on Hermes' scheduler (asks first;
  recurring at most every 15 minutes). Results arrive as notifications, printed or spoken when XENIO is idle.

Not connected yet? XENIO says so and gives the setup step; it never invents events, emails or tasks.
`--health` shows what is connected. Emails, web pages, issues and documents are treated as **data**: text in
them that tries to give orders is flagged, and XENIO then asks before doing anything but reading
([docs/PROMPT_INJECTION_SECURITY.md](docs/PROMPT_INJECTION_SECURITY.md)).

## Screen awareness (V5)

* "Hey Xenio, look at my screen." · "What's wrong here?" · "Read the text on my screen." · "What app is open?"
  · "Explain this graph." · "Summarize this page." Then follow-ups like "What line was that on?"
* XENIO takes one screenshot of your main display when you ask, has your main model (gpt-6-luna, same
  subscription, no extra cost) describe it, and Hermes answers. The voice says "Taking a look at your screen."
  right away; the answer typically takes 10-20 s.
* Private by design: the screenshot stays in memory and is discarded at once (never saved, never logged),
  only a text description reaches Hermes, passwords and API keys on screen are not read out, and text on the
  screen can never give XENIO orders. XENIO looks only when you refer to your screen (otherwise it asks), never
  in scheduled checks, and it cannot click or type on the screen.
* Details: [docs/VISION_CAPABILITIES.md](docs/VISION_CAPABILITIES.md) · [docs/VISION_ARCHITECTURE.md](docs/VISION_ARCHITECTURE.md)
  · [docs/VISION_SECURITY.md](docs/VISION_SECURITY.md).

## Safety

Every tool call Hermes makes is checked first by XENIO's permission engine
(`app/safety/policy.py`, enforced inside Hermes by the `xenio-guard` plugin — install/update with
`python -m app.main --install-guard`). Details: [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md).

| Level | Examples | What happens |
|---|---|---|
| READ | list/read files, `git status`, `python --version`, web search, reading pages, opening a page in your browser, `gh pr list`, reading calendar/email/GitHub/tasks, the briefing, looking at your screen when you ask | runs |
| SAFE_WRITE | create folders/files in your folders, `git add/commit`, creating a branch, typing/clicking ordinary things on a page, searching on a site, downloading a document, adding/completing tasks, project notes, memory | runs |
| CONFIRM_REQUIRED | delete or move files, `git push/merge`, deleting a branch, submitting forms, posting/sending/uploading, `gh issue/pr create`, `gh pr merge`, downloading a program, creating a calendar event, deleting a task, creating a scheduled check, unknown commands; anything but reading right after outside content tried to give instructions | XENIO asks: *"This will … Do you want me to continue?"* — say **yes** or **no** |
| HIGH_RISK | payments/purchases, password/2FA/account changes, force-push, `git branch -D`, releases, repo settings, deleting outside your folders | you must say **confirm** |
| blocked | `.env`/SSH keys/OAuth tokens/saved browser passwords, cookies and site storage, printing API keys/tokens (`gh auth token`), putting a secret into a web page, memory, task or project, sending email (V4), deleting system or top-level folders | never runs; Hermes is told why |

Unclear answers get *"Please confirm yes or no."*; silence, unrelated sentences, timeouts and errors
always mean **no**. A "no" cancels only that action: if you ask again later, XENIO asks again.
API keys/tokens that appear in any tool output are redacted before Hermes sees them.
Extra trusted folders (e.g. `D:\Projects`): `XENIO_ALLOWED_DIRS` in `.env`.

## Configuration

All settings are in `.env` (copy `.env.example`). Required: `STT_API_KEY`, `TTS_API_KEY`.
Everything else has defaults — see the comments in [.env.example](.env.example). Hermes needs
nothing from XENIO; it keeps using its own configuration (including its model — see
[docs/SETUP.md](docs/SETUP.md#model-reliability) for stronger models; XENIO never changes it).

Logs: `%LOCALAPPDATA%\xenio\logs\xenio.log` (keys and tokens are redacted). Add `-v` to see INFO
logs in the console.

## Tests

```powershell
python -m pytest -q
```

No test calls a real API: the Hermes bridge is tested against a fake ACP agent over real stdio,
STT/TTS against mocked HTTP, downloads against a local test web server.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "I can't reach my reasoning engine right now." | `hermes status`; `hermes acp --check`; check the model/provider works with `hermes -z "hi"` |
| Voice mode keeps saying "I didn't hear anything" | `python -m app.main --mic-test` shows what XENIO hears; `-v` logs the levels of every recording. Unmute / raise input volume, pick a device (`python -m sounddevice` → `XENIO_INPUT_DEVICE`), or set `XENIO_SPEECH_THRESHOLD` |
| Can't interrupt XENIO while it talks | `python -m app.main --barge-in-test` tells you whether your voice reaches the mic during playback; if not, use headphones or lower the speaker volume |
| Wake word never triggers | speak clearly "Hey Xenio" / "Xenio"; raise sensitivity `XENIO_WAKE_SENSITIVITY=0.7`; or use `--no-wake` |
| Wakes by itself | lower `XENIO_WAKE_SENSITIVITY=0.3`, or `XENIO_WAKE_NAME_ONLY=false` |
| "Sorry, I didn't catch that." every time | check `STT_API_KEY`; `python -m app.main --health --live` |
| XENIO cuts you off mid-sentence | raise `XENIO_TURN_PATIENCE_SECONDS` (default 2) in `.env` |
| Replies printed but not spoken | check `TTS_API_KEY`; the log file has the TTS error |
| Health: `Safety … not active` or "XENIO's tools are not enabled" | `python -m app.main --install-guard` |
| XENIO says a tool doesn't exist (`xenio_…`) | run `--install-guard`, then restart XENIO |
| A spoken file/folder name comes out wrong ("Xenio test" vs "XenioTest") | say it again or spell it; XENIO feeds recently used names to speech recognition, so it improves after the first use |
| Something was blocked that you want allowed | if it's a folder you trust, add it to `XENIO_ALLOWED_DIRS`; secrets and system folders stay blocked by design |
| First start is slow | Hermes cold start is ~30 s; later starts take a few seconds |
| "Open X" works but nothing appears | XENIO opens pages in your default browser (`xenio_open_url`); if the model used Hermes' hidden browser instead, ask "open it in my browser" |
| A page "could not be read" | the actual reason is printed after `[FAILED]`; some sites block automated reading — ask XENIO to open it in your browser |
| GitHub issues/PRs of private repos | install the GitHub CLI (`winget install GitHub.cli`) and run `gh auth login` yourself |
| Answers ignore instructions / pick the wrong tool | check which model answered (`--health` shows the chain; a free fallback is weaker) — see [docs/SETUP.md](docs/SETUP.md#model-reliability) |
| "Google is not connected" | [docs/PERSONAL_INTEGRATIONS.md](docs/PERSONAL_INTEGRATIONS.md#google-calendar--gmail-one-sign-in-minimal-permissions), then `--connect-google` |
| Google sign-in stopped working after a week | your OAuth app is in "Testing" (7-day tokens): publish it, then `--connect-google` again |
| Scheduled checks never report | XENIO must be running (or a Hermes gateway); `python -m app.main --tick` runs what is due now; `hermes cron list` |
| "openai-codex cannot look at images right now" | `hermes auth reset openai-codex` (a cooldown on another model blocks it; `--health` shows it), then ask again |
| XENIO describes an old screen | say "look again"; a capture is only taken when asked |
| The screen capture comes back black | the screen is locked, or the window blocks screenshots (protected video, some banking/password apps) |

## Project layout

```
app/        main.py · config.py · orchestrator.py · health.py · logging_setup.py · sources.py
            notifications.py · proactive.py · briefing.py
  hermes/   client.py (interface) · bridge.py (ACP transport) · adapter.py (sessions) · guard.py · model_info.py
  voice/    audio.py · stt.py · tts.py · wakeword.py · speech_text.py · assistant.py
  safety/   policy.py (permission engine) · permissions.py (confirmations)
  tools/    base.py · registry.py · filesystem.py · terminal.py · web.py · github.py · personal.py · vision.py
  vision/   capture.py (screen capture) · providers.py (vision models) · service.py (look, mask, discard)
  integrations/  base.py · google.py · calendar.py · email.py · github.py
  personal/ store.py (personal.db) · tasks.py · projects.py
  memory/   manager.py          interface/ console.py
hermes_plugin/xenio-guard/      Hermes plugin enforcing XENIO's policy
tests/      pytest suite + fake ACP agent
docs/       ARCHITECTURE.md · HERMES_INTEGRATION.md · HERMES_CAPABILITIES.md · WEB_CAPABILITIES.md
            SECURITY_MODEL.md · SETUP.md · MEMORY_ARCHITECTURE.md · PERSONAL_INTEGRATIONS.md
            PROACTIVE_AGENT.md · PROMPT_INJECTION_SECURITY.md
```
