"""Deterministic verification (V6): XENIO itself finds and runs a project's tests and reads the outcome.

An agent saying "tests pass" is never evidence. XENIO detects the test command from the project files, runs it in the
run's worktree, and classifies the result from the runner's own output:

* ``passed``    tests ran and none failed (at least one test actually ran);
* ``failed``    at least one failure or error;
* ``no_tests``  the runner exited but no test ran (exit 0 with nothing collected is not success);
* ``blocked``   the suite could not start: a missing dependency, runner or script;
* ``timeout``   it did not finish in time (the process tree is killed);
* ``error``     anything else unexpected; ``not_run`` when no test command exists.

Tests execute repository code with the user's rights, so on macOS XENIO runs them under ``sandbox-exec``: writes only
inside the worktree and temporary folders, no network except localhost. The environment is reduced to the basics
(no API keys or tokens from XENIO's or the user's shell). Output kept for the report is redacted and clipped.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_TIMEOUT = 600
_KEEP_ENV = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR", "TZ")
_MISSING = re.compile(r"ModuleNotFoundError|No module named|command not found|Cannot find module|ERR_MODULE_NOT_FOUND|"
                      r"missing script: \"?test|not found: (?:jest|vitest|mocha)|No such file or directory: '?(?:pytest|"
                      r"python)|could not find (?:`?Cargo|go\.mod)|ImportError while importing|pytest: command not found",
                      re.IGNORECASE)


@dataclass
class TestCommand:
    name: str  # pytest | npm | go | cargo | make
    argv: list[str]
    focused: bool = False


@dataclass
class TestRun:
    command: str
    status: str  # passed | failed | no_tests | blocked | timeout | error | not_run
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    exit_code: int | None = None
    seconds: float = 0.0
    sandboxed: bool = False
    detail: str = ""  # one-line reason
    tail: str = ""  # last lines of output (redacted)
    failures: list[str] = field(default_factory=list)  # names of failing tests, when the runner lists them

    def to_dict(self) -> dict:
        return asdict(self)

    def line(self) -> str:
        counts = ", ".join(f"{n} {k}" for k, n in (("passed", self.passed), ("failed", self.failed),
                                                   ("errors", self.errors), ("skipped", self.skipped)) if n)
        return f"{self.status.upper()}: {self.command}" + (f" ({counts})" if counts else "") + \
            (f" - {self.detail}" if self.detail else "") + f" [{self.seconds:.1f} s{', sandboxed' if self.sandboxed else ''}]"


# ---- detection --------------------------------------------------------------------------------------------------

def _python_for(source: Path) -> str:
    """The project's own virtualenv when it has one (dependencies live outside the worktree), else python3."""
    for venv in (".venv", "venv", "env"):
        candidate = source / venv / "bin" / "python"
        if candidate.is_file():
            return str(candidate)
    return shutil.which("python3") or sys.executable


def detect(worktree: Path, source: Path | None = None) -> list[TestCommand]:
    """Test commands this project declares, most specific first. ``source``: the user's original folder (venvs)."""
    source = source or worktree
    found: list[TestCommand] = []
    names = {p.name for p in worktree.iterdir()}
    pyproject = (worktree / "pyproject.toml").read_text(encoding="utf-8", errors="replace") if "pyproject.toml" in names else ""
    has_py_tests = any(worktree.glob("test_*.py")) or any(worktree.glob("tests/**/test_*.py")) or \
        any(worktree.glob("tests/**/*_test.py"))
    if "pytest.ini" in names or "conftest.py" in names or "[tool.pytest" in pyproject or has_py_tests:
        found.append(TestCommand("pytest", [_python_for(source), "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rfE"]))
    if "package.json" in names:
        try:
            scripts = (json.loads((worktree / "package.json").read_text(encoding="utf-8")).get("scripts") or {})
        except (ValueError, OSError):
            scripts = {}
        test = str(scripts.get("test") or "")
        if test and "no test specified" not in test:
            runner = "pnpm" if "pnpm-lock.yaml" in names else "yarn" if "yarn.lock" in names else "npm"
            found.append(TestCommand(runner, [runner, "test"] + (["--silent"] if runner == "npm" else [])))
    if "go.mod" in names:
        found.append(TestCommand("go", ["go", "test", "./..."]))
    if "Cargo.toml" in names:
        found.append(TestCommand("cargo", ["cargo", "test"]))
    if not found and "Makefile" in names and re.search(r"^test:", (worktree / "Makefile").read_text(errors="replace"), re.M):
        found.append(TestCommand("make", ["make", "test"]))
    return found


