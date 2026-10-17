"""Structured, secret-redacting logging.

Every record is rendered as ``time [LEVEL] logger: message key=value ...`` and the *final*
string (message, fields and any traceback) passes through the redactor, so a secret cannot
reach a log file or the console even if it ends up in an exception message.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Iterable
from logging.handlers import RotatingFileHandler
from pathlib import Path

_PATTERNS = (
    re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bnbh_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)\b\s*[=:]\s*)[^\s,;]+"),
)


class Redactor:
    def __init__(self, secrets: Iterable[str] = ()):
        # Longest first so a secret containing another is masked whole.
        self._secrets = sorted({s for s in secrets if s and len(s) >= 6}, key=len, reverse=True)

    def __call__(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        for pattern in _PATTERNS:
            text = pattern.sub(lambda m: (m.group(1) if m.groups() else "") + "***", text)
        return text


class StructuredFormatter(logging.Formatter):
    def __init__(self, redactor: Redactor):
        super().__init__("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        self._redact = redactor

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        fields = getattr(record, "fields", None)
        if fields:
            rendered = " ".join(f"{k}={_render(v)}" for k, v in fields.items())
            head, sep, tail = text.partition("\n")
            text = f"{head} {rendered}{sep}{tail}"
        return self._redact(text)


def _render(value: object) -> str:
    s = str(value)
    return f'"{s}"' if (" " in s or not s) else s


def event(logger: logging.Logger, message: str, level: int = logging.INFO, **fields: object) -> None:
    """Log one structured event: ``event(log, "TTS started", chars=120)``."""
    logger.log(level, message, extra={"fields": fields})


def setup_logging(log_dir: Path, secrets: Iterable[str], level: str = "INFO", verbose: bool = False) -> Path:
    """Configure the ``xenio`` logger tree. Returns the log file path."""
    redactor = Redactor(secrets)
    formatter = StructuredFormatter(redactor)

    root = logging.getLogger("xenio")
    root.handlers.clear()
    root.setLevel(logging.DEBUG)
    root.propagate = False

    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "xenio.log"
    file_handler = RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file_handler.setLevel(getattr(logging, level, logging.INFO))
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.INFO if verbose else logging.WARNING)
    console.setFormatter(formatter)
    root.addHandler(console)

    # Third-party HTTP loggers can print request URLs/headers at DEBUG; keep them quiet.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return log_file
