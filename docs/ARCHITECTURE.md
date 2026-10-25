# XENIO architecture

XENIO is the interface, voice, safety and orchestration layer. **Hermes is the brain**: every
user request is answered by Hermes Agent over ACP; XENIO never calls an LLM itself.

```
 USER ──voice──► Microphone ──► local wake word ──► record ──► STT (nabh.cloud) ─┐
   │                (sherpa-onnx, on device)                                     │ text
   └──text──► Terminal (python -m app.main) ─────────────────────────────────────┤
                                                                                  ▼
                                                                    XENIO ORCHESTRATOR
                                                                    app/orchestrator.py
                                                                       │        ▲
                                                 session/prompt (ACP)  │        │ streamed reply,
                                                                       ▼        │ permission requests
                                                                   HERMES AGENT (hermes acp)
                                                      memory · skills · tools · subagents · web
                                                      terminal · files · MCP · cron  (+ xenio-guard)
                                                                       │
                     permission request ──► XENIO permission gate ──► you (console y/N or voice yes/no)
                                                                       │
 USER ◄──speaker◄── TTS (nabh.cloud) ◄── speakable text ◄── reply ◄────┘   (text is always printed)
```

## Modules

| Path | Responsibility |
|---|---|
| `app/main.py` | CLI entry: `--voice`, `--health`, `--install-guard`, `--connect-google`, `--briefing`, `--tick`, text mode default |
| `app/config.py` | `.env` + environment → typed, frozen `Settings`; keys wrapped in `Secret` |
| `app/logging_setup.py` | structured `key=value` logs, redaction of keys/tokens on every line |
| `app/orchestrator.py` | single entry for all interfaces: text → Hermes → `XenioResponse`; error messages; interrupt; routes Hermes permission prompts to the gate |
| `app/hermes/client.py` | transport-agnostic `HermesClient` interface + exceptions |
| `app/hermes/bridge.py` | ACP transport: subprocess, JSON-RPC framing, request/response matching, agent→client requests, crash detection |
| `app/hermes/adapter.py` | `HermesAcpClient`: handshake, session new/load/persist, streaming turns, cancel, permission answers |
| `app/hermes/guard.py` | install/inspect the `xenio-guard` Hermes plugin |
| `app/safety/policy.py` | the permission engine (paths, commands, git/gh, web links, JavaScript, browser code, downloads, memory); stdlib only, also runs inside Hermes |
| `app/safety/permissions.py` | `PermissionGate`, mapping of Hermes permission requests, yes/no + "confirm" parsing |
| `app/memory/manager.py` | read-only recall of Hermes memory; remember via Hermes |
| `app/tools/registry.py` | catalog of Hermes-backed and XENIO tools; executes XENIO's own behind the gate |
| `app/tools/filesystem.py`, `web.py`, `github.py`, `terminal.py` | XENIO tools (folders, open in your browser, downloads) and catalog entries for reused Hermes tools |
| `app/sources.py` | reads the web sources the plugin recorded for a turn; formats them under the answer |
| `app/voice/audio.py` | mic (WASAPI on Windows), interruptible speaker, WAV codec, endpointing |
| `app/voice/stt.py`, `tts.py` | provider interfaces + OpenAI-compatible implementations |
| `app/voice/wakeword.py` | local sherpa-onnx keyword spotting (wake + stop spotters) |
| `app/voice/speech_text.py` | Markdown → speakable text, TTS segmenting |
| `app/voice/assistant.py` | voice state machine, barge-in, spoken confirmations |
| `app/interface/console.py` | terminal REPL, Ctrl+C handling, console confirmations |
| `app/health.py` | system check |
| `app/integrations/` | V4: `base` (Integration shape), `google` (sign-in + REST), `calendar`, `email` (Gmail / Himalaya, read-only), `github` |
| `app/personal/` | V4: `store` (personal.db), `tasks`, `projects` |
| `app/notifications.py`, `app/proactive.py`, `app/briefing.py` | V4: notifications, scheduled checks on Hermes cron, daily briefing |
| `app/tools/personal.py` | V4: the personal tools registered in Hermes (`xenio_calendar_events` … `xenio_integrations`) |
| `app/vision/` | V5: `capture` (Windows GDI screen capture, PNG in memory), `providers` (`VisionProvider`: Hermes' model access, strict; optional OpenAI-compatible), `service` (`ScreenLook`: capture → describe → mask → discard) |
| `app/tools/vision.py` | V5: `xenio_look_at_screen` (observe-only) |
| `app/desktop/` | V5.5 desktop control; `common.py` shared; Windows `windows`/`win32`/`uia`; macOS `macos` (CoreGraphics, Accessibility, ImageIO via ctypes + a persistent accessibility helper process), `mac_windows`, `ax`, `mac_terminal` (a command in a new visible Terminal window, read back) |
| `app/fastpath.py`, `app/media.py` | simple commands carried out by XENIO itself without the model (apps, windows, music, media keys, volume, time); same policy, only READ/SAFE_WRITE steps |
| `app/tools/media.py` | `xenio_media`: the same player code for the chat (play a song, pause/next/previous, what is playing, volume) |
| `app/ui/` + `ui/` | UI V1 (`--ui`): a local web interface over the same components; `server` (127.0.0.1, token, SSE), `controller` (every page -> a real component), `events`, `assistant` (the orb's states), `approvals` (the PermissionGate's web confirmer). See [UI.md](UI.md) |

One action layer for voice and text: both go through `Orchestrator.process_text` - XENIO's fast path for simple
commands, else Hermes with XENIO's guarded `xenio_*` tools. The fast path and the tools share the desktop service and
the player, so "open Spotify" or "pause" behave the same whichever path handles them.
| `app/engineering/` | V6 engineering team: `service` (runs, status, cancel, apply/push/PR, socket for the chat's Hermes), `orchestrator` (plan → tasks → execute → review → report), `agents` (Hermes ACP sessions per task), `graph` (task graph), `roles` (enforced capabilities, cost policy), `repo` (snapshot, worktrees, merges), `verify` (tests run by XENIO, sandboxed), `security` (deterministic scan), `prompts` |
| `app/tools/engineering.py` | V6: `xenio_engineering`, the tool Hermes uses to work with the engineering team |
| `hermes_plugin/xenio-guard/` | Hermes plugin enforcing XENIO's policy inside Hermes (and, V6, each engineering session's role and file scope) |

Dependencies point one way: interfaces → orchestrator → `HermesClient` interface. Nothing outside
`app/hermes/` knows ACP exists.

## Text mode

`ConsoleIO` reads stdin on a daemon thread, so exit never hangs on a blocked `input()`. Replies
stream to the terminal as Hermes produces them. Ctrl+C during an answer sends `session/cancel`;
Ctrl+C at the prompt quits. Lines typed while XENIO is busy are discarded before a permission
prompt so they can't accidentally answer it.

## Voice mode

One listener task owns the microphone and routes each 80 ms frame by state:

| State | Frames go to | Leaves the machine? |
|---|---|---|
| `IDLE` | wake spotter ("hey xenio", "xenio") | no |
| `RECORDING` | utterance recorder (Silero VAD + early transcription decide when the turn ends) | **only this audio**, to STT |
| `TRANSCRIBING` | dropped | — |
| `THINKING` / `SPEAKING` | stop spotter ("xenio stop") | no |

Flow: wake → record (starts on the wake frame; the earcon plays concurrently) → STT →
orchestrator → **speech starts while Hermes is still writing** → follow-up window (`RECORDING`
for `XENIO_FOLLOW_UP_SECONDS`, no wake word) → `IDLE` when the user stays quiet.

### Real time (`app/voice/vad.py`, `app/voice/turns.py`, `app/voice/live.py`, `LiveSpeaker`)

```
wake word / first speech frame ─► pre-warm the STT and TTS connections (HEAD to the server root, no credentials)
mic ─► Silero VAD (local, 0.3 ms/frame) ─► 0.25 s pause ─► early transcript (nabh Whisper, hedged after 3 s)
       user speaks again ─► early transcript dropped, a new one at the next pause
       transcript arrives, sentence finished, ≥ 0.55 s quiet ─► turn ends (transcript already here)
       transcript trails off ("open the", "um", a comma) ─► wait up to 2 s for the rest
    ─► noise ("Thank you." on silence) ignored · closer ("thanks", "stop", "bye") ─► cached reply, sleep
    ─► Hermes streams text ─► SentenceChunker ─► ReplyStream (hold-back rule) ─┐
       0.3 s and nothing said yet ─► "One moment." (cached)                    ├─► SpeechQueue ─► LiveSpeaker
       tool starts ─► "Checking your calendar." (cached)                       ┘   (TTS streamed, next sentence
                                                                                   synthesized while one plays)
```

* **Endpointing**: Silero VAD (sherpa-onnx, already used for the wake word; model 0.6 MB, SHA-256 pinned)
  marks each frame as speech or not, robust to noise and needing no calibration. nabh.cloud offers no
  streaming VAD (checked: its model list has none; its Whisper accepts `vad_filter`, which only trims
  uploaded audio). Without the model XENIO falls back to the energy endpointing below.
* **Turn-taking** (`EarlyTranscriber`): a fixed silence either cuts people off mid-sentence or makes every
  answer late. XENIO starts transcribing at the first 0.25 s pause; if the user stays quiet and the words
  form a finished sentence, the turn ends when that transcript arrives, so speech recognition costs no
  extra time after the turn. If the words trail off it waits up to `XENIO_TURN_PATIENCE_SECONDS` (2 s). If
  the user speaks again, the early transcript is discarded (at most 4 per turn). Measured through the
  real pipeline: "Search the web for, um, [1.2 s pause] the population of Japan" arrived whole.
* **STT**: nabh.cloud Whisper large-v3-turbo, `STT_LANGUAGE=en` (no misdetection of accented English).
  Measured 2026-10-04: 0.75-2.7 s, median ~1.4 s on a warm connection; a cold connection adds 0.4-0.8 s,
  which is why the client keeps connections for 120 s (httpx default: 5 s) and warms them when the user
  starts talking. Occasional 5-10 s stalls are hedged with a second request after 3 s. The on-device
  Moonshine model (`app/voice/local_stt.py`, `XENIO_STT_ENGINE=local`) looked good with synthetic voices
  but failed with the user's real voice ("Close Chrome" → "Close words of enchanted beauty"), so it is
  off by default. Whisper's prompt-echo and silence phantoms ("Thank you.", "you") are filtered out.
* **Streaming replies**: each complete sentence is spoken as soon as Hermes writes it (a long first sentence
  may be cut at a comma). **Hold-back rule**: a sentence that claims an outcome ("Done.", "I've created…",
  "you declined", "I couldn't…") and everything after it, or anything after a failed tool or a permission
  question, waits until the turn is over and `truth_check` has compared it with the tool results. A false
  "Done" is replaced by "The operation failed: …" before it is ever spoken.
* **Acknowledgments and narration**: cached phrases (synthesized once, stored in `<data dir>/voice_cache`),
  at most two per turn and only before the real answer starts.
* **Output**: `LiveSpeaker` keeps one output stream open for the session. Measured on this laptop: opening a
  stream costs ~230 ms each time (5.4 s when the device had gone to sleep); with the persistent stream the
  first sound comes 10-18 ms after the audio is ready, sentences follow without gaps, and barge-in silences
  it instantly. TTS audio is played from its first streamed bytes (~0.35-0.6 s instead of 0.9-1.4 s).

Endpointing without the VAD (`XENIO_VAD=false`) is energy-based with a threshold **calibrated from the wake phrase** (15% of its
loudest frame, ≥ 2.5× the idle noise floor, between 25 and 400 RMS) because mic gain differs
~20× between devices; the original fixed 400 never triggered on this laptop's noise-suppressing
mic. Short blips (beep echo, clicks) are discarded instead of ending a recording. In voice mode
the orchestrator appends a note asking Hermes for 1–3 spoken sentences (`XENIO_VOICE_BRIEF`), and
a leading "Hey Xenio" in a transcript is stripped before it reaches Hermes.

**Barge-in**: during `THINKING`/`SPEAKING` both spotters listen, so "Xenio", "Hey Xenio",
"Xenio, stop" or "stop Xenio" interrupt: playback is aborted (`Speaker.stop()`, measured 70 ms
from detection to silence on real hardware), a running Hermes turn gets `session/cancel`, and a
recorder starts on the same frame so the user's next words are captured; leading "stop"/"wait"
is stripped from that transcript. While a TTS segment containing the name is playing (and 1 s
after), name hits are ignored so XENIO cannot interrupt itself.

Why keywords and not "any speech": on this laptop the hardware echo canceller removes most of
XENIO's voice from the mic, but residual bursts of 375-1500 RMS remain — as loud as the user —
and neither level nor spectral similarity to the played audio separates them (measured).
Keyword spotting does: 12/12 detections at the user's level over real echo, 0 false triggers on
echo alone, and 0 self-interruptions in a 30 s live playback test. `TalkOverDetector` (interrupt
by just talking) arms itself only when a full playback showed both typical and peak echo far
below speech level — headphones, or (re-measured 2026-10-04 at 100 % volume: median 0, peak 95 RMS
during a 17 s story) this laptop's echo canceller on a good day. It learns only from frames while audio
is actually playing (it used to also count the silence while Hermes thought, which made it arm too
easily), needs the VAD to call the sound speech (a cough, a door or music does not interrupt), and the
~0.5 s heard while it decided are kept as the start of the user's next request. A WebRTC AEC3 echo
canceller (LiveKit) was tested on top and removed: it changed the residual from 95 to 79 RMS.

**Wake word spelling**: the acoustic model hears a word-initial X as Z. Measured on TTS speech in
two voices: "ZENIO" matched 6/6 "Xenio" utterances vs 2/6 for "XENIO", so each phrase is also
registered in its Z spelling (`pronunciation_variants`). Zero false fires on 14 control phrases.

**Failure behaviour**: STT error → "Sorry, I didn't catch that."; Hermes down → "I can't reach
my reasoning engine right now."; TTS error → reply is still printed, one warning, voice continues
to listen; microphone loss → clear message and exit.

**Spoken names**: the ~0.3 s heard before the wake word fired is kept, so the first word of a
command is not clipped. File names are normalised before Hermes sees them ("HelloTXT" /
"hello dot txt" → `hello.txt`), and the names of files/folders XENIO recently touched are sent to
Whisper as a vocabulary prompt so it spells them the same way next time.

## Hands: tools (V2)

```
USER ─voice─► STT ─► XENIO ─► HERMES ─► picks a tool (terminal, read_file, write_file, xenio_create_directory, …)
                                  │
                xenio-guard pre_tool_call ─► app/safety/policy.py (allow / ask / block)
                                  │                       └─ ask: Hermes approval gate ─► ACP ─► XENIO
                                  │                              "This will … Do you want me to continue?" ─► yes/no
                                  ▼
                       tool runs inside Hermes ─► structured result ─► (secrets redacted) ─► Hermes
                                  ▼
          XENIO prints the reply + "[done]/[FAILED]" per tool (from ACP tool_call statuses) ─► TTS
```

* Hermes' own tools are reused (terminal = bash, read_file, write_file, patch, search_files,
  web, browser, execute_code, delegate_task…). XENIO adds only what Hermes lacks as tools:
  `xenio_list_directory`, `xenio_inspect_path`, `xenio_create_directory`, `xenio_move_path`,
  `xenio_delete_path` (`app/tools/filesystem.py`, run inside Hermes by the plugin).
