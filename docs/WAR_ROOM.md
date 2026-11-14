# The War Room

Your digital company. You are the CEO, XENIO is the manager, and the agents do the work. Give it a sentence: a website, a report, a deck, a spreadsheet, a job on a website or in an app.

This builds on [The Foundry](FOUNDRY.md), which is still what runs the team.

## Talking to it, while it works

You talk to the team as you talk to one person, at any moment.

| You say | What happens |
|---|---|
| Anything | XENIO acknowledges it at once, for no tokens. Whoever is working has it before their next step, and a reply they were still writing on the old plan is dropped |
| To one agent, by the menu or "ask the strategist to…" | Only they get it. If they have nothing on, it becomes their task |
| "Switch to X" or "use a different approach" | The plan changes in one small call. Nobody votes on what the CEO decided |
| "Reconsider this" or "what do you think" | They debate it, because you asked |
| "Stop" or "wait" | Everyone stops where they are, files kept |
| "Stop all agents" | The work ends |
| "Continue" | It carries on |
| A question | XENIO answers from what has really happened, or the agent you asked does |

Attach a picture, or paste a screenshot. It is looked at once, and the team reads what was seen.

**Your orders** in the side panel shows how each one stands: heard, with the team, done or not done. Only the independent reviewer can mark one done, an order it says is not done goes back as work like a failing test, and a job is not called verified while an order is unconfirmed.

Agents acknowledge you in their own words, in what they were going to say anyway. Every task is handed over and taken up out loud. A message to a teammate who is working reaches them before their next step.

## Any kind of work

One call works out what the job is and staffs it, so there is no separate step to pay for.

| Kind | Who | What proves it is done |
|---|---|---|
| Software | Engineers, a tester, a reviewer | Tests XENIO runs itself, independent review |
| One page | One designer, one call | It opens, it is measured and looked at on a laptop and a phone |
| Research, documents, decks, sheets | Researchers, writers, analysts | Every file opened and read back, formulas worked out, sources checked against what was really read |
| Work on a website | Someone with a private browser | XENIO's own log of what happened, and reading the saved page |
| Work in apps on this Mac | Someone allowed your screen, after your yes | The app's own state, read back |
| A question | Nobody but the manager | One answer |

If a job wants something XENIO does not have, it says so before spending anything. **What it can do** lists it all, found by asking this Mac.

## Why pages stopped looking the same

The prompt used to say what good looks like, so everything came out as the same dark poster. Now each job gets an art direction, out of 22 very different ones, never one used lately and not dark after a run of dark. The fonts really used are remembered and ruled out. A small call chooses the direction that suits the subject and the skills that suit that direction, out of all you have installed.

A page is then opened, measured and looked at. If it needs pictures, Stable Diffusion XL makes them from what the page says it needs.

## Everyone has their own arrow

Like a shared document in Figma.

* **In a private browser**, which is what most website work uses, each agent has a real WebKit browser with its own cookies and no window on your screen. They really do run side by side. Its arrow is drawn in that page, at the point really clicked. Pictures of it are for you: a model only sees one if an agent asks to look.
* **On your real desktop**, agents press and fill by name through the Accessibility API, as VoiceOver does. Your mouse does not move, nothing comes to the front, and you can carry on working. Several can do this at once in different windows. Only two in the same window take turns. A labelled arrow of their own is drawn at the place they acted.
* **The real pointer** is borrowed only when nothing else will do: a drag, a canvas, a page that ignores the Accessibility API. It is held for one action, in turn, and only while you are not using it. If you start using it, the agent stops and waits for you.

An arrow is drawn and labelled as an agent's. It is never your mouse moving.

**Take over** in the Live tab gives you everything at once. No agent touches your screen until you hand it back, and they read the screen again first.

Your screen is off by default. Each job asks once, and a send, delete or purchase asks again each time. Password managers and live tests are refused whatever anyone says.

## What it can reach

| | How | Limit |
|---|---|---|
| The web | Search and reading, no key needed | Public addresses only, checked at every redirect. A site's robots.txt is respected. 40 searches and 80 pages a job |
| Your mail, calendar, GitHub | Your existing sign-ins | Read only, after your yes for each job. No agent tool can send anything |
| Your logins | [The vault](VAULT.md) | Placed after your fingerprint, and never seen |
| Google Drive | Nothing yet | Reported as missing |
| LinkedIn, a CRM | A private browser signed in through your vault | No connector. **LinkedIn's terms restrict automated use, so that is your call to make.** Saving a change asks you first |

