"""Install / inspect the ``xenio-guard`` Hermes plugin (see hermes_plugin/xenio-guard).

Installing touches the user's Hermes setup in these places, all reversible:
  1. copies the plugin to ``HERMES_HOME/plugins/xenio-guard/``
  2. ``hermes plugins enable xenio-guard`` (Hermes' own command; adds it to plugins.enabled)
  3. ``platform_toolsets.acp`` lists XENIO's toolset; ``tools.tool_search.enabled: off`` (unless the user chose a value)
``config.yaml`` is backed up to the XENIO data dir first.
Undo: ``hermes plugins disable xenio-guard`` (and delete the plugin folder).
"""

from __future__ import annotations

import filecmp
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from app.config import PROJECT_ROOT, default_hermes_home

PLUGIN_NAME = "xenio-guard"
TOOLSET = "xenio"  # the toolset the plugin registers XENIO's tools under
ACP_PLATFORM = "acp"
# What --install-guard wrote before Hermes 0.22: off only in XENIO's own Hermes process (from its environment). A
# 0.22 gateway multiplexes profiles and resolves ${VAR} from the profile's .env only, so the placeholder reads as
# "auto" there and hides XENIO's tools (observed). Replaced by "off" on the next --install-guard.
TOOL_SEARCH_PLACEHOLDER = "${XENIO_TOOL_SEARCH}"
_TOOL_SEARCH_OFF = {"off", "false", "no", "0"}
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

    pid = gateway_pid(hermes_home)
    if pid is None:
        return None, "no Hermes gateway is running"
    try:
        marked = int(json.loads((data_dir / "guard.json").read_text(encoding="utf-8")).get("pid") or 0)
    except (OSError, ValueError):
        marked = 0
    if marked == pid or _process_env_has(pid, "XENIO_GUARD=1"):
        return True, f"the Hermes gateway (pid {pid}) carries XENIO's guard"
    return False, (f"a Hermes gateway (pid {pid}) is running that XENIO did not start, so XENIO's safety guard is not "
                   "active in it. Stop it with `hermes gateway stop` (or quit the Hermes app), then start XENIO again")


def stale_gateway(data_dir: Path, hermes_home: Path | None = None) -> int | None:
    """The pid of a running gateway that carries XENIO's guard but loaded older XENIO code than this, or None.

    A guarded gateway with no record of its code predates the record itself, so it is older code too. A gateway XENIO
    did not start is never touched here (guard_live reports it instead)."""
    from app.hermes.fingerprint import code_fingerprint, recorded_gateway_code

    pid = gateway_pid(hermes_home)
    if pid is None or guard_live(data_dir, hermes_home)[0] is not True:
        return None
    record = recorded_gateway_code(data_dir)
    if record.get("pid") == pid and record.get("code") == code_fingerprint(PROJECT_ROOT):
        return None
    return pid


def restart_stale_gateway(data_dir: Path, command: tuple[str, ...] = (), hermes_home: Path | None = None,
                          run=subprocess.run, wait: float = 15.0) -> int | None:
    """Stop a gateway that runs outdated XENIO code; the next connection starts a fresh one (it is started on demand).
    Returns the stopped gateway's pid, or None when nothing had to be done."""
    import logging
    import os
    import time

    from app.logging_setup import event

    pid = stale_gateway(data_dir, hermes_home)
    if pid is None:
        return None
    event(logging.getLogger("xenio.hermes"), "Restarting Hermes' gateway: it runs XENIO code from before an update",
          pid=pid)
    env = {**os.environ, **({"HERMES_HOME": str(hermes_home)} if hermes_home else {})}
    try:
        run([_hermes_executable(command), "gateway", "stop"], capture_output=True, text=True, timeout=30, env=env,
            stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline and gateway_pid(hermes_home) == pid:
        time.sleep(0.2)
    return pid


def gateway_pid(hermes_home: Path | None = None) -> int | None:
    """The running Hermes gateway's pid (``gateway.lock``), or None when none runs."""
    import json
    import os

    try:
        pid = int(json.loads(((hermes_home or default_hermes_home()) / "gateway.lock").read_text(encoding="utf-8"))
                  .get("pid") or 0)
        if pid > 0:
            os.kill(pid, 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    return pid if pid > 0 else None


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
        configured = configured_tool_search((home / "config.yaml").read_text(encoding="utf-8"))
    except OSError:
        configured = None
    tools_visible = (configured or "").lower() in _TOOL_SEARCH_OFF
    return GuardStatus(installed, up_to_date, enabled, toolset_enabled, tools_visible)


def configured_tool_search(config_text: str) -> str | None:
    """The ``tools.tool_search.enabled`` value written in Hermes' config.yaml (None when the file does not set it;
    ``hermes config get`` cannot tell, it answers with Hermes' default)."""
    path, stack = ("tools", "tool_search", "enabled"), []
    for line in config_text.splitlines():
        m = re.match(r"^( *)([A-Za-z_][\w-]*):\s*(.*?)\s*$", line)
        if not m or line.lstrip().startswith("#"):
            continue
        indent, key, value = len(m.group(1)), m.group(2), m.group(3)
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, key))
        if tuple(k for _, k in stack) == path:
            value = value.split(" #", 1)[0].strip().strip("'\"")
            return value or None
    return None


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
    _keep_tools_visible(hermes_command, home)
    return guard_status(hermes_command, home)


def _keep_tools_visible(hermes_command: tuple[str, ...], hermes_home: Path) -> None:
    """Hermes' Tool Search (default ``auto``) replaces every plugin tool with tool_search/tool_describe/tool_call,
    and models then claim XENIO's tools don't exist (observed on Windows and again with Hermes 0.22). Set
    ``tools.tool_search.enabled: off`` for the profile: a 0.22 gateway runs every session of the profile, so the
    switch can no longer be limited to XENIO's process. Replaces XENIO's old placeholder; never a user's own value."""
    try:
        configured = configured_tool_search((hermes_home / "config.yaml").read_text(encoding="utf-8"))
    except OSError:
        configured = None
    if configured is not None and configured != TOOL_SEARCH_PLACEHOLDER:
        return  # already off, or the user's own choice (--health warns)
    result = subprocess.run(
        [_hermes_executable(hermes_command), "config", "set", "tools.tool_search.enabled", "off"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Could not switch off Hermes' tool search: {(result.stderr or result.stdout).strip()[:300]}")


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
