"""Running a learner's code (V4 phase 3). Only ever because they pressed Run, on their own Mac, in a macOS sandbox that denies the network,
everything in their home folder and any write outside a throwaway folder, with limits on CPU time, output and file size.

Measured: inside it, reading and listing the home folder, writing to it and connecting out all fail, and an endless loop ends at 5 s. If the
sandbox cannot be used, nothing runs: code is never run outside it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MAX_CODE = 40_000
MAX_INPUT = 20_000
MAX_OUTPUT = 40_000
CPU_SECONDS = 5
WALL_SECONDS = 10
COMPILE_SECONDS = 25
_SANDBOX = "/usr/bin/sandbox-exec"
_TOOLS = "/Library/Developer/CommandLineTools"
_PROFILE = """(version 1)
(deny default)
(allow process-exec*)
(allow process-fork)
(allow signal (target self))
(allow sysctl-read)
(allow mach-lookup)
(allow ipc-posix-shm)
(allow file-read-metadata)
(allow file-read* (subpath "/usr") (subpath "/bin") (subpath "/System") (subpath "/Library/Developer") (subpath "/Library/Apple") (subpath "/Library/Preferences/Logging")
                  (subpath "/private/var/db/dyld") (subpath "/private/var/db/timezone") (subpath "/dev") (subpath "/private/etc") (subpath "/opt/homebrew/bin") (subpath "/opt/homebrew/Cellar") (subpath "/opt/homebrew/opt") (subpath "/opt/homebrew/lib") (subpath "/opt/homebrew/etc")
                  (literal "/") (literal "/private") (literal "/private/tmp") (literal "/tmp") (literal "/var") (literal "/etc") (literal "/opt") (literal "/opt/homebrew"))
