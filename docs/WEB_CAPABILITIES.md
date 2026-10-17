# Web, browser and GitHub capabilities

Audited on 2026-10-03 against the installed Hermes Agent **v0.21.5** (`tools/web_tools.py`,
`tools/browser_tool*.py`, `tools/browser_use_cli.py`, `tools/browser_cdp_tool.py`,
`tools/browser_dialog_tool.py`, `tools/browser_vault_tool.py`, `toolsets.py`, `acp_adapter/tools.py`,
the Hermes `config.yaml` and `.env` key names, `hermes mcp list`, the `github` skill).

**Rule followed:** Hermes' own web and browser tools are reused, not duplicated. XENIO adds only
what Hermes does not have — opening a page in *your* browser and a checked download — plus the
permission rules and source tracking. The flow stays:

```
XENIO (voice/text) → Hermes (ACP) → Hermes web/browser tools → xenio-guard checks each call → result → XENIO
```

## What exists and how XENIO uses it

| Capability | Hermes implementation | On this PC | How XENIO uses it | Permission |
|---|---|---|---|---|
| Web search | `web_search` → `{title, url, description}` results. Backend ladder: Tavily, Perplexity, Exa, Parallel, Keenable, Firecrawl, SearXNG, Brave, **ddgs** (DuckDuckGo, keyless) | No search API keys configured → Hermes' **keyless free tier** (exa / parallel / firecrawl / keenable, rotating, failing over on rate limits). The Codex-native search plugin is disabled (search-only, broke page reading, invisible to XENIO's policy) | Reused as is | READ |
| Web fetching | `web_extract` → clean page text, 15 000 characters per page (head+tail truncated, full text stored), private-network (SSRF) and secret-in-link checks built in | Works (e.g. developers.strava.com); some sites return nothing usable (the Hermes docs site did once) — then the browser is used | Reused as is | READ |
| Browser automation | **Default "Browser Use" mode** (`browser.backend` unset + browser-harness present): one tool, `browser_exec`, runs Python with helpers `new_tab`, `goto_url`, `wait_for_load`, `page_info`, `js`, `fill_input`, `click_at_xy`, `capture_screenshot`, `cdp`, `ensure_real_tab` | Active. Engine = Hermes' packaged Chromium (local), **invisible**; closed after 120 s idle | Reused; every call is analysed helper by helper (see below) | per action |
| Step-by-step browser tools | `browser_navigate`, `browser_snapshot`, `browser_click`, `browser_type`, `browser_press`, `browser_scroll`, `browser_back`, `browser_get_images`, `browser_vision`, `browser_console` — used when `browser.backend: off` | Replaced by `browser_exec` here | Reused; clicks judged by the element's label from the last snapshot | per action |
| Navigation / links | `new_tab`/`goto_url` (or `browser_navigate`); links via `js(...)` / snapshot | ✓ | Reused | READ (http/https) |
| Interaction | `js("…click()")`, `fill_input`, `click_at_xy`, `browser_click/type/press` | ✓ | Reused | SAFE_WRITE → CONFIRM/HIGH by target |
| Screenshots | `capture_screenshot()` / `browser_vision` | ✓ (the current model is text-only, so Hermes works text-first) | Reused | READ |
| Raw DevTools | `cdp(...)` helper; `browser_cdp` tool only with a CDP endpoint (`/browser connect`) | helper only | Reused; method allow/deny list | per method |
| JS dialogs | `browser_dialog` (CDP backends) | not offered here | Reused | accept = CONFIRM |
| Saved logins | `browser_vault_*` | present in the toolset | Reused | HIGH_RISK |
| Real browser profile | `browser.use_real_profile` (drives a copy of *your* Chrome profile, logins included) | **off** — XENIO does not turn it on | — | — |
| Visible automation browser | `browser.headed` / `AGENT_BROWSER_HEADED` | off | `XENIO_SHOW_BROWSER=true` sets `AGENT_BROWSER_HEADED=1` for XENIO's Hermes only | — |
| Open a page in **your** browser | **not available** | — | **`xenio_open_url`** (added) | READ (http/https only) |
| Downloads | **no download tool** (only `curl -o` through the terminal) | — | **`xenio_download_file`** (added) | SAFE_WRITE; programs CONFIRM |
| Browser sessions | `browser_exec(session=…)` named, isolated sessions; cloud providers (Browserbase, Browser Use cloud, Firecrawl) need keys | local only | Reused | — |
| MCP browser integrations | `hermes mcp add …` | **none configured** | — | unknown `mcp_*` tools = CONFIRM |

## What XENIO added

