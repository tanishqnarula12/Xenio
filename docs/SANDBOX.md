# The sandbox: what runs where, and its limits

Agents in the War Room write code, install packages and run tests. None of that code was written by XENIO, so none
of it may run with your rights. This page lists every place XENIO starts a program, how each is contained, what
macOS's sandbox does and does not give us, and where this is going.

## The rule

**Code XENIO did not write never runs unprotected.** If the sandbox is not available, the step is refused: it is
never quietly run without it. This holds for every path below marked *sandboxed*, and tests prove it
(`tests/test_foundry_sandbox.py`, `tests/test_engineering_verify.py`).

## Every place XENIO starts a program

Audited 2026-11-14 over `app/foundry/` and `app/engineering/`.

| Where | What runs | Contained how |
|---|---|---|
| `foundry/runtime.py` (agent terminal) | commands an agent asks for | **sandboxed** (`sandbox.run`): no network beyond this Mac, writes only in its task folder, your home folder unreadable, clean environment, time and CPU limits |
| `engineering/verify.py` (tests) | the project's tests, written by agents | **sandboxed** (`sandbox-exec`): no network beyond this Mac, writes only in the work folder and caches, your home folder unreadable except the language tools themselves, clean environment, time limit. Refused without a sandbox (fixed 2026-11-14: it used to run anyway). |
| `foundry/deps.py`, `foundry/skills/sources.py` | installing packages, fetching skills | **sandboxed**, network on for that step only, wheels only (no build scripts), npm with `--ignore-scripts`, public registries only |
| `foundry/skills/manager.py` | running a skill's scripts | **sandboxed** |
| `foundry/preview.py` | starting the finished app for you to try | **sandboxed** (`sandbox.spawn`), on a port of this Mac only |
| `engineering/repo.py`, `foundry/engine.py`, `foundry/deps.py` | `git` on the job's own folders | not sandboxed: XENIO's own commands, argument lists (no shell), git hooks switched off (`core.hooksPath=/dev/null`), environment cleaned |
| `engineering/service.py`, `foundry/github_ops.py` | `git push`, `gh pr create`, `gh repo create` | not sandboxed (they must reach GitHub): only on your click or your yes, never forced, hooks off, no token passes through XENIO |
| `engineering/security.py` | `bandit` (static scan) | reads code, does not run it |
| `foundry/toolbox.py`, `foundry/sight.py`, `foundry/deliverables.py` | Apple's `sips`, `avconvert`, `qlmanage`, `textutil` | fixed system programs with argument lists; the files they touch are checked against your safety policy first |
| `foundry/computer.py`, `desktop/safari_script.py` | `osascript` (AppleScript) | fixed scripts; what an agent says is passed as data (`argv`), never as script |
| `foundry/hands.py`, `foundry/vault.py` | XENIO's own Swift helpers | XENIO's code, built from `native/` |
| `foundry/mcp.py` | MCP servers **you** added | not sandboxed, like in every MCP app: they are your programs. They get a clean environment (never XENIO's keys), only you can add one, and an agent's first use in each job asks you. |

## What `sandbox-exec` gives us, and what it does not

XENIO uses macOS's own sandbox (`/usr/bin/sandbox-exec` with a profile written per run).

**It gives:** real, kernel-enforced rules on file reads and writes and on the network, per process tree, at no cost
and with nothing to install. Tests run real escape attempts against it: reading the home folder and `~/.ssh`,
writing outside, symlinks leading out, reaching the internet, inheriting secrets.

**Its limits:**

- **Deprecated, though still used by Apple itself.** Apple marks `sandbox-exec` deprecated and documents neither the
  profile language nor its future. It works on macOS 26, and macOS's own services use the same mechanism.
- **macOS only.** There is no fallback on other systems; there the War Room refuses to run code.
- **No memory or process-count limit.** CPU time, file size and wall-clock time are limited; memory and the number of
  processes are not. A runaway test can use a lot of memory until its time limit ends it.
- **Same user.** The sandboxed process runs as you, inside the rules. Anything the rules allow, it can do.
- **Reads outside the home folder are allowed** (system libraries, `/usr`, `/opt/homebrew`), because language tools
  need them. Nothing secret is kept there by XENIO.
- **`localhost` is reachable**, so a test can talk to a server it started. That also means it could talk to other
  local services on this Mac.

## Where this is going

A replaceable backend, so the same rules can be enforced by something stronger where it exists:

```python
class Sandbox(Protocol):
    def available(self) -> bool: ...
    def run(self, argv, cwd, writable, *, readable, network, timeout, env, limits) -> Result: ...
    def spawn(self, argv, cwd, writable, *, readable, env, limits) -> Process: ...
```

Every caller already goes through `app/foundry/sandbox.py` (and `engineering/verify.py`, to be moved behind it), so
the backend can change without touching them.

| Candidate | For | Notes |
|---|---|---|
| `sandbox-exec` (today) | macOS | as above |
| Apple's Containerization framework (macOS 26) | macOS | a lightweight Linux VM per container: memory and process limits, a separate kernel. Needs the code to run on Linux. |
| Virtualization.framework VM | macOS | the strongest wall, the most cost per run |
| bubblewrap + seccomp, or gVisor | Linux | for running the War Room on Linux or in CI |
| Docker / OrbStack | both | familiar, but needs a daemon and is heavier than the work it would wrap |

Open items, tracked in [ROADMAP.md](ROADMAP.md): the backend interface, memory and process limits, `localhost`
limited to ports the job opened, and moving `verify.py` onto the shared sandbox module.
