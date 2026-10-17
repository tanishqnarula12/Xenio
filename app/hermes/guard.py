"""Install / inspect the ``xenio-guard`` Hermes plugin (see hermes_plugin/xenio-guard).

Installing touches the user's Hermes setup in exactly two places, both reversible:
  1. copies the plugin to ``HERMES_HOME/plugins/xenio-guard/``
  2. ``hermes plugins enable xenio-guard`` (Hermes' own command; adds it to plugins.enabled)
``config.yaml`` is backed up to the XENIO data dir first.
Undo: ``hermes plugins disable xenio-guard`` (and delete the plugin folder).
"""

from __future__ import annotations

import filecmp
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from app.config import PROJECT_ROOT, default_hermes_home

PLUGIN_NAME = "xenio-guard"
TOOLSET = "xenio"  # the toolset the plugin registers XENIO's tools under
ACP_PLATFORM = "acp"
TOOL_SEARCH_PLACEHOLDER = "${XENIO_TOOL_SEARCH}"
SOURCE_DIR = PROJECT_ROOT / "hermes_plugin" / PLUGIN_NAME
PLUGIN_FILES = ("plugin.yaml", "__init__.py")


@dataclass
class GuardStatus:
    installed: bool
    up_to_date: bool
    enabled: bool
    toolset_enabled: bool = True
    tools_visible: bool = True  # False: Hermes' tool_search may hide XENIO's tools from the model

    @property
    def ok(self) -> bool:
        return self.installed and self.up_to_date and self.enabled and self.toolset_enabled


def _target(hermes_home: Path) -> Path:
    return hermes_home / "plugins" / PLUGIN_NAME


def _hermes_executable(command: tuple[str, ...]) -> str:
    return command[0] if command else "hermes"