### `xenio_open_url` (app/tools/web.py)
Opens an http(s) page in the user's default browser (`os.startfile`), so "open GeeksforGeeks on my
laptop" shows the page on screen. Anything else (`ms-settings:`, `file:`, `javascript:`, links with
a login or a token) is refused. Nothing is automated in the user's browser.

### `xenio_download_file` (app/tools/web.py)
| Check | Behaviour |
|---|---|
| Link | http/https only; checked again after redirects; no logins/tokens in the link |
| Destination | Downloads folder by default, or `destination` (folder or file); the path policy applies (secrets/system blocked, AppData asks) |
| File name | taken from the link (or the server's name when the link has none); `..`, `\`, `/`, forbidden characters and device names (`CON`, `NUL`…) removed |
| Overwrite | never, unless `overwrite=true` — which asks you first |
| Size | 200 MB default (`XENIO_DOWNLOAD_MAX_MB`, per call `max_mb`); checked on `Content-Length` and while streaming |
| Programs | `.exe .msi .bat .ps1 .js .jar .iso …` need confirmation; a server that slips in `setup.exe` for a nameless link is refused |
| Execution | **never** opened or run; written as `.xenio-part` then renamed; marked "downloaded from the internet" (Mark of the Web) so Windows warns before opening |
| Errors | DNS failure, timeout, refused connection, certificate, 401/403 (login needed), 404, 429 (rate limit), 5xx — each reported in plain words with "Nothing was saved." |

### Policy for web and browser actions (app/safety/policy.py)
`classify_url`, `classify_js`, `classify_browser_code`, `classify_cdp`, `classify_typing`,
`classify_download` — see [SECURITY_MODEL.md](SECURITY_MODEL.md). The calls from the first V3 bug
report (`new_tab(…)`, `page_info()`, `js("document.title")`, `js("console.log(…)")`) are all READ
now, so reading a page never asks; the old rule asked before every `browser_exec` call.

### Source awareness (app/sources.py + plugin)
The plugin records each source in `%LOCALAPPDATA%\xenio\sources.jsonl`: search results, pages read,
pages visited in Hermes' browser, pages opened for you, downloads. Each record holds the title,
link, site, retrieval time, a ≤300-character excerpt, the search query, and the Hermes session
and turn. XENIO prints the sources an answer used under the reply. Pages that failed to load are not
recorded. Nothing changes what Hermes sends to the model; `web_extract` already limits page size.

## Research workflow
Guidance in the plugin's system prompt (no second agent, no second LLM):
1. search (`web_search`), 2. read the 1–3 most relevant sources (`web_extract`), preferring official
docs, the project's own site/GitHub and standards bodies, 3. answer from what the sources say, keeping
their facts apart from XENIO's own conclusions, 4. name the sources, 5. never invent a source, quote,
link or search; say so when searching or reading failed. For "should I use X or Y for this project",
the code workflow below runs first so the comparison fits the project.

## Git and GitHub
| | Status | How |
|---|---|---|
| git | Git for Windows 2.53 | Hermes' `terminal` (bash); classified by `_classify_git` |
| GitHub CLI `gh` | **not installed** | the Hermes `github` skill uses `gh`; without it, public data is read with `web_extract` / GET `https://api.github.com/...` |
| GitHub skill | `software-development/github` (PRs, issues, reviews, repos, auth, CI) | used by Hermes when relevant; its commands are policed like any other |

Repository workflow (system prompt): find the repository → look at its structure and the relevant
files → `git status` → plan → change → run the tests → report the real result.

Permission summary: status/log/diff/branch list, `gh … list/view/diff/checks`, `gh api` GET = READ ·
add/commit/fetch/stash, creating a branch, `gh pr checkout` = SAFE_WRITE · push, pull, merge,
checkout, `branch -d`, `gh issue/pr create/comment/edit`, `gh pr merge`, `gh api` POST/PATCH =
CONFIRM · force push, `push --delete`, `reset --hard`, `branch -D`, `gh release …`, `gh repo
edit/delete/rename/archive`, secrets/variables/keys, `gh auth login`, `gh api` DELETE = HIGH_RISK ·
`gh auth token`, `--show-token` = blocked.

## Limitations
* Some sites block or defeat `web_extract`; XENIO then reads them in Hermes' browser or opens them for you.
* Search and page reading use Hermes' keyless free tier here (rate-limited public endpoints); a keyed
  backend (e.g. `TAVILY_API_KEY` in Hermes' `.env`) gives better results — your choice, it may cost money.
* Hermes' browser is a separate profile: it is not logged in to your accounts (by design).
* Private GitHub repositories need `gh` installed and signed in by you.
* Click targets are judged by their label/selector text; a coordinate click (`click_at_xy`) always asks.
