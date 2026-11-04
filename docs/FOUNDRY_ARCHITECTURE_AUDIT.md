# The Foundry: architecture audit (Phase 0)

The Foundry turns XENIO's engineering team into a persistent organization of specialist agents that can be created, reused, argue with evidence, work through a graph under bounded loops, stay under your control while they run, and gain new skills safely.

This audit records what exists, what is reused, what is missing and how it gets built. Everything below was read from the code, not assumed.

## What exists today

XENIO already has a real, tested engineering team (`app/engineering/`, 2,913 lines, 110 tests, 12 live acceptance runs in `ENGINEERING_TEAM.md`).

```
you -> XENIO -> Hermes (chat) -> xenio_engineering -> local socket + token -> EngineeringService
                                                                                   |
                                                                       EngineeringOrchestrator
              plan_project -> create_tasks -> execute_plan -> review_results -> generate_report
                  |                |               |                |
            repo.py worktrees   graph.py        agents.py       verify.py (tests, sandboxed)
                                TaskGraph       one Hermes      security.py (deterministic scan)
                                                ACP session     reviewers (read-only)
                                                per task
                                                   |
                       xenio-guard: every tool call -> policy + role rules for that session
```

| Piece | File | What it gives The Foundry |
|---|---|---|
| Enforced roles | `roles.py` | capabilities mapped to Hermes tools and terminal modes; `FORBIDDEN_TOOLS`; cost policy |
| Agent sessions | `agents.py` | one ACP session per task, per-session model, `SessionRegistry` read by the guard |
| Enforcement | `hermes_plugin/xenio-guard`, `policy.classify_engineering` | a reviewer that tries to write is blocked, not merely asked not to |
| Isolation | `repo.py` | snapshot, worktrees, per-task branches, merge, conflict handling; your checkout untouched |
| Verification | `verify.py`, `security.py` | XENIO runs tests itself, sandboxed; deterministic scan; test tampering detection |
| Task graph | `graph.py` | dependencies, cycle check, scope overlap, parallelism |
| Approvals | `app/safety/permissions.py`, `app/ui/approvals.py` | the one gate everything high-risk goes through |
| Events | `app/ui/events.py` | sequence-numbered events, replay with `?since=`, gap detection |
| Mission Control | `ui/src/pages/Orchestration.tsx`, `Agents.tsx`, `Tools.tsx` | runs, tasks, graph, roles, skill switches |
| Models | `app/integrations/nabh.py`, Hermes | Nabh direct (streaming, limits handled), Hermes main model and fallback chain |
| Hermes skills | `hermes skills` CLI | a hub with search, inspect and audit for Markdown skills, and the Skills page |

Reached through `EngineeringService.dispatch`, from the `xenio_engineering` tool over a token-protected local socket.

## What is reused, unchanged

The guard, roles and capabilities, worktrees, verification, test tampering checks, the approval gate, event hub and authentication. The live tests that proved them (prompt injection, approval boundary, cancel, dirty tree) stay valid because the same code still runs.

**The rule that makes this safe:** every Foundry agent is compiled to an enforced `Role` before it runs. A new specialist gets only capabilities from the existing table, chosen by XENIO from what the task needs. A model never chooses its own tools, and no agent can change its own permissions.

## What is missing

| Asked for | Today | Gap |
|---|---|---|
| Agent lifecycle | 8 fixed roles in code and one TOML | no registry, versions, temporary agents, promotion, history |
| Automatic specialists | none | no factory, no capability matching |
| Agent communication | none: agents only hand results to the orchestrator | no messages, no disagreement, no evidence trail |
| Decisions | `_decide` picks an outcome from test and review facts | no proposals, debate or dissent |
| Workflow graph | tasks only; plan, test and review are fixed code | no typed nodes, branches, approval nodes, or bounded loops as data |
| Improvement loop | one review pass, a task retried on failure | issues found in review are reported, not worked on |
| Intervention | cancel a whole run, approve a plan | no pause, no messages mid-run, no re-evaluation |
| Recovery | "a run does not survive restarting XENIO" | no durable node state |
| War Room | a run page with tasks and events | no conversations, decisions, or controls |
| Skills from GitHub, npm, npx | Hermes hub for Markdown skills only | no package inspection, no isolation, no per-agent assignment |

## Agent lifecycle, as designed

`DRAFT -> VALIDATED -> ENABLED -> (RUNNING) -> EVALUATED -> PROMOTED | ARCHIVED`

* **Built-in** agents are seeded from `DEFAULT_ROLES` and keep their ids.
* **Temporary** specialists belong to one workflow and are archived after it; a good evaluation promotes them.
* **Identity is separate from the model:** the definition never holds a provider key, and the model is a replaceable setting per agent.

## Skill lifecycle, as designed

`IDENTIFY -> FETCH (no code runs) -> INSPECT -> ASSESS -> ASK YOU -> INSTALL (sandboxed, scripts off) -> TEST -> REGISTER -> ENABLE per agent -> MONITOR -> UPDATE | ROLLBACK | DISABLE | UNINSTALL`

