# V6 baseline: audit before building the engineering team

Recorded 2026-10-17 on the MacBook Air (Apple M5, 16 GB, macOS 26.6) after moving XENIO from Windows. Everything below
was inspected or measured on this machine; nothing is copied from older documents without checking.

## 1. Repository and git state

| | |
|---|---|
| Location | `~/Desktop/Xenio/Xenio Personal Ai` |
| History | The `.git` folder copied from Windows held **no commits**. Baseline commit `5b6abef` records the code exactly as it arrived; work continues on branch `macos-port` |
| Line endings | 4 files still had CRLF; normalized in a separate commit (`1f97643`) so diffs stay readable |
| Remote | none (no GitHub repository for XENIO exists under `tanishqnarula12`) |
| Secrets | `.env` and `user_required_steps/` are git-ignored; tracked files contain only placeholder and test keys (scanned) |

## 2. Test results

| State | Result |
|---|---|
| Reported historically (Windows) | 788 passing (V5.5 + "today's changes"); V1-V5 regression not rerun |
| This Mac, untouched import (`5b6abef`) | **746 passed, 36 failed, 4 errors, 2 skipped** — every failure platform-related: Windows paths in the safety policy, Recycle Bin, Win32-only desktop control, a `/private/var` symlink bug in the path policy |
| After the macOS port and fast path (`7f3beb7`) | **847 passed, 3 skipped, 0 failed** (the 3 skips are Windows-only GDI/path tests). This run *is* the V1-V5.5 regression suite: voice, Hermes bridge, safety policy, tools, web, personal integrations, memory, vision, desktop |

## 3. XENIO architecture (as found)

XENIO is the interface, voice, safety and orchestration layer; Hermes is the only reasoning engine. XENIO talks to
`hermes acp` over the Agent Client Protocol (`app/hermes/adapter.py`), holds one conversation session, answers Hermes'
permission requests through `PermissionGate` (voice or console yes/no, "confirm" for high risk), and enforces its
policy *inside* Hermes through the `xenio-guard` plugin (`pre_tool_call` allow/ask/block, `transform_tool_result`
redaction and source tracking, `pre_llm_call` notes). The policy (`app/safety/policy.py`) is standard-library only and
covers paths, shell commands, git/gh, web, browser, memory, personal tools, vision and desktop actions. V4 adds
projects and tasks (`personal.db`), V5 screen vision, V5.5 desktop control and multilingual intents. See
[ARCHITECTURE.md](ARCHITECTURE.md).

Relevant existing pieces for V6:

* **Git/GitHub**: no git tool; git runs through Hermes' `terminal`, classified by `classify_command`/`classify_gh`:
  status/log/diff READ; add/commit/branch SAFE_WRITE; push/merge/checkout CONFIRM; force-push, `reset --hard`,
  `branch -D` HIGH_RISK; `gh pr create` CONFIRM; `gh auth token` blocked.
* **Terminal/filesystem permissions**: path categories (secret/system/app_data/user/outside); writes in the user's
  folders SAFE_WRITE, except executables/scripts (`.exe`, `.js`, `.ps1`, `.command`...), `.git` internals and
  recursive edits, which need a yes (a problem for coding agents writing JavaScript; V6 adds a task-scope rule). Unknown commands fail closed to CONFIRM_REQUIRED.
* **Task/project persistence**: `Projects` (name, GitHub repository, local path, focus, notes; ambiguous names raise
  "ask the user") and `TaskList` in `personal.db`.
* **Memory and prompt injection**: memory stays in Hermes (`MEMORY.md`/`USER.md`); the plugin blocks secrets in memory
  and drops low-value entries. Outside content (web pages, emails, issues, screen text, control names) is data:
  instruction-like text is flagged and taints the turn, after which only plain reading runs without a yes.

## 4. Hermes: installed version and configuration

