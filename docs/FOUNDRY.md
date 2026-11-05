# The Foundry

Your engineering organization. You give the command and supervise. Agents plan, argue, build, test and fix, and come to you only for what is really yours to decide.

Open **War Room** in the menu and type what you want, or say it in chat or by voice: "build me a gym management app". That is the whole interface.

## What happens after one command

```
you: "Build a gym management app"
 |
 v
FACTORY     works out the expertise needed, reuses agents that have it, creates specialists only for real gaps
 |
 v
DEBATE      the choices that are expensive to get wrong: different models think alone, then challenge each other.
 |          A judge who was not arguing decides against stated criteria. Dissent is kept.
 v
PLAN        milestone 1: something that really runs end to end, with tests
 |
 v
BUILD       agents work in isolated git worktrees. Every tool call is checked. Every command runs in the OS sandbox.
 |          Packages they list are installed by XENIO: public registries only, install scripts off
 v
LOOP        XENIO runs the tests -> independent review -> problems go back to whoever wrote that code -> fix -> test again
 |          Limit: 3 rounds, 1 hour, 400 tool calls per milestone
 v
COVERAGE    someone who did not build it compares the goal with what is really there
 |
 +-- things missing --> next milestone (at most 5) --> PLAN
 |
 v
DONE        a project XENIO created lands in ~/Xenio/projects/<name>, committed.
            Your own repository is never written to: that is a step you confirm.
```

## Being the CEO

| You | How | What happens |
|---|---|---|
| Give a command | War Room box, chat, or voice | A team forms and starts |
| Redirect | "Stop. I want PostgreSQL instead of MongoDB." | It pauses at once, agents keep their files, the decision reopens with your requirement binding, the plan is adjusted, and work resumes by itself |
| Ask anything | "Why this design?" | The agent you asked answers from what really happened |
| Pause, Resume, Stop | Buttons | Stop ends all of it now |
| Overrule | Decision Room | Choose an option, or say what you want |
| Hand work over | Graph → click a step | Only to an agent able to do it |
| Add a requirement | Side panel | Everyone still to work is told |
| Ask for a review | Side panel | That agent reviews it and answers you |
| Try what it built | The Result tab | It is already running, in the page and on a local address. **Run it** starts it again |
| Ask for a change | Type in the box under a finished piece of work | The same team plans just that, builds it, tests it and runs it again |
| Delete work | The bin on the list, or **Delete this work** | The conversation and graph go. A project it built stays in its folder |
| Add a skill | Skills tab, or chat | You see what it would do before anything runs. Once added, every agent can use it |

**What still comes to you, on purpose:**

* a decision where a security or data-loss objection is still unanswered
* a decision with no clear option, or too few voices
* anything an agent wants that policy rates as high risk
* running out of budget
* writing to your own files, pushing, opening a pull request, deploying
* installing a skill, or giving one to an agent

Everything else the team decides itself and flags, and one message from you reopens it. For more control, tick **Ask me before building** under Options: that also makes every judgment call wait for you.

## Which model does what

No one model carries the work. Each kind of work has its own pool, a task goes to whichever model in the pool is least busy, and a model that is out of credit or failing is skipped.

| Work | Models | Why |
|---|---|---|
| Backend, general building | GLM-5.2, Kimi Code, Qwen 3.5 | Measured on one real tool job: 6 s, 9 s and 8 s |
| Pages and frontend | GLM-5.2, Kimi Code, Qwen 3.5 | GLM-5.2 made the best page on the same request |
| Tests | Kimi Code, Qwen 3.5, GLM-5.2 | Same tool-job measurement, a different one first, so the load spreads |
| Docs and small jobs | Qwen 3.5, Kimi Code, GLM-5.2 | The cheapest that does the job well |
| Review | DeepSeek V4 Pro, GPT-OSS, GLM-5.2 | Reads a diff and answers once, with no tools |
| Debating | DeepSeek V4 Flash, GPT-OSS, DeepSeek V4 Pro, Kimi Code | Different models on purpose: 5 to 9 s each |
| Judging, coverage | GLM-5.2 | Did not take part |
| A model you pin | Set it on the agent (Agents tab) | Anything Hermes offers runs over ACP in its own session. This costs far more tokens: see below |

The DeepSeek models answered without calling tools at all in that test, so they think and do not build. A built-in agent is no longer pinned to one model unless you pin it, and an old pin left by the engineering team's settings is released when XENIO starts.

## What it costs

A plain page costs about 25,000 tokens. It once cost 990,000, and the first real work through the War Room cost 8.7 million. Each cut below comes from reading where real runs spent their tokens.

