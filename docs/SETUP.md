# Configuring XENIO

To install XENIO, start with [INSTALLATION.md](INSTALLATION.md). This page is the reference for what you can change
afterwards. Every setting goes in `.env`; `.env.example` lists them all with their defaults.

## Speech

| Setting | Default | Effect |
|---|---|---|
| `STT_API_URL`, `STT_MODEL`, `STT_API_KEY` | nabh.cloud Whisper | any OpenAI-compatible `/audio/transcriptions` endpoint |
| `TTS_API_URL`, `TTS_MODEL`, `TTS_VOICE`, `TTS_API_KEY` | nabh.cloud Kokoro | any OpenAI-compatible `/audio/speech` endpoint that can return WAV |
| `XENIO_LANGUAGES` | `en,hi,de` | languages XENIO listens and answers in |
| `XENIO_STT_ENGINE` | `cloud` | `local` uses the on-device Moonshine model instead |

## Choosing audio devices

XENIO uses the system's default microphone and speaker. To pick others, list the devices:

```bash
.venv/bin/python -m sounddevice
```

then set `XENIO_INPUT_DEVICE=<index>` and `XENIO_OUTPUT_DEVICE=<index>` in `.env`.

## Web and browser

| Setting | Default | Effect |
|---|---|---|
| `XENIO_SHOW_BROWSER` | `false` | `true` shows Hermes' own automation browser while it works. Pages you ask to *see* always open in your normal browser. |
| `XENIO_DOWNLOAD_MAX_MB` | `200` | size limit for downloads |

Web search uses Hermes' own configuration. Without a search API key, Hermes uses its keyless free tier.

## Looking at the screen

| Setting | Default | Effect |
|---|---|---|
| `XENIO_VISION` | `true` | `false` removes the ability to look at the screen |
| `XENIO_VISION_PROVIDER` | `main` | `main` is your main Hermes model; `auto` is Hermes' own routing (**may use pay-per-use providers**); `openai-compatible` is your own endpoint |
| `XENIO_VISION_MODEL` | the main model | another model for looking |
| `XENIO_VISION_MAX_EDGE` | `1600` | screenshots are scaled to this many pixels on the long side (320-4096) |
| `XENIO_VISION_API_URL`, `XENIO_VISION_API_KEY` | | only for `openai-compatible` |

## The engineering team and the War Room

| Setting | Default | Effect |
|---|---|---|
| `NABH_API_KEY` | the speech key, when that is a nabh.cloud key | the models the War Room's agents think with |
| `XENIO_ENGINEERING` | `true` | `false` switches the team off |
| `XENIO_ENGINEERING_MAX_PARALLEL` | `3` | agents working at once (at most 4) |
| `XENIO_ENGINEERING_SANDBOX` | `true` | tests of code XENIO did not write run in the macOS sandbox. Without a sandbox they are refused, never run unprotected. |
| `XENIO_ENGINEERING_TASK_TIMEOUT` | `1800` | seconds one agent task may take |
| `XENIO_ENGINEERING_TEST_TIMEOUT` | `600` | seconds one test run may take |

### Commits: you and XENIO

Like Claude Code, a commit XENIO makes is yours (your name and email from `git config --global`) and names XENIO in a
`Co-Authored-By:` line, so GitHub shows you both, with XENIO's picture from its account,
[@xenio101](https://github.com/xenio101). If git does not know you on this Mac, XENIO is the author, on the same account.

| Setting | Default | Effect |
|---|---|---|
| `XENIO_GIT_EMAIL` | `358429157+xenio101@users.noreply.github.com` | XENIO's address in the line: GitHub links it to @xenio101 |
| `XENIO_GIT_NAME` | `XENIO` | the name in the line |
| `XENIO_COMMIT_AS_XENIO` | `false` | `true` makes XENIO the author instead |

For your own name to appear next to XENIO's, tell git who you are, once, with the email of your GitHub account:
`git config --global user.name "Your Name"` and `git config --global user.email you@example.com`.

Token limits per job and per agent task are set in the War Room itself (Options when you start a job, and Limits on a
running one). How the sandbox works and what it does not cover: [SANDBOX.md](SANDBOX.md).

## Choosing the chat model

XENIO's chat runs on whatever model Hermes is configured with, and XENIO's safety does not depend on that choice:
every tool call is checked by the guard plugin whatever the model decides. Change the model with `hermes model`;
XENIO starts a fresh Hermes conversation when it changes.

## Trusting more folders

Your home folder, Desktop, Documents, Downloads, Pictures and Music, the XENIO workspace and the temporary folder count
as your folders: creating and editing files there needs no confirmation. To add more:

```ini
XENIO_ALLOWED_DIRS=/Volumes/Projects;~/Work      # separated by ; or ,
```

Secrets (`.env` files, SSH keys, browser password stores, credentials) and system folders stay protected regardless.

## Undoing XENIO's change to Hermes

```bash
hermes plugins disable xenio-guard
hermes config unset platform_toolsets.acp
hermes config unset tools.tool_search.enabled
rm -r ~/.hermes/plugins/xenio-guard
```

Without the plugin XENIO still talks to Hermes, but it loses its tools (`xenio_*`) and its policy: Hermes then only
asks before file edits and before the commands its own detector flags. `--health` reports this.

## What XENIO keeps, and where

All of it is local, in `~/.local/share/xenio`: redacted logs, the War Room's database, the sources it used on the web,
the Hermes session id and the wake-word models. The vault's passwords are encrypted with a key held by the Mac's
Secure Enclave ([VAULT.md](VAULT.md)); connector tokens are in the macOS Keychain.
