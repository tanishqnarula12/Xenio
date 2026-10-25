"""Hermes skills, as Mission Control shows and manages them (V3).

A Hermes skill is a folder ``<HERMES_HOME>/skills/<category>/<name>/`` with a ``SKILL.md`` (front matter: name,
description, version, platforms, prerequisites) and optional ``references/``, ``templates/``, ``scripts/`` and
``assets/``. Skills are instructions the model can load; a script inside one only runs through a tool call, which
XENIO's guard checks like any other command.

What is real here:

* availability: the skill's platforms against this Mac, and its prerequisite commands found on PATH;
* on/off: Hermes' own ``skills.disabled`` list in its config.yaml, applied by Hermes on every call - changed with
  ``hermes config set`` after XENIO backs the file up, never by editing the YAML by hand. Hermes' essential skill
  (``hermes-agent``) cannot be switched off, as in Hermes itself;
* what changed recently (file times), and whether a skill carries scripts.

Installing or updating skills from outside is NOT offered: that would download instructions and code; XENIO leaves it
to ``hermes skills`` in a terminal, where the user sees what they install.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ESSENTIAL = frozenset({"hermes-agent"})  # Hermes' agent/skill_utils.ESSENTIAL_SKILLS
PLATFORMS = {"darwin": "macos", "linux": "linux", "win32": "windows"}
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")


class SkillError(Exception):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def _front_matter(text: str) -> dict:
    """The few fields XENIO reads, from the YAML front matter (no YAML library: plain lines, flow lists)."""
    match = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
    if not match:
        return {}
    block = match.group(1)

    def scalar(key: str) -> str:
        hit = re.search(rf"(?m)^{key}:\s*(.+)$", block)
        return hit.group(1).strip().strip("'\"") if hit else ""

    def flow_list(raw: str) -> list[str]:
        raw = raw.strip()
        if raw.startswith("[") and raw.endswith("]"):
            return [x.strip().strip("'\"") for x in raw[1:-1].split(",") if x.strip()]
        return [raw.strip("'\"")] if raw else []

    commands = re.search(r"(?m)^prerequisites:\s*\n(?:[ \t]+.*\n)*?[ \t]+commands:\s*(.+)$", block)
    return {"name": scalar("name"), "description": scalar("description"), "version": scalar("version"),
            "author": scalar("author"), "platforms": flow_list(scalar("platforms")),
            "commands": flow_list(commands.group(1)) if commands else []}


def list_skills(hermes_home: Path, disabled: set[str], limit: int = 300) -> list[dict]:
    here = PLATFORMS.get(sys.platform, sys.platform)
    out = []
    for path in sorted((hermes_home / "skills").glob("*/*/SKILL.md"))[:limit]:
        try:
            meta = _front_matter(path.read_text(encoding="utf-8", errors="replace")[:4000])
        except OSError:
            continue
        folder = path.parent
        name = meta.get("name") or folder.name
        files = [p for p in folder.rglob("*") if p.is_file()][:200]
        scripts = [str(p.relative_to(folder)) for p in files if "scripts" in p.relative_to(folder).parts]
        missing = [c for c in meta.get("commands") or [] if not shutil.which(c)]
        platform_ok = not meta.get("platforms") or here in [p.lower() for p in meta["platforms"]]
        changed = max((p.stat().st_mtime for p in files), default=path.stat().st_mtime)
        if not platform_ok:
            availability = f"not for this computer (made for {', '.join(meta['platforms'])})"
        elif missing:
            availability = f"needs {', '.join(missing)} (not installed)"
        else:
            availability = "ready"
        out.append({"name": name, "category": folder.parent.name, "description": meta.get("description", "")[:240],
                    "version": meta.get("version") or None, "author": meta.get("author") or None,
                    "enabled": name not in disabled, "essential": name in ESSENTIAL,
                    "available": platform_ok and not missing, "availability": availability,
                    "requires": meta.get("commands") or [], "scripts": scripts[:10],
                    "changed": datetime.fromtimestamp(changed).isoformat(timespec="seconds")})
    return out


def _run(hermes: str, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run([hermes, "config", *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


def disabled_skills(hermes: str) -> set[str]:
    """Hermes' skills.disabled list (empty when not set)."""
    done = _run(hermes, "get", "skills.disabled", "--json")
    text = (done.stdout or "").strip()
    if done.returncode != 0 or not text or "not set" in text:
        return set()
    try:
        value = json.loads(text)
    except ValueError:
        return set()
    if isinstance(value, str):
        return {value} if value else set()
    return {str(v) for v in value} if isinstance(value, list) else set()


def set_enabled(hermes: str, hermes_home: Path, backup_dir: Path, name: str, enabled: bool) -> set[str]:
    """Switch one installed skill on or off in Hermes' config (backed up first). Returns the new disabled list."""
    if not NAME.match(name or ""):
        raise SkillError("not a skill name")
    installed = {s["name"] for s in list_skills(hermes_home, set())}
    if name not in installed:
        raise SkillError(f"no installed skill called {name!r}", 404)
    if name in ESSENTIAL and not enabled:
        raise SkillError(f"{name} is essential to Hermes and cannot be switched off")
    disabled = disabled_skills(hermes)
    wanted = (disabled - {name}) if enabled else (disabled | {name})
    if wanted == disabled:
        return disabled
    config = hermes_home / "config.yaml"
    if config.is_file():
        backup_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(config, backup_dir / f"hermes-config-{time.strftime('%Y%m%d-%H%M%S')}-skills.yaml")
    done = _run(hermes, "set", "skills.disabled", json.dumps(sorted(wanted)))
    if done.returncode != 0:
        raise SkillError(f"Hermes did not take it: {(done.stderr or done.stdout).strip()[:200]}", 502)
    now = disabled_skills(hermes)
    if now != wanted:
        raise SkillError("Hermes' config did not change as expected; nothing else was touched", 502)
    return now