## What it costs

See [what real runs measured](FOUNDRY_ACCEPTANCE.md#the-whole-company).

The things that keep it cheap: reading a page as text rather than pictures, a picture only when asked for, one call for one page, edits rather than rewriting a page, a limit on what each kind of task may spend, the cheapest model that can do each job, no debate unless the work is really code, and waiting for the mouse costing nothing.

## Not built, or not proved

* The real sensor, a real Google or LinkedIn sign-in, and the real pointer path on the real desktop have not been run by a machine. See the acceptance results.
* An agent cannot drag, switch windows or use the clipboard on your real desktop. Reading, pressing, typing, keys and scrolling are all it has.
* There is no live video of your screen in the War Room, on purpose. You can see your screen.
* Files outside a job's own folder are not touched by any tool here, so "organise these project files" cannot yet be done safely. A desktop agent could only do it by hand in Finder.

## In your own browser and your own apps

After a real job on LinkedIn failed and spent 157,920 tokens, agents now work where you are: in your own browser, signed in as you, and in your own apps.

* **Your browser.** Each agent works in a window of its own in your Safari, or in a tab you already have open once you say yes to that tab. The first time a job wants your browser it asks you, once.
* **On any desktop.** If you work in a full-screen app, your Safari windows are on another desktop (Space), and macOS Accessibility cannot reach them there. Safari's own scripting can: agents then read and act in their own window or in your named tab, in the background, without your mouse or keyboard and without changing the tab you are looking at. This needs one switch that only you can turn on: Safari > Settings > Advanced > "Show features for web developers", then Develop > Developer Settings > "Allow JavaScript from Apple Events". It lets apps you have allowed to control Safari run scripts in your pages, so turn it on only if you are happy with that. Without it, agents use Accessibility, which reaches Safari only on the desktop you are looking at, and they tell you exactly that instead of failing silently. A window they could not use is closed again.
* **Signing in on another desktop.** The vault places a login only into a window on your screen. When the agent's window is on another desktop, it asks you to sign in there yourself and waits.
* **Planning work on a website.** Code rules do not apply to it. "Review and update my LinkedIn" is the work itself, not a look at code before changing it: a real job died twice on that rule. A project XENIO made for a job is always a change, never "explain the code": "update what does not look good" had been read as "what does ...". If the planner still cannot make a usable plan, one person does the whole job rather than the job ending.
* **Who does website work.** On a website or desktop job every task goes to someone with hands there, however the planner split it. A real job gave the audit of the CEO's LinkedIn to a writer with no browser, who asked the CEO to paste the profile. A question to a teammate is answered by a model with no tools: it says the site is worked in that teammate's own task, never "I have no browser".
* **Slow pages.** LinkedIn's profile kept growing for 6 s after it said it had loaded, and its edit form showed its Headline box 4 to 6 s after the pencil was pressed. A page is read once it has looked the same for 1.5 s, and pressing or typing waits up to 10 s for its button or box to appear, as a person would. Boxes inside web components (open shadow roots) are found too. LinkedIn itself did not block anything: it showed the CEO signed in, with no human check.
* **Research comes from the researcher.** When XENIO creates a researcher for a software job, no debate settles first what the research is for, and the plan must give the researcher their part. A real job had three models pick a market niche from opinion, and the plan then skipped the research the CEO asked for.
* **Tests already written.** If the engineer already wrote the tests, the Test engineer having nothing to add is not a failure: the checks run them next. Tests beside the code (app/test_main.py) are found as pytest finds them. A real job failed there after 127,000 tokens and never ran its backend.
* **Reading the same file again and again.** The newest copy of each file an agent read stays in its context, and a third read of an unchanged file is called out. A real tester read app/main.py ten times (read_file, cat, head, tail), because its only copy had been trimmed while it was told to use what it had.
* **Before it starts.** For websites you are signed in to, XENIO asks macOS whether it may drive Safari (it only counts windows, and only if Safari is open). macOS asks this of the app XENIO was started from (XENIO Orb, when you start it from the Dock or Finder), so the first time it asks you whether XENIO Orb may control Safari: say yes, or agents cannot reach your Safari. Accessibility is needed only for the screen-reader route and for other apps.
* **Signing in.** Only if a page asks for it. XENIO tells you which email it would use, on which website, and your fingerprint is the yes. Your email and password are then placed into that window through Accessibility, with no keyboard and no mouse. XENIO knows your email. The password never leaves the vault's own helper.
* **Asking you.** An agent that needs something only you know asks you directly, once, and waits, which costs nothing. Your reply is its answer, not a new order.
* **Any app.** Agents see your open windows, find the right installed app for a job, and use its own menus and controls by name. Pictures and videos can also be edited straight from the file, always into a new file: resize, crop, rotate, change format, shorten a video.
* **What it measured.** On a stand-in profile page in a real WebKit window: it found the open window, typed the new headline, asked before pressing Save, and the site saved it. 34 seconds, 29,282 tokens, verified. Photo and video editing in real apps has not been tried on a real job yet.
* **What the real Safari showed (2026-11-06).** With your permission, three real runs: Nabh had most models "busy" for minutes (now: try the next model at once); a reused agent with no browser asked you three times to paste your profile (now: account work always goes to someone with a browser); and every open failed because Accessibility could not see the new Safari window. Safari's windows were not on the current desktop, and the Mac's display may also have gone to sleep during the run, which macOS treats as locked; both shut Accessibility out, and the run could not tell which (now: Safari's own scripting, as above, and a window that cannot be used is closed again). On a locked Mac agents still open, press and type nothing, whichever route. The page script was checked on real WebKit pages: it read the form, typed, pressed Save and the site saved, and it refused the password box without reading it. Working in your real Safari end to end still waits for the switch above.

## One builder, your limits, and connectors

* **One person per job.** A small or medium job is done end to end by one builder: code, tests, a README and how to start it. Real jobs failed at every hand-over (a tester given the build, a tester with nothing to add, a README that cost 22,000 tokens). Research that has to come first stays its own step. A page that already exists is edited, never written again from nothing: a rewrite once dropped every call to its backend. Proven on real models: a notes app with FastAPI, SQLite and a page, verified in 57,000 tokens, and a change to it verified in 71,000.
* **Your token limits.** Under Options when you start a job: the most tokens for the whole job, and for one agent's task. "Make these my default" keeps them for every new job. A running job's limits are changed from its header (Limits). Empty means XENIO decides by the size of the job. Only you can set them.
* **Connectors (MCP).** Add a server as a program on this Mac or an https address, or paste the usual mcpServers block. A token goes to the macOS Keychain (written through `security -i` on stdin, so it never shows in a process list), never to a file, log or model. A server's program gets a clean environment, never XENIO's own keys. Agents reach every server you switched on through one tool, `connector`, so fifty tool descriptions do not ride along on every step; the first use in each job asks you, and what comes back is data, not instructions. Reviewers never get it.
* **Figma.** Figma's MCP server lets in only apps on its own list and refuses XENIO (checked: it answers with a sign-in refusal). A job that names Figma stops before any token is spent and says so. The way in is a XENIO plugin in the Figma desktop app, which needs that app installed.
* **Working like an IDE agent.** A task's files are in its prompt from the start, with line numbers, and it changes them with exact replacements. A read of a file it already has, unchanged and still in its context, does not run: it gets one line saying so. A piece of a file it has whole counts too. A page that exists is updated in one call that sees it whole, with the backend's routes, and the result is not saved if it loses the page's calls to its backend or comes back far shorter.
* **GitHub.** Finished work in a project XENIO made: "Create the repository and push" (private unless you untick it), then "Push latest" and "Pull" (fast-forward only). Through the `gh` sign-in already on this Mac: no token passes through XENIO, hooks do not run, nothing is ever forced. Agents get a github tool only when your goal mentions GitHub, a repository, pushing, CI or deploying; each use asks you, and happens once the finished work is in the project.
* **Figma.** XENIO's own plugin (native/figma-plugin) in the Figma app: import it once (Plugins, Development, Import plugin from manifest), run it in your file, paste the pairing code from Connectors. It talks to XENIO on this Mac only (port 3779, the pairing code required, other addresses refused). On a job that mentions Figma, the designer draws whole frames as trees (auto layout, text, colours, shadows), exports them and looks, and fixes with updates. The first drawing in each job asks you, and Figma's Undo takes back any of it.
