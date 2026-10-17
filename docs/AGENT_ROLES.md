# Agent roles (V6)

A role is a job description plus an **enforced** set of capabilities. Roles are not always-running agents: XENIO
starts a Hermes session for a role only when a task needs it, and ends it when the task ends. Source:
`app/engineering/roles.py`.

## The eight roles

| Role | Does | Capabilities | Terminal | Writes |
|---|---|---|---|---|
| **planner** | analyses the request, inspects the project, splits the work into tasks with file scopes, acceptance criteria and risks | repository_read, project_inspection | read-only commands | nothing |
| **frontend** | UI: components, styling, accessibility, state, frontend tests | repository_read, project_inspection, scoped_file_write, scoped_tests | normal policy, inside its worktree | its task's files |
| **backend** | API endpoints, business logic, auth integration, validation, error handling, backend tests | same as frontend | same | its task's files |
| **database** | schema, migrations, queries, indexes, data integrity, database tests. Migrations are written, never applied | same as frontend | same (no database clients) | its task's files |
| **tester** | finds test gaps, writes tests, runs suites, reproduces failures, verifies fixes | repository_read, project_inspection, test_execution, scoped_file_write | normal policy, inside its worktree | test files in its scope; never deletes, skips or weakens tests |
| **security** | reviews auth, secrets, validation, injection, path traversal, deserialization, files, dependencies, configuration, debug endpoints, permissions, prompt injection, sensitive logging | repository_read, security_checks, diff_review | read-only commands | nothing |
| **reviewer** | reviews the diff for correctness, regressions, maintainability and the acceptance criteria | repository_read, diff_review, project_inspection | read-only commands | nothing |
| **docs** | updates documentation the change affects | repository_read, docs_write | none | Markdown / `docs/` files only |

## Capabilities

| Capability | Hermes tools | Terminal mode |
|---|---|---|
| repository_read | read_file, search_files, xenio_list_directory, xenio_inspect_path, skills_list, skill_view, todo_list | — |
| project_inspection, diff_review, security_checks | terminal | read (READ-level commands only: `ls`, `cat`, `grep`, `git diff/log/show`...) |
| test_execution, scoped_tests | terminal, process_manage | test (READ-level + test runners) |
| scoped_file_write | write_file, patch, xenio_create_directory, terminal | write (normal policy, inside the worktree) |
| docs_write | write_file, patch, xenio_create_directory | — |
| web_research | web_search, web_extract | — (off by default for every role) |

**Never available to any engineering session** (whatever the configuration): `delegate_task` (no hidden second level
of agents), `memory`, `cronjob`, `send_message`, `computer_use`, `skill_manage`, browser automation, desktop control,
`xenio_delete_path`, `xenio_download_file`, `xenio_schedule`, `xenio_open_url`, and XENIO's personal tools (calendar,
email, tasks...). `execute_code` is not in any capability.

Enforcement is in `xenio-guard` (`policy.classify_engineering`), for every tool call of a registered session:
the tool must be in the role's set; write targets must resolve (symlinks included) inside the task's worktree and
inside the task's `allowed_paths`; `.git` internals are never written; documentation-only roles write only
documentation; terminal commands must run inside the worktree and, for read-only roles, be READ-level; for every role
git write commands, package installs, network tools, deployment/infrastructure tools and database clients are
blocked. Anything the normal policy blocks (secrets, system folders...) stays blocked.

## Configuration

Built-in defaults need no file. To change a role, create `~/.local/share/xenio/engineering_roles.toml` (XENIO's data
folder; it is never read from the repository being worked on):

```toml
[engineering]
allow_paid = false          # default: only free (":free") or subscription models (openai-codex, copilot)

[roles.frontend]
enabled = false             # a project without a UI

[roles.backend]
model = "nvidia/nemotron-3-super-120b-a12b:free"   # pin a model for this role (free)
max_attempts = 2            # 1-3 attempts per task (provider errors, rejected results)

[roles.reviewer]
capabilities = ["repository_read", "diff_review", "project_inspection"]
```

Unknown capabilities are refused at load time. A role pinned to a paid model is refused when a run needs it
("not a free model"), never silently swapped for another model. A model Hermes cannot serve fails the task with
Hermes' reason; Hermes' own (free) fallback chain still applies to a role on the main model.
