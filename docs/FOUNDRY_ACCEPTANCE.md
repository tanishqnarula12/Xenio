# The Foundry: acceptance results

Tested on 2026-11-04 with real models, real package installs and the real macOS sandbox, in scratch folders. Not in live XENIO, and not with Hermes.

**The headline: a real build now finishes verified, and running, for 337,000 tokens.** See [Tokens and a finished build](#tokens-and-a-finished-build). The first runs below are kept as they were: the Nabh account ran out of credit partway through independent review on them, and what they found is why the later fixes exist.

| Scenario | Result | What really happened |
|---|---|---|
| A. "Build a gym SaaS" | **Most of it** | See below. Built and tested, not reviewed or accepted |
| B. Genuine disagreement | **Passed** | Real debates between different models, with positions changing |
| C. "Stop, use PostgreSQL" | **Mostly** | Paused, re-debated, re-planned and rewrote the code. Then blocked, and it stopped before finishing. Reopened the wrong question (since fixed) |
| D. Install a skill for two agents | **Partly** | Real fetch and read passed. Real install and run were refused by the permission system on this machine, so they are verified only on local packages |
| E. Reuse a specialist | **Passed** | Reused in a real factory run and in tests, with separate workspaces |
| F. Recovery after a crash | **Passed** | The real process was killed mid-write. Nothing was repeated. Then stopped at the credit limit |

## Tokens and a finished build

Real models, a real sandbox and the real War Room page, in scratch folders, on 2026-11-05. The goal was a small Flask landing page with a menu, opening hours, a contact form saved to SQLite, and tests.

| | The first real work, on one model through Hermes | Before the loop guards | With them |
|---|---|---|---|
| Result | Done, not verified | Paused, out of budget | **Done and verified** |
| Tokens | 8.7 million | 659,000 and not finished | **337,000** |
| Models | 1 | 5 | **5** |
| Test agent, all its work | 720,000 | 189,000, wrote the same files 16 times | **45,000, 13 tool calls** |
| First round of fixes | | 269,000 on one turn | **60,000 across two agents** |

Those runs had different goals in the first column and the same goal in the other two.

What the finished build showed:

* 569 seconds, from one sentence to a running app. Independent review passed on the second round.
* The page opened on the Result tab with the running app in it, and the app answered 200.
* Agents read the skill, but in this build did not follow it: its rule appears 0 times in the stylesheet. Its text had been trimmed out of the agent's context before it wrote the CSS. It now stays in context, and in one real agent turn afterwards the stylesheet used the rule on 21 lines, with 0 raw colours outside the variables.
* The page itself could not scroll, and the header and message box stayed put on every tab.

The change ("add a dark mode toggle") was built, tested and passed review for about 138,000 tokens. The check that followed could not see it and began building it again, so that run was stopped by hand to save credit, and the check no longer runs after a change. **That fix is covered by tests only.** So are seniors and helpers: no real build has used them since they were written. Skills going by job are also covered by tests. In that one real turn the frontend engineer was offered the skill and used it, but no real test agent has yet been shown to go without it.

## A. One command, a whole app

Goal: *"Build a gym management web app: members, membership plans, class schedules, class bookings with capacity limits and a waitlist, and payment records, with a REST API, a database, a simple web interface, and tests."*

The third attempt got this far, with no input from anyone:

| Time | What happened |
|---|---|
| 8 s | A team assembled, reusing some agents and creating others |
| to about 5 min | Two design debates, four models each, then the plan |
| by 8 min 22 s | Database, API, web UI, README and tests written in isolated worktrees |
| 8 min 23 s | XENIO ran the tests itself, in the sandbox: **38 passed, 1 failed** |
| from 8 min 23 s | Independent review began, and ran out of credit partway through |

The result is 1,578 lines of code: `app/` (Flask routes and SQLite), `web/`, and three test files. Flask and its dependencies were really installed in the sandbox.

**The failed test is a real bug the team wrote and its own tester caught:** cancelling a booking does not free the seat (`booked_count` stays at 2). This is what the fix loop exists for, and it did not get to run. So this app is **not finished and was not accepted**. Coverage checking and a second milestone never ran.

Two earlier attempts failed in ways that led to fixes:

1. A stray `gymapp.db`, created by running the app, failed the whole build. Junk is now cleared, out-of-scope changes are undone with a note, and a failed task gets a second try.
2. Debates stopped to ask about latency, and then about a generic auth concern. Now only security or data-loss objections can stop the work, and the judge must rule on each one.

## B. Disagreement

Four models on "PostgreSQL or MongoDB for a gym SaaS, and how to separate tenants". All first said PostgreSQL with row-level security. They then found things none had said at the start: transaction-mode connection pooling breaks session-level tenant context, table owners bypass row-level security, and JSONB is a poor fit for full-text search on notes. Three changed position. The judge cited the pooling issue as its reason.

All four started in the same place, so this shows real challenge, not a real split. A later debate on booking locks did start out split, across optimistic locking, pessimistic locking and database constraints.

## C. "Stop. I want PostgreSQL instead of SQLite. Re-evaluate the architecture."

| | |
|---|---|
| Paused | **7 ms** after the message, before any model was asked |
| Understood | 2.6 s later, recorded as a requirement |
| Re-debated | About 87 s, by the same people |
| Plan adjusted | 5 s, then it resumed by itself |
| Code | Rewritten for PostgreSQL. `psycopg2-binary` was installed |

**What went wrong:**

* It reopened the most recent decision (testing strategy) instead of the database. Fixed: it now debates what you actually said.
* The tester reached for Testcontainers, which needs Docker, and the sandbox has none. It made its tests skip themselves, and XENIO's tampering check refused that, correctly. Agents are now told what the sandbox offers.
* Task titles still said "SQLite". Fixed: a plan change can rename tasks.
* It ran out of credit before finishing, so the final PostgreSQL code was **never accepted**.

**Known limit:** code that needs a real PostgreSQL server cannot be fully tested inside the sandbox.

## D. Skills

| Step | Evidence |
|---|---|
| Fetch and read `left-pad` from npm | Real: 1.5 s, low risk, nothing ran |
| Fetch and read a skill from GitHub (`anthropics/skills`, `pdf`) | Real: 2.1 s, medium risk, with file and line for each finding |
| Hostile packages | Local fixtures: every one was caught, and critical ones cannot be installed |
| Install, test, run, hand to named agents, update, roll back, uninstall | Real sandbox, **local packages only** |

**Not verified:** installing and running a real third-party package. The permission system on this machine refused that test (`Code from External`), and it was not worked around. To check it, install a package you trust from the Skills tab.

## E. Reuse

In a real factory run, the second project reused the Database Architect, Payments Engineer and others from the first, and only created what was new. Tests confirm one definition, separate runs and worktrees, and performance recorded across both.

## F. Recovery

The real process was killed with `os._exit` while an agent was writing. A new process then:

* kept 5 finished steps and repeated none
* flagged the writer as uncertain, checked it, and found it had not landed
* waited, paused, until Resume
* continued that task from its files, and finished it in about 2 minutes

## Live War Room

The real server, UI and models, in a private browser, on a real run: messages appear without reloading, Pause and Resume work, and Stop took 0.31 s with the agents reported as stopped.

## Not tested live

* Chat and voice commanding the team. The `xenio_foundry` tool is covered by tests, but needs `--install-guard` and a restart before it exists in live XENIO.
* A build started from live XENIO on the final code. The results above come from the same code in scratch folders. Live XENIO was checked only for serving this build and refusing calls without its key.
* An agent on a Hermes-only model doing real work in The Foundry. The ACP route is covered by tests with a stand-in.
* The running-out-of-credit pause, which is covered by tests only.
* A real third-party skill installed by this test rig: the permission system refused it. A real fetch and read of `usestrix/strix`, `anthropics/skills`, `emilkowalski/skills` passed with nothing run, an agent used a local skill written for the test, and the user's own install in live XENIO reached "tested in the sandbox".

## On the real records, after setup

The real server and UI, on a copy of the user's own records, with no model calls. Run it started the project that had failed earlier, its page answered 200 and showed inside the War Room, and Stop stopped it. The page could not scroll on any tab, and the goal and message box stayed in place.

## Tests

The real sandbox tests run against `sandbox-exec` itself. See the commit message for the counts on the code as pushed.

Tests changed because the behaviour was changed on purpose, at the user's request:

* Specialists stay after a workflow, where they used to be archived.
* A medium finding gets one pass, not several.
* Something that looks dangerous can be installed, locked down, if you confirm twice. This relaxes a rule that had no override.
* A skill reaches the agents whose job it suits, where it used to reach every agent.
* Rounds and budgets depend on the size of the job.

`test_real_hermes_gateway_shows_xenios_tools_only_with_tool_search_off` takes minutes, because Hermes installs its dependencies the first time it sees a data folder, and this test makes a new one each run. Its time limit was raised for that reason, and its assertions are unchanged. A test in `chat-page.test.tsx` built its dates in UTC and failed every night until 05:30 in India. It now uses local time, as XENIO does.

One test (`test_real_capture_is_jpeg_in_memory`) fails when the display is asleep, and passes when it is awake. It is about screen capture and has nothing to do with this work.
