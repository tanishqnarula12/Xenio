"""Packages for a project the team is building. Agents are not allowed to install anything, and that stays: a package's install script would run with your rights.

So XENIO does it, between their steps: agents only list what they need in ``requirements.txt`` or ``package.json``. Only from the public registries, with install scripts off and Python wheels only (no setup.py runs), inside the sandbox, into the project's own folder. This is only ever done for a project XENIO created itself, never in one of your folders.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

from app.foundry import sandbox

_NOT_A_REGISTRY_NAME = re.compile(r"(^|[\s;])(-e|--editable|-r|--requirement|-c|--constraint|-f|--find-links|-i|--index-url|--extra-index-url|--trusted-host|--no-binary)\b|git\+|\bhttps?://|\bfile:|\bssh://|\s@\s", re.I)
_JS_REGISTRY_VERSION = re.compile(r"^(\^|~|>=|<=|>|<|=)?\s*[0-9*xX][\w.*+-]*(\s*(\|\||-|\s)\s*(\^|~|>=|<=|>|<|=)?\s*[0-9*xX][\w.*+-]*)*$|^(latest|next|\*)$")
_SKIP_DIRS = {"node_modules", ".git", ".venv", "venv", ".deps", "dist", "build", "__pycache__"}
MAX_DEPTH = 2
INSTALL_SECONDS = 420.0
_REQUIREMENT = re.compile(r"^[A-Za-z0-9][\w.-]*(?:\[[\w,.\s-]+\])?\s*(?:(?:==|>=|<=|~=|!=|>|<)\s*[\w.*+-]+\s*,?\s*)*(?:;.*)?$")
_NODE_NAME = re.compile(r"^(?:@[a-z0-9][\w.-]*/)?[a-z0-9][\w.-]*(?:@[\w.^~*+-]+)?$", re.I)
_MISSING_MODULE = re.compile(r"(?:ModuleNotFoundError|ImportError): No module named ['\"]([A-Za-z0-9_]+)")
# where the name you import is not the name you install
_PACKAGE_FOR_MODULE = {"yaml": "pyyaml", "PIL": "pillow", "cv2": "opencv-python-headless", "sklearn": "scikit-learn", "bs4": "beautifulsoup4", "dotenv": "python-dotenv", "jwt": "pyjwt", "dateutil": "python-dateutil", "psycopg2": "psycopg2-binary", "Crypto": "pycryptodome", "flask_sqlalchemy": "flask-sqlalchemy", "flask_cors": "flask-cors", "jose": "python-jose", "multipart": "python-multipart"}
_NOT_FROM_PYPI = frozenset({"app", "src", "tests", "test", "main", "server", "config", "models", "utils", "api", "db", "web", "core", "lib"})


def find_manifests(root: Path) -> list[Path]:
    found = []

    def walk(folder: Path, depth: int) -> None:
        try:
            entries = sorted(folder.iterdir())
        except OSError:
            return
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_file() and entry.name in ("requirements.txt", "requirements-dev.txt", "package.json"):
                found.append(entry)
            elif entry.is_dir() and depth < MAX_DEPTH and entry.name not in _SKIP_DIRS:
                walk(entry, depth + 1)

    walk(Path(root), 0)
    return found


def refused(manifest: Path) -> list[str]:
    """What this manifest asks for that does not come from the public registries. A git URL or a private index can hide anything."""
    problems = []
    try:
        text = manifest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [f"{manifest.name} cannot be read"]
    if manifest.name == "package.json":
        try:
            data = json.loads(text)
        except ValueError:
            return ["package.json is not valid JSON"]
        for section in ("dependencies", "devDependencies", "optionalDependencies"):
            items = data.get(section)
            for name, version in (items.items() if isinstance(items, dict) else []):
                if not _JS_REGISTRY_VERSION.match(str(version).strip()):
                    problems.append(f"{name}: {str(version)[:60]} is not a registry version")
        return problems
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line and _NOT_A_REGISTRY_NAME.search(f" {line}"):
            problems.append(f"{line[:60]} is not a plain package from PyPI")
    return problems


def missing_packages(output: str, own_modules: set[str] | None = None) -> list[str]:
    """What a crash says is not installed. Installing it costs no tokens, where sending an agent to read the traceback does."""
    found = []
    for module in _MISSING_MODULE.findall(output or ""):
        if module in _NOT_FROM_PYPI or module in sys.stdlib_module_names or module in (own_modules or set()):
            continue  # Python's own modules are never a package to fetch: if they are missing, Python itself could not start
        package = _PACKAGE_FOR_MODULE.get(module, module.replace("_", "-"))
        if package not in found:
            found.append(package)
    return found[:12]


def plain_packages(names: list[str], ecosystem: str) -> tuple[list[str], list[str]]:
    """Names only. Not URLs, paths or options, which can hide anything."""
    good, bad = [], []
    for name in names:
        text = str(name).strip()
        valid = bool(_NODE_NAME.match(text)) if ecosystem == "node" else bool(_REQUIREMENT.match(text)) and ";" not in text and not _NOT_A_REGISTRY_NAME.search(f" {text}")
        (good if valid and len(text) <= 100 else bad).append(text)
    return good[:20], bad


def names_in(manifest: Path) -> list[str]:
    try:
        text = manifest.read_text(encoding="utf-8", errors="replace")
        if manifest.name == "package.json":
            data = json.loads(text)
            return [n for s in ("dependencies", "devDependencies") for n in (data.get(s) or {})]
        return [re.split(r"[\s=<>!~\[;]", line.strip(), maxsplit=1)[0] for line in text.splitlines() if _REQUIREMENT.match(line.split("#", 1)[0].strip())]
    except (OSError, ValueError, AttributeError):
        return []


class Dependencies:
    """``project`` is the folder XENIO created; ``cwd`` is the worktree an agent is in, inside the run's workspace."""

    def __init__(self, project: Path, writable_extra: list[Path] | None = None, runner=sandbox.run):
        self.project = Path(project)
        self.home = self.project / ".deps" / "home"
        self.extra = list(writable_extra or [])
        self.runner = runner
        self.done_file = self.project / ".deps" / "installed.json"

    def _done(self) -> dict:
        try:
            return json.loads(self.done_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _remember(self, key: str, value: str) -> None:
        done = self._done()
        done[key] = value
        self.done_file.parent.mkdir(parents=True, exist_ok=True)
        self.done_file.write_text(json.dumps(done), encoding="utf-8")

    def exclude_from_git(self) -> None:
        """A new worktree must never commit installed packages, whatever the agents do to .gitignore."""
        common = self.project / ".git" / "info"
        if common.parent.is_dir():
            common.mkdir(exist_ok=True)
            with open(common / "exclude", "a", encoding="utf-8") as fh:
                fh.write("\n.venv\nnode_modules\n.deps\n__pycache__\n.pytest_cache\n")

    def ensure(self, cwd: Path) -> dict:
        """Install whatever the manifests in this worktree list and is not there yet. Returns what happened, in words for the team."""
        cwd = Path(cwd)
        out = {"installed": [], "refused": [], "failed": [], "unchanged": 0}
        for manifest in find_manifests(cwd):
            problems = refused(manifest)
            if problems:
                out["refused"] += [f"{manifest.relative_to(cwd)}: {p}" for p in problems[:5]]
                continue
            digest = hashlib.sha256(manifest.read_bytes()).hexdigest()[:16]
            if manifest.name == "package.json":
                self._node(cwd, manifest, digest, out)
            else:
                self._python(manifest, digest, out)
        return out

    def _writable(self, *more: Path) -> list[Path]:
        (self.project / ".deps").mkdir(parents=True, exist_ok=True)
        self.home.mkdir(parents=True, exist_ok=True)
        return [self.project, *more, *self.extra]

    def _python(self, manifest: Path, digest: str, out: dict) -> None:
        key = f"py:{digest}"
        if self._done().get(key):
            out["unchanged"] += 1
            return
        venv = self.project / ".venv"
        base = getattr(sys, "_base_executable", sys.executable)
        env = {"HOME": str(self.home), "PIP_CACHE_DIR": str(self.project / ".deps" / "pip"), "TMPDIR": str(self.project / ".deps"), "PIP_NO_INPUT": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
        readable = [manifest.parent, *sandbox.tool_roots(base)]
        if not (venv / "bin" / "python").exists():
            made = self.runner([base, "-m", "venv", str(venv)], self.project, self._writable(), readable=readable, timeout=120.0, env=env)
            if not made.ok:
                out["failed"].append(f"a Python environment could not be made: {made.output[-300:]}")
                return
        lines = [line.split("#", 1)[0].strip() for line in manifest.read_text(encoding="utf-8", errors="replace").splitlines()]
        junk = [line for line in lines if line and not _REQUIREMENT.match(line)]  # a sentence in a requirements file would fail the whole install
        cleaned = self.project / ".deps" / f"requirements-{digest}.txt"
        cleaned.parent.mkdir(parents=True, exist_ok=True)
        cleaned.write_text("\n".join(line for line in lines if line and _REQUIREMENT.match(line)) + "\n", encoding="utf-8")
        pip = [str(venv / "bin" / "python"), "-m", "pip", "install", "--only-binary=:all:", "pytest", "-r", str(cleaned)]
        done = self.runner(pip, manifest.parent, self._writable(), readable=[*readable, cleaned.parent], network=True, timeout=INSTALL_SECONDS, env=env)
        if done.ok:
            self._remember(key, "ok")
            out["installed"] += names_in(manifest)
            if junk:
                out["refused"] += [f"{manifest.name}: '{line[:50]}' is not a package, so it was skipped: take it out" for line in junk[:3]]
        else:
            out["failed"].append(f"{manifest.name}: {done.output.strip()[-400:] or 'it timed out'}")

    def install_now(self, cwd: Path, names: list[str], ecosystem: str = "python") -> dict:
        """When an agent needs something in the middle of its work, or a crash says it is missing. The same rules as a manifest: registry names only, no scripts, in the wall, in the project's own folders."""
        good, bad = plain_packages(names, ecosystem)
        out = {"installed": [], "refused": [f"{b[:60]} is not a plain package name" for b in bad], "failed": [], "unchanged": 0}
        if not good:
            return out
        env = {"HOME": str(self.home), "TMPDIR": str(self.project / ".deps"), "PIP_CACHE_DIR": str(self.project / ".deps" / "pip"), "PIP_NO_INPUT": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1", "npm_config_cache": str(self.project / ".deps" / "npm"), "npm_config_ignore_scripts": "true", "npm_config_audit": "false", "npm_config_fund": "false", "npm_config_update_notifier": "false"}
        writable = self._writable(Path(cwd))
        if ecosystem == "node":
            npm = _which_npm()
            if npm is None:
                out["failed"].append("npm is not installed")
                return out
            done = self.runner([npm, "install", "--ignore-scripts", "--no-audit", "--no-fund", "--no-save", *good], cwd, writable, readable=sandbox.tool_roots("node", "npm"), network=True, timeout=INSTALL_SECONDS, env=env)
        else:
            venv = self.project / ".venv"
            base = getattr(sys, "_base_executable", sys.executable)
            readable = sandbox.tool_roots(base)
            if not (venv / "bin" / "python").exists():
                made = self.runner([base, "-m", "venv", str(venv)], self.project, writable, readable=readable, timeout=120.0, env=env)
                if not made.ok:
                    out["failed"].append(f"a Python environment could not be made: {made.output[-300:]}")
                    return out
            done = self.runner([str(venv / "bin" / "python"), "-m", "pip", "install", "--only-binary=:all:", "pytest", *good], cwd, writable, readable=readable, network=True, timeout=INSTALL_SECONDS, env=env)
        if done.ok:
            out["installed"] = good
            self._note_extra(ecosystem, good)
        else:
            out["failed"].append(f"{', '.join(good)}: {done.output.strip()[-400:] or 'it timed out'}")
        return out

    def _note_extra(self, ecosystem: str, names: list[str]) -> None:
        path = self.project / ".deps" / f"extra-{ecosystem}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        known = set(path.read_text(encoding="utf-8").split()) if path.is_file() else set()
        path.write_text("\n".join(sorted(known | set(names))) + "\n", encoding="utf-8")

    def extras(self, ecosystem: str = "python") -> list[str]:
        path = self.project / ".deps" / f"extra-{ecosystem}.txt"
        return sorted(path.read_text(encoding="utf-8").split()) if path.is_file() else []

    def _node(self, cwd: Path, manifest: Path, digest: str, out: dict) -> None:
        folder = manifest.parent
        stamp = folder / "node_modules" / ".xenio-stamp"
        if stamp.is_file() and stamp.read_text(encoding="utf-8").strip() == digest:
            out["unchanged"] += 1
            return
        if not names_in(manifest):
            return
        env = {"HOME": str(self.home), "npm_config_cache": str(self.project / ".deps" / "npm"), "npm_config_ignore_scripts": "true", "npm_config_audit": "false", "npm_config_fund": "false", "npm_config_update_notifier": "false", "TMPDIR": str(self.project / ".deps")}
        readable = sandbox.tool_roots("node", "npm")
        npm = _which_npm()
        if npm is None:
            out["failed"].append("npm is not installed")
            return
        done = self.runner([npm, "install", "--ignore-scripts", "--no-audit", "--no-fund", "--no-package-lock"], folder, self._writable(cwd), readable=readable, network=True, timeout=INSTALL_SECONDS, env=env)
        if done.ok:
            stamp.parent.mkdir(exist_ok=True)
            stamp.write_text(digest, encoding="utf-8")
            out["installed"] += names_in(manifest)
        else:
            out["failed"].append(f"{manifest.name}: {done.output.strip()[-400:] or 'it timed out'}")


def _which_npm() -> str | None:
    import shutil

    return shutil.which("npm")


def in_words(result: dict) -> str:
    parts = []
    if result.get("installed"):
        parts.append("Installed for the team (sandboxed, no install scripts): " + ", ".join(dict.fromkeys(result["installed"]))[:500])
    if result.get("refused"):
        parts.append("Not installed, because it does not come from a public registry: " + "; ".join(result["refused"])[:500])
    if result.get("failed"):
        parts.append("Could not be installed: " + "; ".join(result["failed"])[:700])
    return " ".join(parts)


def commit_project(project: Path, message: str) -> subprocess.CompletedProcess:
    """A finished build lands in the project XENIO made, committed, so you find it there and not in a hidden worktree."""
    for args in (["add", "-A"], ["-c", "user.name=XENIO", "-c", "user.email=xenio@localhost", "commit", "-q", "-m", message]):
        done = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=project, capture_output=True, text=True, timeout=60)
    return done
