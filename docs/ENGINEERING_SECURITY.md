# Engineering team security (V6)

What can go wrong when AI agents work on your code, and what stops it. The engineering team adds no second
permission system: every agent action passes the same `xenio-guard` policy as the rest of XENIO, plus engineering
rules that only ever make things stricter.

## Threats and controls

| Threat | Control | Where |
|---|---|---|
| An agent changes files outside its task | the guard blocks writes outside the task's worktree and `allowed_paths` (symlinks resolved, multi-file patches checked per file); XENIO re-checks the real diff and never merges a task that touched other files | `policy.classify_engineering`, `orchestrator._record_writer` |
| An agent damages the user's checkout or uncommitted work | agents never see it: they work in git worktrees outside the repository, based on a snapshot built with a temporary index (index, files, stash untouched); applying the result is a separate, confirmed step whose patch holds only the agents' changes and is checked against the current files | `engineering/repo.py` |
| Destructive git (reset, clean, force-push, branch deletion) | agents may only run read-only git; XENIO's own git calls never reset, clean or force anything; branch cleanup touches only `xenio/<run>/...` branches it created | guard, `repo.py` |
| Pushing, PRs, deployments, production databases | agents cannot push, use `gh`, deploy (kubectl, terraform, docker, vercel...) or connect to databases; push and PR are separate actions that ask the user; migrations are written, never applied | guard, `service.py` |
| Installing packages / system software | blocked for agents (pip, npm/pnpm/yarn/bun, npx, brew, cargo/go/gem/composer installs); agents report needed dependencies instead | guard |
| Network access from agents | curl, wget, ssh, scp, rsync, nc... blocked in engineering sessions | guard |
| Secrets | the normal secret rules stay in force (.env, keys, credential stores blocked; secret env vars cannot be printed); tool output is redacted; agents' environment is Hermes' (XENIO's own keys are never in it); test runs get a minimal environment | policy, `verify.py` |
| Prompt injection from repository content | repository files, comments, docs and issues are data; prompts say so; the guard's injection flag still taints the turn; but above all, planted instructions cannot widen what the session may do (scope, tools and commands are enforced, not requested) | prompts, guard |
| Agents granting themselves more | the session registry is XENIO's file outside every worktree, and commands naming it are blocked; `delegate_task` is never available, so there is no unregistered second level of agents | guard |
| Repository config running programs | XENIO's git calls disable hooks, fsmonitor, pagers, external diff and textconv | `repo.git` |
| Tests running hostile code | XENIO's own test runs use `sandbox-exec` on macOS: writes only in the worktree and temp folders, no network but localhost, minimal environment; agents' terminal still uses the normal policy | `verify.py` |
| False success | a task's "done" is a claim: XENIO checks scope on the real diff, detects deleted/skipped/weakened tests, runs the tests itself against the baseline, and only then calls a run verified | `orchestrator._decide` |
| Invented review findings | reviewer findings whose file/line does not exist are marked as unverified in the report | `orchestrator._checked_findings` |
| Paid models | roles run on Hermes' main model unless a free model is pinned; paid pins are refused unless `allow_paid`; Hermes' `auxiliary.free_only` keeps side tasks free | `roles.CostPolicy` |
| An unguarded Hermes gateway | Hermes 0.22 runs ACP sessions inside its gateway. A gateway started outside XENIO has no guard: engineering refuses to start on it and `--health` fails with the fix | `hermes/guard.guard_live` |

## Enforcement path for one tool call

```
agent (Hermes session, cwd = task worktree)
  └─ xenio-guard pre_tool_call
       ├─ normal XENIO policy (paths, commands, secrets, web, ...)           -> may block / ask
       ├─ engineering rules for this session (registry: role, tools, root, scope)
       │    tool in role? path in worktree and scope? read-only role? git/install/network/deploy/db?   -> may block
       └─ prompt-injection escalation (outside content seen this turn)       -> may ask
  └─ write_file / patch: Hermes asks the ACP client (the gateway has no accept-edits mode)
       └─ XENIO answers with the engineering rules only: in scope -> yes, otherwise -> no
  └─ commands that need a yes -> XENIO's PermissionGate, labelled "(engineering task <id>, <role>)"
```

## Deterministic security checks

After the tasks, XENIO scans the run's diff (added lines only) and changed file names: secrets, SQL built from
strings, shell commands from input, eval/exec, pickle/marshal/unsafe YAML, TLS verification off, debug mode, raw
HTML injection, path traversal from request input, world-writable permissions, secrets in logs, CORS wildcards, weak
hashes, disabled auth checks, prompt-construction from user input; plus dependency manifests, CI pipelines, Docker
files, `.env` files, migrations and auth code as risky files. Installed scanners (bandit) are used when present.
The security reviewer role then reviews the diff with evidence requirements. **A clean result is not proof that a
change is secure**; the report says so.

## Known limits

* The agents' terminal is not sandboxed (Hermes' local backend): the guard's command rules are the protection.
  Commands that need a yes (for example `python script.py`, `npm run build`) are asked about.
* Read-only commands can read files outside the worktree that are not secrets (for example `cat ~/notes.txt`);
  secrets stay blocked.
* `sandbox-exec` is deprecated by Apple but present and working on macOS 26; elsewhere tests run unsandboxed.
* The gateway check reads `~/.hermes/gateway.lock` and the process environment; it depends on Hermes keeping that
  lock format.