(allow file-read* (subpath "{work}"))
(allow file-write* (subpath "{work}") (literal "/dev/null") (literal "/dev/dtracehelper") (literal "/dev/tty"))
(deny network*)
"""
LANGUAGES = {"python": "Python", "javascript": "JavaScript", "c": "C", "cpp": "C++"}
_ALIASES = {"py": "python", "python3": "python", "js": "javascript", "node": "javascript", "nodejs": "javascript", "c++": "cpp", "cxx": "cpp"}
_NOISE = re.compile(r"^.*(?:xcrun_db|couldn't create cache file|xcrun: error: cannot be used within an App Sandbox).*$\n?", re.MULTILINE)


class CodeError(Exception):
    pass


def language_of(name: str) -> str | None:
    wanted = str(name or "").strip().lower()
    wanted = _ALIASES.get(wanted, wanted)
    return wanted if wanted in LANGUAGES else None


def _python() -> str | None:
    for candidate in (f"{_TOOLS}/usr/bin/python3", "/usr/bin/python3"):
        if os.path.exists(candidate):
            return candidate
    return None


def _node() -> str | None:
    for candidate in ("/opt/homebrew/bin/node", "/usr/local/bin/node", shutil.which("node")):
        if candidate and os.path.exists(candidate):
            return candidate
    return None


def _clang(cpp: bool) -> str | None:
    name = "clang++" if cpp else "clang"
    for candidate in (f"{_TOOLS}/usr/bin/{name}", f"/usr/bin/{name}"):
        if os.path.exists(candidate):
            return candidate
    return None


def _sdk() -> list[str]:
    """The SDK for this macOS (the Command Line Tools can ship a newer one that their linker cannot read: measured)."""
    folder = Path(_TOOLS) / "SDKs"
    try:
        major = subprocess.run(["/usr/bin/sw_vers", "-productVersion"], capture_output=True, text=True, timeout=5).stdout.split(".")[0].strip()
        found = sorted(p for p in folder.glob(f"MacOSX{major}*.sdk") if not p.is_symlink())
    except (OSError, subprocess.SubprocessError):
        return []
    return ["-isysroot", str(found[-1])] if found else []


def available() -> dict:
    """Which languages can really run here."""
    if sys.platform != "darwin" or not os.path.exists(_SANDBOX):
        return {}
    return {name: LANGUAGES[name] for name, there in (("python", _python()), ("javascript", _node()), ("c", _clang(False)), ("cpp", _clang(True))) if there}


def _limits() -> None:
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS))
    resource.setrlimit(resource.RLIMIT_FSIZE, (10_000_000, 10_000_000))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.setsid()


def _sandboxed(argv: list[str], work: str, stdin: str, timeout: float) -> dict:
    started = time.monotonic()
    env = {"PATH": "/usr/bin:/bin:/opt/homebrew/bin", "HOME": work, "TMPDIR": work, "LANG": "en_US.UTF-8", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"}
    process = subprocess.Popen([_SANDBOX, "-p", _PROFILE.format(work=work), *argv], cwd=work, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, preexec_fn=_limits)
    try:
        out, err = process.communicate(stdin.encode("utf-8"), timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, 9)
        except OSError:
            process.kill()
        out, err = process.communicate()
        timed_out = True
    code = process.returncode
    cpu_out = code in (-24, -9, 152) and not timed_out
    return {"stdout": out.decode("utf-8", "replace"), "stderr": _NOISE.sub("", err.decode("utf-8", "replace")), "exit": code, "timed_out": timed_out or cpu_out, "ms": int((time.monotonic() - started) * 1000)}


def run(language: str, code: str, stdin: str = "") -> dict:
    """{"ok", "stdout", "stderr", "exit", "timed_out", "ms", "phase"}. A program that fails is an ordinary result, not an exception."""
    name = language_of(language)
    if name is None:
        raise CodeError(f"XENIO can run {', '.join(LANGUAGES.values())}, not {language}")
    if name not in available():
        raise CodeError(f"{LANGUAGES[name]} cannot run on this Mac: it is not installed, or the sandbox is missing")
    if len(code) > MAX_CODE or len(stdin) > MAX_INPUT:
        raise CodeError("that is too long to run")
    with tempfile.TemporaryDirectory(prefix="xenio-run-") as raw:
        work = os.path.realpath(raw)
        source = Path(work) / {"python": "main.py", "javascript": "main.js", "c": "main.c", "cpp": "main.cpp"}[name]
        source.write_text(code, encoding="utf-8")
        built = None
        if name in ("c", "cpp"):
            argv = [_clang(name == "cpp"), *_sdk(), *(["-std=c++17"] if name == "cpp" else []), "-O1", "-o", "main", source.name]
            built = _sandboxed(argv, work, "", COMPILE_SECONDS)
            if built["exit"] != 0:
                return _result({**built, "stderr": built["stderr"] or "the compiler stopped"}, "compile")
            argv = ["./main"]
        elif name == "python":
            argv = [_python(), "-I", "-S", source.name]
        else:
            argv = [_node(), source.name]
        done = _sandboxed(argv, work, stdin, WALL_SECONDS)
        if built:
            done["compile_ms"] = built["ms"]
        return _result(done, "run")


def _result(done: dict, phase: str) -> dict:
    def clip(text: str) -> str:
        return text if len(text) <= MAX_OUTPUT else text[:MAX_OUTPUT] + "\n… (output cut)"
    return {**done, "phase": phase, "stdout": clip(done["stdout"]), "stderr": clip(done["stderr"]), "ok": done["exit"] == 0 and not done["timed_out"]}


def same_output(expected: str, actual: str) -> bool:
    """The output is right if the words and numbers are, whatever the spacing at line ends."""
    norm = lambda text: "\n".join(line.rstrip() for line in str(text).strip().splitlines())  # noqa: E731
    return norm(expected) == norm(actual)


def check(language: str, code: str, checks: list[dict]) -> dict:
    """An exercise: the learner's code against its test inputs, read from what really ran."""
    results, passed = [], 0
    for item in checks[:8]:
        out = run(language, code, str(item.get("stdin") or ""))
        ok = out["ok"] and same_output(item.get("expected") or "", out["stdout"])
        passed += ok
        results.append({"name": item.get("name") or "", "stdin": item.get("stdin") or "", "expected": item.get("expected") or "", "got": out["stdout"][:2000], "error": out["stderr"][:1200] if not out["ok"] else "", "timed_out": out["timed_out"], "passed": bool(ok)})
        if out.get("phase") == "compile":
            break
    return {"passed": passed, "total": len(checks[:8]), "results": results, "all_passed": passed == len(checks[:8]) and bool(checks)}