| Where tokens went | What it does now |
|---|---|
| A goal check that reads only the start of each file said a page had no CSS, and started a second milestone (619,000) | A small job is judged by its checks and its review, and you can ask for a change. Bigger jobs are told that a file they cannot see into is not missing |
| An agent with tools re-read and rewrote its own page (29,000 to 83,000) | One page is one call with no tools (about 9,000), and XENIO saves what comes back. Anything bigger still gets agents with tools |
| Flask, tests and a README for a page that needed none | The planner makes a static site one task, with no server, tests or README. XENIO opens the page to check it |
| Agents told to read every skill tagged for their job (up to 6 at once, kept in context on every step) | The one or two that really fit, distilled once and put in the prompt, so there is nothing to read |
| The docs agent still on the older team's model through Hermes (151,000 for a README) | Released when XENIO starts. A model you chose yourself stays |
| A task stopped at its limit was run again (208,000) | A task that hits its limit or gets stuck is not repeated |
| Python could not start inside the sandbox, so agents spent whole turns on `which python` | Fixed: a project's Python can reach its own files |
| A test agent wrote the same two files 16 times and never ran them | A file is written at most 3 times between runs |
| Failing commands retried with small changes each time | 8 failed commands in one turn end it, as do 5 of the same one |
| An agent kept fixing tests when the bug was in code it may not change | Failing tests go to the code's owner when the review points there |
| One turn could spend past the whole budget | Each task has a limit: 60,000 tokens on a small job, 120,000 medium, 180,000 large. Fixes get 20,000 to 80,000, and docs 20,000. It is told to finish at 70% and stopped at 100% |
| Reviewers explored the repository with tools | One message with the diff, then only what changed. A security review is skipped when only static files changed, and the scanner still reads them |
| Small findings sent back to be fixed | On a small job only what blocks it gets fixed. On a page, that includes anything a visitor would see broken |

Each workflow stops and asks when it reaches its budget: 200,000 tokens for a small job, 800,000 for a medium one, 2.5 million for a large one. **Add more budget** on the page doubles it.

* The War Room shows real tokens as a bar at the top and, for each model, what it used. Nabh reports real usage at the end of a stream, and that is what is counted. A model that does not report it is estimated from its text.
* If the account runs out, the work pauses with nothing lost. Add credit and press Resume. Nabh has no balance check, so XENIO learns this only when a request is refused.
* Voice transcription, the teacher and images share that account. A big build can leave them short.
* Pinning an agent to a model through Hermes goes back to the expensive route for that agent, with no per-task limit.

## How a page gets made

A page is one model call with a strong brief: a concept first, type with character, visuals it can really draw, and nothing covering the text. It runs at a higher temperature than code does, on GLM-5.2. That is what made the best pages on the same request, ahead of Kimi Code and of any agent with tools. XENIO saves the file and opens it, and one reviewer reads it.

Pages vary from run to run. **Nobody looks at a page before you do:** the reviewer reads the code, and XENIO checks that it opens. So a layout problem can still reach you. Tell the team, and it fixes it.

## People helping each other

Agents work as a team, and asking for help is cheap compared with going round in circles.

**A senior, when someone is stuck.** When the same command has failed 3 times, the agent asks a senior on the team, who sees the error and up to 3 files it names, and answers in a few sentences. They have no tools and cannot change anything. It costs a few thousand tokens, at most twice a task and 8 times a workflow, and the exchange shows in the conversation. Private files (`.env`, anything hidden, keys, anything named secret or token) are never sent, even if an error names them.

**A helper, rarely.** On medium and large jobs, an agent can bring in a temporary helper for one separate piece of work. It is not offered on small jobs, or under **Ask me before building**, so those pay nothing for it.

| Limit | What it is |
|---|---|
| What a helper may do | Never more than the agent who asked: the same tools, inside the same folders or fewer |
| How many | 1 per task, 3 per workflow |
| When | Only with at least 40% of the budget left |
| What it may spend | 14 steps and 40,000 tokens, from the same budget |
| Bringing in others | A helper cannot |
| Afterwards | Archived. It stays in the record of this work and does not join the permanent team |

Agents cannot create AI models. They can create agents, and each runs on a model XENIO already has.

## What it built, running

When the work is finished, XENIO starts it and asks it for its front page. A server that crashes or answers with an error counts as a failure, and the team is sent to fix it. It costs no tokens, because the machine checks it and no model is asked.

