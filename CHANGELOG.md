# Changelog

All notable changes to XENIO. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
versions follow [Semantic Versioning](https://semver.org/): see [docs/RELEASING.md](docs/RELEASING.md).

## [Unreleased]

### Added
- **Agents keep a task list you can watch.** A `todo` tool: each agent writes its plan, marks one step doing and
  each step done, and its seat on the Floor shows the list live. One short line per update, so it costs almost nothing.
- **Real names for what the War Room makes.** A project is named for what it is (`arts-college-landing-page`, not
  `build-a-landing-page-for-a-new-a-b69f`): the folder, the README's title, and the name offered for GitHub, which the
  plan chooses and you can change. A repository already on GitHub can be renamed from the War Room.
- **Commits are yours, with XENIO as co-author**, like Claude Code: a `Co-Authored-By: XENIO` line on every commit
  XENIO makes, linked to XENIO's GitHub account ([@xenio101](https://github.com/xenio101)) so its picture shows. A
  hook in `.githooks/` adds the same line to commits you make in this repository.
- **The War Room grew into a digital company.** One sentence starts a team; you can talk to any agent mid-task; every
  message is acknowledged.
- **Agents in your own Safari**, through Safari's own scripting, on any desktop and without your mouse, with
  auto-waiting for slow pages.
- **Token limits** for a whole job and for one agent's task, with saved defaults and a Limits control on a running job.
- **MCP connectors:** add a server as a program or an address, or paste an `mcpServers` block. Tokens are kept in the
  macOS Keychain; agents ask you the first time in each job.
- **GitHub from the War Room:** create a repository for finished work (private by default), push, and pull
  (fast-forward only). Never forced.
- **Figma:** a XENIO plugin and a local bridge, so a designer agent draws whole frames in your open file.
- **A password vault** agents can use but never read, opened by your fingerprint.
- **CI:** Python lint and tests on macOS (3.12 and 3.14), the interface's type check, tests and build, a dependency
  audit, and a secret scan of the whole history.
- **Docs:** installation, development, the sandbox audit, security policy, contributing guide, roadmap, release
  process. Screenshots and the demo video in the README.
- `requirements.lock` (exact, tested versions) and `requirements-dev.txt`.

### Changed
- **The War Room's live graph:** a count of where the work is, work flowing along the line into the running step,
  corner brackets on what is running or waiting for you, and pointing at a step lights its own lines. Motion stops
  when you ask for reduced motion.
- **One builder per small or medium job**, end to end, instead of hand-overs between agents: real jobs failed at every
  hand-over.
- Agents get the files they work on at the start, and **a read of a file they already have, unchanged, does not run**.
  Page updates happen in one call that sees the whole page.
- **Fix rounds may only edit existing files**, never rewrite them whole: a real fix round rewrote `main.py` and broke 19
  tests.
- Reviews of a personal tool no longer demand logins and server hardening nobody asked for.
- A new medium-sized project is no longer debated before it is built (a real one spent 130 s on whether a personal
  to-do app should be single-user); a medium change to your own code still settles one question first.
- **README:** how a request flows, how approvals work, and the system's parts, as diagrams, with screenshots of Home,
  the chat and Approvals from a real conversation.
- Django projects' own tests are found and run; a multi-line `.xenio-run` start script runs as a whole.

### Fixed
- **Security:** agent-written tests ran unsandboxed when the sandbox was missing; they are now refused.
- **Security:** the test runner's sandbox let agent-written tests read your home folder; it no longer does.
- **Security:** the menu tool built AppleScript from an agent's text, so a crafted menu name could run a shell
  command; names now travel as data. Spotify track addresses are validated before they reach a script.
- **Security:** the guard plugin's own output masking never ran (an unbound name, caught and hidden); it runs now.
- `certifi` updated to 2026.10.18 (PYSEC-2026-4320).
- **Deleting to the Trash failed on every Mac.** macOS's `trash` command took the `--` XENIO passed for a file name,
  so every delete failed and Hermes fell back to `rm`. Tested now against the real command.
- C and C++ lessons in the Teacher stopped compiling after a macOS update: the newest SDK could not be read by its own
  linker. XENIO now picks the newest SDK that really links.
- A War Room task failed with "is not in the subpath of" when its folder was reached through a link (on macOS, `/var`
  is `/private/var`).
- A one-answer build cut off at its token limit was paid for again and reported as finished; it now keeps what came
  complete, says what is missing, and is not run again.
- The demo video plays as it downloads (its index was at the end), and the README shows a moving preview of it.
- CI: tests that assumed India's time zone, a `~/.zshrc`, or being run from `.venv` now pass on any Mac.
- The repository holds only XENIO: coding-assistant skill packs and a stray root `package.json` were removed, and
  version notes moved to `docs/history/`.
- A one-page site skipped its fast single-call path after the planner shortcut for small apps; pages keep it.
- A review written as JSON showed raw braces in the conversation.
- On the home screen, a running job's title was squeezed to one word per line.
- A connection to the model provider that dropped mid-answer ended a whole job; it is now treated as a network error.

## [0.1.0] - 2026-11-05

The first version on GitHub, before this changelog existed. In short, by date:

- **2026-11-05** The Foundry: an engineering team you command from one sentence. Start XENIO from its Dock icon.
  A plain page costs 25,000 tokens instead of 990,000.
- **2026-11-04** Teaching in the chat; chats are kept.
- **2026-11-03** XENIO V4: the real-time desktop, forms, the Teacher, Image Studio and chat.
- **2026-10-25** Mission Control.
- **2026-10-18** The desktop app, the README and its banner, the MIT license.
- **2026-10-17** The macOS port, the fast path, and the V6 engineering team: roles, task graph, isolated repositories,
  verification and a security scan.

[Unreleased]: https://github.com/tanishqnarula12/Xenio/compare/2f1fb73...HEAD
[0.1.0]: https://github.com/tanishqnarula12/Xenio/commits/2f1fb73
