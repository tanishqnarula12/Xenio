# Roadmap

Where XENIO stands against its engineering roadmap. **Done** means built and checked, **partly** says what is missing,
**open** has not started. Last updated 2026-11-14.

## Phase 1: critical engineering fixes

### 1. CI/CD (GitHub Actions)

| Item | Status |
|---|---|
| `.github/workflows/ci.yml` | **Done** ([ci.yml](../.github/workflows/ci.yml)). It has not run on GitHub yet: it runs on the first push. |
| Python tests on every pull request | **Done**: macOS, Python 3.12 and 3.14 |
| Interface tests, type check, production build | **Done** |
| Python lint | **Partly**: correctness rules (syntax, undefined names, unused imports in the app) pass; formatting and style rules are not enforced yet |
| Security and dependency scanning | **Done**: `pip-audit`, `npm audit`, gitleaks over the whole history |
| Required checks before merging | **Open**: a repository setting (branch protection on `main`) that the owner switches on |
| CI badge in the README | **Done** |

### 2. Sandbox security

| Item | Status |
|---|---|
| Audit every execution path | **Done**: [SANDBOX.md](SANDBOX.md) lists each one and how it is contained |
| Document `sandbox-exec`'s limits | **Done** |
| A replaceable sandbox backend | **Partly**: designed (the interface is in SANDBOX.md); not built |
| Evaluate alternatives | **Done**: Containerization framework, VMs, bubblewrap/gVisor, Docker |
| Files limited to approved folders | **Done**. Fixed 2026-11-14: the test runner's sandbox let agent-written tests read the home folder. |
| Network off by default | **Done** |
| No SSH keys, secrets or credentials | **Done**, with tests that try |
| CPU, memory, process and time limits | **Partly**: CPU, time and file size are limited; memory and process count are not |
| Fail closed | **Done**. Fixed 2026-11-14: agent-written tests ran unsandboxed when the sandbox was missing. |
| Security regression tests, malicious scripts, path traversal | **Done** (`tests/test_foundry_sandbox.py`, `tests/test_engineering_verify.py`) |

### 3. Public documentation

| Item | Status |
|---|---|
| Review `SETUP.md`, remove machine-specific paths | **Done**: it is now a configuration reference |
| `INSTALLATION.md`, `DEVELOPMENT.md` | **Done** |
| Every setting in `.env.example` | **Done**: 10 were missing |
| No secrets in tracked files or history | **Done**: scanned; only deliberately fake test values and placeholders |
| Rotate possibly exposed credentials | **Open, owner's action**: earlier setup notes say a nabh.cloud key and a Gemini key were once pasted into chats. Neither is in the repository, but replacing them is the safe choice. |
| Supported systems and Python versions | **Done** ([INSTALLATION.md](INSTALLATION.md)) |

## Phase 2: reliability and code quality

### 4. Dependencies

| Item | Status |
|---|---|
| Reproducible lockfile | **Done**: `requirements.lock` (exact versions). Hash checking is open. |
| Runtime and development split | **Done** |
| Audit vulnerable packages | **Done**: `certifi` updated for PYSEC-2026-4320; npm 0 vulnerabilities |
| Outdated packages | **Partly**: listed. Minor updates are pending; `sherpa-onnx` 2.0 is a major update that needs testing with the wake word. |
| Root and interface npm manifests | **Done**: the root one holds `gsap` for built pages |
| How to update dependencies | **Done** ([DEVELOPMENT.md](DEVELOPMENT.md#dependencies)) |

### 5. Testing and reliability

About 3,500 Python tests and 400 interface tests, many written from real failures.

| Item | Status |
|---|---|
| Orchestration integration tests | **Done** (`tests/test_foundry_engine.py` and others) |
| Permission enforcement, malformed and adversarial input | **Done**, including injection attempts on AppleScript (2026-11-14) |
| Interrupted and concurrent agents, API failures and time-outs | **Done** |
| Memory persistence and recovery, desktop automation failures | **Partly** |
| Find untested critical paths | **Open**: needs coverage measurement |
| Publish test results and coverage | **Open** |

### 6. Architecture

| Item | Status |
|---|---|
| Oversized modules | **Open**: `app/foundry/engine.py` is 3,700 lines and holds orchestration, policy and execution together |
| Separate orchestration, execution and policy | **Open** |
| Consistent errors, logging, structured tracing | **Partly**: every War Room step is an event with its agent, task and timing; logging is not uniform |
| Module boundaries documented | **Partly** ([ARCHITECTURE.md](ARCHITECTURE.md), [FOUNDRY.md](FOUNDRY.md)) |
| Unused code | **Partly**: unused imports removed; a dead-code pass is open |

## Phase 3: production engineering

| Item | Status |
|---|---|
| Observability: execution and correlation ids, tool durations, token use | **Partly**: every job, task and message has an id; each agent turn records tokens sent and written, tool steps and seconds |
| Error reporting without sensitive data, diagnostic report | **Partly**: logs are redacted; `--health` reports every part; a single diagnostic bundle is open |
| Performance benchmarks | **Open**: token and time figures exist from real runs ([FOUNDRY.md](FOUNDRY.md)); repeatable benchmarks do not |
| Versioned releases, changelog, upgrade and rollback | **Done** ([RELEASING.md](RELEASING.md), [CHANGELOG.md](../CHANGELOG.md)) |
| Installation verification on a clean machine | **Open** |
| Known limitations | **Done**: the README's "What is verified" table and [SANDBOX.md](SANDBOX.md) |

## Phase 4: presentation

| Item | Status |
|---|---|
| Demo video | **Done**, with personal details blurred |
| Screenshots | **Done**: the War Room from a real verified job; Home, the chat and Approvals from a real conversation through Hermes |
| Verified features separate from experimental | **Done** |
| Architecture diagram | **Done**: the request flow, the approval flow and the system's parts in the README; the detail is in [ARCHITECTURE.md](ARCHITECTURE.md) |
| Contribution guidelines, security policy, roadmap | **Done** |
| Benchmark methodology and results | **Open** |

## The War Room itself

What real jobs showed, and what is next:

- **Full-stack builds** now finish verified on real models, but not every time: when the checks still fail after three
  rounds, XENIO stops and says so. Next: more real runs across stacks, and measuring how often each kind of job ends
  verified.
- **A live edit on a real website** in your own Safari, with your approval before saving.
- **A full design in the Figma app** through the XENIO plugin.
- **Canva**: not built. The way in would be a Canva app, like the Figma plugin.
