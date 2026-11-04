"""A real wall around code XENIO did not write: macOS ``sandbox-exec``. Used for installing dependencies and for running skills.

Your home folder cannot be read, writes go only where allowed, the network is off unless this step needs it, nothing inherits your environment or keys, and time and file size are limited. Where the sandbox is not available, this refuses: it never quietly runs the same thing without it.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
_KEEP_ENV = ("LANG", "LC_ALL", "TERM")
_SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"
MAX_OUTPUT = 200_000


class SandboxUnavailable(Exception):
    pass


@dataclass
class SandboxResult:
    code: int | None
    output: str
    seconds: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.code == 0 and not self.timed_out


def available() -> bool:
    return sys.platform == "darwin" and os.path.exists(SANDBOX_EXEC)


def _quote(path: Path | str) -> str:
    return '"' + str(Path(path).resolve()).replace("\\", "\\\\").replace('"', '\\"') + '"'


def profile(writable: list[Path], readable: list[Path] | None = None, network: bool = False, home: Path | None = None, localhost: bool = False, protected: list[Path] | None = None) -> str:
    """The rules. In this language the last rule that matches wins, so what is allowed back comes after what is denied."""
    home = Path(home or Path.home())
    lines = ["(version 1)", "(allow default)", f"(deny file-read* (subpath {_quote(home)}))", "(allow file-read-metadata)"]
    open_up = [*writable, *(readable or [])]
    if open_up:
        lines.append("(allow file-read* " + " ".join(f"(subpath {_quote(p)})" for p in open_up) + ")")
    lines.append("(deny file-write*)")
    lines.append("(allow file-write* " + " ".join(f"(subpath {_quote(p)})" for p in writable) + ' (literal "/dev/null") (literal "/dev/tty") (regex #"^/dev/fd/"))')
    if protected:
        lines.append("(deny file-write* " + " ".join(f"(subpath {_quote(p)})" for p in protected) + ")")
    if not network:
        lines.append("(deny network*)")
        if localhost:  # a test server talking to itself, and nothing else. Outbound is matched on where it goes: a rule on the local side let the whole internet through (a test caught that)
            lines.append('(allow network-outbound (remote ip "localhost:*") (remote unix-socket))')
            lines.append('(allow network-bind (local ip "localhost:*"))')
            lines.append('(allow network-inbound (local ip "localhost:*"))')
    return "\n".join(lines) + "\n"


def tool_roots(*programs: str) -> list[Path]:
    """Where a tool's own files live, if that is inside the home folder (uv's Python, nvm's Node): they have to stay readable for it to run at all."""
    home = Path.home().resolve()
    found = []
    for program in programs:
        path = shutil.which(program) if not os.path.isabs(program) else program
        if not path:
            continue
        real = Path(path).resolve()
        for candidate in (real, Path(path)):
            if candidate.is_relative_to(home):
                found.append(candidate.parent.parent)
    return list(dict.fromkeys(found))


def run(argv: list[str], cwd: Path, writable: list[Path], *, readable: list[Path] | None = None, network: bool = False, timeout: float = 300.0, env: dict | None = None, home: Path | None = None, cpu_seconds: int = 900, file_mb: int = 1024, stdin: str | None = None, localhost: bool = False, protected: list[Path] | None = None, on_start=None) -> SandboxResult:
    if not available():
        raise SandboxUnavailable("this Mac has no sandbox-exec, so XENIO will not run this")
    handle, rules = tempfile.mkstemp(suffix=".sb")
    os.write(handle, profile(writable, readable, network, home, localhost, protected).encode())
    os.close(handle)
    clean = {k: os.environ[k] for k in _KEEP_ENV if k in os.environ}  # nothing else: no keys, no tokens, no agent environment
    clean.update({"PATH": _SYSTEM_PATH, "CI": "1", "NO_COLOR": "1"})
    clean.update(env or {})
    limited = ["/bin/sh", "-c", f'ulimit -t {int(cpu_seconds)}; ulimit -f {int(file_mb) * 2048}; exec "$@"', "sh", *argv]  # -f counts 512-byte blocks
    started = time.monotonic()
    try:
        proc = subprocess.Popen([SANDBOX_EXEC, "-f", rules, *limited], cwd=str(cwd), env=clean, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL, text=True, errors="replace", start_new_session=True)
        if on_start is not None:
            on_start(proc)
        try:
            output, _ = proc.communicate(stdin, timeout=timeout)
            return SandboxResult(proc.returncode, output[-MAX_OUTPUT:], round(time.monotonic() - started, 1))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            output, _ = proc.communicate()
            return SandboxResult(None, (output or "")[-MAX_OUTPUT:], round(time.monotonic() - started, 1), True)
    finally:
        os.unlink(rules)


def spawn(argv: list[str], cwd: Path, writable: list[Path], *, readable: list[Path] | None = None, env: dict | None = None, log: Path | None = None, cpu_seconds: int = 7200) -> subprocess.Popen:
    """Something that keeps running, like a preview of an app, inside the same wall. It can talk to itself on localhost and to nothing else."""
    if not available():
        raise SandboxUnavailable("this Mac has no sandbox-exec, so XENIO will not run this")
    handle, rules = tempfile.mkstemp(suffix=".sb")
    os.write(handle, profile(writable, readable, False, None, True).encode())
    os.close(handle)
    clean = {k: os.environ[k] for k in _KEEP_ENV if k in os.environ}
    clean.update({"PATH": _SYSTEM_PATH, "NO_COLOR": "1"})
    clean.update(env or {})
    out = open(log, "ab") if log else subprocess.DEVNULL  # noqa: SIM115 - it lives as long as the process
    limited = ["/bin/sh", "-c", f'ulimit -t {int(cpu_seconds)}; exec "$@"', "sh", *argv]
    try:
        return subprocess.Popen([SANDBOX_EXEC, "-f", rules, *limited], cwd=str(cwd), env=clean, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    finally:
        if log:
            out.close()