* `npx` is never run on an unknown package. It is treated as "an npm package with a command": fetched as a tarball, read, and only then run in the sandbox if you approve.
* Fetching uses `npm pack` and a shallow `git clone` with hooks off, so nothing from the package executes before you have seen the assessment.
* macOS `sandbox-exec` confines installs and runs: writes only inside the skill's own folder, home folder unreadable, network off by default and on only where approved. A package that cannot run under that is reported, not run another way.
* Hermes' own hub is left alone. A Markdown skill can be installed from GitHub the same way and attached to chosen agents.

## Security risks and what answers them

| Risk | Answer |
|---|---|
| A new agent gets too much power | capabilities picked by XENIO from declared needs; `FORBIDDEN_TOOLS` always removed; write access always scoped to a task |
| An agent edits its own permissions | the registry refuses permission fields from anything but you, server-side |
| Prompt injection through agent messages, documents or skills | every message and skill output is framed as data; a message can never grant approval |
| A team "agrees" its way past a rule | decisions cannot change policy; the guard and the gate run regardless of what a decision says |
| Agents approving their own work | the loop requires a different agent to review, and high-risk changes need you |
| Runaway loops and cost | loops are explicit nodes with hard limits on iterations, time and tool calls; graphs reject cycles |
| A malicious package | nothing runs before assessment; scripts off; sandboxed; high risk needs a real "confirm"; integrity recorded |
| Secrets in messages or events | redaction on write; agents' tools cannot read `.env` or token files, as today |
| Stale recovery repeats side effects | completed nodes are not repeated; an interrupted writer is checked before any retry |

**Known limit, now mostly closed:** under Hermes, agents' terminal commands have no OS sandbox (only XENIO's own test runs do). The native runtime added later runs every command in one. Agents pinned to a Hermes-only model keep the guard alone.

**Added after this audit:** the user directed that they will be CEO ("whatever i say i just have to give command") and that Hermes can go if it blocks anything. So The Foundry gained a supervisor mode, milestones with an independent coverage check, sandboxed package installs, and its own agent runtime. See [FOUNDRY.md](FOUNDRY.md).

## Many models, one protocol

Every agent can run on its own model, and ACP is what makes that uniform. `session/set_model` is per session, and Hermes offers 91 models over it on this Mac: OpenRouter free models (Inkling, Nemotron), the OpenAI Codex subscription and Anthropic.

| Route | Models | Used for | Who enforces limits |
|---|---|---|---|
| ACP session through Hermes | whatever Hermes lists, set per session | work with tools, and thinking on a pinned model | xenio-guard on every tool call, plus the cost policy |
| Nabh directly | GLM, Kimi, DeepSeek, Qwen, MiniMax, GPT-OSS | fast thinking with no tools at all | nothing to enforce: it has no tools |

A debate deliberately seats different models, so disagreement comes from different reasoning, not from one model arguing with itself. The model that actually answered is recorded on every message.

**Other ACP agents** (Gemini CLI, Codex, and so on) are not used for now. None is installed here, and they would not carry xenio-guard. `AgentSessions` already refuses a Hermes process without the guard, and that rule stays.

## Where it lives

```
app/foundry/
  store.py       foundry.db: versioned migrations, all tables, event log
  registry.py    agents: create, version, enable, archive, search, promote, performance
  models.py      model per agent over ACP or Nabh, fallback, usage and time budgets
  factory.py     goal -> capabilities -> reuse or create -> team
  messages.py    structured agent messages, persisted, framed as data
  decisions.py   independent proposals, debate, reasoned decision, dissent, escalation
  graph.py       typed nodes, dependencies, branches, bounded loops, cycle rejection, recovery
  loop.py        plan, implement, test, review, revise, re-test, accept or escalate
  engine.py      runs a workflow: parallel nodes, pause, resume, stop, interventions
  service.py     the one entry point for the UI and the chat
  skills/        sources, fetch, inspect, sandbox, registry
ui/src/pages/WarRoom.tsx
```

`app/engineering/` is reused, not moved: existing runs, the `xenio_engineering` tool and the Orchestration page keep working.

## Phases

| Phase | Delivers | Verified by |
|---|---|---|
| 0 | this audit, baseline tests | this page |
| 1 | store, registry, factory | unit tests, a real factory call |
| 2 | messages, decisions | unit tests, a real debate with real models |
| 3 | graph, loop | unit tests incl. recovery, a real loop on a disposable repository |
| 4 | interventions, live events | unit tests, a real pause and redirect |
| 5 | War Room | UI tests, viewed on a real run in a private browser |
| 6 | skill acquisition | unit tests with hostile fixtures, a real npm package and GitHub repository |
| 7 | skills on agents | unit tests, a real agent using a real skill |
| 8 | models, budgets, hardening | full suites, the acceptance scenarios |

## Baseline

Measured before any Foundry code:

* Python: 2264 passed, 3 skipped, **1 failed** (`tests/test_tutor.py`, streaming test), 109 s. Frontend: 237 passed, TypeScript 0 errors.
* The failure was a real bug, in the teacher added just before this work: in about 1 run of 4, every streamed word reached the chat after the reply had already ended. It was fixed by delivering whatever is left before the reply returns, with a regression test, and passed 20 runs of 20 afterwards.

The count includes the tutor and quick plan tests, which were written before The Foundry began.
