"""Git and GitHub capabilities.

Hermes has no Git or GitHub tool of its own: it runs ``git`` and the GitHub CLI ``gh`` through its
``terminal`` tool, and it ships GitHub skills (``hermes skills list``) that do the same. XENIO adds
no second Git client - only the policy the xenio-guard plugin applies to every such command
(``app.safety.policy._classify_git`` / ``classify_gh``):

    READ        git status/log/diff/show/branch --list, gh issue/pr/repo/run list|view, gh api GET
    SAFE_WRITE  git add/commit/fetch/stash, creating a branch, gh pr checkout, gh repo clone
    CONFIRM     git push/pull/merge/rebase/checkout, deleting a branch, gh issue/pr create|comment|edit,
                gh pr merge, other gh writes, gh api POST/PATCH/PUT
    HIGH_RISK   force push, push --delete, reset --hard, clean -f, branch -D, gh release create|edit|delete,
                gh repo edit|delete|rename|archive, secrets/variables/keys, gh auth login|logout, gh api DELETE
    blocked     gh auth token / --show-token (prints your GitHub token), reading credential files

Without ``gh`` (not installed on this PC), public GitHub data is read with web_extract or a GET
request to https://api.github.com - both READ.
"""

from __future__ import annotations

import shutil
import subprocess

from app.safety.policy import RiskLevel
from app.tools.base import Tool

_CMD = {"type": "object", "properties": {"command": {"type": "string"}}}

TOOLS: list[Tool] = [
    Tool("git.run", "Git commands for the current repository through Hermes' terminal (status, log, diff, branch, "
         "commit, push...). Each command is classified before it runs.", _CMD, RiskLevel.READ, "hermes", "terminal"),
    Tool("github.cli", "GitHub CLI 'gh' through Hermes' terminal (issues, pull requests, runs, releases). Needs "
         "'gh' installed and 'gh auth login' done by the user.", _CMD, RiskLevel.READ, "hermes", "terminal"),
]


def gh_status(timeout: float = 10.0) -> tuple[bool, bool, str]:
    """(installed, signed_in, detail) for the GitHub CLI. Never prints or returns a token."""
    exe = shutil.which("gh")
    if not exe:
        return False, False, "GitHub CLI 'gh' is not installed (public GitHub data is still readable through the web)"
    try:
        proc = subprocess.run([exe, "auth", "status"], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return True, False, f"'gh' is installed but did not answer ({type(exc).__name__})"
    if proc.returncode == 0:
        return True, True, "GitHub CLI signed in"
    return True, False, "GitHub CLI installed but not signed in (run: gh auth login)"
