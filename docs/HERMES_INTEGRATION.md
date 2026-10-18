# Hermes integration — what is installed and how XENIO talks to it

Everything below was observed on this machine on 2026-10-02 (commands and source files cited),
not assumed. Re-check after `hermes update`.

## 1. Environment

| Item | Value | How it was found |
|---|---|---|
| OS | Windows 11 Home Single Language 10.0.26200 | session environment |
| Python (XENIO) | 3.14.2 (`python`, Microsoft Store build) | `python --version` |
| Node.js / npm | v24.11.1 / 11.6.2 | `node --version`, `npm --version` |
| Hermes Agent | v0.21.5+5764.g0cdba5e (2026.9.24), git install, 1 commit behind upstream | `hermes --version` |
| Hermes executable | `C:\Users\aniln\AppData\Local\hermes\bin\hermes.exe` (also `hermes.cmd`, `hermes-acp.exe`) | `where.exe hermes` |
| Hermes source | `C:\Users\aniln\AppData\Local\hermes\hermes-agent` | `hermes --version` |
| Hermes Python | bundled 3.14.7, venv under `...\hermes\installs\<id>\environments\<id>\venv` | `hermes --print-runtime-command`, `installs\...\facts.json` |
| Hermes home (`HERMES_HOME`) | `C:\Users\aniln\AppData\Local\hermes` (`config.yaml`, `.env`, `memories\`, `plugins\`, `state.db`) | directory listing |
| Model / provider | `cohere/north-mini-code:free` via OpenRouter (`model.provider: openrouter`) | `hermes status`, `config.yaml` |
| Credentials in Hermes `.env` | `OPENROUTER_API_KEY`, `TELEGRAM_*` (names only were read) | `.env` key names |
| Gateway | running (manual process), Telegram configured | `hermes status --full` |
| Approvals | `approvals.mode: smart` (default) — an LLM guardian triages flagged commands | `hermes_cli/config_defaults.py` |

## 2. Interfaces evaluated

| Interface | What it is | Decision |
|---|---|---|
| `hermes -z PROMPT` | one-shot, prints final text | **Rejected**: new process per message, and the help text says *"approvals are auto-bypassed"* — incompatible with XENIO's safety layer. |
| `hermes chat -q … --format stream-json --resume ID` | single query, JSONL events, resumable | **Rejected**: one Hermes process per turn; cold start measured at 12 s + 16 s before the turn even begins. |
| `hermes serve` | JSON-RPC/WebSocket backend for the Hermes desktop app (port 9119, auth required) | Not chosen: built for Hermes' own desktop client; ACP is the documented third-party client protocol. |
| `hermes gateway` (`api_server` platform) | OpenAI-compatible HTTP API inside the messaging gateway | Not chosen: not enabled here, and the gateway is the process serving Telegram. |
| `hermes mcp serve` | exposes Hermes conversations to *other agents* as MCP tools | Not a streaming chat-client protocol. Useful later. |
| `hermes proxy` | OpenAI-compatible proxy **to the LLM provider** | **Rejected**: would bypass the agent (no tools/memory) — a second LLM path. |
| **`hermes acp`** | Hermes as an **Agent Client Protocol** server: JSON-RPC 2.0, one JSON object per line, on stdio | **Chosen.** |

### Why ACP

* One long-lived Hermes process: warm turns start immediately (measured: ~3 s handshake + ~2–3 s
  session on a warm machine; 12 s + 16 s cold; a simple turn 5–13 s, dominated by the model).
* Streaming replies (`agent_message_chunk`) — needed for responsive voice.
* `session/cancel` — the hook for barge-in / Ctrl+C.
* `session/request_permission` — Hermes asks the *client* before dangerous commands and file
  edits, so XENIO's permission layer sits in the real decision path.
* `session/load` — the conversation survives XENIO restarts.
* `hermes acp --check` → `Hermes ACP check OK`; library: `agent-client-protocol` 0.9.0
  (schema ref v0.11.2, `PROTOCOL_VERSION = 1`).

## 3. Protocol as used by XENIO

Source of truth: `hermes-agent/acp_adapter/server.py`, `permissions.py`, `edit_approval.py`,
`venv/.../acp/meta.py`, `acp/schema.py`. Implementation: `app/hermes/bridge.py` (transport) and
`app/hermes/adapter.py` (conversation).

```
XENIO -> Hermes  initialize      {"protocolVersion":1,"clientCapabilities":{"fs":{"readTextFile":false,"writeTextFile":false},"terminal":false},"clientInfo":{...}}
Hermes -> XENIO  result          {"protocolVersion":1,"agentInfo":{"name":"hermes-agent","version":"0.21.5"},"agentCapabilities":{"loadSession":true,...}}
XENIO -> Hermes  session/load    {"sessionId":"<saved>","cwd":"C:\\Users\\aniln\\Xenio","mcpServers":[]}   -> null if unknown
XENIO -> Hermes  session/new     {"cwd":"...","mcpServers":[]}                                              -> {"sessionId":"..."}
XENIO -> Hermes  session/prompt  {"sessionId":"...","prompt":[{"type":"text","text":"hello"}]}
Hermes -> XENIO  session/update  {"sessionId":"...","update":{"sessionUpdate":"agent_message_chunk","messageId":"...","content":{"type":"text","text":"Hel"}}}
Hermes -> XENIO  session/request_permission (request)  {"sessionId":"...","toolCall":{...},"options":[{"optionId":"allow_once","kind":"allow_once"},...]}
XENIO -> Hermes  result          {"outcome":{"outcome":"selected","optionId":"allow_once"}}   or {"outcome":{"outcome":"cancelled"}}
Hermes -> XENIO  result (prompt) {"stopReason":"end_turn","usage":{...}}          ("cancelled" after session/cancel)
XENIO -> Hermes  session/cancel  {"sessionId":"..."}   (notification)
```

Notes learned the hard way:

* XENIO advertises **no** fs/terminal capabilities, so Hermes uses its own file and terminal tools.
* `session/load` replays history as `session/update` notifications *before* answering; XENIO only
  collects chunks while one of its own prompts is in flight, so replay is ignored.
* `session/resume` creates a new session on an unknown id but does not return the new id — so
  XENIO uses `session/load` and falls back to `session/new`.
* A prompt for a session Hermes no longer knows returns `stopReason: "refusal"` with no text;
  XENIO then forgets the session id and opens a fresh one next turn.
* XENIO only ever answers with `allow_once` / `reject_once`. It never selects "allow for session"
  or "allow always", so no standing permission is created inside Hermes.
* Closing Hermes' stdin is its graceful shutdown (it ends its ACP sessions and exits 0).
  On timeout XENIO kills the process tree (`taskkill /T /F`) because `hermes.exe` is a launcher.
* Hermes' own logs go to its stderr; XENIO keeps the last 40 lines for error messages and logs
  them at DEBUG through its redacting formatter.

### Permission requests Hermes sends

| `toolCall.kind` | When | What XENIO reads |
|---|---|---|
| `edit` | every `write_file` / `patch` in ACP's default mode ("Ask before edits") | `content[].{type:"diff",path,oldText,newText}` — `oldText: null` means a new file |
| `execute` | a command Hermes' own detector flags as dangerous | `rawInput.{command,description}` |
| `execute` with `rawInput.command = "<tool> (plugin approval rule)"` | a `pre_tool_call` plugin escalated a tool call (see §4) | `rawInput.description` holds the real action |

## 4. The safety gap and the `xenio-guard` plugin

Test (real Hermes, XENIO workspace, throwaway session): asking Hermes to delete a file it had
just created made it run `rm -f "<file>"` **without any permission request** — Hermes' detector
and smart guardian do not treat a single-file delete as dangerous. XENIO's policy says deletes are
CONFIRM_REQUIRED, so relying on Hermes' prompts alone was not enough.

Hermes-native fix: a plugin `pre_tool_call` hook may return `{"action": "approve", "message": …}`,
which sends the call through Hermes' *own* human-approval gate (`tools/approval.py:
request_tool_approval` → `_human_decision`; it bypasses the smart guardian and fails closed with no
human). Over ACP that gate is `session/request_permission`, answered by XENIO.

* Plugin source: `hermes_plugin/xenio-guard/` (rules for deletes, `git push`/history rewrites,
  package publishing, mail, DB drops, credential changes, killing processes, registry/services;
  `execute_code` deletes; messaging tools).
* Scope: active only when `XENIO_GUARD=1`, which XENIO sets on the `hermes acp` process it starts.
  The Telegram gateway and normal `hermes` sessions are unaffected.
* Install: `python -m app.main --install-guard` copies it to `HERMES_HOME\plugins\xenio-guard\`,
  backs up `config.yaml` to `%LOCALAPPDATA%\xenio\backups\`, and runs
  `hermes plugins enable xenio-guard --no-allow-tool-override`. Resulting config change (verified by
  diff): `plugins.enabled: [xenio-guard]`, `plugins.entries.xenio-guard.allow_tool_override: false`.
* Undo: `hermes plugins disable xenio-guard`.
* Re-test after install: the same delete produced a permission request, XENIO denied it, the file
  was **not** deleted, and Hermes replied that the deletion "was blocked by XENIO policy".

### V2 (2026-10-03): the plugin became XENIO's hands

`xenio-guard` v0.2 now checks **every** tool call with `app/safety/policy.py` (loaded from the
XENIO folder via `XENIO_ROOT`; fails closed to read-only tools if it cannot load), registers the
`xenio_*` filesystem tools under the toolset `xenio`, redacts secrets in `transform_tool_result`,
and adds a system-prompt section. Two facts learned while wiring it:

* Plugin toolsets are "on by default", but while plugin discovery is still running Hermes decides
  from `HERMES_HOME/cache/plugin_toolset_keys.json` — written by whichever Hermes process ran
  last (the Telegram gateway or a plain CLI run writes it without `xenio`). The first live test hit
  exactly that: the tools were registered but not offered to the model. Fix:
  `platform_toolsets.acp: [hermes-acp, xenio]` (set by `--install-guard`; verified with Hermes'
  own resolver that all `hermes-acp` toolsets are kept).
* Hermes freezes a session's system prompt when the session is created, so XENIO stores
  `policy_version` with the saved session id and starts a fresh session when the policy changes.
* Hermes' **Tool Search** (default `auto`) hides every non-core plugin tool behind
  `tool_search`/`tool_describe`/`tool_call`; the free model then answered "xenio_inspect_path is not
  available" instead of searching (observed live, and the Hermes docs warn about exactly this).
  There is no per-tool "keep visible" flag. Until Hermes 0.21 `--install-guard` set
  `tools.tool_search.enabled: ${XENIO_TOOL_SEARCH}` and XENIO started Hermes with `XENIO_TOOL_SEARCH=off`,
  so only XENIO's own Hermes process had it off.
* **Hermes 0.22 broke that (found 2026-10-18).** ACP sessions now run inside the profile's shared gateway, and
  the gateway *multiplexes profiles* (`gateway.multiplex_profiles: true`, the installer's default): a config
  `${VAR}` is then resolved from the profile's `.env` only, never from the process environment (Hermes'
  `_env_ref_lookup`, so one profile's secrets never leak into another). The placeholder stayed unexpanded, read as
  `auto`, and the model saw 19 tools: **no `xenio_*` tool at all**, plus the tool_search bridge. It called
  `xenio_project`/`xenio_engineering` from XENIO's prompt and got "Tool does not exist", then tried `tool_search`
  (which XENIO's guard asks about). Reproduced with Hermes' own resolver (`tests/test_chat_tools.py` runs it against
  the installed Hermes). Fix: `--install-guard` now writes `tools.tool_search.enabled: off` (it replaces the old
  placeholder, never a value you chose). Because one gateway runs every session of the profile, this applies to all
  of them: Hermes' own rarely used tools (todo_list, session_search, process_manage...) are then always offered too.
  Undo: `hermes config set tools.tool_search.enabled auto`. Live after the fix: 46 tools, all 26 `xenio_*` among them.
* **The engineering tool in the gateway.** XENIO handed the engineering service's socket and token to its
  `hermes acp` through the environment; in 0.22 the tool runs in the gateway, which never had them (and a gateway
  outlives the XENIO that started it). The service now also writes `engineering/token.json` (owner-only; a secret by
  name to XENIO's policy, so agents' file and terminal tools cannot read it), and the socket answers only XENIO
  itself, its own `hermes acp`, or the gateway process that carries the guard (checked with the socket's peer pid).
  This also closes an older gap: Hermes passes `XENIO_ENGINEERING_TOKEN` on to the commands its agents run
  (measured: `hermes_subprocess_env` keeps it), so an agent's command could have used it - its process is now refused.
* **A repeated reply (Hermes 0.22).** When a turn streams text before a tool call that never starts (XENIO's guard
  blocked it, or the user said no), Hermes' ACP projection keeps that text in its stream buffer, its "already shown?"
  prefix check fails, and it sends the whole final reply again (`acp_adapter/gateway_reply.py`, `_final_remainder`;
  captured live). XENIO's ACP client drops exactly that repeat (a "\n\n" chunk that repeats the end of what was just
  streamed), so text and voice show and speak it once. It is Hermes' bug; this is the narrowest place XENIO can fix it.
  Policy hooks run on the real tool name either way.

Full capability inventory and decisions: [HERMES_CAPABILITIES.md](HERMES_CAPABILITIES.md).

## 5. Hermes capabilities XENIO deliberately does not duplicate

| Capability | Hermes provides | XENIO does |
|---|---|---|
| Reasoning, tool calling, subagents (`delegation`), skills (`hermes skills`), MCP (`hermes mcp`), cron, web, terminal, files, browser | yes | nothing — it just sends the user's words |
| Memory | `MEMORY.md` + `USER.md` in `HERMES_HOME\memories` (entries separated by `\n§\n`, char-limited, curated by the agent's memory tool); currently empty | `MemoryManager.recall()` reads those files (read-only); `remember()` asks Hermes to save via its tool and only reports success if Hermes reports a completed memory tool call |
| Approvals | pattern detection + smart guardian + ACP prompts | answers the prompts; adds `xenio-guard` for its own policy |
| Voice | STT (local faster-whisper, Groq, OpenAI, Mistral…), TTS (Edge default…), wake word ("hey hermes"; openWakeWord / sherpa-onnx / Porcupine) | **Not used**: these are tied to Hermes' own CLI/TUI/desktop surfaces, and their optional packages (faster-whisper, sherpa-onnx, openwakeword, sounddevice, edge-tts) are not installed in Hermes' venv. XENIO owns mic/speaker and uses the user's nabh.cloud STT/TTS. For the wake word XENIO uses the same engine family Hermes chose (sherpa-onnx KWS, same model, same sensitivity mapping). |

## 6. Configuration Hermes needs from XENIO

None. Over ACP Hermes uses its own `config.yaml` and `.env` (model, provider, keys). That is why
`.env.example` has **no** `HERMES_URL` / `HERMES_API_KEY`. `HERMES_COMMAND` (default `hermes acp`)
is XENIO's own setting for how to launch it. `HERMES_HOME` is honoured if you set it (Hermes' own
variable) — XENIO uses it to find memory files and to install the guard plugin.
