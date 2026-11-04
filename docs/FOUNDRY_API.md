# The Foundry: API

All routes are under `/api/v1`, on XENIO's local UI server, and need its session token. Coming through this signed-in session is what makes a call yours: agents have no route here.

| Method | Path | Returns |
|---|---|---|
| GET | `/foundry` | workflows, agents, skills, model seats |
| GET | `/foundry/workflows/<id>` | the full state: team, graph, messages, decisions, interventions, loop, events |
| POST | `/foundry/<command>` | the result of a command, with its fields in the JSON body |

Errors are `{"error": "..."}` with a meaningful status: 403 only you can do that, 404 no such thing, 409 not in that state, 422 bad request, 503 switched off or no model key.

## Commands

### Work

| Command | Fields | Effect |
|---|---|---|
| `start` | `goal`, optional `path` or `project`, `autonomy` (`supervisor` by default, or `careful`), `constraints` | Starts a workflow. No path means a new project of XENIO's own |
| `status` | optional `workflow` | Short enough to say aloud |
| `state`, `list`, `overview` | | Read |
| `pause`, `resume` | `workflow` | Nothing new starts while paused |
| `stop` | `workflow`, optional `reason` | Everything, now |
| `stop_all` | | Every running workflow |
| `message` | `text`, optional `workflow`, `to` (agent id, or the whole team) | Recorded and delivered at once. The effect on the plan is reported |
| `approve`, `reject` | `workflow`, optional `node`, `note` | A waiting gate |
| `decide` | `workflow`, `decision`, `choice` (a label) or `text` | Settles an escalated decision |
| `discuss` | `workflow`, `decision`, `text` | Your words go to everyone and bind the judge |
| `constraints` | `workflow`, `constraints` | Replaces what you require |
| `require_evidence` | `workflow`, `required` | Decisions need evidence XENIO can check |
| `priority` | `workflow`, `node`, `priority` | Between -10 and 10 |
| `reassign` | `workflow`, `node`, `agent` | Unstarted tasks only, to someone able to do them |
| `cancel_task` | `workflow`, `node` | What depends on it will not run |
| `review` | `workflow`, `agent`, `text` | That agent reviews and answers you |
| `extend_budget` | `workflow` | Doubles it |
| `change` | `workflow`, `text` | A change to finished work: the same team plans it, builds it, tests it and runs it again. `message` to a finished workflow does the same |
| `delete` | `workflow`, optional `with_files` | Removes finished work. Its project stays unless `with_files` is set. Refused while it is running |
| `preview_start`, `preview_stop` | `workflow` | Runs or stops what it built. Failure comes back with its reason, and `state.preview` keeps it |

### Agents

`agent_create` (`agent`), `agent_update` (`agent`, `changes`, `why`), `agent_enable`, `agent_disable`, `agent_archive`, `agent_restore`, `agent_promote`, `agent_delete` (each takes `agent`). Built-in agents can be switched off, not deleted.

### Skills

| Command | Fields | Effect |
|---|---|---|
| `skill_request` | `request` (your words, a link, or `owner/repo`), optional `agents` | Fetches and reads it. Nothing runs. A repository of many skills comes back with `kind: "collection"` and `contains` |
| `skill_install` | `skill`, optional `network`, `agents`, `picks` (folders from `contains`), `accept_risk` | Installs in the sandbox, tests, registers. `accept_risk` is only honoured from you, and forces the network off |
| `skill_everyone` | `skill`, `everyone` | Every agent whose job it suits, now and in the future, or only those named |
| `skill_work` | `skill`, `work` (any of `ui`, `build`, `test`, `security`, `docs`, `review`, `plan`; empty means any job) | Which jobs it is for |
| `skill_reject` | `skill` | Removes what was fetched |
| `skill_assign`, `skill_unassign` | `skill`, `agents` | Who has it |
| `skill_enable`, `skill_disable` | `skill` | At once, no restart |
| `skill_update` | `skill` | Fetches and reads a new version. The old one keeps working until you approve |
| `skill_pin` | `skill`, `pinned` | No updates while pinned |
| `skill_rollback` | `skill`, optional `version` | Only if the old files are unchanged |
| `skill_uninstall` | `skill` | Removes it from everyone |
| `skill_history`, `skill_search` | `skill` / `text` | Read |

## Tools agents have

| Tool | What it does |
|---|---|
| `xenio_team` | `kind=ask` or `tell` a teammate. `kind=hire`, only where offered, with `name`, `text` and `paths` |
| `xenio_skill` | With `skill`: read it. With `read`, `script` or `args`: read a file or run it, sandboxed. With `add_to_project`: copy it to `.agents/skills/` |
| `xenio_install` | Plain package names, in a project XENIO made |

## Agents talking while they work

Native agents have one more tool, `xenio_team`: `kind=ask` gets an answer from one teammate now, and `kind=tell` passes something on. Who is speaking comes from the session, not from the model. Whoever answers can only think, with no tools. Each agent has at most 8 messages per task. All of it appears in the conversation as ordinary messages.

## Chat tool

`xenio_foundry` takes `command` and the same fields. It reaches the service over the engineering team's local socket (a token, and a check of which process is calling). The guard classifies each call first (`policy.classify_foundry_tool`):

* reading: allowed
* starting, redirecting, stopping, requesting a skill: runs at once
* approving, deciding, installing, assigning, changing an agent: always asks you

## Events

Every change is an event: `id`, `seq` (ordered), `type`, `workflow`, `agent`, `task`, `at`, `data`. They are kept in `foundry.db`, with secrets redacted before storing, and published on the UI stream as `foundry.event`. The page then reads the real state again, at most about once a second.

Types: `workflow`, `workflow.started`, `node`, `message`, `decision`, `decision.opened`, `decision.absent`, `team.assembled`, `agent.created`, `agent.reused`, `agent.promoted`, `agent.permission_refused`, `agent.turn`, `agent.tool`, `dependencies`, `loop.iteration`, `loop.finished`, `intervention`, `engineering`, `skill`, `skill.used`, `skill.refused`.

## Messages

`id`, `seq`, `workflow`, `task`, `sender`, `recipient` (agent, `team` or `you`), `type`, `content`, `correlation`, `reply_to`, `evidence`, `artifacts`, `model`, `at`.

Types: `INFORMATION`, `PROPOSAL`, `QUESTION`, `CHALLENGE`, `COUNTERARGUMENT`, `REVIEW`, `DELEGATION`, `RESULT`, `BLOCKER`, `DECISION`, `ESCALATION`.

## Graph nodes

`id`, `type`, `title`, `status`, `agent`, `deps`, `inputs`, `outputs`, `when`, `always`, `retries`, `max_retries`, `budget`, `error`, `started`, `finished`, `priority`, `writes`, `payload`, `history`.

Types: `goal`, `planning`, `agent`, `task`, `communication`, `review`, `testing`, `decision`, `approval`, `loop`, `completion`. Statuses: `pending`, `running`, `waiting`, `done`, `failed`, `skipped`, `blocked`, `cancelled`.
