# The harness plan

Where XENIO's agent harness goes next, in order. Written 2026-11-19; nothing here is built yet unless it says so.

1. **A single agent as good as it can be** (the foundation).
2. **Many agents on one project, without conflicts** (built on 1).
3. **The VS Code extension** (XENIO working in your editor, on 1 and 2).
4. **Measuring all of it**, from the start.

The aim is not to compete with Claude. It is an environment that gets the most out of whatever model XENIO runs on. The
harness is model-agnostic: when a better model arrives, on Nabh or elsewhere, everything gets better at once.

What real jobs already showed: the War Room's early failures were mostly the harness, not the models (hand-overs between
agents, re-reading the same files, a fix round rewriting a whole file, reviews demanding logins nobody asked for). Once
those were fixed, a full-stack habit tracker finished verified in 3 minutes and 62,700 tokens on Nabh models.

---

## 1. A single agent as good as it can be

Every agent, alone or in a team, gets all of this.

### Precise tools
| Tool | What it does | Today |
|---|---|---|
| `read` | a file, or a range of lines; an unchanged file is never sent twice | partly (the file cache exists) |
| `edit` | replaces an exact piece of text that must match once; otherwise nothing changes | partly (fix rounds use EDIT blocks) |
| `write` | a new file | yes |
| `glob`, `grep` | find files and text | yes |
| `bash` | a command, through the policy and the macOS sandbox | yes |
| `todo` | a visible task list the agent keeps up to date, shown live on its seat on the Floor | **yes** (2026-11-19) |
| `ask` | one question to you, then it waits | yes (`ask_ceo`) |
| sub-agents | a helper for a wide search or a side task, returning only its conclusion | no |

### Checking its own work
After editing, the agent runs the type check, lint and tests, reads the failures and fixes them before it says "done".
The War Room's test → review → fix loop does this today; every agent should do it within its own turn too.

### Memory and context
- `XENIO.md` in each project: its rules and commands, read at the start of every session.
- Long sessions are summarised instead of running out of room.
- What it learned about a project carries over to the next session.

### Safety (already XENIO's)
Every action through the policy; code it did not write runs in the sandbox, with no network unless allowed; approvals
for risky actions; secrets masked; nothing outside the project's folder.

### Order
1. ~~`todo` tool and its display.~~ Done 2026-11-19.
2. Self-checking within a turn (type check, lint, tests) before "done".
3. `XENIO.md` project memory.
4. Summarising long sessions.
5. Sub-agents for searches.

---

## 2. Many agents on one project, without conflicts

Today a small or medium job goes to one builder, because hand-overs failed on real jobs. Many agents pay off only when
the work splits along clean seams. These make that work.

### What exists
Each agent in its own git worktree; a task graph with dependencies (up to 4 at once); `allowed_paths` per task, enforced;
a message bus; the test → review → fix loop after merging.

### What to add

**Contracts first, then parallel work.** Before anyone builds, an architect step writes and freezes the shared parts: the
API (routes, request and response shapes), the data model, and the file layout (who owns which folder). Backend,
frontend and tests then build against the same contract at the same time. Most conflicts come from two agents inventing
the same thing differently; a contract removes that.

**Strict ownership.** Every file has exactly one owner per round, decided by the plan and enforced. Shared files
(`package.json`, `requirements.txt`, routes, config) get one owner; others send a request ("I need `bcrypt`", "add
`/api/habits`") and the owner applies it, as XENIO already does for dependencies. Two tasks that need the same file run
one after the other, never in parallel.

**Merge continuously.** A finished task merges into the integration branch at once, and its tests run. Agents still
working pull the latest before their next step. A conflict is fixed straight away by the task that caused it.

**A lead and a shared board.** Everyone can read the plan, the contracts, who is doing what, what is done and what was
decided. The lead answers "who owns this?", re-plans when something is blocked, and stops agents drifting apart.

**Checked twice.** Each agent proves its own part against the contract; after merging, everything runs together.
Contract tests catch the classic failure: backend and frontend each "working", but disagreeing on a field name.

### One agent or several
| Job | Setup |
|---|---|
| a page, a script, a small fix | one agent |
| a medium app with clear parts | a contract, then 2–3 agents in parallel |
| a large product | a lead, an architect, several builders, a tester, reviewers |

The planner decides from the job's seams, not its size alone. Without clean seams it stays with one agent: forcing
parallel work on tangled code makes it worse.

### Order
1. The contract step (API, data model, ownership map, frozen).
2. Requests for shared files, applied by their owner.
3. Continuous merge: merge on finish, refresh before the next step, the cause fixes the conflict.
4. The lead and the shared board.
5. Contract tests at integration.

---

## 3. The VS Code extension

XENIO's Python backend stays the brain (the agent loop, models, policy, sandbox, memory, tests). The extension is its
hands and eyes in the editor: a chat panel, and tools only an editor has, sent over XENIO's local API (`127.0.0.1`, a
per-run key from the Keychain, events as a live stream).

### What the editor adds
- The Problems panel: every error and warning, live.
- Language-server tools: go to definition, find references, rename a symbol safely.
- Your open files, cursor and selection ("fix this" means what you selected).
- Edits through VS Code's own edit system: Cmd+Z undoes them, a diff per change with Accept / Reject.
- Modes: ask before each edit, auto-accept, or plan only. A checkpoint before each turn, and rewind.
- Approvals as buttons in VS Code, and on the orb.

### Two ways to work
- **Pair:** XENIO edits your open project live.
- **Team:** send it to the War Room; the team builds it in an isolated copy and it comes back as one diff to review.

### Layout
```
vscode/                     the extension (TypeScript)
  package.json              sidebar, commands, settings
  src/extension.ts          start-up, connects to XENIO
  src/client.ts             local API + live event stream
  src/panel/                chat panel (reuses XENIO's React parts and look)
  src/editor-tools.ts       diagnostics, language server, selection, edits, diffs
app/editor/                 backend: an "editor session" per workspace
```

### Order
1. A chat panel connected to XENIO, with the workspace attached.
2. Precise tools and diffs through VS Code (Accept / Reject, undo).
3. Editor tools: diagnostics, language server, selection.
4. Self-checking after edits, checkpoints and rewind.
5. Modes and approvals inside VS Code.
6. Team mode: War Room jobs from the editor, back as a diff.
7. Package and publish (VS Code Marketplace and Open VSX), with XENIO's logo.

---

## 4. Measuring it

A fixed set of real tasks, run before and after each change, so "better" is a number:

- XENIO's own past failures: the habit tracker, the Django app, the fix round that rewrote `main.py`, the one-page site.
- Everyday work: add a feature, fix a failing test, rename across files, update dependencies.
- For multi-agent: the same tasks with one agent and with a team.

For each run: did it finish verified, how long it took, how many tokens, how many fix rounds. Keep a change only when the
numbers say it helped; keep a team only where it beats one agent.
