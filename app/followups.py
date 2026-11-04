"""The next steps a reply offers, as buttons under it (as in the Teacher's style: one thing at a time, and the user picks what comes next). The model ends a reply with one line,
<<next: Walk through an example | Show it as a diagram>>. That line is never shown as words and never spoken: it is taken out here, also while a reply is streaming."""

from __future__ import annotations

import re

MARK = "<<next:"
_LINE = re.compile(r"\s*<<next:(.*?)(?:>>|\Z)\s*", re.DOTALL)
MAX_CHOICES = 3
MAX_CHARS = 70


def split(text: str) -> tuple[str, list[str]]:
    """The reply without the line, and the choices it offered."""
    found = _LINE.findall(text or "")
    if not found:
        return text or "", []
    choices: list[str] = []
    for choice in found[-1].split("|"):
        words = " ".join(choice.split())[:MAX_CHARS].strip()
        if words and words.lower() not in (c.lower() for c in choices):
            choices.append(words)
    return _LINE.sub("\n", text).strip(), choices[:MAX_CHOICES]


class StreamFilter:
    """Words as they arrive, minus the line. What could still turn out to be the start of it waits for the next piece."""

    def __init__(self) -> None:
        self._held = ""
        self._hiding = False

    def feed(self, piece: str) -> str:
        if self._hiding:
            return ""
        text = self._held + (piece or "")
        self._held = ""
        at = text.find(MARK)
        if at >= 0:
            self._hiding = True
            return text[:at].rstrip()
        keep = next((size for size in range(min(len(MARK) - 1, len(text)), 0, -1) if text.endswith(MARK[:size])), 0)
        body = text[:len(text) - keep]
        words = body.rstrip()  # the blank space before the line must not stay behind it
        self._held = text[len(words):]
        return words

    def flush(self) -> str:
        """The end of the reply: what was held back was only words after all."""
        held, self._held = self._held, ""
        return "" if self._hiding else held