def focused(commands: list[TestCommand], changed: list[str], worktree: Path) -> list[TestCommand]:
    """Tests that cover the changed files (pytest: changed test files, and test_<module>.py for changed modules)."""
    out = []
    for cmd in commands:
        if cmd.name != "pytest":
            continue
        targets = []
        for path in changed:
            p = Path(path)
            if p.suffix != ".py":
                continue
            if p.name.startswith("test_") or p.name.endswith("_test.py"):
                targets.append(path)
            else:
                targets += [str(t.relative_to(worktree)) for t in worktree.glob(f"**/test_{p.stem}.py")
                            if ".git" not in t.parts and "node_modules" not in t.parts]
        targets = sorted({t for t in targets if (worktree / t).exists()})
        if targets:
            out.append(TestCommand("pytest", cmd.argv + targets, focused=True))
    return out


# ---- running ----------------------------------------------------------------------------------------------------

def _sandbox_profile(worktree: Path) -> str:
    allowed = {str(worktree.resolve()), "/private/var/folders", "/private/tmp", os.path.realpath(tempfile.gettempdir())}
    allowed |= {str(Path.home() / d) for d in (".cache", "Library/Caches", ".npm", ".cargo/registry", "go/pkg")}
    subpaths = " ".join(f'(subpath "{p}")' for p in sorted(allowed))
    return ("(version 1)\n(allow default)\n(deny network-outbound)\n(allow network-outbound (remote ip \"localhost:*\"))\n"
            "(allow network-outbound (remote unix-socket))\n(deny file-write*)\n"
            f"(allow file-write* {subpaths} (literal \"/dev/null\") (literal \"/dev/tty\") (regex #\"^/dev/fd/\"))\n")


def _env() -> dict:
    env = {k: os.environ[k] for k in _KEEP_ENV if k in os.environ}
    env.update({"CI": "1", "PYTHONDONTWRITEBYTECODE": "1", "NO_COLOR": "1", "FORCE_COLOR": "0"})
    return env


def run(cmd: TestCommand, worktree: Path, timeout: float = DEFAULT_TIMEOUT, sandbox: bool = True,
        redact=lambda s: s) -> TestRun:
    argv = list(cmd.argv)
    label = " ".join(cmd.argv[1:] if cmd.name == "pytest" else cmd.argv)
    label = ("python " + label) if cmd.name == "pytest" else label
    if not (os.path.isabs(argv[0]) and os.path.exists(argv[0])) and not shutil.which(argv[0]):
        return TestRun(label, "blocked", detail=f"{argv[0]} is not installed")  # checked first: the sandbox hides it
    sandboxed = sandbox and sys.platform == "darwin" and os.path.exists("/usr/bin/sandbox-exec")
    profile = None
    if sandboxed:
        fd, profile = tempfile.mkstemp(suffix=".sb")
        os.write(fd, _sandbox_profile(worktree).encode())
        os.close(fd)
        argv = ["/usr/bin/sandbox-exec", "-f", profile, *argv]
    started = time.monotonic()
    try:
        proc = subprocess.Popen(argv, cwd=str(worktree), env=_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, text=True, errors="replace", start_new_session=True)
    except FileNotFoundError:
        return TestRun(label, "blocked", detail=f"{cmd.argv[0]} is not installed", sandboxed=sandboxed)
    try:
        output, _ = proc.communicate(timeout=timeout)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)  # the whole tree: a test runner's workers too
        output, _ = proc.communicate()
        return TestRun(label, "timeout", exit_code=None, seconds=round(time.monotonic() - started, 1),
                       sandboxed=sandboxed, detail=f"did not finish within {timeout:.0f} s; stopped",
                       tail=redact(_tail(output)))
    finally:
        if profile:
            os.unlink(profile)
    result = parse(cmd.name, output, code)
    result.command, result.seconds, result.sandboxed = label, round(time.monotonic() - started, 1), sandboxed
    result.tail = redact(_tail(output))
    return result


