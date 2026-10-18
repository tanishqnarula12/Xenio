# XENIO setup

## This Mac (MacBook Air M5, macOS 26.6; set up 2026-10-17)

| Step | Status |
|---|---|
| Code copied from the Windows laptop; baseline git commit (the old `.git` had no commits), work on branch `macos-port` | done |
| Virtualenv `.venv` from the CPython 3.14.7 that Hermes' installer already put on this Mac (`~/.local/share/uv/python`), `requirements.txt` installed | done |
| `.env` carried over (STT/TTS keys, `XENIO_GITHUB_USER`) | done |
| Wake-word and voice-activity models in `~/.local/share/xenio/models` (20 MB) | done |
| Hermes Agent 0.22.2 (`~/.hermes`): `xenio-guard` plugin 0.9 installed and enabled, `platform_toolsets.acp: [hermes-acp, xenio]`, `tools.tool_search.enabled: off` (see [HERMES_INTEGRATION.md](HERMES_INTEGRATION.md): Hermes 0.22's gateway runs every session of the profile, so this applies to all of them); `config.yaml` backed up to `~/.local/share/xenio/backups` | done |
| Google sign-in (Calendar + read-only Gmail) with the *Desktop app* client `user_required_steps/calendar_credentials.json.json`; token in `~/.local/share/xenio/google_token.json` (owner-only) | done 2026-10-18 |
| Hermes model chain, free models only (see [Model reliability](#model-reliability)): main `thinkingmachines/inkling:free` (sees images), reasoning effort `low`, `auxiliary.free_only: true` (side tasks can never fall back to a paid model) | done |
| macOS port: safety rules for macOS folders, Trash, desktop control through the Accessibility API, in-memory screen capture, media keys | done (`macos-port` branch) |
| Fast path for simple commands (apps, music, volume, time) | done |
| GitHub CLI signed in as `tanishqnarula12` | already done |
| IDE uses `.venv` (`../.vscode/settings.json`) | done |

## What only you can do (macOS)

> **MANUAL STEP REQUIRED — permissions for the app XENIO runs in.** macOS grants these to the app you start XENIO
> from, not to Python: on this Mac that is **Antigravity IDE** (XENIO finds it and names it in every permission
> message). Allow, in *System Settings > Privacy & Security*:
> * **Microphone** — voice mode (macOS asks on the first `--voice` run);
> * **Accessibility** — reading a window's controls, typing, clicking, window control, media keys, finding a chat in
>   WhatsApp (switch on Antigravity IDE, then restart XENIO). **Off as of 2026-10-18.** Opening apps, running a
>   command in Terminal and Spotify/Music control work without it;
> * **Screen & System Audio Recording** — "look at my screen" and window titles (on);
> * **Automation > Terminal / Spotify / Music** — "open Terminal and run X", pause/next/play in those apps (macOS asks
>   the first time; answer *OK*).
>
> Check with `.venv/bin/python -m app.main --health`: missing permissions are named there.

> **Your call — Codex (ChatGPT sign-in) as the main model, like on Windows.** Run
> `hermes auth add openai-codex --type oauth` and approve it in the browser. Then switch the main model with
> `hermes config set model.provider openai-codex` and `hermes config set model.default gpt-6-luna`; the free
> OpenRouter models stay as the fallback chain behind it.

> **Your call — Gemini as the last fallback.** Put `GEMINI_API_KEY=...` into `~/.hermes/.env` yourself (never into a
> chat), then `hermes fallback add` and pick *gemini*. The Google AI Studio free tier costs nothing; if billing is
> enabled on that Google project it can.

> **Optional — songs on Spotify instead of YouTube.** Create a free app at developer.spotify.com and put
> `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` into XENIO's `.env`. Without them "play <song>" opens the song on
> YouTube in one tab. To make it start by itself in Safari: *Safari > Settings > Websites > Auto-Play > youtube.com:
> Allow All Auto-Play*.

> **Google sign-in (Calendar + Gmail, read-only mail): done 2026-10-18.** To redo it:
> `.venv/bin/python -m app.main --connect-google --client-file "user_required_steps/calendar_credentials.json.json"`
> and press *Allow* in the browser (that is the *Desktop app* client; `credentials.json` there is a *Web application*
> client, which XENIO refuses). Details: [PERSONAL_INTEGRATIONS.md](PERSONAL_INTEGRATIONS.md#google-calendar--gmail-one-sign-in-minimal-permissions).

> **MANUAL STEP REQUIRED — say the words.** `.venv/bin/python -m app.main --voice`, say **"Hey Xenio"**, then
> **"What can you do?"**.

> **Your call — rotate the nabh.cloud key.** It was pasted into a chat on Windows; it is stored only in `.env`.

> **Optional: other mailboxes.** `brew install himalaya`, then run `himalaya` and follow its wizard.

## Windows (the previous machine)

### What was done on the Windows laptop

| Step | Status |
|---|---|
| Virtualenv `.venv` (Python 3.14.2) with `requirements.txt` installed | done |
| `.env` created with your nabh.cloud key for STT and TTS | done (git-ignored) |
| Wake-word model downloaded to `%LOCALAPPDATA%\xenio\models` (~13 MB) | done |
| `xenio-guard` Hermes plugin v0.4 installed and enabled (Hermes `config.yaml` backed up to `%LOCALAPPDATA%\xenio\backups`) | done |
| Web, browser and GitHub rules, `xenio_open_url`, `xenio_download_file`, source tracking (V3) | done — part of the plugin, nothing to configure |
| XENIO's tools enabled for ACP sessions: `platform_toolsets.acp: [hermes-acp, xenio]` in Hermes' config | done |
| `tools.tool_search.enabled: ${XENIO_TOOL_SEARCH}` in Hermes' config: Tool Search is off only inside XENIO's Hermes (so the model sees XENIO's tools); every other Hermes process keeps the default | done |
| XENIO workspace `C:\Users\aniln\Xenio` | created on first run |
| V4: plugin 0.5 with personal tools, memory policy and injection defense; `personal.db` (tasks, projects, notifications) | done |
| V4: Himalaya v2.2.1 email CLI in `%LOCALAPPDATA%\Programs\himalaya` (official release, SHA-256 checked), on your PATH | done |
| V4: `XENIO_GITHUB_USER=tanishqnarula12` in `.env` (checked against GitHub) | done |
| V4: Hermes `USER.md` duplicates merged (1,287 → 514 chars; original in `%LOCALAPPDATA%\xenio\backups`) | done |
| Real-time voice: Silero VAD (0.6 MB) in `%LOCALAPPDATA%\xenio\models`, SHA-256 checked; speech recognition by nabh Whisper with `STT_LANGUAGE=en` in `.env` (the optional on-device Moonshine model, 106 MB, is also there but off by default) | done |
| Hermes web plugin `web-openai-native` disabled (it broke page reading after the Codex sign-in; undo: `hermes plugins enable web-openai-native`) | done |
| V5: plugin 0.6 with `xenio_look_at_screen` (screen vision with gpt-6-luna, nothing to install: capture uses Windows itself) | done |
| V5: `hermes auth reset openai-codex` cleared stale model cooldowns (gpt-6-sol, gpt-5.4, ... refused for ChatGPT sign-ins) that blocked Codex for vision; sign-in tokens untouched | done |

### What only you could do there

> **MANUAL STEP REQUIRED — microphone access.** Windows must allow desktop apps to use the
> microphone: *Settings → Privacy & security → Microphone → "Let desktop apps access your
> microphone" = On* (currently On). If the laptop has a mic-mute key or the input volume is 0,
> XENIO hears silence: voice mode keeps printing "(I didn't hear anything.)" and `--health` may
> report `[WARN] Audio … digital silence`. (This laptop's Intel Smart Sound mic suppresses room
> noise, so `--health` often reads level 0 in a quiet room — that alone is not a fault.)

> **MANUAL STEP REQUIRED — say the words.** The voice acceptance test needs a human:
> run `python -m app.main --voice`, say **"Hey Xenio"**, wait for the beep, then **"What can you do?"**.

> **Your call — rotate the nabh.cloud key.** It was pasted into a chat to set this up. It is
> stored only in `.env`, but if you want to be safe, create a new key in the nabh.cloud dashboard
> and replace `STT_API_KEY` / `TTS_API_KEY` in `.env`.

> **Optional — GitHub CLI** (only for private repositories, issues/PRs you own, creating PRs).
> XENIO cannot sign in for you:
> ```powershell
> winget install GitHub.cli
> gh auth login          # opens your browser; XENIO never sees or stores the token
> ```
> Without it, git works fully and public GitHub data is read through the web. `--health` shows
> the GitHub status.

> **MANUAL STEP REQUIRED: Google sign-in (Calendar + Gmail, read-only mail).** Create a "Desktop app"
> OAuth client in your own Google Cloud project, download its JSON into Downloads, then run
> `.\.venv\Scripts\python.exe -m app.main --connect-google` and press *Allow* in the browser. Step by step:
> [PERSONAL_INTEGRATIONS.md](PERSONAL_INTEGRATIONS.md#google-calendar--gmail-one-sign-in-minimal-permissions).

> **Optional: other mailboxes.** Run `himalaya` in a terminal and follow its wizard (app password).

> **Your call: rotate the Gemini key.** It was pasted into a chat. It is stored only in Hermes' `.env`
> (`GEMINI_API_KEY`); create a new key at aistudio.google.com and replace it there.

> **Your call — the model.** See [Model reliability](#model-reliability). XENIO never changes it.

Nothing else is manual. Hermes itself needs no new configuration (it keeps using its own
`config.yaml` and OpenRouter key).

## Web and browser settings (`.env`, all optional)

| Setting | Default | Effect |
|---|---|---|
| `XENIO_SHOW_BROWSER` | `false` | `true` shows Hermes' own automation browser window while it works (XENIO's Hermes only). Pages you ask to *see* always open in your normal browser. |
| `XENIO_DOWNLOAD_MAX_MB` | `200` | size limit for `xenio_download_file` |

Web search uses Hermes' configuration: on this PC no search API key is set, so Hermes uses its
keyless free tier (exa / parallel / firecrawl / keenable, with automatic failover). The
`web-openai-native` plugin is disabled: after the Codex sign-in Hermes auto-selected it, but it can
only search (page reading failed), runs searches on OpenAI's side where XENIO's policy cannot check the
query, and does not work when a free fallback model answers. Adding e.g. `TAVILY_API_KEY` to Hermes' `.env`
(`%LOCALAPPDATA%\hermes\.env`) switches Hermes to that service (it may cost money).

## Screen vision settings (`.env`, all optional)

| Setting | Default | Effect |
|---|---|---|
| `XENIO_VISION` | `true` | `false` removes the ability to look at the screen |
| `XENIO_VISION_PROVIDER` | `main` | `main` = your main Hermes model (gpt-6-luna), never another provider; `auto` = Hermes' vision routing (**may use pay-per-use providers**); `openai-compatible` = your own endpoint |
| `XENIO_VISION_MODEL` | (main model) | another model for looking (same provider), or the endpoint's model |
| `XENIO_VISION_MAX_EDGE` | `1600` | screenshots are scaled to this many pixels on the long side (320-4096) |
| `XENIO_VISION_API_URL` / `XENIO_VISION_API_KEY` | - | only for `openai-compatible` (e.g. nabh.cloud `qwen2-5-vl-72b`; check its price first) |

No manual step is needed for vision. If `--health` ever shows "blocked for vision by a cooldown", run
`hermes auth reset openai-codex`. See [VISION_CAPABILITIES.md](VISION_CAPABILITIES.md) for model options and costs.

## Model reliability

**This Mac (2026-10-17, chosen by measurement, free models only):**

| Order | Model | Measured through XENIO (warm Hermes session) |
|---|---|---|
| main | `thinkingmachines/inkling:free` (OpenRouter), reasoning `low` | greeting 4.0 s, a tool task 20.9 s, arithmetic 6.6 s; reads images (test card read correctly in 12.6 s) |
| fallback 1 | `nvidia/nemotron-3.5-super-vl:free` | sees images; direct API: tool call 2.0 s, vision 4.3 s, but 20-128 s per turn with Hermes' full prompt |
| fallback 2 | `google/gemma-4-31b-it:free` | sees images; often rate-limited |
| fallback 3 | `nvidia/nemotron-3-super-120b-a12b:free` | text only, another vendor's capacity |
| fallback 4 | `nvidia/nemotron-3-ultra-550b-a55b:free` | text only (was the main model: 32 s for "OK", 79 s then a 503 in a real turn) |

`auxiliary.free_only: true` keeps Hermes' side tasks (titles, compression, vision fallback) on free models. The
OpenRouter account has purchased credits, which raises the free-model limit to 1,000 requests a day; nothing in this
setup spends them. `thinkingmachines/inkling:free` answers only "agentic harnesses" (Hermes qualifies; a plain API
call gets 403). A local model was considered and not used: on this 16 GB MacBook Air an 8B model is the practical
size, too weak at choosing among XENIO's 60+ tools, and would take ~5 GB of memory while you work.

Model turns stay at seconds; the **fast path** answers simple commands without the model (see README).

**Windows laptop (2026-10-03):**

| Order | Model | Why |
|---|---|---|
| main | `gpt-6-luna` via `openai-codex` (your ChatGPT sign-in), `agent.reasoning_effort: low` | the cheapest Codex tier your plan allows (gpt-5.5, gpt-5.4, gpt-6-sol are refused for ChatGPT sign-ins); correct in every test; ~35k tokens per tool task with 0 reasoning tokens at low effort |
| fallback 1 | `thinkingmachines/inkling:free` (OpenRouter) | best free model in three live tests (correct 3/3, fastest) |
| fallback 2 | `nvidia/nemotron-3-ultra-550b-a55b:free` (OpenRouter) | correct 3/3, slower; a different provider, so one busy free model doesn't block the other |
| fallback 3 | `gemini-3.8-flash` (Google AI Studio key in Hermes' `.env`) | works; the free API tier is slow on long tasks |

Hermes switches down the chain automatically on rate limits (e.g. the Codex usage limit), overload,
server or connection errors — verified live by making Codex unreachable: the request was answered
by `inkling:free`, then Codex took over again. `--health` shows the chain and whether Codex is
signed in. XENIO starts a fresh Hermes conversation whenever this setup changes (Hermes keeps the
model a conversation was created with).

```powershell
hermes fallback list                  # show the chain
hermes auth status openai-codex       # Codex sign-in
hermes model                          # change the main model (XENIO picks it up on its next start)
```

The previous main model, for reference:

| | |
|---|---|
| **Previous model** | `cohere/north-mini-code:free` via OpenRouter. Free, tool-capable, 256K context — but small: in the live tests it sometimes mislabeled a "no" as "blocked for security", said a site "blocks access" when only page extraction failed, and ignored "file name only" in the model comparison. |
| **Stronger free options** (OpenRouter, tool calling, checked 2026-10-03) | `nvidia/nemotron-3-ultra-550b-a55b:free`, `qwen/qwen3.8-27b:free`, `nvidia/nemotron-3-super-120b-a12b:free`, `google/gemma-4-31b-it:free` — free models are rate-limited and can disappear. |
| **Low-cost paid** ($ per million tokens in/out) | `deepseek/deepseek-v4-flash` (0.03/0.06), `qwen/qwen3.7-plus` (0.32/1.28), `deepseek/deepseek-v4-pro` (0.21/0.42), `google/gemini-3.5-flash` (1.50/9.00) |
| **Strongest for agent work** | `anthropic/claude-sonnet-5.5` (2/10), `openai/gpt-5.5` (5/30), `anthropic/claude-opus-5` (5/25), `google/gemini-3.1-pro-preview` (2/12) |
| **Recommendation** | For a voice agent that must follow safety instructions and choose tools reliably, a mid-tier paid model (e.g. `anthropic/claude-sonnet-5.5` or, cheaper, `deepseek/deepseek-v4-pro`) is the biggest single improvement. Try a stronger free one first if you want to stay free. |

Prices and availability change; check openrouter.ai/models. To switch (your decision — paid
models are billed to your OpenRouter account):

```powershell
hermes model                      # interactive picker, or:
hermes config set model.default "deepseek/deepseek-v4-pro"
```

XENIO's safety does not depend on the model: every tool call is checked by the plugin whatever
the model decides.

## Setting up from scratch (another machine)

macOS:

```bash
# 1. Hermes Agent installed and working:  hermes status
cd "path/to/Xenio Personal Ai"
python3.14 -m venv .venv          # any CPython 3.11+; Hermes' installer ships 3.14 in ~/.local/share/uv/python
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env              # then put your keys into STT_API_KEY / TTS_API_KEY
.venv/bin/python -m app.main --install-guard
.venv/bin/python -m app.main --health
```

Windows:

```powershell
# 1. Hermes Agent must already be installed and working:  hermes status
# 2. XENIO
cd "C:\path\to\Xenio Personal Ai"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
copy .env.example .env          # then put your keys into STT_API_KEY / TTS_API_KEY
python -m app.main --install-guard
python -m app.main --health
```

Only these two lines in `.env` are required:

```
STT_API_KEY=PASTE_YOUR_KEY_HERE
TTS_API_KEY=PASTE_YOUR_KEY_HERE
```

The endpoints default to nabh.cloud in `.env.example`. To use another OpenAI-compatible provider
(OpenAI, Groq, a local Whisper/Kokoro server) change `STT_API_URL`/`STT_MODEL` and
`TTS_API_URL`/`TTS_MODEL`/`TTS_VOICE`. The TTS endpoint must be able to return WAV.

## Choosing audio devices

XENIO uses the WASAPI default microphone on Windows (the MME default on this laptop delivers
silence — see ARCHITECTURE.md) and the system default speaker. To pick others:

```powershell
.\.venv\Scripts\python.exe -m sounddevice      # lists devices with their index
```

then set `XENIO_INPUT_DEVICE=<index>` / `XENIO_OUTPUT_DEVICE=<index>` in `.env`. Prefer devices
whose host API is "Windows WASAPI".

## Undoing XENIO's change to Hermes

```powershell
hermes plugins disable xenio-guard
hermes config unset platform_toolsets.acp
hermes config unset tools.tool_search.enabled
Remove-Item -Recurse "$env:LOCALAPPDATA\hermes\plugins\xenio-guard"
```

Without the plugin XENIO still talks to Hermes, but it loses its hands (the `xenio_*` tools) and
its policy: Hermes then only asks before file edits and before the commands its own detector
flags (single-file deletes, `git push`, form submissions in the browser etc. would run unasked).
`--health` reports this.

Data XENIO keeps (all local, in `%LOCALAPPDATA%\xenio`): `logs\` (redacted), `sources.jsonl` (web
sources used: title, link, short excerpt), the session id, the wake-word model.

## Trusting more folders

Your home folder, Desktop, Documents, Downloads, Pictures, Music, Videos, the XENIO workspace and
the temp folder count as "your folders": creating and editing files there needs no confirmation.
To add more (e.g. a projects drive), set in `.env`:

```
XENIO_ALLOWED_DIRS=D:\Projects;E:\Work
```

Secrets (`.env`, SSH keys, browser password stores, API credentials) and system folders stay
protected regardless.
