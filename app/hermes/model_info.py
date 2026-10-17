"""Which model Hermes is configured to use: main model and fallback chain (read-only).

Read straight from Hermes' ``config.yaml`` (``model.provider`` / ``model.default`` and the
top-level ``fallback_providers`` list) with a tiny line parser, so XENIO needs no YAML package and
never changes Hermes' model choice. Used to start a fresh Hermes session when the model changed
(Hermes keeps the model a session was created with) and to show the chain in ``--health``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ModelSetup:
    provider: str = ""
    model: str = ""
    reasoning_effort: str = ""
    fallbacks: tuple[tuple[str, str], ...] = field(default_factory=tuple)  # (provider, model)

    @property
    def signature(self) -> str:
        return f"{self.provider}:{self.model}" if self.model else ""

    def describe(self) -> str:
        main = f"{self.model} via {self.provider or 'auto'}" if self.model else "Hermes default"
        if self.reasoning_effort:
            main += f" (reasoning {self.reasoning_effort})"
        chain = ", ".join(f"{m} via {p}" for p, m in self.fallbacks)
        return main + (f"; if it fails or hits a limit: {chain}" if chain else "; no fallback models")


def _block(text: str, name: str) -> str:
    """The indented lines under a top-level ``name:`` key (blank and indented comment lines included)."""
    m = re.search(rf"^{re.escape(name)}:[ \t]*\n((?:[ \t]+[^\n]*\n|[ \t]*\n)*)", text, re.M)
    return m.group(1) if m else ""


def _value(block: str, key: str, indent: str = "  ") -> str:
    m = re.search(rf"^{indent}{re.escape(key)}:[ \t]*['\"]?([^'\"#\n]*)", block, re.M)
    return m.group(1).strip() if m else ""


def read_model_setup(hermes_home: Path) -> ModelSetup:
    try:
        text = (hermes_home / "config.yaml").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ModelSetup()
    text = text.replace("\r\n", "\n")
    if not text.endswith("\n"):
        text += "\n"
    model = _block(text, "model")
    agent = _block(text, "agent")
    fallbacks: list[tuple[str, str]] = []
    for item in re.split(r"^[ \t]*-[ \t]+", _block(text, "fallback_providers"), flags=re.M)[1:]:
        provider = re.search(r"^(?:[ \t]*)provider:[ \t]*['\"]?([^'\"#\n]*)", item, re.M)
        name = re.search(r"^(?:[ \t]*)model:[ \t]*['\"]?([^'\"#\n]*)", item, re.M)
        if name and name.group(1).strip():
            fallbacks.append(((provider.group(1).strip() if provider else ""), name.group(1).strip()))
    return ModelSetup(provider=_value(model, "provider"), model=_value(model, "default") or _value(model, "model"),
                      reasoning_effort=_value(agent, "reasoning_effort"), fallbacks=tuple(fallbacks))
