<p align="center">
  <img src="docs/assets/xenio-banner.svg?v=3" alt="XENIO — a voice-first control layer for Hermes Agent" width="100%">
</p>

<p align="center">
  <strong>Your assistant, with a voice — and your approval in the loop.</strong><br>
  XENIO gives <a href="https://github.com/NousResearch/hermes-agent">Hermes Agent</a> a hands-free voice interface, a desktop UI, and a safety layer for the tools it can use.
</p>

<p align="center">
  <a href="https://github.com/tanishqnarula12/Xenio/stargazers"><img alt="GitHub stars" src="https://img.shields.io/github/stars/tanishqnarula12/Xenio?style=flat&color=8b9cff&label=stars"></a>
  <a href="https://github.com/tanishqnarula12/Xenio/commits/main"><img alt="Last commit" src="https://img.shields.io/github/last-commit/tanishqnarula12/Xenio?style=flat&color=76dfcb"></a>
  <img alt="Python" src="https://img.shields.io/badge/Python-voice%20%26%20orchestration-3776AB?style=flat&logo=python&logoColor=white">
  <img alt="React and TypeScript" src="https://img.shields.io/badge/React%20%2B%20TypeScript-desktop%20UI-3178C6?style=flat&logo=react&logoColor=white">
</p>

<p align="center">
  <a href="#get-started">Get started</a> ·
  <a href="#what-it-does">What it does</a> ·
  <a href="docs/ARCHITECTURE.md">Architecture</a> ·
  <a href="docs/SECURITY_MODEL.md">Safety model</a> ·
  <a href="docs/SETUP.md">Full setup guide</a>
</p>

---

## The idea

Talk naturally. XENIO handles the voice, desktop controls, approvals, and interface; Hermes remains the assistant that reasons and uses tools. Actions that need your consent pause for an explicit approval before they run.

```text
  your voice ──► wake word & speech ──► XENIO ──► Hermes Agent
                                           │           │
                                           │           ├── tools & skills
                                           │           └── answer
                                           ▼
                                  safety checks + approvals
                                           │
                                           └──► spoken reply
```

## What it does

|  |  |
|---|---|
| **Talk, then keep talking**<br>Wake-word voice, interruption, follow-up listening, and replies spoken as they arrive. | **A real desktop companion**<br>Open and control apps, work with files, manage music, and see what XENIO is doing. |
| **Approvals where they matter**<br>Permission checks sit in front of tool actions; risky actions need a clear yes, and some are blocked. | **Mission Control UI**<br>Conversation, voice orb, tasks, projects, schedules, tools, activity, and pending approvals in one place. |
| **Useful without a model round-trip**<br>Common commands like app launch, volume, music, and time use a fast path. | **A team for engineering work**<br>Specialist agents work in isolated checkouts; applying changes, pushing, and opening PRs each need your approval. |

XENIO also supports screen questions when you ask, personal context integrations, and scheduled briefings. See the [capabilities guide](docs/ARCHITECTURE.md) and the [UI guide](docs/UI.md).

## Get started

### You’ll need

- Python and Node.js/npm
- [Hermes Agent](https://github.com/NousResearch/hermes-agent) installed and configured
- Speech service credentials for the voice features you enable

### Install

```bash
git clone https://github.com/tanishqnarula12/Xenio.git
cd Xenio
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
cp .env.example .env             # Windows PowerShell: Copy-Item .env.example .env
```

Add the credentials for the features you want to use to `.env`, then check the setup:

```bash
python -m app.main --health
python -m app.main
```

Launch the interface with `python -m app.main --ui`. Build it once first:

```bash
cd ui
npm install
npm run build
cd ..
python -m app.main --ui
```

The [setup guide](docs/SETUP.md) covers Hermes, OS permissions, voice providers, Google integrations, and troubleshooting. Run `python -m app.main --help` for the available options.

## Safety, by design

XENIO checks tool requests against its permission policy before execution. Destructive, external, or otherwise sensitive actions require approval; high-risk actions require a stronger confirmation, and secrets are blocked. The UI is bound to localhost and uses a per-run token. See the [security model](docs/SECURITY_MODEL.md), [prompt-injection defenses](docs/PROMPT_INJECTION_SECURITY.md), and [UI security notes](docs/UI.md#security-boundaries).

## Under the hood

- **Core:** Python · Hermes Agent ACP · permission gate and `xenio-guard` plugin
- **Voice:** wake-word detection · voice activity detection · speech recognition · streaming speech output
- **Interface:** React · TypeScript · GSAP · local HTTP API and server-sent events
- **Platforms:** macOS and Windows desktop support

## Project map

```text
app/                 assistant, orchestration, safety, voice, tools, integrations
app/ui/              local API, events, approvals, and UI controller
ui/                  React + TypeScript interface
hermes_plugin/       xenio-guard policy enforcement inside Hermes
tests/                Python and UI test suites
docs/                architecture, setup, security, and capability guides
```

## Docs

- [Setup and troubleshooting](docs/SETUP.md)
- [Architecture](docs/ARCHITECTURE.md) · [Hermes integration](docs/HERMES_INTEGRATION.md)
- [UI and local API](docs/UI.md)
- [Security model](docs/SECURITY_MODEL.md) · [Engineering security](docs/ENGINEERING_SECURITY.md)
- [Engineering team and workflows](docs/ENGINEERING_TEAM.md) · [Agent roles](docs/AGENT_ROLES.md)
- [Personal integrations](docs/PERSONAL_INTEGRATIONS.md) · [Memory](docs/MEMORY_ARCHITECTURE.md)
- [Voice](docs/ARCHITECTURE.md#voice-mode) · [Screen awareness](docs/VISION_CAPABILITIES.md)

---

<p align="center"><sub>Built to make a capable assistant feel natural to use — while keeping you in control.</sub></p>
