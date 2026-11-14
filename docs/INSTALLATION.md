# Installing XENIO

This guide takes a new Mac from nothing to a working XENIO. It needs no knowledge of how XENIO was built.

## What is supported

| | Supported | Notes |
|---|---|---|
| **Operating system** | macOS 26 or later, Apple silicon | Everything is built and tested here. The sandbox, desktop control, Safari scripting and the native helpers are macOS-only. |
| | Windows 11 | The voice assistant and the chat ran on Windows earlier. The War Room, desktop control and the sandbox do not; Windows is not tested in CI. |
| **Python** | 3.11 or later | CI tests 3.12 and 3.14. Development uses 3.14. |
| **Node.js** | 22 | Only to build the interface (`ui/`). |
| **Hermes Agent** | 0.22 or later | XENIO's chat assistant. It is installed separately; XENIO never imports its code. |

## 1. Get the code

```bash
git clone https://github.com/tanishqnarula12/Xenio.git
cd Xenio
```

## 2. Python environment

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.lock   # exact, tested versions
```

`requirements.txt` lists the same packages with version ranges, if you prefer those.

## 3. Keys and settings

```bash
cp .env.example .env
```

Open `.env` and set at least:

```ini
STT_API_KEY=PASTE_YOUR_KEY_HERE   # speech to text
TTS_API_KEY=PASTE_YOUR_KEY_HERE   # text to speech
```

The defaults use [nabh.cloud](https://nabh.cloud), whose key also runs the War Room's models. Any OpenAI-compatible
speech endpoint works: change `STT_API_URL`, `STT_MODEL`, `TTS_API_URL`, `TTS_MODEL` and `TTS_VOICE`. Every other
setting in `.env.example` is optional and documented there. `.env` is git-ignored; never commit it.

## 4. Hermes Agent

Install [Hermes Agent](https://github.com/NousResearch/hermes-agent) and check it works with `hermes status`. Then
install XENIO's guard plugin into it, which gives Hermes XENIO's tools and puts XENIO's safety policy in front of every
tool call:

```bash
.venv/bin/python -m app.main --install-guard
```

To undo this later, see [SETUP.md](SETUP.md#undoing-xenios-change-to-hermes).

## 5. The interface

```bash
cd ui && npm ci && npm run build && cd ..
```

## 6. Native helpers (macOS)

The floating orb, the War Room's private browsers and the password vault are small Swift programs. They build on
first use; to build them up front:

```bash
bash native/orb/build.sh
bash native/hands/build.sh
```

They need Apple's Command Line Tools (`xcode-select --install`).

## 7. Check everything

```bash
.venv/bin/python -m app.main --health
```

It names anything missing, including each macOS permission below.

## 8. Start it

```bash
.venv/bin/python -m app.main --ui      # the app: chat, War Room, Mission Control
.venv/bin/python -m app.main --voice   # voice only: say "Hey Xenio"
```

## macOS permissions

macOS grants these to the app XENIO is started from (Terminal, your editor, or XENIO Orb when you start it from the
Dock), not to Python. Allow them in *System Settings > Privacy & Security*:

| Permission | Needed for |
|---|---|
| **Microphone** | voice mode (macOS asks the first time) |
| **Accessibility** | reading and pressing controls in other apps, typing, window control |
| **Screen & System Audio Recording** | "look at my screen", window titles |
| **Automation** | controlling Safari, Terminal, Music and Spotify (macOS asks the first time) |

## Optional

| What | How |
|---|---|
| **Agents in your own Safari** | Safari > Settings > Advanced > *Show features for web developers*, then Develop > Developer Settings > *Allow JavaScript from Apple Events*. This lets any app you allowed to control Safari run scripts in your pages, so turn it on only if you are comfortable with that. |
| **Figma** | In the Figma desktop app: Plugins > Development > *Import plugin from manifest*, pick `native/figma-plugin/manifest.json`, run it in your file and paste the pairing code from War Room > Connectors. |
| **GitHub** | `gh auth login`. XENIO uses that sign-in and never sees the token. |
| **Google Calendar and Gmail (read-only)** | [PERSONAL_INTEGRATIONS.md](PERSONAL_INTEGRATIONS.md) |
| **MCP servers** | War Room > Connectors. Tokens are kept in the macOS Keychain. |
| **Spotify instead of YouTube for songs** | `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET` in `.env` |

## Uninstall

Remove the guard from Hermes ([SETUP.md](SETUP.md#undoing-xenios-change-to-hermes)), delete the folder, and delete
XENIO's data in `~/.local/share/xenio`.
