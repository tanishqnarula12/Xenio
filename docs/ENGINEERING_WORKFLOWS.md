# Engineering workflows (V6)

How a request becomes a result, what you can ask, and what every status means.

## Asking

Through XENIO (voice or text), in your own words; Hermes calls the `xenio_engineering` tool:

* "Inspect my project and explain its architecture." -> **explain** (read-only answer)
* "Plan how to add a settings page to this project." -> **plan** (plan only, nothing implemented)
* "Add password reset to my CRM." / "Find and fix this bug." / "Build a responsive dashboard." -> **change**
* "Write tests for this module." -> **change** with the test engineer
* "Review my changes for security problems." -> **review** (your uncommitted changes, nothing changed)
* "Run the test suite and explain the failures." -> **test** (XENIO runs the tests, a reviewer explains)
* "What are you working on?" / "Show the current plan." / "Which agents are running?" / "What has failed?" -> status
* "Go ahead." -> starts a planned team run · "Cancel the engineering task." -> cancel · "Summarize the completed
  work." -> report
* "Apply it." / "Push it." / "Open a pull request." -> each asks for your confirmation first

From the terminal:

```bash
.venv/bin/python -m app.main --engineer "Add password reset to the CRM" --repo ~/code/crm
.venv/bin/python -m app.main --engineer "Explain the architecture" --project CRM --mode explain
```

**Which repository**: a path you give, else the project you name (XENIO's projects, V4: its recorded local folder),
else your current project. Several matching projects -> XENIO asks which one. Your current project is never changed
by a run.

## A change run, step by step

1. **Inspect**: root, branch, HEAD, your staged/modified/untracked files, stack, test commands. A merge or rebase in
   progress stops the run (XENIO does not touch a repository in that state).
2. **Isolate**: snapshot of your current files (uncommitted work included) as the base; an integration worktree in
   `~/Xenio/engineering/<run>/`. Your checkout is not touched.
3. **Baseline**: XENIO runs the project's tests once before any change, so failures that already existed are not
   blamed on the agents.
4. **Plan**: a small, specific change ("fix the typo in README.md", "add count_open() to src/x.py") -> one engineer,
   no planner, no team overhead. Anything else -> the planner (read-only) proposes tasks with roles, dependencies,
   file scopes and acceptance criteria; XENIO validates the plan (no cycles, known roles, scopes inside the repo).
   **A team plan waits for your "go ahead"** (unless you said to just do it, or `XENIO_ENGINEERING_AUTO_START=true`).
5. **Execute**: tasks whose dependencies succeeded start, up to 3 at once (`XENIO_ENGINEERING_MAX_PARALLEL`, at most
   4: Hermes' limit); two writers only in parallel when their file scopes do not overlap. Each writer gets its own
   worktree from the current integration state. After each task XENIO itself commits the changes, checks they stay
   in scope, looks for deleted/skipped/weakened tests, runs the tests that cover the changed files, and merges into
   the integration branch (one at a time). A merge conflict gets one controlled re-apply by the same role; then it
   is reported.
6. **Verify and review**: the full test suite again (compared with the baseline), the deterministic security scan,
   then the security reviewer and the code reviewer (read-only) against the diff and the acceptance criteria.
7. **Report** (`~/Xenio/engineering/<run>/report.md` and `changes.patch`): outcome, tasks with roles, models and
   time, what changed, tests before/after, security findings, review findings (with checked locations), problems
   XENIO caught, where the result is, and which steps still need your approval. Nothing was pushed, merged into your
   branch, applied to your files or deployed.

## Outcomes

| Outcome | Meaning |
|---|---|
| **Done and verified** | every task succeeded, tests ran and pass (no new failures), no high-severity finding, no test tampering |
| **Done, but NOT verified: ...** | the tasks succeeded, but the evidence is incomplete or negative; the reasons are listed (no test suite, new failures, high findings...) |
| **Not completed: ...** | a task failed or was blocked; finished work is kept on its branch |
| **Not accepted** | an agent changed files outside its task |
| **Cancelled** | you cancelled; finished tasks are kept |

Task statuses: `pending` (waiting for dependencies), `ready`, `running`, `succeeded`, `failed`, `blocked` (a
dependency did not succeed), `cancelled`. Test statuses: `passed`, `failed`, `no_tests` (the runner exited but no
test ran: never counted as passing), `blocked` (missing dependency or runner), `timeout`, `error`, `not_run`.

## Failures and recovery

* Provider errors (busy free models, rate limits) and timeouts: a task is retried at most `max_attempts` times (2 by
  default, 1-3), continuing from the files as they are. Hermes' own free fallback chain applies inside each turn.
  XENIO never switches to a paid model.
* Rejected results (no change made, a test deleted, files outside scope): the task fails with the reason; protocol
  violations are not retried with the same agent.
* A failed task blocks the tasks that depend on it; independent work continues and is kept.
* A Hermes restart does not resume a running task: it fails with the reason, completed work stays.

## Cancelling

"Cancel the engineering task": no new task starts, every running agent gets Hermes' cancel, XENIO waits up to about
10 s and then says which agents (if any) are still stopping. Finished tasks stay on their branches.

## Applying, pushing, pull requests

* **apply**: writes the run's changes (only the agents', never your own work) into your working tree, uncommitted;
  refused if they no longer apply cleanly to your current files. Asks first.
* **push**: pushes the run's branch `xenio/<run>/integration` (never your main branch, never with force). Asks first.
* **pull_request**: after a push, opens a draft PR with the report as its description (GitHub CLI). Asks first.

## Costs

Every agent turn records its model, time and the token usage Hermes reports. Money cost is reported as **unknown**
unless the provider reports it; with the free models in this setup it is zero. Small changes skip the planner and the
team, to keep the number of model calls low.
