# The Foundry: security

The short version: agents get a lot of autonomy over work, and none over rules.

## What no agent, decision or team can do

| It cannot | Because |
|---|---|
| Widen its own permissions | `registry.update` refuses permission fields from anything but you, server-side. Tested for every field and actor |
| Pick its own tools | The factory chooses capabilities from the kind of work. What the model says about tools is ignored |
| Use a forbidden tool | `FORBIDDEN_TOOLS` is removed when a role compiles, and the model is never told those tools exist |
| Approve something | There is no route. Saying "the user approved" in a message moves nothing. Tested |
| Speak as you | `MessageBus` refuses the senders `you` and `xenio` unless XENIO's own code sends them |
| Give itself a skill | `skills` is a permission field, and `assign` needs you |
| Approve its own code | Reviewers cannot write. A run with no independent review cannot be accepted |
| Run forever | Loops, rounds, milestones, steps and budgets all have hard limits. A graph with a cycle is refused |
| Install a package | The guard blocks it, and the sandbox has no network anyway |

A decision is text. Nothing that enforces anything reads it. Consensus changes nothing about what is allowed.

## The wall

Every command an agent runs on the native runtime, every package install, and every skill runs under macOS `sandbox-exec` (`app/foundry/sandbox.py`). These are checked by tests that run the real sandbox, not by reading the rules:

* the home folder cannot be read, including SSH keys and `.env` files
* writes go only to the task's own folder
* no internet: a program can only talk to itself on localhost
* a clean environment: no API keys, no tokens
* CPU time, file size and wall-clock limits
* the worktree's `.git` pointer cannot be replaced
* no sandbox available means it does not run at all

**A test caught a real hole here during development.** A rule meant to allow localhost also let the whole internet through. It is fixed, and now covered by a test against the real sandbox. That is why these are tested for real.

**What still gets through:** `sandbox-exec` is deprecated by Apple, though it works on this macOS. Reads outside the home folder are allowed (system libraries need them), so something inside could read `/etc/hosts`, for example. It is one strong layer, not a virtual machine.

## Prompt injection

Everything agents did not write themselves is framed as data: other agents' messages, tool output, repository content, skill output and skill instructions.

* A frame cannot be closed from inside: closing tags in the content are removed.
* What you say is framed as requirements. Nobody else's words can get that frame.
* The judge is told options, not who holds them or how many do.

Models can still be talked into things. That is why the framing is not what stops anything: the policy and the sandbox are, and they do not read prompts.

## Skills

```
identify -> fetch (nothing runs) -> read -> assess -> YOU -> install (sandbox, no scripts) -> test -> register -> give to named agents
```

* **npx is never simply run.** It is treated as an npm package with a command. An npx line using `-c` or `-p` is refused.
* **Fetching runs nothing.** `npm pack --ignore-scripts`, and `git clone --depth 1` with hooks off and symlinks off. Archives cannot climb out of their folder, or contain links.
* **Integrity.** The npm tarball is checked against the published sha512. A GitHub skill records its commit. Installed files get a fingerprint checked before every use, and a skill whose files changed switches itself off.
* **Looks dangerous, and never installed without you:** pipes a download into a shell, reads SSH or cloud keys, reads browser data, reads this assistant's own secrets, posts to throwaway addresses, makes itself start on login, decodes and runs hidden code. The reason says where it was found, and offers a safer way when there is one.
  No agent and no approval by a team can install one of these. You can, after seeing what was found and confirming a second time: **Install anyway, locked down**. It then runs with no internet, no keys and no access to your files. This was a rule with no override until 2026-11-04, when it was blocking real skills you wanted. It is a deliberate relaxation, and the sandbox is what holds. Files a package never runs (`docs`, `tests`, `examples`, `.github`, Dockerfiles) are shown as notes, and do not make it look dangerous.
* **High risk needs a look:** install scripts, dependencies from outside the registries, `eval`, obfuscation, prebuilt binaries, instructions written against you, invisible characters.
* **It is not called active until it has loaded and passed its test** in the sandbox.

**Limit:** the reading is static, so a determined author can hide from it. Passing is not proof of safety. That is why an approved skill still only ever runs inside the wall.

## Packages for a project

Agents ask for what they need with the `xenio_install` tool, or list it. XENIO installs it, and only in a project XENIO created itself: public registries only (a git URL, private index or local path is refused), install scripts off, Python wheels only. In your own repository a needed package is reported to you instead.

**Limit:** this does not check whether a package is itself malicious, or a typo-squat of a real one. It stops install-time code, and the sandbox contains what runs later. Before you run a finished project outside the sandbox, read what it depends on, as you would with any code.

## Secrets

Redacted before anything is stored or sent to a model: messages, events, graph nodes, decisions, agent memory, file contents and command output. A definition or memory holding a key is refused. `foundry.db` is mode 600. Commands get no keys. A key pasted into a goal goes no further. Redaction is pattern-based, so an unusual secret format could slip past it.

## What was deliberately kept

Authentication, the approval gate, the guard, roles, worktrees, XENIO's own test runs, test-tampering detection: all unchanged. `apply`, `push` and `pull_request` still go through the engineering service and its confirmations. Deployment is still never part of an engineering task.

## One judgment call to check

In supervisor mode, a command that policy rates "needs a yes" only because XENIO does not recognise it runs anyway, inside the sandbox, on the reasoning that it can only reach that task's folder. Asking would stall the team on every unusual tool. High-risk commands still ask, and read-only roles still cannot run them. Hermes-routed agents do not get this, because they have no sandbox.

## Known limits

* Hermes-routed agents (a pinned model) have the guard but no OS sandbox. Only native agents do.
* `xenio_skill` exists on the native runtime only. Written skills work on both.
* An agent on a Hermes-only model cannot use `xenio_team` to message teammates while working. It still hears what was said, in its next step.

## Masking secrets without breaking code

Masking is what keeps a key out of a log or a message. An earlier version was too eager on source code: it treated `query_tokens = tokenize(q)` as a secret. That had three effects. The scanner reported false high-severity "secrets", reviewers saw `[REDACTED]` in the middle of a function and called the code invalid, and a build that passed its tests could not be accepted. Code files are now masked by `redact_for_file`, which only takes a value that looks like a literal credential. A real key in a string is still caught, and there are tests for both.

## What it built, running

A preview runs from a throwaway copy under the same sandbox, with its own empty home folder, no keys, and only its project and its own tools readable. It listens on a local port and the page shows it in a sandboxed frame. It can reach the internet only as far as its project needed to in order to install, and it cannot write to your project.

## Agents helping each other

Neither kind of help can widen what an agent may do.

* **Senior advice** is one model call with no tools. The text is framed as advice and as data. Files come only from inside the task's folder, and hidden files, `.env`, keys and anything named secret, credential, token or password are skipped.
* **A helper** is set up by XENIO, not by the model. Its tools and terminal mode are copied from the agent who asked, its folders must be inside that agent's, it cannot install packages or bring in anyone else, and it spends from the same budget. The right to ask comes from the job's size and your autonomy setting, in the session's scope, where a model cannot change it.

## A skill kept with a project

`add_to_project` copies files only, and only for a skill you approved that this agent has. It skips symlinks and refuses anything over 400 files or 5 MB. Files land in the agent's isolated copy, inside `.agents/skills/`, so the check on what a task changed lets exactly that folder through. Those files are left out of the security scan and the review, because they were assessed when you installed the skill and are not the team's code.
