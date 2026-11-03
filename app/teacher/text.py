"""Text from a model, made safe to show: nothing ends in the middle of a word (seen in real lessons: "blue giv", "550 cup", "at $")."""

from __future__ import annotations


def cut(value, limit: int = 600) -> str:
    """One line, at most `limit` characters: ending at a sentence when one ends near the limit, or else at a word."""
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    part = text[:limit]
    end = max(part.rfind(". "), part.rfind("? "), part.rfind("! "), part.rfind("। "))
    if end > limit * 0.6:
        return part[:end + 1]
    return part[:limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"
