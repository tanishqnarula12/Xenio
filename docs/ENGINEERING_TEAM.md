# XENIO's engineering team (V6)

Ask XENIO for a feature, a fix, tests, a review or an explanation of a repository, and it works like a small
engineering team: it inspects the project, plans, hands tasks to specialist agents (planner, frontend, backend,
database, tester, security reviewer, code reviewer, documentation), runs them in isolated copies of your repository,
tests the result itself, reviews it, and reports with evidence. You stay in control: nothing leaves the isolated
copies (your files, your branches, a remote, production) without your confirmation.

* How it is built and why: this page. Audit and decisions: [V6_BASELINE.md](V6_BASELINE.md).
* Roles and their enforced capabilities: [AGENT_ROLES.md](AGENT_ROLES.md).
* What you can ask, run lifecycle, statuses, cancel, apply/push/PR: [ENGINEERING_WORKFLOWS.md](ENGINEERING_WORKFLOWS.md).
* Threats and controls: [ENGINEERING_SECURITY.md](ENGINEERING_SECURITY.md).

## Architecture

```
you ──voice/text──► XENIO ──ACP──► Hermes (chat) ──calls──► xenio_engineering ──local socket + token──┐
                                                                                                      ▼
                                         XENIO EngineeringService  (app/engineering/service.py)
                                           project → repository · background runs · status · cancel · apply/push/PR
                                                              │
                                         EngineeringOrchestrator (orchestrator.py)
            plan_project ─► create_tasks ─► execute_plan ─► review_results ─► generate_report
               │               │               │                 │
         repo.py: inspect,  graph.py:     agents.py: one Hermes  verify.py: tests run by XENIO (sandboxed)
         snapshot, worktrees roles,deps,   ACP session per task  security.py: deterministic scan
         (your checkout       scopes,      (cwd = worktree,      reviewers: security + code (read-only)
          untouched)          parallelism  model per role)
                                                 │
                                   Hermes gateway (sessions, tools, models, free fallback chain)
                                                 │
                                   xenio-guard: every tool call → XENIO policy + engineering rules for the
                                   session's role, worktree and file scope (registry in XENIO's data folder)
```

**Reused from Hermes** (nothing re-implemented): the agent loop, tools (files, search, terminal, process control),
model access and fallback chain, ACP sessions with a working directory and a per-session model, cancellation,
concurrency (up to 4 agent turns per Hermes process), the plugin hooks XENIO's guard uses.

**Added by XENIO** (what Hermes does not offer in a usable, safe form for this): the engineering workflow and task
graph, roles with *enforced* tool and file scopes, repository preflight and isolation that never touches the user's
checkout, deterministic verification and security checks, evidence-based outcomes, reports, and the boundary where
your confirmation is required. Why not Hermes' `delegate_task` or kanban: see [V6_BASELINE.md](V6_BASELINE.md#6-what-this-means-for-v6-decisions)
(no per-child model or tool limits; kanban workers run unattended outside XENIO's guard).

## Configuration

`.env` (all optional):

| Setting | Default | Effect |
|---|---|---|
| `XENIO_ENGINEERING` | `true` | `false` removes the engineering team |
| `XENIO_ENGINEERING_MAX_PARALLEL` | `3` | agents at once (1-4; Hermes runs at most 4 turns per process) |
| `XENIO_ENGINEERING_AUTO_START` | `false` | `true`: team plans start without waiting for your "go ahead" |
| `XENIO_ENGINEERING_TEST_TIMEOUT` | `600` | seconds for one test command run by XENIO |
| `XENIO_ENGINEERING_TASK_TIMEOUT` | `1800` | seconds for one agent turn |
| `XENIO_ENGINEERING_SANDBOX` | `true` | macOS: XENIO's own test runs under `sandbox-exec` |

Roles and per-role models: `~/.local/share/xenio/engineering_roles.toml` ([AGENT_ROLES.md](AGENT_ROLES.md)).
Runs live in `~/Xenio/engineering/<run>/` (worktrees, `report.md`, `changes.patch`, `run.json`); branches are
`xenio/<run>/...` in your repository.

## Model routing and cost

Every role runs on Hermes' main model and its fallback chain unless a model is pinned for that role. In this setup
those are free OpenRouter models (`thinkingmachines/inkling:free` first), and Hermes' `auxiliary.free_only` keeps
side tasks free. A pinned paid model is refused (never swapped) unless you set `allow_paid`. A model Hermes cannot
serve fails the task with Hermes' reason. Per turn XENIO records the role, model, time, stop reason and the token
usage Hermes reports; money cost is reported as unknown unless a provider reports it. Small changes skip the planner
and the team (one engineer), which keeps model calls low; the free tier allows 1,000 requests a day on this account.

## Known limitations

* Free models are slower and less reliable than paid ones: a team run takes minutes, and a model can produce an
  unusable plan or result. XENIO then fails the task honestly (bounded retries) instead of claiming success.
* Hermes 0.22 runs ACP sessions inside its gateway; if a gateway started outside XENIO is running, the engineering
  team refuses to start (`hermes gateway stop`, then try again).
* The gateway asks before every file edit (no accept-edits mode for ACP); XENIO answers those for the task's own
  files, which costs a round trip per edit.
* Agents' terminal commands run without an OS sandbox (only XENIO's own test runs are sandboxed); the guard's
  command rules protect, and commands that need a yes ask you.
* Dependency installation is never done by agents; a change that needs a new package reports it, and you install it.
* Non-git folders are worked on in a private copy; their result is a patch, not an applied change.
* A run does not survive restarting XENIO or Hermes (completed work stays on its branches and in the report).
