# Ideas for later

Not built yet. Nothing here is a promise or a plan.

## Watch the agents from VS Code

A XENIO panel in the VS Code sidebar, like the Claude Code panel: see every agent working live, without leaving the editor.

**What it would show**

* The team: each agent, its logo, what it is doing now, and its model.
* Each step as it happens, and the agents talking to each other.
* The files an agent changed, in VS Code's normal diff view.
* The commands it runs and what they print.
* Controls: approve, pause, stop, and message the team.

**How it would work**

* A VS Code extension that shows XENIO's live events. XENIO already sends most of these to the War Room.
* Agents keep working in their own separate copies, as they do now. The panel only watches them, and their work reaches your files only when you approve it.
* Watching costs no tokens, because no model is asked.

**Still to work out**

* How the extension gets the session key safely. XENIO makes a new one each time it starts.
* Live diffs and command output, which the War Room does not show today.