def guard_live(data_dir: Path, hermes_home: Path | None = None) -> tuple[bool | None, str]:
    """Is the Hermes process that runs XENIO's sessions guarded right now?

    Hermes 0.22 runs ACP sessions inside its gateway. A gateway started outside XENIO (the Hermes app, `hermes
    gateway`, a one-off `hermes -z`) does not carry XENIO_GUARD, and XENIO's sessions would then run without XENIO's
    policy. (True, why) when the gateway in gateway.lock is the process the guard marked; (False, why) when another
    process holds it; (None, why) when no gateway runs (the next one XENIO starts will carry the guard)."""
    import json
    import os

    home = hermes_home or default_hermes_home()
    try:
        lock = json.loads((home / "gateway.lock").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "no Hermes gateway is running"
    pid = int(lock.get("pid") or 0)
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return None, "no Hermes gateway is running"
    try:
        marked = int(json.loads((data_dir / "guard.json").read_text(encoding="utf-8")).get("pid") or 0)
    except (OSError, ValueError):
        marked = 0
    if marked == pid or _process_env_has(pid, "XENIO_GUARD=1"):
        return True, f"the Hermes gateway (pid {pid}) carries XENIO's guard"
    return False, (f"a Hermes gateway (pid {pid}) is running that XENIO did not start, so XENIO's safety guard is not "
                   "active in it. Stop it with `hermes gateway stop` (or quit the Hermes app), then start XENIO again")


def _process_env_has(pid: int, entry: str) -> bool:
    """Whether a process of this user was started with ``entry`` in its environment (macOS/Linux ``ps eww``)."""
    try:
        out = subprocess.run(["ps", "eww", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return f" {entry} " in f" {out.stdout.strip()} "


def guard_status(hermes_command: tuple[str, ...], hermes_home: Path | None = None) -> GuardStatus:
    target = _target(hermes_home or default_hermes_home())
    installed = all((target / f).is_file() for f in PLUGIN_FILES)
    up_to_date = installed and all(filecmp.cmp(SOURCE_DIR / f, target / f, shallow=False) for f in PLUGIN_FILES)
    enabled = False
    if installed:
        try:
            result = subprocess.run(
                [_hermes_executable(hermes_command), "plugins", "show", PLUGIN_NAME],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
            )
            enabled = result.returncode == 0 and "enabled" in result.stdout.lower() and "not enabled" not in result.stdout.lower()
        except (OSError, subprocess.TimeoutExpired):
            enabled = False
    try:
        toolset_enabled = TOOLSET in (_acp_toolsets(hermes_command) or "")
    except (OSError, subprocess.TimeoutExpired):
        toolset_enabled = False
    home = hermes_home or default_hermes_home()
    try:
        tools_visible = TOOL_SEARCH_PLACEHOLDER in (home / "config.yaml").read_text(encoding="utf-8")
    except OSError:
        tools_visible = False
    return GuardStatus(installed, up_to_date, enabled, toolset_enabled, tools_visible)


def install_guard(hermes_command: tuple[str, ...], backup_dir: Path, hermes_home: Path | None = None) -> GuardStatus:
    home = hermes_home or default_hermes_home()
    config = home / "config.yaml"
    if config.is_file():
        backup_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(config, backup_dir / f"hermes-config-{time.strftime('%Y%m%d-%H%M%S')}.yaml")
    target = _target(home)
    target.mkdir(parents=True, exist_ok=True)
    for name in PLUGIN_FILES:
        shutil.copy2(SOURCE_DIR / name, target / name)
    result = subprocess.run(
        [_hermes_executable(hermes_command), "plugins", "enable", PLUGIN_NAME, "--no-allow-tool-override"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"`hermes plugins enable {PLUGIN_NAME}` failed: {(result.stderr or result.stdout).strip()[:300]}")
    _enable_toolset_for_acp(hermes_command)
    _scope_tool_search_switch(hermes_command, home)
    return guard_status(hermes_command, home)


def _tool_search_configured_by_user(hermes_home: Path) -> bool:
    config = hermes_home / "config.yaml"
    try:
        text = config.read_text(encoding="utf-8")
    except OSError:
        return False
    return "tool_search" in text and TOOL_SEARCH_PLACEHOLDER not in text


def _scope_tool_search_switch(hermes_command: tuple[str, ...], hermes_home: Path) -> None:
    """Hermes hides plugin tools behind ``tool_search`` by default, and weaker models then claim the
    tools don't exist (observed). Set ``tools.tool_search.enabled: ${XENIO_TOOL_SEARCH}``: XENIO's own
    Hermes gets ``off`` from its environment; every other Hermes process leaves the placeholder
    unexpanded, which Hermes reads as the default ``auto``. Never overrides a user's own setting."""
    if _tool_search_configured_by_user(hermes_home):
        return
    current = subprocess.run(
        [_hermes_executable(hermes_command), "config", "get", "tools.tool_search.enabled"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    ).stdout.strip()
    if current == TOOL_SEARCH_PLACEHOLDER:
        return
    result = subprocess.run(
        [_hermes_executable(hermes_command), "config", "set", "tools.tool_search.enabled", TOOL_SEARCH_PLACEHOLDER],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Could not scope Hermes' tool search: {(result.stderr or result.stdout).strip()[:300]}")


def _acp_toolsets(hermes_command: tuple[str, ...]) -> str | None:
    result = subprocess.run(
        [_hermes_executable(hermes_command), "config", "get", f"platform_toolsets.{ACP_PLATFORM}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def _enable_toolset_for_acp(hermes_command: tuple[str, ...]) -> None:
    """List the plugin's toolset explicitly for ACP sessions. Hermes otherwise decides plugin toolsets
    from a cache written by the last launch while plugin discovery is still running — a race that
    can leave XENIO's tools out (observed). Existing ACP toolsets are kept."""
    current = _acp_toolsets(hermes_command)
    if current and TOOLSET in current:
        return
    if not current:
        value = f"[hermes-acp, {TOOLSET}]"
    else:
        items = [i.strip().strip("'\"") for i in current.strip("[]").replace("\n", ",").split(",") if i.strip().strip("-").strip()]
        value = "[" + ", ".join([*(i.lstrip("- ").strip() for i in items), TOOLSET]) + "]"
    result = subprocess.run(
        [_hermes_executable(hermes_command), "config", "set", f"platform_toolsets.{ACP_PLATFORM}", value],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Could not enable the {TOOLSET} toolset for ACP: {(result.stderr or result.stdout).strip()[:300]}")
