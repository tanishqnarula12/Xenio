"""Turn Hermes' Markdown replies into text that sounds right when spoken."""

from __future__ import annotations

import re

_FENCED_CODE = re.compile(r"```.*?(```|$)", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`]*)`")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_URL = re.compile(r"https?://\S+")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+", re.MULTILINE)
_QUOTE = re.compile(r"^\s*>\s?", re.MULTILINE)
_EMPHASIS = re.compile(r"(\*\*|__|\*|_|~~)(?=\S)(.+?)(?<=\S)\1")
_TABLE_RULE = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$", re.MULTILINE)


def to_speakable(markdown: str) -> str:
    text = _FENCED_CODE.sub(" (I've shown the code on screen.) ", markdown)
    text = _IMAGE.sub(r"\1", text)
    text = _LINK.sub(r"\1", text)
    text = _URL.sub("a link", text)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _TABLE_RULE.sub("", text)
    text = _HEADING.sub("", text)
    text = _QUOTE.sub("", text)
    text = _BULLET.sub("", text)
    text = _EMPHASIS.sub(r"\2", text)
    text = text.replace("|", ", ")
    lines = [line.strip() for line in text.splitlines()]
    # A line without closing punctuation still needs a pause when spoken.
    text = " ".join(line if line[-1:] in ".!?:;," else f"{line}." for line in lines if line)
    return re.sub(r"\s+", " ", text).strip()


def strip_wake_words(transcript: str, name: str) -> str:
    """Drop a leading "Hey Xenio," from a transcript (also Whisper's spellings Zenio/Xenia/Zenia),
    so a wake phrase said mid-conversation is not sent to Hermes. '' = only the wake phrase."""
    core = re.escape(name.lower()[1:]) if len(name) > 1 else re.escape(name.lower())
    first = name[:1].lower()
    first_class = "[xz]" if first in "xz" else re.escape(first)
    pattern = rf"^\s*(?:(?:hey|hi|hello|ok|okay)[\s,]+)?{first_class}{core[:-1]}\w?\b[\s,.!?;:]*"
    return re.sub(pattern, "", transcript, count=1, flags=re.IGNORECASE).strip()


_EXTENSIONS = (r"txt|md|pdf|docx?|xlsx?|pptx?|csv|json|ya?ml|xml|html?|css|js|jsx|ts|tsx|py|ipynb|java|cs|cpp|go|rs"
               r"|rb|php|sh|bat|ps1|ini|cfg|toml|log|png|jpe?g|gif|svg|webp|mp3|wav|mp4|zip|rar|7z")
_DOT_WORD = re.compile(rf"\b([\w-]+)\s+dot\s+({_EXTENSIONS})\b", re.IGNORECASE)          # "hello dot txt"
_SPACED_DOT = re.compile(rf"\b([\w-]+)\.\s+({_EXTENSIONS})\b", re.IGNORECASE)              # "hello. txt"
_GLUED_EXT = re.compile(rf"\b([A-Za-z0-9_-]*[a-z][A-Za-z0-9_-]*?)({_EXTENSIONS.upper()})\b")  # "HelloTXT"


def normalize_spoken_filenames(text: str) -> str:
    """Speech recognition writes "hello.txt" as "HelloTXT", "hello dot txt" or "hello. txt";
    turn those back into file names before the request reaches Hermes."""
    text = _DOT_WORD.sub(lambda m: f"{m.group(1)}.{m.group(2).lower()}", text)
    text = _SPACED_DOT.sub(lambda m: f"{m.group(1)}.{m.group(2).lower()}", text)
    return _GLUED_EXT.sub(lambda m: f"{m.group(1)}.{m.group(2).lower()}", text)


_INTERRUPT_WORDS = re.compile(
    # "stops"/"stopped": speech recognition often hears "Xenio, stop." as "Xenio stops."
    r"^(?:\s*(?:stop(?:s|ped|ping)?|wait|cancel|hold on|enough|never ?mind|ok(?:ay)?)\s*(?:[,.!?;:]+|$))+", re.IGNORECASE
)


def strip_interrupt_words(transcript: str) -> str:
    """Drop leading "Stop." / "Wait," left over from an interruption. Only when punctuated or
    alone, so "Stop the music" is kept intact."""
    return _INTERRUPT_WORDS.sub("", transcript, count=1).strip()


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def split_for_tts(text: str, max_chars: int = 400) -> list[str]:
    """Sentence-aligned segments of at most ``max_chars`` (the first kept short for fast start)."""
    segments: list[str] = []
    current = ""
    for sentence in _SENTENCE_END.split(text.strip()):
        sentence = sentence.strip()
        if not sentence:
            continue
        for piece in _hard_split(sentence, max_chars):
            limit = max_chars if segments else min(max_chars, 160)
            if current and len(current) + 1 + len(piece) > limit:
                segments.append(current)
                current = piece
            else:
                current = f"{current} {piece}".strip()
    if current:
        segments.append(current)
    return segments


def _hard_split(sentence: str, max_chars: int) -> list[str]:
    if len(sentence) <= max_chars:
        return [sentence]
    parts, current = [], ""
    for word in re.split(r"(?<=[,;:])\s+|\s+", sentence):
        if current and len(current) + 1 + len(word) > max_chars:
            parts.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        parts.append(current)
    return parts
