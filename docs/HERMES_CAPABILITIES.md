# Hermes capabilities and how XENIO uses them

Inspected on 2026-10-03 against the installed Hermes Agent **v0.21.5** (source under
`%LOCALAPPDATA%\hermes\hermes-agent`): `toolsets.py`, `tools/*.py`, `acp_adapter/*`,
`hermes_cli/plugins*.py`, `hermes_cli/tools_config.py`, `tools/approval.py`, the plugin and hook
docs under `website/docs`, plus `hermes skills list`, `hermes mcp list`, `hermes plugins`.

**How XENIO reaches all of this:** XENIO runs `hermes acp` and sends your words with
`session/prompt`. Hermes picks and runs tools itself. ACP sessions get the `hermes-acp` toolset
(the coding tools minus `clarify`) **plus XENIO's `xenio` toolset**, pinned by
`platform_toolsets.acp: [hermes-acp, xenio]`; Hermes' Tool Search is switched off only in
XENIO's Hermes process (`tools.tool_search.enabled: ${XENIO_TOOL_SEARCH}`) so the model sees
those tools directly. The `xenio-guard` plugin checks every tool call before it runs (see
"Permissions" below).

| # | Capability | Hermes implementation | How XENIO invokes it | XENIO wrapper? | Security implications |
|---|---|---|---|---|---|
| 1 | Terminal execution | `terminal` tool (`tools/terminal_tool.py`). Runs **bash** — on Windows the Git-for-Windows bash from Hermes' package manager. Args: `command`, `workdir`, `timeout` (default 180 s), `background`, `pty`, `notify`. | Natural language → Hermes calls `terminal`. | **No executor wrapper.** XENIO adds the command policy (`app/safety/policy.py: classify_command`) applied by the plugin before every command. | Arbitrary code execution as the user. Hermes' own detector only flags commands it considers dangerous (it let `rm -f file` through silently), so XENIO classifies every command: allowlist first, unknown → confirm, secrets/env dumps/system deletes → blocked. |
| 2 | Process execution | `process_manage` (list, poll, log, wait, kill, write, submit, close, handoff) for background processes started by `terminal`. | Hermes calls it. | No. Policy: list/poll/log/wait = READ; close/handoff = SAFE_WRITE; kill/write/submit = CONFIRM_REQUIRED. | Killing or typing into processes. |
| 3 | Filesystem read | `read_file` (line ranges, 100K-char budget), `search_files` (ripgrep-style content search or glob file search). | Hermes calls them. | No wrapper (reused). Path policy blocks secrets (`.env`, SSH keys, `.aws/credentials`, browser credential stores, Hermes `auth.json`/vault). | Could expose secrets → blocked by path; any key that still appears in output is redacted by the plugin's `transform_tool_result`. |
| 4 | File creation / editing | `write_file` (create/overwrite), `patch` (exact replace or V4A multi-file patch). Over ACP in default mode every edit triggers an **edit approval** (`session/request_permission`, kind `edit`, with a diff). | Hermes calls them; the edit approval comes to XENIO. | No wrapper (reused). XENIO answers the edit approval with the path policy: new/changed files in your folders = SAFE_WRITE (auto); system folders = HIGH_RISK; executables/AppData = confirm; secrets = blocked (the plugin blocks before the tool runs). | Overwriting important files; writing autostart/executable files. |
| 5 | Directory operations | **Not provided as tools** — only through shell commands (`mkdir`, `ls`, `mv`, `rm -rf`). | — | **Yes, added**: `xenio_list_directory`, `xenio_inspect_path`, `xenio_create_directory`, `xenio_move_path`, `xenio_delete_path` (Recycle Bin by default), registered into Hermes by the plugin, structured results, path-validated. Raw shell equivalents are still policed. | Recursive deletion is the main danger → confirm in your folders, HIGH_RISK elsewhere, blocked for system/top-level folders. |
| 6 | Web search / browsing | `web_search`, `web_extract` (Hermes' keyless ring exa/parallel/firecrawl/keenable with failover; the `web-openai-native` plugin is **disabled** since the Codex sign-in, because it can only search, broke page reading and runs searches server-side where XENIO's policy cannot see them); default **Browser Use** mode: `browser_exec` (Python helpers new_tab/goto_url/page_info/js/fill_input/click_at_xy/cdp) on Hermes' own invisible Chromium; `browser_*` step tools when `browser.backend: off`; vault_*. No "open in the user's browser", no download tool. | Hermes calls them. | **V3**: policy per link / JavaScript / helper call / CDP method / element label (reading = READ, typing/clicking ordinary things = SAFE_WRITE, submit/send/post/upload = confirm, payments/account/password = HIGH, cookies/storage/password pages = blocked). Added `xenio_open_url` and `xenio_download_file`; sources recorded. See [WEB_CAPABILITIES.md](WEB_CAPABILITIES.md). | Sending data to websites, purchases, account changes, cookie theft. |
| 7 | Git | No git tool; `git` 2.53 via `terminal`. | Natural language → `terminal` (`git status`, …). | No; command policy: status/log/diff/show = READ; add/commit/fetch/clone, **creating a branch** = SAFE_WRITE; push/pull/merge/checkout, `branch -d` = confirm; push --force/--delete, reset --hard, clean -f, `branch -D`, filter-branch, credential changes = HIGH_RISK. | Publishing code, losing work. |
| 8 | GitHub | No GitHub tool; GitHub CLI `gh` installed, **not signed in**; Hermes ships the `github` skill (uses `gh`). | Writes: `gh`/`git` through `terminal`. **V4** reads: `xenio_github` (gh when signed in, else the public API for `XENIO_GITHUB_USER`). | **V3** `classify_gh`: list/view/diff/checks/api GET = READ; create/comment/edit/merge/api POST = confirm; releases, repo edit/delete, secrets, keys, `auth login`, api DELETE = HIGH_RISK; `auth token` = blocked. | Repository/account changes; issue text is untrusted. |
| 9 | MCP | `hermes mcp` (add/list/serve…); **no MCP servers configured**. ACP `session/new` accepts per-session `mcpServers`. | Not used now. | No. MCP tools (`mcp_*`) are unknown to XENIO's policy → CONFIRM_REQUIRED (fail closed). | Third-party tools with unknown effects. |
| 10 | Skills | `skills_list`, `skill_view`, `skill_manage`; many built-in skills installed (incl. email skills such as `himalaya`, `send-email-using-exis…`). | Hermes uses them. | No. Policy: list/view = READ, manage = SAFE_WRITE. Skills act through other tools (e.g. `terminal`), which are policed — e.g. `sendmail`/`mail` commands = confirm. | A skill can instruct shell commands; those are still classified. |
| 11 | Tool calling | Hermes' agent loop + tool registry (`tools/registry.py`, `model_tools.py`); plugins can add tools (`ctx.register_tool`) and hooks (`pre_tool_call` → allow/`block`/`approve`, `transform_tool_result`). | The model chooses tools from your words (no tool names needed). | XENIO's tool registry (`app/tools/registry.py`) catalogs both Hermes-backed and XENIO-native tools; the plugin is the enforcement point inside Hermes. | — |
| 12 | Subagents | `delegate_task` (isolated subagents, same process). | Hermes calls it. | No. SAFE_WRITE; every tool call a subagent makes passes the same `pre_tool_call` hook. | Same policy applies. |
| 13 | Code execution | `execute_code` (Python sandbox that can call `web_search`, `read_file`, `write_file`, `search_files`, `patch`, `terminal` via RPC → `model_tools.handle_function_call`, so those calls are hooked too). | Hermes calls it. | No. Policy analyses the Python (AST): only safe stdlib modules and read-only `open()` = READ; anything else (os, subprocess, writes, exec/eval) = confirm; secret paths = blocked. | Arbitrary Python. |
| 14 | Memory | `memory` tool; files `MEMORY.md`/`USER.md` in `HERMES_HOME\memories` (2,200 / 1,375 chars), frozen into the prompt at session start; `session_search` (FTS5) over past conversations. Audit: [MEMORY_ARCHITECTURE.md](MEMORY_ARCHITECTURE.md). | Hermes uses it; XENIO's `MemoryManager` reads the files for `/memory`. | No store of its own. **V4** policy on every write: secrets blocked; unrequested temporary/guess/log/duplicate entries dropped; memory saved during the run added to the per-turn context. | Stores personal facts locally. |
| 15 | Scheduling | Hermes cron (`cron/jobs.py`: jobs.json, lock, run history, `[SILENT]`, monitor mode); ticked by the gateway or `hermes cron tick`; `cronjob_manage` blocked in XENIO sessions. | **V4** `xenio_schedule` creates `xenio: …` jobs through `cron.jobs` (toolsets `xenio` + `web`, deliver local); XENIO starts `hermes cron tick` with its guarded environment when one is due. [PROACTIVE_AGENT.md](PROACTIVE_AGENT.md) | No second scheduler. | Unattended runs: confirmations refused (`approvals.cron_mode: deny`). |
| 15a | Email | Skills `himalaya` (CLI, IMAP/SMTP) and `google-workspace` (Gmail/Calendar/Drive with broad scopes); Himalaya v2.2.1 installed. | **V4** `xenio_email_search` / `xenio_email_read` (Gmail through XENIO's own read-only Google sign-in, or Himalaya). | Thin read-only integration. | Sending is blocked (no tool; `himalaya … send` blocked); email text is untrusted. |
| 15b | Calendar | `google-workspace` skill (not authenticated; asks for send/Drive/Docs scopes). | **V4** `xenio_calendar_events` / `xenio_calendar_create_event` with XENIO's minimal-scope Google sign-in. | Thin integration. | Creating events = confirm; invitation text is untrusted. |
| 16 | Desktop control | `computer_use` (screenshots + mouse/keyboard) — **not in the ACP toolset**. | Not available (later phase). | **V5**: observe-only `xenio_look_at_screen` instead (no clicking or typing); `computer_use` stays CONFIRM_REQUIRED; `classify_visual_action` is ready for later screen actions. | — |
| 16a | Vision | `vision_analyze` (image URL/file/`data:`; native image routing for vision-capable main models, else an auxiliary vision model), `browser_vision` (Hermes' browser), `video_analyze` (opt-in); auxiliary model access `agent.auxiliary_client` (`call_llm(task="vision")`). Audit: [VISION_CAPABILITIES.md](VISION_CAPABILITIES.md). | **V5** `xenio_look_at_screen`: XENIO captures the screen itself and has it described through Hermes' model access by the main model (gpt-6-luna), strictly (no fallback to paid providers). `browser_vision` and `vision_analyze` reused. | Yes: screen capture (Hermes has none outside `computer_use`) and the look service. `vision_analyze` is now path- and link-checked (was unchecked READ). | Screens hold secrets and other people's text: explicit capture only, text-only results, secrets masked, on-screen instructions flagged, nothing stored. [VISION_SECURITY.md](VISION_SECURITY.md) |
| 17 | Permissions / sandboxing | `approvals.mode: smart` (LLM guardian triages commands Hermes flags); hardline blocks for catastrophic commands; ACP edit approvals; `--yolo`; container backends (docker/singularity/modal) available but the configured backend is **local** (no sandbox). | XENIO is the ACP client, so every Hermes approval prompt arrives as `session/request_permission`. | **Yes**: `xenio-guard` `pre_tool_call` escalates XENIO-policy actions to Hermes' human gate (`request_tool_approval`, which skips the smart guardian and fails closed), XENIO answers by voice/console with `allow_once`/`reject_once` only. | Local backend = commands run with your full user rights; XENIO's policy and confirmations are the guard rail, not an OS sandbox. |

## Permissions in one picture

```
Hermes wants to run a tool
  └─ xenio-guard pre_tool_call ── classify_tool_call() (app/safety/policy.py)
        ├─ blocked            → Hermes is told "Blocked by XENIO policy: <reason>"; nothing runs
        ├─ READ / SAFE_WRITE  → runs
        └─ CONFIRM / HIGH     → hook returns {"action": "approve", "message": XENIO's description}
                                  → Hermes' human gate (tools.approval.request_tool_approval, in the agent thread)
                                  → ACP request_permission → XENIO PermissionGate
                                  → voice/console: "This will … Do you want me to continue?"
                                  → yes: allow_once (runs)   no / unclear / silence / timeout: reject (nothing runs)
write_file / patch: Hermes' ACP edit approval → same XENIO gate (path policy decides; no double prompt)
every tool result → transform_tool_result → API keys, tokens, private keys redacted
user messages after a decline → pre_llm_call adds a note naming the declined operation: "that applied only
  to that attempt; if the user asks again, call the tool again — XENIO will ask again" (≤3 user turns, and
  only until that operation is attempted again)
```

Two Hermes details shaped this:

- `pre_tool_call` hooks run in a worker thread limited to 30 s (`plugins.hook_callback_timeout`) that has no
  ACP approval channel. The plugin therefore never waits for the user itself; it returns `approve`, and Hermes
  asks from the agent thread. A hook that waited timed out, and Hermes then failed the hook closed for 60 s.
- Hermes' text for a declined action ends with "Do NOT retry — the user has explicitly rejected it." The text
  is fixed, and `transform_tool_result` does not run on blocked results. Without the `pre_llm_call` note, the
  model refused the user's *later, fresh* request for the same thing.

## Wrappers XENIO did NOT write (on purpose)

No second terminal, no second file reader/writer, no second web client or browser agent, no
memory store, no scheduler, no agent loop, no LLM calls. The five `xenio_*` filesystem tools exist only because
Hermes has no first-class directory/move/delete tools; `xenio_open_url` and `xenio_download_file`
only because Hermes cannot show a page in the user's own browser or save a file with checks. The V4 personal
tools exist because Hermes' Google skill needs far broader permissions than read-only email, and because tasks,
projects and notifications have no Hermes equivalent. (Hermes' kanban is a work queue for agents, and its
`todo` tool lasts one session.) The V5 `xenio_look_at_screen` exists because Hermes can only capture the
desktop through `computer_use`, which also clicks and types; it uses Hermes' own model access for the
picture, so there is still no second LLM integration. All run inside Hermes' own tool system.