* `app/tools/registry.py` catalogs both (backend `hermes` / `xenio`) with schema and typical
  permission level, and can execute XENIO's tools directly behind the permission gate.
* `app/safety/policy.py` is standard-library only so the same code runs in XENIO and in Hermes:
  path categories (secret / system / app_data / user / outside), a bash + PowerShell command
  classifier (allowlist first; unknown = confirm), tool-call rules, a Python AST check for
  `execute_code`, secret redaction, and the escalation message format.
* The plugin also adds a system-prompt section (real folder paths, "only report success the tool
  confirmed", speech-recognition name hints). Besides enabling the plugin, the only change to
  Hermes' setup is `platform_toolsets.acp: [hermes-acp, xenio]`, which guarantees the tools are
  present (Hermes otherwise decides from a cache that races with plugin discovery — observed).
* A new Hermes session is started whenever `POLICY_VERSION` changes, because Hermes freezes a
  session's system prompt.
* Observability: XENIO logs TOOL_REQUESTED / TOOL_EXECUTION_STARTED / COMPLETED / FAILED from the
  ACP stream and PERMISSION_CHECK / CONFIRMATION_REQUESTED / CONFIRMATION_RECEIVED from its gate
  (`%LOCALAPPDATA%\xenio\logs\xenio.log`); the plugin writes every decision, including silent
  allows and blocks, to `xenio-guard.log` (JSON lines, secrets redacted, never file contents).

## World and web agency (V3)

```
USER ─► XENIO ─► HERMES ─► web_search ─► web_extract ─► answer from the sources
                    │   └► browser_exec (Hermes' own, invisible Chromium) ─► read / interact
                    │   └► xenio_open_url ─► YOUR default browser shows the page
                    │   └► xenio_download_file ─► Downloads (checked, never run)
                    │   └► terminal: git / gh
                    ▼
     xenio-guard: pre_tool_call (policy per URL / JS / helper / git / gh command)
                  transform_tool_result (redact; remember page labels; record sources → sources.jsonl)
                    ▼
XENIO prints the answer + actions ([done]/[FAILED] – reason/[declined]) + sources ─► TTS speaks the answer
```

* Nothing about search, page reading or browser automation is re-implemented; XENIO adds the
  permission rules, two small tools Hermes lacks, and source tracking. Details:
  [WEB_CAPABILITIES.md](WEB_CAPABILITIES.md); the rules: [SECURITY_MODEL.md](SECURITY_MODEL.md).
* Research, browsing, downloading, repository and memory behaviour is steered by a second
  system-prompt section (`xenio-guard.web`; Hermes allows 4 000 characters per section).
* **Source awareness**: `transform_tool_result` sees every raw tool result, so the plugin records
  search hits, pages read/visited/opened and downloads (title, link, site, time, ≤300-character
  excerpt, session/turn ids). The orchestrator reads the lines added during a turn (`SourceLog`)
  and attaches them to `XenioResponse.sources`. Over ACP alone, `web_extract` reports only a title,
  so this could not be done on the XENIO side.
* **Failures are explained**: when Hermes marks a tool call failed, the ACP update carries its error
  text; the adapter keeps it (`ToolActivity.detail`) and prints it after `[FAILED]`. Actions the
  user declined produce no tool call in Hermes, so the orchestrator lists them from its own gate
  (`[declined]`). If the reply contradicts those records, `truth_check` corrects it (V4: a false
  "done" is replaced before it is spoken; see Safety).
* **"No" is scoped**: Hermes words every refusal "Do NOT retry". The plugin's `pre_llm_call` hook
  finds declined tool calls in the history and adds a note to the user's next messages. The note
  names the operation and says the "no" covered only that attempt. It applies only to declines from
  the last 3 user turns that were not attempted again since, so it does not grow every prompt.

## Personal context and proactive agent (V4)

```
USER ─► XENIO ─► HERMES (+ [XENIO context]: current project, open tasks, unread notifications)
                    │   └► xenio_calendar_events / xenio_email_* / xenio_github ─► Google · Himalaya · GitHub
                    │   └► xenio_tasks / xenio_project ─► personal.db          (memory stays in Hermes)
                    │   └► xenio_daily_briefing ─► all of the above, unavailable parts named
                    │   └► xenio_schedule ─► Hermes cron job "xenio: …" (toolsets xenio + web, deliver local)
                    ▼
     xenio-guard: memory policy (secrets / low value / duplicates) · injection flag + taint · personal-tool levels
                    ▼
XENIO ProactiveEngine (every 30 s): due XENIO job? ─► `hermes cron tick` (guarded env) ─► run output
                                    ─► one Notification per run ─► printed / spoken when idle
```

* Memory: [MEMORY_ARCHITECTURE.md](MEMORY_ARCHITECTURE.md). Integrations, authentication, tasks, projects,
  briefing: [PERSONAL_INTEGRATIONS.md](PERSONAL_INTEGRATIONS.md). Scheduler and notifications:
  [PROACTIVE_AGENT.md](PROACTIVE_AGENT.md). Outside content: [PROMPT_INJECTION_SECURITY.md](PROMPT_INJECTION_SECURITY.md).
* The plugin now has three system-prompt sections (`environment`, `web`, `personal`, together under Hermes'
  8 000-character limit; a test checks it).
* **Reliability layer** (phase 16): tool selected → arguments validated (`Tool.validate`) → permission evaluated
  (policy) → executed → actual result recorded (`ToolActivity` status + error text, `[declined]` from the gate)
  → final reply checked against those records (`truth_check`). The model is never the source of truth for
  whether an action succeeded.

## Vision and screen awareness (V5)

```
USER "look at my screen" ─► XENIO ─► HERMES decides it must see ─► xenio_look_at_screen
        consent (your words / recent look / ask / never unattended) ─► GDI capture in memory (1600 px PNG)
        ─► main model describes it (Hermes' model access, no fallback) ─► secrets masked ─► pixels discarded
        ─► TEXT back to Hermes (flagged as outside content) ─► reasoning + tools ─► voice reply
```

Hermes stays the only brain; vision is one observe-only tool. Hermes' `browser_vision` (its own browser) and
`vision_analyze` (image files and links, now path-checked) are reused. Details and measurements:
[VISION_CAPABILITIES.md](VISION_CAPABILITIES.md), [VISION_ARCHITECTURE.md](VISION_ARCHITECTURE.md),
[VISION_SECURITY.md](VISION_SECURITY.md).

## Safety

```
Hermes wants to act ──► (edit)    ACP edit approval ─────────────┐
                    ──► (command) Hermes detector / guardian ─────┤
                    ──► (policy)  xenio-guard pre_tool_call ──────┤
                                                                  ▼
                                    session/request_permission ─► from_hermes_request() ─► RiskLevel
                                                                  ▼
                 READ / SAFE_WRITE: auto-allow   CONFIRM_REQUIRED: yes/no   HIGH_RISK: say/type "confirm"
                                                                  ▼
                                   allow_once / reject_once  (timeouts, errors, unclear answers = deny)
```

* `SAFE_WRITE` (no question) covers creating/changing non-executable files in your folders (home,
  Desktop, Documents, Downloads…, the XENIO workspace, `XENIO_ALLOWED_DIRS`); executables, AppData
  and other locations ask; system folders are HIGH_RISK.
* Credential/key files (`.env`, `id_*`, `.ssh`, `*.pem`, browser login stores…) are **blocked**.
* Unknown actions fail closed to `CONFIRM_REQUIRED`. Full rules: [SECURITY_MODEL.md](SECURITY_MODEL.md).
* XENIO reports what Hermes reports. Its own integrations return `ToolResult(ok=…)` and the
  registry never reports success unless the handler returned `ok=True`.

## Secrets

* Keys live only in `.env` (git-ignored). They are read with `dotenv_values`, never exported to
  `os.environ`, so Hermes and the commands it runs cannot see them.
* `Secret` hides values in `repr`/`str`; the log formatter masks known secrets plus bearer tokens,
  `nbh_…`, `sk-…` and `key=`/`password=` patterns on the final rendered line, tracebacks included.

## Engineering team (V6)

```
xenio_engineering (chat's Hermes) ─socket+token─► EngineeringService ─► EngineeringOrchestrator
   inspect + snapshot (user's checkout untouched) → baseline tests → plan (one engineer for small changes)
   → tasks as Hermes ACP sessions (cwd = own worktree, model per role, ≤4 parallel, scopes disjoint)
   → per task: XENIO commits, checks scope, test tampering, focused tests, merges (serialized)
   → final tests vs baseline, deterministic security scan, security + code reviewers → evidence-based outcome
   → report + patch; apply / push / PR only with the user's confirmation
xenio-guard: every tool call of an engineering session → normal policy + role tools, worktree, file scope
```

Details: [ENGINEERING_TEAM.md](ENGINEERING_TEAM.md), [V6_BASELINE.md](V6_BASELINE.md).

## Extension points

* **New STT/TTS provider**: subclass `STTProvider` / `TTSProvider`, return it from `create_stt` /
  `create_tts`. Nothing else changes.
* **New integration (Strava, Outlook…)**: an `Integration` in `app/integrations/` (name, capabilities with
  permission levels, `auth_state()` with the setup step, stdlib only), tools for it in `app/tools/personal.py`,
  and their levels in `classify_personal_tool`. Reuse Hermes' own tools/skills where they exist.
* **New notification channel (desktop pop-up, Telegram…)**: implement `Channel.deliver(notification)` and
  call it where text/voice mode deliver today.
* **New interface (Telegram is already Hermes', a web UI, a hotkey)**: call
  `Orchestrator.process_text()` and provide a `Confirmer`.
* **Different Hermes transport**: implement `HermesClient`; the orchestrator is unaffected.
