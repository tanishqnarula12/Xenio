"""Who a commit XENIO makes is from.

Like Claude Code: the commit is yours (your own git name and email, from `git config --global`), and XENIO is named in a
`Co-Authored-By:` line, so GitHub shows you both. Without a git identity of yours, XENIO is the author and the line is
left out. Either way a commit XENIO made always says so.

GitHub shows a picture next to a co-author only when the email belongs to a GitHub account. XENIO has one, @xenio101,
and its email (xenio0120@gmail.com, verified there) is the default here, so XENIO's mark appears on every commit it made or helped with,
for anyone who runs XENIO. XENIO_GIT_EMAIL points it somewhere else.
"""

from __future__ import annotations

import functools
import os
import re
import subprocess

_EMAIL = re.compile(r"[^@\s<>]+@[^@\s<>]+")
ACCOUNT = "xenio0120@gmail.com"  # github.com/xenio101: GitHub links this address to that account, and shows its picture


def xenio() -> tuple[str, str]:
    """XENIO's own name and email."""
    name = " ".join(os.environ.get("XENIO_GIT_NAME", "").split())[:60] or "XENIO"
    email = os.environ.get("XENIO_GIT_EMAIL", "").strip()
    return name, email if _EMAIL.fullmatch(email) else ACCOUNT


@functools.lru_cache(maxsize=4)
def _global(key: str) -> str:
    try:
        done = subprocess.run(["git", "config", "--global", "--get", key], capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def you() -> tuple[str, str] | None:
    """Your git identity on this Mac, or None. XENIO_COMMIT_AS_XENIO=true keeps XENIO as the author regardless."""
    if os.environ.get("XENIO_COMMIT_AS_XENIO", "").strip().lower() in ("1", "true", "yes"):
        return None
    name, email = " ".join(_global("user.name").split())[:80], _global("user.email")
    return (name, email) if name and _EMAIL.fullmatch(email) else None


def author(fallback: tuple[str, str] | None = None) -> tuple[str, str]:
    """Who a XENIO commit is authored by: you when git knows you, else XENIO (or the given fallback)."""
    return you() or fallback or xenio()


def config(fallback: tuple[str, str] | None = None) -> list[str]:
    """`git -c` arguments that set the author of the next commit."""
    name, email = author(fallback)
    return ["-c", f"user.name={name}", "-c", f"user.email={email}"]


def message(text: str, fallback: tuple[str, str] | None = None) -> str:
    """The commit message, with XENIO as co-author when the commit is yours."""
    if you() is None:
        return text
    name, email = xenio()
    line = f"Co-Authored-By: {name} <{email}>"
    return text if line.lower() in text.lower() else f"{text.rstrip()}\n\n{line}"


__all__ = ["author", "config", "message", "xenio", "you"]
