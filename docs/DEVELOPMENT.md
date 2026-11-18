# Developing XENIO

## Set up

Follow [INSTALLATION.md](INSTALLATION.md), then add the development tools:

```bash
.venv/bin/pip install -r requirements.lock -r requirements-dev.txt
(cd ui && npm ci)
```

## The checks (the same as CI)

```bash
.venv/bin/ruff check .                               # lint: correctness rules, see ruff.toml
.venv/bin/python -m pytest -q -p no:cacheprovider    # about 3,500 Python tests, about 10 minutes
(cd ui && npm run typecheck && npm test && npx vite build)
.venv/bin/pip-audit -r requirements.lock             # known vulnerabilities in the Python dependencies
(cd ui && npm audit --omit=dev)                      # and in the interface's
```

CI ([.github/workflows/ci.yml](../.github/workflows/ci.yml)) runs all of these on every pull request, plus a gitleaks
scan of the whole history. The Python tests run on macOS, because the sandbox, AppleScript and Accessibility code is
macOS-only.

Tests that need this Mac's hardware or permissions skip themselves elsewhere: the native helpers (`native/build`),
Touch ID, Accessibility, an unlocked screen. Run them locally before changing that code.

## Layout

| Folder | What |
|---|---|
| `app/` | the assistant: orchestration, voice, tools, safety, integrations |
| `app/foundry/` | the War Room: engine, agent runtime (`runtime.py`), sandbox, toolbox, connectors (MCP), GitHub, Figma bridge |
| `app/engineering/` | isolated workspaces (git worktrees), test detection and runs (`verify.py`), reviews, the security scan |
| `app/safety/` | the permission policy every tool call passes |
| `app/ui/` | the local HTTP API and event stream |
| `ui/` | the React + TypeScript interface; its tests are in `ui/src/test` |
| `native/` | Swift helpers (`orb`, `hands`) built by their `build.sh`, and the Figma plugin |
| `hermes_plugin/xenio-guard/` | the plugin that puts XENIO's policy inside Hermes |
| `tests/` | the Python tests |

## Writing tests

- **Every real failure gets a test named after what went wrong**, with the real story in its docstring. Many tests in
  `tests/test_foundry_*.py` read like this; keep it that way.
- **A test never touches anything real of yours**: not your Safari, not your GitHub, not your files. Use stand-ins and
  temporary folders. `tests/test_foundry_github.py` fails any test that tries to run the real `gh`.
- **Never weaken a test to make a change pass.** If a test encodes behaviour that is now wrong, change it in the same
  commit as the behaviour and say why.
- War Room changes are proven twice: by the tests, and by a real job on real models in a scratch folder (a separate
  `XENIO_DATA_DIR` and `XENIO_WORKSPACE`, never your own XENIO).

## Dependencies

- `requirements.txt`: what XENIO needs to run, as version ranges.
- `requirements-dev.txt`: the test, lint and audit tools, pinned.
- `requirements.lock`: the exact versions everything was tested with. CI installs from it.

To update: install the new version into `.venv`, run the checks, then regenerate the lock:

```bash
.venv/bin/pip install -U <package>
.venv/bin/python -m pytest -q && .venv/bin/pip-audit -r requirements.lock
{ echo "# Exact versions, tested together on Python 3.14 / macOS. Regenerate as docs/DEVELOPMENT.md says."; .venv/bin/pip freeze --exclude-editable | sort -f; } > requirements.lock
```

The interface's dependencies are locked by `ui/package-lock.json`; update them with `npm install <package>@<version>`
in `ui/`, then run its checks.

## Native helpers

`native/orb/build.sh` and `native/hands/build.sh` build the Swift helpers into `native/build/` (git-ignored). They
need Apple's Command Line Tools. If `swiftc` refuses the newest SDK after a macOS update, build against an older one
that is installed: `-sdk /Library/Developer/CommandLineTools/SDKs/MacOSX26.sdk`.

## Pull requests

See [CONTRIBUTING.md](../CONTRIBUTING.md).

## XENIO as co-author of your commits

`.githooks/prepare-commit-msg` adds `Co-Authored-By: XENIO` to every commit you make here, so GitHub shows XENIO's mark
([@xenio101](https://github.com/xenio101)) next to yours. Switch it on once per clone:

```bash
git config core.hooksPath .githooks
```

`XENIO_NO_COAUTHOR=1 git commit ...` leaves it out of one commit.