def _tail(text: str, lines: int = 40, chars: int = 4000) -> str:
    return "\n".join((text or "").strip().splitlines()[-lines:])[-chars:]


def _num(pattern: str, text: str) -> int:
    return sum(int(m) for m in re.findall(pattern, text))


def parse(runner: str, output: str, code: int | None) -> TestRun:
    """Classify a finished test command from its output and exit code."""
    text = output or ""
    run_ = TestRun("", "error", exit_code=code)
    if runner == "pytest":
        summary = "\n".join(line for line in text.splitlines() if re.search(r"=+ .* in [\d.]+s", line) or
                            re.match(r"^\d+ (passed|failed)", line)) or text[-2000:]
        run_.passed = _num(r"(\d+) passed", summary)
        run_.failed = _num(r"(\d+) failed", summary)
        run_.errors = _num(r"(\d+) errors?\b", summary)
        run_.skipped = _num(r"(\d+) skipped", summary)
        run_.failures = re.findall(r"^(?:FAILED|ERROR) (\S+)", text, re.M)[:20]
        if code == 5 or "no tests ran" in summary:
            run_.status, run_.detail = "no_tests", "pytest collected no tests"
            return run_
    elif runner in ("npm", "yarn", "pnpm"):
        jest = re.search(r"Tests:\s+(.*?)\s+total", text)
        vitest = re.search(r"Tests\s+(.*?)\(\d+\)", text)
        line = jest.group(1) if jest else vitest.group(1) if vitest else ""
        if line:
            run_.passed, run_.failed = _num(r"(\d+) passed", line), _num(r"(\d+) failed", line)
            run_.skipped = _num(r"(\d+) (?:skipped|todo|pending)", line)
        else:  # mocha / node:test / tap
            run_.passed = _num(r"(\d+) passing", text) or _num(r"^# pass (\d+)", text) or _num(r"ℹ pass (\d+)", text)
            run_.failed = _num(r"(\d+) failing", text) or _num(r"^# fail (\d+)", text) or _num(r"ℹ fail (\d+)", text)
            run_.skipped = _num(r"(\d+) pending", text) or _num(r"ℹ skipped (\d+)", text)
    elif runner == "go":
        run_.passed = len(re.findall(r"^ok\s+\S+", text, re.M))
        run_.failed = len(re.findall(r"^--- FAIL", text, re.M)) or len(re.findall(r"^FAIL\s+\S+", text, re.M))
        run_.failures = re.findall(r"^--- FAIL: (\S+)", text, re.M)[:20]
        if not run_.passed and not run_.failed and "no test files" in text:
            run_.status, run_.detail = "no_tests", "no Go test files"
            return run_
    elif runner == "cargo":
        for m in re.finditer(r"test result: \w+\. (\d+) passed; (\d+) failed; (\d+) ignored", text):
            run_.passed += int(m.group(1))
            run_.failed += int(m.group(2))
            run_.skipped += int(m.group(3))
    if (run_.failed or run_.errors) or (code not in (0, None) and run_.passed and not _MISSING.search(text)):
        run_.status = "failed"
        if not run_.failed and not run_.errors:
            run_.detail = f"exit code {code}"
        return run_
    if code not in (0, None) and _MISSING.search(text):
        run_.status = "blocked"
        run_.detail = "a dependency or tool is missing: " + _MISSING.search(text).group(0)
        return run_
    if code == 0 and run_.passed:
        run_.status = "passed"
        return run_
    if code == 0:
        run_.status, run_.detail = "no_tests", "the command exited 0 but XENIO saw no test results in its output"
        return run_
    run_.status, run_.detail = "error", f"exit code {code} and no test results"
    return run_
