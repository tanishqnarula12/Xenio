"""A good name for what the War Room makes: the project folder, its README title and its GitHub repository.

A real repository was called `build-a-landing-page-for-a-new-a-b69f`: the first 32 letters of the command and a random
code. A name says what the thing is, like `arts-college-landing-page`. The model that reads the job suggests one; this
is the check on it, and the fallback when there is none.
"""

from __future__ import annotations

import re
from pathlib import Path

MAX_WORDS, MAX_CHARS = 5, 40

# words that say how to make it, or who is asking, not what it is
_FILLER = set("""
a an the this that these those it its my our your me us we i you they them their his her
build make create write design develop generate code craft put set get do give show help add implement
please pls bro hey can could would should will just some new simple normal basic quick nice cool good great proper
use using used with without which what who where when how and or but so then also too very really
skills skill have has had already there here want wants need needs like as of in on at to from by into onto about
is are was be been being
research find compare analyse analyze review explain fix update under over
""".split())
_SPLIT_FOR = re.compile(r"\s+for\s+", re.I)
_CLAUSE = re.compile(r"[:;,.!?\n(]|\s+(?:with|using|that|which|where|so|and then)\s+", re.I)  # what comes after is detail, not the name


def slug(text: str) -> str:
    """Lower-case words joined by dashes, cut on a word, at most MAX_CHARS. "" when nothing is left."""
    words = re.findall(r"[a-z0-9]+", str(text or "").lower())
    out = ""
    for word in words:
        joined = f"{out}-{word}" if out else word
        if len(joined) > MAX_CHARS:
            break
        out = joined
    return out


def _content(text: str, limit: int, phrase: bool = False) -> list[str]:
    """The words that say what something is. With phrase, only the first run of them: "a new arts college normal page"
    is "arts college", not "arts college page"."""
    seen, words = set(), []
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        if word in _FILLER or word in seen or len(word) < 2 and not word.isdigit():
            if phrase and words and word in _FILLER:
                break
            continue
        seen.add(word)
        words.append(word)
        if len(words) == limit:
            break
    return words


def from_goal(goal: str) -> str:
    """A name worked out from the command alone: what it is for, then what it is. "project" when nothing is left."""
    first = _CLAUSE.split(str(goal or ""), maxsplit=1)[0]  # the first clause says what it is; the rest is detail
    parts = _SPLIT_FOR.split(first, maxsplit=1)  # "a landing page for an arts college": what it is, and what it is for
    head, tail = parts[0], parts[1] if len(parts) > 1 else ""
    what, purpose = _content(head, 4), _content(tail, 3, phrase=True)
    words = [w for w in [*purpose, *what] if w][:MAX_WORDS]
    words = list(dict.fromkeys(words))
    return slug(" ".join(words)) or "project"


def checked(suggested: str, goal: str) -> str:
    """A model's suggestion, if it is a usable name; otherwise the one from the command."""
    name = slug(suggested)
    return name if name and name != "project" and 1 <= name.count("-") + 1 <= 6 else from_goal(goal)


def title(name: str) -> str:
    """`arts-college-landing-page` -> `Arts College Landing Page`."""
    small = {"and", "of", "for", "the", "a", "an", "to", "in", "on"}
    words = [w for w in name.split("-") if w]
    return " ".join(w if i and w in small else ({"saas": "SaaS", "api": "API", "ui": "UI", "ai": "AI", "cli": "CLI", "crm": "CRM", "sql": "SQL", "pdf": "PDF", "ios": "iOS"}.get(w) or w.capitalize()) for i, w in enumerate(words)) or "Project"


def unique(root: Path, name: str) -> Path:
    """`root/name`, or `name-2`, `name-3` ... when that folder is taken."""
    folder, n = Path(root) / name, 2
    while folder.exists():
        folder, n = Path(root) / f"{name}-{n}", n + 1
    return folder


__all__ = ["checked", "from_goal", "slug", "title", "unique"]