| | |
|---|---|
| Version | Hermes Agent **v0.22.2+1128** (2026.9.24), git install in `~/.hermes/hermes-agent`, bundled Python 3.14.7 |
| Credentials present | OpenRouter API key (`~/.hermes/.env`); GitHub Copilot (borrowed from `gh`). **Not signed in**: openai-codex (the Windows main model), Anthropic, Nous |
| Model (set during this setup) | main `thinkingmachines/inkling:free` (sees images; only reachable through an agentic harness such as Hermes), reasoning effort `low`; fallbacks `nvidia/nemotron-3.5-super-vl:free`, `google/gemma-4-31b-it:free`, `nvidia/nemotron-3-super-120b-a12b:free`, `nvidia/nemotron-3-ultra-550b-a55b:free`; `auxiliary.free_only: true` |
| Budget | User decision: **no paid credits**. OpenRouter key spending cap $0.50; free-model limit 1,000 requests/day (account has purchased credits). Usage today: $0.00 |
| XENIO integration | `xenio-guard` 0.7 enabled; `platform_toolsets.acp: [hermes-acp, xenio]`; Tool Search scoped off for XENIO |
| Measured | warm XENIO turn: 4.0 s greeting, 6.6 s arithmetic, 20.9 s one tool task; prompt ~12.7k tokens, cached (`cachedReadTokens`) |

## 5. Hermes delegation and multi-agent capabilities (verified in code and docs of the installed version)

