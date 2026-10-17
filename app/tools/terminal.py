"""Terminal capabilities.

Execution is Hermes' own ``terminal`` tool (bash; Git for Windows on this PC) and its
``process_manage`` tool — XENIO does not run a second shell. What XENIO adds is the command
policy (``app.safety.policy.classify_command``), applied by the xenio-guard plugin before every
command:

    READ / SAFE_WRITE   run immediately      (python --version, git status, ls, tests, mkdir in your folders)
    CONFIRM_REQUIRED    asks you first       (git push, package installs, deletes, unknown programs)
    HIGH_RISK           asks for "confirm"   (recursive deletes outside your folders, formatting, credentials)
    blocked             never runs           (printing API keys/env, reading .env or SSH keys, deleting system folders)
"""

from __future__ import annotations

from app.safety.policy import RiskLevel, classify_command  # noqa: F401  (re-exported for callers)
from app.tools.base import Tool

SAFE_EXAMPLES = ("python --version", "node --version", "git status", "git log --oneline -5", "ls", "pytest", "npm test")

TOOLS: list[Tool] = [
    Tool("terminal.run", "Run a shell command (Hermes terminal; bash). Each command is classified before it runs.",
         {"type": "object", "properties": {"command": {"type": "string"}, "workdir": {"type": "string"},
                                           "timeout": {"type": "integer"}, "background": {"type": "boolean"}}},
         RiskLevel.CONFIRM_REQUIRED, "hermes", "terminal"),
    Tool("terminal.processes", "Inspect or stop background processes started by the terminal (Hermes process_manage).",
         {"type": "object", "properties": {"action": {"type": "string"}}}, RiskLevel.READ, "hermes", "process_manage"),
    Tool("terminal.python", "Run Python code that can call tools (Hermes execute_code). Read-only code runs; anything else is confirmed.",
         {"type": "object", "properties": {"code": {"type": "string"}}}, RiskLevel.CONFIRM_REQUIRED, "hermes", "execute_code"),
]
