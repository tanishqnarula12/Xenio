"""Source awareness: which web pages an answer is based on.

The xenio-guard plugin records every web source Hermes uses (search results, pages read with
web_extract, pages opened in Hermes' browser, pages opened for the user, downloads) as one JSON line
in ``<data dir>/sources.jsonl``: title, link, site, retrieval time and a short excerpt - never whole
pages. XENIO reads the lines added during a turn and shows them under the answer, so the user can
check where a fact came from and nothing can pretend a search happened when it did not.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("xenio.sources")

# Most informative first: a page that was actually read beats a search hit for the same link.
_KIND_ORDER = {"page_read": 0, "page_visited": 1, "opened_for_user": 2, "downloaded": 3, "search_result": 4}
_KIND_LABEL = {"page_read": "read", "page_visited": "visited", "opened_for_user": "opened for you",
               "downloaded": "downloaded", "search_result": "search result"}


@dataclass(frozen=True)
class Source:
    url: str
    title: str = ""
    site: str = ""
    kind: str = "search_result"
    retrieved: str = ""
    excerpt: str = ""
    query: str = ""


class SourceLog:
    def __init__(self, path: Path | None):
        self.path = path

    def mark(self) -> int:
        """Current end of the log; pass it to ``since`` after the turn."""
        try:
            return self.path.stat().st_size if self.path is not None else 0
        except OSError:
            return 0

    def since(self, offset: int, session_id: str | None = None) -> list[Source]:
        """Sources added after ``offset``. With ``session_id``, only that Hermes session's: every XENIO
        process shares the file, so a second XENIO running at the same time must not leak into this answer."""
        if self.path is None:
            return []
        try:
            with open(self.path, "rb") as fh:
                fh.seek(max(0, offset))
                raw = fh.read().decode("utf-8", errors="replace")
        except OSError:
            return []
        best: dict[str, Source] = {}
        for line in raw.splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if not isinstance(item, dict) or not isinstance(item.get("url"), str) or not item["url"].strip():
                continue
            if session_id and item.get("session_id") and item["session_id"] != session_id:
                continue
            source = Source(url=item["url"].strip(), title=str(item.get("title") or ""), site=str(item.get("site") or ""),
                            kind=str(item.get("kind") or "search_result"), retrieved=str(item.get("retrieved") or ""),
                            excerpt=str(item.get("excerpt") or ""), query=str(item.get("query") or ""))
            key = source.url.rstrip("/")
            current = best.get(key)
            if current is None or _KIND_ORDER.get(source.kind, 9) < _KIND_ORDER.get(current.kind, 9):
                best[key] = source if (source.title or current is None) else Source(**{**source.__dict__, "title": current.title})
        return sorted(best.values(), key=lambda s: _KIND_ORDER.get(s.kind, 9))


def describe_sources(sources: list[Source], limit: int = 5) -> list[str]:
    """Lines shown under an answer: the pages actually used; search hits only when no page was opened."""
    used = [s for s in sources if s.kind != "search_result"]
    shown = used[:limit] if used else sources[:3]
    if not shown:
        return []
    lines = ["   sources:"]
    for s in shown:
        name = s.title or s.site or s.url
        lines.append(f"   - {name[:80]} ({_KIND_LABEL.get(s.kind, s.kind)}) {s.url[:140]}")
    return lines