| Capability | `delegate_task` (in-process subagents) | Kanban (durable board) | ACP sessions (XENIO's connection) |
|---|---|---|---|
| Subagent delegation | yes, isolated fresh conversation per child | yes, a full worker *process* per card | one Hermes agent per session |
| Parallel tasks | batch of up to `max_concurrent_children` (10) | dispatcher spawns many workers | **4 concurrent turns per Hermes process** (`ThreadPoolExecutor(max_workers=4)` in `acp_adapter/server.py`) |
| Task status tracking | `action=list` (running children), live transcripts in `~/.hermes/cache/delegation/live/` | statuses `triage, todo, ready, running, blocked, review, done, archived` in `kanban.db` | XENIO tracks it |
| Result collection | summary + `status` (`completed/failed/timeout/interrupted/stalled/unknown`); `output_schema` validation with one correction turn | `kanban_complete(summary, metadata)` | final text + tool-call statuses + `usage` per turn |
| Dependencies | none (parent dispatches sequentially) | `parents` links; `todo -> ready` when parents are done | XENIO |
| Model per task | **no**: one global `delegation.model`/`provider` | yes (`--model`, per profile) | **yes: `session/set_model` per session** |
| Tool restrictions per agent | **no**: children inherit the parent's toolsets; fixed blocks (`delegate_task` for leaves, `clarify`, `memory`, `send_message`, `cronjob`) | per-profile toolsets | via `xenio-guard` (see section 7) |
| Timeouts | none by wall clock by default; inactivity stall monitor (450 s idle / 1,200 s in a tool); optional `child_timeout_seconds` | heartbeats; stale claims reclaimed after 4 h | ACP turn timeout (XENIO) + `session/cancel` |
| Cancellation | `action=stop`, ACP cancel ends background children (interrupted result with partial output) | block / archive; worker reaping | `session/cancel` per session |
| Nested delegation | `role=orchestrator`, bounded by `max_spawn_depth` (default 1 = flat) | orchestrator profiles create child cards | n/a |
| Shared or isolated context | isolated; only goal/context + repo context files (AGENTS.md, CLAUDE.md...) | comment thread + parent handoffs | isolated per session; `cwd` per session |
| Repository isolation | `delegation.worktree_isolation: true` (worktree per child under `<repo>/.worktrees/`) | `worktree` workspaces | `cwd` = any directory XENIO prepares |
| Human approvals | children's tool calls pass the same `pre_tool_call` hook; approvals reach the ACP client | **workers are unattended**: no ACP client, `xenio-guard` only active if `XENIO_GUARD=1` is in the worker's environment; anything needing a yes is denied | approvals reach XENIO's `PermissionGate` |
| Review | `/review` command (CLI) | `review_dispatch: true` with the bundled `sdlc-review` skill | — |

Also present: skills `codebase-inspection`, `requesting-code-review`, `simplify-code`, `sdlc-review`, `claude-code`,
`codex`, `opencode`; Hermes CLI `worktree: true` for whole sessions; the gateway was stopped (kanban's dispatcher runs
inside it).

## 6. What this means for V6 (decisions)

1. **Do not run engineering work through kanban workers.** They would be unattended processes outside XENIO's
   permission system (the guard is off unless injected, and no one can be asked). That would weaken the existing
   approval model, which the V6 rules forbid. Kanban stays available to the user; V6 does not depend on it.
2. **Do not rely on `delegate_task` for roles.** It cannot give a child its own model or a restricted tool set, and
   its children would not be identifiable as "the test engineer" by XENIO's guard. V6 sessions are not allowed to call
   it (no hidden second level of agents).
3. **Use Hermes ACP sessions as the agents.** Each task runs as one Hermes agent turn in its own ACP session on a
   dedicated Hermes process: `cwd` = the task's isolated working directory, model set per role with
   `session/set_model` (only models allowed by the cost policy), up to 4 in parallel. Hermes keeps all reasoning,
   tools, skills and fallback; every tool call still passes `xenio-guard`; every confirmation still reaches the user
   through `PermissionGate`.
4. **XENIO adds only what Hermes does not have in a usable form here**: the engineering workflow (plan, task graph with
   dependencies and file ownership, scheduling), repository preflight and isolation (git worktrees from a snapshot that
   includes the user's uncommitted work, never touching the user's checkout), deterministic verification (XENIO runs
   the tests itself and parses the results), deterministic security checks on the diff, conflict detection and a
   controlled integration step, honest reporting, status and cancellation.
5. **Role restrictions are enforced, not requested**: the guard learns each engineering session's role, task scope and
   allowed paths from a registry XENIO writes, and blocks tools, write paths and commands outside them.

## 7. Proposed architecture

```
"Xenio, add password reset to my CRM"
   │ (Hermes understands the request and calls xenio_engineering, or the user runs --engineer)
   ▼
XENIO EngineeringService (app/engineering/, in XENIO's process)
   ├─ resolve project → repository (V4 Projects; ambiguous → ask)
   ├─ preflight: git root, branch, HEAD, dirty/untracked/staged files, stack, test commands
   ├─ isolation: run base = HEAD + snapshot of the user's tracked changes (git stash create; working tree untouched)
   │              one git worktree per writing task, outside the repository; non-git → temporary copy
   ├─ plan: planner role (read-only Hermes session) → JSON plan, validated → TaskGraph
   │        small requests → single implementer task (no team overhead)
   ├─ schedule: ready tasks whose dependencies succeeded; writers in parallel only with disjoint file scopes; ≤4
   │   └─ each task → Hermes ACP session (cwd=worktree, model=role model) + guard registry entry (role, scope)
   ├─ collect: final structured result + tool activity + usage; bounded retries; timeout/cancel
   ├─ verify (XENIO itself): changed files vs allowed paths, tests deleted/expectations changed, run test commands,
   │   parse results (passed / failed / skipped / not run / blocked by missing dependency / timeout)
   ├─ integrate: merge task branches in an integration worktree; conflicts → integration task or report
   ├─ review: deterministic security scan of the diff + security reviewer and code reviewer roles (read-only)
   └─ report: evidence (diff stats, tests, findings, approvals still needed); never pushes, merges or deploys
             unless the user approves that separate step through PermissionGate
```

Interfaces: a `xenio_engineering` tool in the plugin (start / status / plan / cancel / report / apply) that talks to
XENIO's process over a local socket, the `--engineer` CLI, and notifications for milestones (spoken only when idle).

## 8. Known limits found during the audit

* Free models are the only allowed models: quality and rate limits (1,000 requests/day, occasional 503/429) bound how
  much a team run can do. Every role model is checked against the cost policy; there is no paid fallback.
* At most 4 agent turns run at once per Hermes process (Hermes' executor size).
* A Hermes restart does not resume a running task; XENIO marks it failed/unknown and keeps completed work.
* The accessibility permission is still missing on this Mac (desktop typing/clicking); not needed for V6.
