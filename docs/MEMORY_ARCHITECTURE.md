# XENIO memory architecture (V4)

XENIO keeps no memory system of its own. It uses **Hermes' native memory**, and adds a policy that decides
what is worth keeping and refuses secrets. Tasks and project notes are separate records in XENIO's own small
database (see [PERSONAL_INTEGRATIONS.md](PERSONAL_INTEGRATIONS.md)). They are not "memory".

## 1. Audit: what Hermes' memory is (Hermes Agent 0.21.5, checked in its source)

| Question | Answer |
|---|---|
| **What is stored** | Two curated lists of short text entries: `MEMORY.md` (the agent's notes: environment facts, conventions, lessons) and `USER.md` (the user profile: name, preferences, style). Nothing else: no transcripts, no embeddings. |
| **Where** | `%LOCALAPPDATA%\hermes\memories\MEMORY.md` and `USER.md` (Hermes' profile folder). Entries are separated by a line holding `§`. |
| **How memories are created** | Only through Hermes' `memory` tool (`add` / `replace` / `remove`, or one atomic batch of `operations`). The model calls it during a turn. Hermes also nudges the model to consider saving every 10 user turns (`memory.nudge_interval`), and a background-review fork can save on its own. |
| **How they are recalled** | Not by search. Both files are injected into the **system prompt** as a frozen snapshot when a session starts. Writes during a session reach the disk but do not change that session's prompt (this keeps the prompt cache intact). |
| **Persistent?** | Yes. They are plain files, kept across sessions and restarts. |
| **Searchable?** | The memory itself is not; it is always fully in the prompt. Past *conversations* are searchable through Hermes' `session_search` tool (SQLite FTS5 over `state.db`). |
| **Updatable / deletable** | Yes: `replace` overwrites a whole entry chosen by a substring; `remove` deletes it. A batch applies atomically against the size limit. |
| **Size** | Small by design: `MEMORY.md` 2,200 characters, `USER.md` 1,375 characters (`memory.memory_char_limit` / `user_char_limit`). A full store rejects an `add` until stale entries are removed. |
| **Scope** | Per Hermes profile: every session of that profile (XENIO, the Hermes CLI, the gateway) shares the same two files. There is no per-session memory. |
| **Security controls in Hermes** | A strict injection/exfiltration scanner on every entry (memory enters the system prompt, so a poisoned entry would persist). File locks and a "drift" guard that refuses to overwrite a file edited outside the tool. An optional `memory.write_approval` gate (off by default) that stages writes for review. |

**Verdict:** native memory is sufficient for what memory should hold: a few lasting facts about the user
and their world. It is not a database. Structured, growing records (tasks, project decisions, files, issues)
would overflow 2,200 characters and do not need to be in every prompt. XENIO V4 keeps those in
`personal.db` and looks them up with tools. No Mem0 or other external memory was added.

Hermes ships optional memory-provider plugins (holographic, honcho, mem0, supermemory and others). None is
enabled, and V4 does not need one.

## 2. XENIO memory policy

**XENIO remembers** (when the user says so, or when it is a clear lasting fact):
project names and goals, technical preferences, recurring workflows, important decisions, preferred tools,
how the user wants answers, standing instructions ("from now on...").

**XENIO never stores:** API keys, passwords, tokens, private keys, card numbers, session cookies, recovery
or 2FA codes, `.env` contents or any credential. This holds even when the user explicitly asks. Raw
conversation transcripts are not stored as memory either: Hermes keeps conversations in its own session store
for normal context, and that is not memory.

**Where things go:**

| Kind | Store |
|---|---|
| About the user (name, preferences, standing instructions) | Hermes memory, `USER.md` |
| Environment facts, conventions | Hermes memory, `MEMORY.md` |
| About one project (stack, database, decisions, relevant files) | `xenio_project` note in `personal.db` |
| Things to do | `xenio_tasks` in `personal.db` |
| What happened in a conversation | Nothing: Hermes' session history and `session_search` cover it |

## 3. Memory confidence: what is worth saving

Every `memory` tool call passes through the xenio-guard plugin before Hermes runs it
(`app.safety.policy.assess_memory_write`). The plugin knows the user's own words for the current turn
(Hermes passes them to the `pre_llm_call` hook), so it can tell an explicit request from the model saving
on its own (a nudge or background review).

| Verdict | When | Result |
|---|---|---|
| `SECRET` | a key, token, password, card number (Luhn-checked), cookie or recovery code, in any form ("my API key is ...") | **blocked** always; the model tells the user it was not saved |
| `DUPLICATE` | an `add` that says the same as an existing entry (word overlap of 75% or more) | not saved; if the user asked, the model says it is already remembered |
| `LOW_VALUE` | **the user did not ask**, and the entry is temporary ("right now", "today", "this session"), an event log ("user asked...", "initially tried..."), a guess ("appears to", "probably"), a transcript, or longer than 400 characters | not saved, silently (the user never asked) |
| `HIGH_VALUE` | everything else: an explicit request ("remember...", "from now on...", "I prefer...", "my main project is..."), or a lasting fact | saved by Hermes |

Explicit requests can still be refused as `SECRET` or `DUPLICATE`, and a transcript or an entry over 400
characters is refused even when asked: memory holds short facts. `remove` is always allowed.

If the same turn read outside content that tried to give instructions (an email, a web page, a document),
every memory write needs the user's spoken yes (see [PROMPT_INJECTION_SECURITY.md](PROMPT_INJECTION_SECURITY.md)).
This stops a page from planting a "memory".

**If uncertain, XENIO does not store.** For a model-initiated save, any of the low-value signals is enough
to drop it.

## 4. Freshness

Hermes freezes memory into the prompt at session start, and XENIO resumes its last session after a restart.
So the xenio-guard plugin adds entries saved during the current XENIO run to its per-turn
`[XENIO context]` line. A new memory is visible at once, even after Hermes compresses a long conversation.
A brand-new session (`/new`, or after a model change) loads everything from disk.

## 5. What the user can see and change

* **Mission Control > Memory (V3):** every entry, searchable, with Hermes' real usage ("514 of 1,375 characters");
  **correct**, **delete** or **add** an entry. XENIO writes Hermes' files exactly as Hermes' memory tool does
  (`app/memory/files.py`: an exclusive lock on `<file>.lock`, a re-read under that lock, an atomic replace, `§`
  between entries, owner-only permissions), so a save by Hermes and an edit here never lose each other's entries.
  The memory policy holds for these edits too (no secrets even when typed by the user, at most 400 characters,
  within the size limit). Entries are addressed by a hash of their text: an entry Hermes changed meanwhile is
  refused ("reload") instead of overwritten. Activity records "memory changed", never the text.
* A conversation that is already running keeps the memory block it started with (Hermes freezes it). The guard plugin
  therefore lists, on the next turn, the entries removed or corrected since it started ("do not rely on them").
* `/memory [words]` in text mode lists the stored entries (`app/memory/manager.py`, read-only, no model call).
* "Forget that ..." makes the model call `memory remove`.
* The files are plain text: `~/.hermes/memories/` (macOS) or `%LOCALAPPDATA%\hermes\memories\` (Windows).

What is **not** memory, and where it is: projects and tasks (`personal.db`, looked up by tools and by the V3 context
engine, `app/context.py`); conversations (Hermes' session history); what XENIO did (Mission Control's Activity, kept
as long as Settings says, never with what the user typed or said); the screen (never kept).
* Before V4, `USER.md` held three near-identical auto-saved entries (1,287 of 1,375 characters). They were
  merged into three distinct entries (514 characters); the original is backed up in
  `%LOCALAPPDATA%\xenio\backups\USER.md.before-v4-*`.

## 6. Tests

`tests/test_v4_security.py`: memory value verdicts, duplicates, batches, explicit-request detection, secret
blocking at the policy level, and the plugin using the user's own words. Live: V4 acceptance tests 1
(remember, then recall in a **new** session) and 8 (a fake API key is refused and never written anywhere).