It runs from a throwaway copy, inside the sandbox, on a local port: it cannot change your project, and your home folder stays closed to it. To say how a project starts, put the command in a `.xenio-run` file. Otherwise XENIO works it out: `package.json` scripts, Django, FastAPI, Flask, a Python server, or a static page.

## Packages

Agents install what they need with the `xenio_install` tool, and XENIO installs what a crash says is missing. This happens only in a project XENIO made, and it goes in the project's own `.venv` or `node_modules`. In your own repository, what is needed is reported to you.

So if you open a built project in your editor and it says Flask is missing, your editor is using another Python. Point it at the project's `.venv`, or run it from the War Room.

## Your skills base

Give it a GitHub link, or just `owner/repo`. A repository holding many skills shows as a list you choose from, and each one you keep becomes its own entry. A skill you add stays in the base with no installing it again, and goes to the agents whose job it suits.

A skill goes by job, as it would on a real team. XENIO works out what a skill is for from what it says about itself: frontend and design, backend, testing, security, docs, review or planning. The design guide reaches the frontend engineer, and a tester cannot even open it. Someone building whatever is needed also gets a skill when their task calls for it, because on a small team that may be the page itself. A skill that names no job, such as a memory skill, is for anyone, and an agent is told to read it only if it fits the task. You can change the jobs on the skill's card, or name particular agents.

For each task XENIO picks the one or two skills that really fit, from what you asked for and what each skill says about itself, at no cost in tokens. The first time a skill is used, `gpt-oss-120b` distils it into its craft rules, about 4,400 tokens once. After that its digest goes into the agent's prompt for free, until the skill's files change. An agent can also keep a skill with the project: `add_to_project` copies its files to `.agents/skills/<name>`. That is files only, in the isolated copy they are working on, so nothing from it runs and you still review it before it reaches your own files. In a project XENIO made, a skill the team really used is kept there automatically when the work is delivered, at no cost in tokens. A large base costs few tokens. Each skill's card says how often agents really used it, and who.

`npx skills add owner/repo --skill name` works as you would paste it from skills.sh, and so does a skills.sh link. `skills` in that command is the installer, not the skill: what comes in is the skill you named, under its own name. `npx skills` alone is refused with an explanation.

## Two ways to run an agent

* **Native** (`app/foundry/runtime.py`), the default. XENIO's own tool loop on Nabh's models.
* **ACP** through Hermes, for an agent pinned to a model only Hermes reaches (OpenRouter free models, the OpenAI Codex subscription). Paid models are refused unless `engineering.allow_paid` is set.

Both obey the same `policy.classify_engineering`. Native is stricter: its commands also run inside the OS sandbox.

Hermes still runs chat and voice. It was not removed because nothing there needs it to be.

## Setup

After pulling this work:

```
.venv/bin/python -m app.main --install-guard
(cd ui && npm install && npm run build)
native/orb/build.sh
```

Then restart `--ui`. The first step teaches Hermes the new `xenio_foundry` tool, so that chat can command the team. The War Room itself works without it. The last step only redraws the Dock icon. It needs `NABH_API_KEY` in `.env`, and `npm` and `git` for skills.

## Where things live

```
app/foundry/
  store.py       foundry.db: migrations, every table, the event log
  registry.py    agents: versions, enable, archive, promote, performance; agent memory scopes
  factory.py     goal -> team: reuse first, create only for gaps
  models.py      which model thinks for whom, budgets, ACP thinking
  runtime.py     XENIO's own agent loop: same policy, plus the sandbox
  sandbox.py     the wall: sandbox-exec, clean environment, limits
  deps.py        packages for a project: registries only, no scripts
  preview.py     what it built, running, from a throwaway copy in the sandbox
  messages.py    structured messages, framed as data
  decisions.py   proposals, challenges, judge, dissent, escalation
  graph.py       typed nodes, branches, no cycles, recovery
  loop.py        test, review, fix, bounded
  engine.py      runs a workflow; pause, stop, messages, milestones, recovery
  service.py     the one door for the War Room and the chat
  skills/        sources.py (fetch), inspect.py (read), manager.py (lifecycle)
app/tools/foundry.py        the xenio_foundry chat tool
ui/src/pages/WarRoom.tsx    the War Room
```

`foundry.db` is in XENIO's data folder (mode 600). Built projects are in `~/Xenio/projects`. Runs are in `~/Xenio/engineering`.

See [FOUNDRY_SECURITY.md](FOUNDRY_SECURITY.md), [FOUNDRY_API.md](FOUNDRY_API.md) and [FOUNDRY_ACCEPTANCE.md](FOUNDRY_ACCEPTANCE.md).
